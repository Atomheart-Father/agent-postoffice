"""The controlled wire: three frame shapes, and nothing else.

One JSON object in, one JSON object out. Identity is never read from the body: the entry fixes it
when it registers the binding, and `Core.handle` re-reads that fact inside the business transaction.

The three answers are deliberately distinct, because they ask the caller to do three different
things:

* ``{"ok": true, "result": {...}}`` — the business action happened. Read the result.
* ``{"ok": false, "code": ..., "message": ...}`` — the business action was refused. Read the
  reason and the next step; retrying the same request unchanged will be refused again.
* ``{"ok": false, "code": "unknown", "retry_ref": ...}`` — the transport does not know whether it
  was carried. Retry with **the same** `retry_ref`; the action ledger decides whether the original
  attempt already committed.

Folding the third into the second is how a caller ends up blindly re-sending a write that already
happened, so the shapes are kept apart here even though two of them share `ok: false`.
"""
import hashlib
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path


def _load_sibling_module(path, name):
    """按路径装载同目录的胶水模块（`r/artifact_read.py`、`r/publish.py`）。

    `r/publish.py` 用 `import artifact_read` 借同一个版本令牌拼法，所以装载前要把 `r/` 放进
    `sys.path`（只加这一层目录，不把整棵树摊进去）。
    """
    here = str(Path(path).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_core(path):
    """Load `office_core.py` as its own module object (path-keyed, no package assumptions)."""
    path = Path(path)
    loader = importlib.machinery.SourceFileLoader("office_core_mod", str(path))
    spec = importlib.util.spec_from_file_location("office_core_mod", str(path), loader=loader)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _bare_one(prefix, one):
    """相对交付根 → 相对交付范围的工作目录；`{path, op}` 只换 `path`，`op` 跟着走。

    曾经这里把每一项折成字符串，于是 `{"path": …, "op": "delete"}` 的 op 在分组时被丢掉：
    删除请求变成"写一个已经不存在的文件"，真实入口上**永远删不了**，只报"文件不存在"。
    """
    if isinstance(one, str):
        p = one
    else:
        p = one.get("path") if isinstance(one, dict) else None
        if not isinstance(p, str):
            return one
    bare = (p[len(prefix):].lstrip("/") or ".") if (p == prefix or p.startswith(prefix + "/")) else p
    if isinstance(one, str):
        return bare
    return {**one, "path": bare}


class Endpoints:
    """The controlled entry: register a binding, then dispatch requests for it."""

    def __init__(self, mod, core, directory=None):
        self.mod = mod
        self.core = core
        self.dir = Path(directory) if directory else None

    # ---------------------------------------------------------------- 受控入口
    def bind_and_open(self, binding_id, actor, generation, instance, *,
                      host_session_ref=None, channel_lifetime_ref=None,
                      source=None, surface=None):
        """Record the binding fact and return the trusted context the daemon will dispatch with.

        The caller passes no identity per request: this is the only place a connection becomes a
        person. `source`/`surface` are fixed here by the deployment side, never by the wire — a
        body that claims a source is refused by `Core.handle` as an identity field.
        """
        channel = channel_lifetime_ref or ("ch-" + binding_id)
        self.core.binding_register(binding_id, actor, generation, instance,
                                   channel_lifetime_ref=channel,
                                   host_session_ref=host_session_ref)
        ctx = {"actor": actor, "binding_id": binding_id, "generation": generation,
               "instance": instance, "channel_lifetime_ref": channel}
        if host_session_ref:
            ctx["host_session_ref"] = host_session_ref
        if source is not None:
            ctx["source"] = source
        if surface is not None:
            ctx["surface"] = surface
        return dict(ctx)

    # ---------------------------------------------------------------- 公开形状 → 收件列表
    def translate_letter(self, req, actor):
        """`{to, cc, ...}` → Core 的收件列表 + 稳定信件编号。**只在这里翻一次。**

        为什么在这里：`to`/`cc` 是**工具面**的形状（spec public），`recipients` 是 Core 的形状；
        让每个客户端各翻一次，就会出现“CLI 认 @岗位、MCP 不认”这种两套解析。端点已经是唯一的
        受控入口，翻译与登记核对都放这里，CLI/MCP/Pi 只把 `to` 原样交上来。

        岗位按岗位记录核对；普通箱按现有目录或 Core 登记事实核对。
        """
        to = req.get("to")
        cc = req.get("cc") or []
        if not isinstance(to, str) or not to.strip():
            raise self.mod.CoreError("invalid_request", "letter.send 需要 to（信箱名或 @岗位）",
                                     en="letter.send needs to (a mailbox or @position)", field="to")
        if not isinstance(cc, list) or any(not isinstance(c, str) or not c.strip() for c in cc):
            raise self.mod.CoreError("invalid_request", "cc 必须是信箱名或 @岗位的数组",
                                     en="cc must be an array of mailboxes or @positions", field="cc")
        positions = {p["position"] for p in
                     (self.core.position_facts(actor, all=True).get("positions") or [])}
        recipients = []
        for value, category in [(to, "to")] + [(c, "cc") for c in cc]:
            raw = value.strip()
            if raw.startswith("@"):
                unit = raw[1:].strip()
                if not unit:
                    raise self.mod.CoreError("invalid_request", "`@` 后面缺岗位名",
                                             en="no position name after `@`", field="to")
                if unit not in positions:
                    raise self.mod.CoreError(
                        "not_found", "没有登记这个岗位：%s（发信只能发给已登记的岗位或信箱）" % unit,
                        en="no such registered position: %s (mail goes to a registered position or mailbox)"
                           % unit, position=unit, reason_code="position_unregistered")
                recipients.append({"box": unit, "logical_address": unit, "category": category})
                continue
            home = Path(self.core.home)
            known = (home / raw).is_dir() or raw in positions
            if not known:
                with self.core._txn(read_only=True) as conn:
                    known = self.core._is_registered_principal(raw, conn)
            if not known:
                raise self.mod.CoreError(
                    "not_found", "没有登记这个信箱：%s（发信只能发给已登记的岗位或信箱）" % raw,
                    en="no such registered mailbox: %s (mail goes to a registered position or mailbox)"
                       % raw, box=raw, reason_code="box_unregistered")
            recipients.append({"box": raw, "category": category})
        return recipients, self.letter_id_for(actor, req.get("act_key"))

    @staticmethod
    def letter_id_for(actor, act_key):
        """同一笔动作必然是同一封信：编号由 `(actor, act_key)` 定出来，与 act_key 一起生成。"""
        raw = "%s\x00%s" % (actor, act_key)
        return "L" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    # ---------------------------------------------------------------- 一帧
    #: 线路只接受 spec public 的字段；内部形状（`recipients`/`letter_id`）不由外部给。
        #: 两项端点胶水：交付时 `files` → `change_set`，读回时 `bytes` → 展示视图。
    #: 它们都在 `r/` 里，Core 不认识"文件"也不认识"视图"。
    @property
    def _artifacts(self):
        if getattr(self, "_artifact_mod", None) is None:
            self._artifact_mod = _load_sibling_module(
                Path(__file__).resolve().parent / "artifact_read.py", "office_r_artifact_read")
        return self._artifact_mod

    @property
    def _publisher(self):
        if getattr(self, "_publish_mod", None) is None:
            self._publish_mod = _load_sibling_module(
                Path(__file__).resolve().parent / "publish.py", "office_r_publish")
        return self._publish_mod

    _WIRE_ONLY = {
        "letter.send": {"op", "act_key", "to", "cc", "subject", "need", "body",
                        "reply_to", "keep_open", "retry_ref"},
        "letter.archive": {"op", "act_key", "letter_id", "unit"},
        # `work.read` 的展示字段：读权与字节鉴别全在 Core 的 `work.read`（唯一判据），
        # 分页/搜看形状全在 `r/artifact_read.view`（唯一投影器）。两者都不认这四个字段 ——
        # 原样透传只会被 Core 的签名校验拒掉（“请求含未知字段”），于是线路永远拿不到分页。
        # 分流在端点：先扣下来，Core 只收身份与定位（work_ref/sub_ref/path），
        # 视图在 `_as_read_view` 里用**原有**分页调用现算。不另开 op，不重写投影器。
        "work.read": {"op", "work_ref", "sub_ref", "path", "lines", "search", "cursor", "limit"},
    }

    def _with_prepared_change_set(self, req, trusted_ctx):
        """工具收 `files`，Core 收 `change_set`：字节在 Core 的写事务里、`publish` 之前进库。

        `commit` 必须与 `publish` 同一个写事务（否则字节与提交记录会分家）；`close` 交给调用层
        （Core 在验证绑定/权限时就可能拒，那时 `prepare` 还没被叫过）。`precheck` 由 Core 给。"""
        out = dict(req)
        files = out.pop("files")
        work_ref = out.get("work_ref")
        groups = out.pop("groups", None) or self._groups_for(work_ref, files)
        prepared = self._publisher.prepare_change_sets(
            groups, artifact_root=str(self.core._artifact_root()))

        def prepare(precheck=None):
            changes, skipped, commit = prepared[:3]
            if precheck is not None:            # 车的另一头：先判业务，再落字节
                precheck(changes)               # 原样的 change_set：`_outside_scope` 认的是 dict
            commit()
            return changes

        out["change_set"] = None
        out["prepare"] = prepare
        return out, prepared[3]                 # `close`：归调用层，Core 提前拒绝也有人收

    #: 轮 op 在线上带 `box`（投递单元），Core 的形参叫 `unit`；`box == actor` 时不传
    #: （那是"自己的箱"，Core 的默认就是 actor）。这不是第二套轮次规则，是**同一条换算**：
    #: 旧 `RoundRPC._core_call` 就是这一句，别处不再重复。
    #: 轮 op：**不在这里再抄一份名单** —— 判据是 `round.` 前缀，源在 Core 的 `_HANDLE_OPS`
    #: （`core_mod._ROUND_OPS`）。抄一份的代价是两边只能一处放宽、一处没改。
    @property
    def _ROUND_OPS(self):
        return self.mod._ROUND_OPS
    #: 只用于**校对**、不进 Core 正文的身份字段（Core 的身份只来自 trusted_ctx）。
    _ROUND_CLAIMS = ("instance", "pi_instance", "binding_id", "binding_gen")

    def translate_round(self, req, trusted_ctx):
        """轮 op 的线上形状 → Core 的参数。唯一的一处换算。

        - `box` → `unit`（`box == actor` 时不传）；
        - `act_key` 丢弃（轮靠令牌/所有权判权，它不是事实的一部分）；
        - `round.consume` 的 `reported_instance` **只**取自可信 ctx，不从正文自报；
        - `round.record_payload` 的 `reported_*` 是把正文里的**声称**交给 Core 去和冻结事实比，
          所以它们保留为 `reported_*`；与 ctx 冲突的声称按旧口径直接拒。
        """
        actor = trusted_ctx["actor"]
        out = dict(req)
        box = out.pop("box", None)
        if isinstance(box, str) and box and box != actor:
            out["unit"] = box
        out.pop("act_key", None)
        for name in self._ROUND_CLAIMS:
            claimed = out.get(name)
            if claimed is None:
                continue
            trusted = trusted_ctx.get(name if name != "binding_gen" else "generation")
            if name in ("instance", "pi_instance") and trusted and claimed != trusted:
                raise self.mod.CoreError("invalid_request", "%s 与可信上下文不一致" % name,
                                         en="%s does not match the trusted context" % name,
                                         field=name)
            if name in ("binding_id", "binding_gen"):
                continue          # 作为"声称"留给 Core 与冻结事实比较（不删）
            out.pop(name, None)
        if req.get("op") == "round.consume":
            instance = trusted_ctx.get("instance")
            if not isinstance(instance, str) or not instance:
                raise self.mod.CoreError("unauthorized_attempt",
                                         "round.consume 缺少可信连接实例",
                                         en="round.consume lacks a trusted connection instance")
            out["reported_instance"] = instance
        return out

    def _workspace_map(self):
        """宿主的 `workspace-map.json`：`scope → 本机目录`。**宿主事实**，不从线路取。

        读不出/不是对象就是空映射：那会让每个范围都按 `workspace_unmapped` 拒，而不是悄悄
        回落到信箱目录（回落到一个"能写但不对"的地方，比拒绝更坏）。
        """
        path = Path(self.core.home) / "workspace-map.json"
        try:
            with open(path, "r", encoding="utf-8") as fh:
                got = json.load(fh)
        except (OSError, ValueError):
            return {}
        return got if isinstance(got, dict) else {}

    def _workspace_root(self, scope, mapping):
        """一个交付范围 → 本机上那个工作目录（`None` = 没映射）。

        范围可以带子目录（`repo/app`）而映射只登记到仓库根（`repo`）：那就取**最长的已映射祖先**
        再把剩余段接上去。登记根**就是** `workspace-map` 里那个范围自己（它可以是仓库的子目录，
        也可以是另一个仓库）—— 所以这里不拼仓库根。
        """
        name = str(scope or "").strip("/")
        while name:
            hit = mapping.get(name)
            if isinstance(hit, str) and hit.strip():
                return str(Path(hit) / "" if name == str(scope or "").strip("/")
                           else Path(hit) / str(scope).strip("/")[len(name) + 1:])
            name = name.rsplit("/", 1)[0] if "/" in name else ""
        return None

    def _groups_for(self, work_ref, files):
        """一个交付范围一组：`(scope_root, prefix, files)`。范围由**每个文件自己**定。

        `scope_root` 是**范围在 `workspace-map` 里映射的工作目录**（宿主事实），`prefix` 是 ref 的
        前缀（Core 的 `work.scope_ref`）。两者不是同一个东西：文件相对工作目录寻址，ref 由前缀
        拼回去。基线不再回落到信箱目录 —— 没映射就拒（调用方靠 reason_code 分清"没配"与"磁盘没有"）。

        ★ 多范围（两个仓库）：一个 Work 可以同时交付到两个登记范围，而它们的根是**两个不同目录**。
          所以按**每个文件最长匹配的那个映射范围**分组 —— 不能把第二个范围的文件拼到第一个范围的
          根上（那会得到 `repoA/docs/src/feature.py`）。是否真的允许这个范围，仍由
          `work_flow._outside_scope` 拿 Work 自己的 scopes 判：这里只算路径，不判权。
        """
        scope = self.core.work_scope_of(work_ref) if hasattr(self.core, "work_scope_of") else None
        fallback = (scope or "").strip("/")
        mapping = self._workspace_map()
        keys = sorted((str(k).strip("/") for k in mapping), key=len, reverse=True)
        buckets = {}
        for item in files if isinstance(files, list) else [files]:
            path = item if isinstance(item, str) else (
                item.get("path") if isinstance(item, dict) else None)
            if not isinstance(path, str) or not path:
                continue
            rel = path.strip("/")
            hit = next((k for k in keys if k and (rel == k or rel.startswith(k + "/"))), None)
            buckets.setdefault(hit, []).append(item)     # ★ 原样留着：`{path, op}` 里的 op 是删除的唯一表达
        groups = []
        for hit, listing in buckets.items():
            prefix = hit or fallback
            root = self._workspace_root(prefix, mapping) if prefix else None
            if prefix and root is None:
                raise self.mod.CoreError(
                    "capability_missing",
                    "这件 Work 的交付范围 %r 在宿主的 workspace 映射里没有登记；不会回落到信箱目录"
                    % prefix,
                    en="this Work's delivery scope %r is not mapped in the host's workspace map; "
                       "there is no fallback to the mailbox directory",
                    reason_code="workspace_unmapped", scope=prefix)
            if prefix and not Path(root).is_dir():
                raise self.mod.CoreError(
                    "capability_missing",
                    "映射到的工作目录不在本机：%s" % root,
                    en="the mapped working directory is not on this machine: %s" % root,
                    reason_code="workspace_missing", scope=prefix, root=root)
            # 工具给的是**相对交付根**的路径（`repo/app/a.md`）；`prepare_change_sets` 要的是
            # 相对交付范围工作目录的那一段（`a.md`），ref 由它按前缀拼回去。只在这里脱一次。
            if prefix:
                listing = [_bare_one(prefix, one) for one in listing]
            groups.append((root or str(self.core.home), prefix, listing))
        return groups

    def _as_read_view(self, result, req):
        """`work.read` 的核心返回是字节；线路上的形状由 `r/artifact_read.view` 现算。"""
        if not isinstance(result, dict) or not isinstance(result.get("bytes"), (bytes, bytearray)):
            return result
        data = bytes(result["bytes"])
        view = self._artifacts.view(data, lines=req.get("lines"), search=req.get("search"),
                                    cursor=req.get("cursor"), limit=req.get("limit"))
        shown = {k: v for k, v in result.items() if k != "bytes"}
        shown.update(view if isinstance(view, dict) else {"text": view})
        return shown

    def dispatch(self, req, trusted_ctx):
        """One request → one frame. Never raises for a business refusal."""
        close_prepared = None         # 备好的临时对象/fd：无论 Core 怎么拒，都在 finally 收
        try:
            view_args = None          # `work.read` 的展示字段：局部变量，不碰实例状态
                                      # （dispatch 是多线程共用的，实例字段会跨请求串线）
            if isinstance(req, dict) and "prepare" in req:
                # 不是线路字段：只有本端点自己造的胶水才会带它。
                raise self.mod.CoreError("invalid_request",
                                         "prepare 是端点内部字段，线路不接受",
                                         en="prepare is internal to the endpoint; the wire does not accept it")
            op = req.get("op") if isinstance(req, dict) else None
            if op in self._WIRE_ONLY:
                extra = sorted(set(req) - self._WIRE_ONLY[op])
                if extra:
                    return self.refused(self.mod.CoreError(
                        "invalid_request", "%s 不接受字段: %s" % (op, extra),
                        en="%s does not accept fields: %s" % (op, extra)))
                req = dict(req)
                if op == "letter.send":
                    if req.get("to") is None and req.get("reply_to"):
                        original = self.core.handle({"op": "letter.read", "letter_id": req["reply_to"],
                                                     "include_archived": True, "limit": 1}, trusted_ctx)
                        letter = original["letter"]
                        if letter.get("system"):
                            raise self.mod.CoreError("invalid_request", "系统信需要指定收件地址",
                                                     en="A reply to system mail needs an explicit recipient")
                        req["to"] = letter["from"]
                    recipients, letter_id = self.translate_letter(req, trusted_ctx["actor"])
                    req = dict(req)
                    req.pop("to", None)
                    req.pop("cc", None)
                    req.pop("retry_ref", None)
                    req["recipients"] = recipients
                    req["letter_id"] = letter_id
            if op in self._ROUND_OPS:
                req = self.translate_round(req, trusted_ctx)
            if op == "work.publish" and "files" in req:
                req, close_prepared = self._with_prepared_change_set(req, trusted_ctx)
            if op == "work.read":
                # 分流：这四个是展示字段，不进 Core。Core 的 `work.read` 只认身份与定位，
                # 读权与字节鉴别全在那边；这里扣下后原样转交给 `_as_read_view` 的原有分页调用。
                view_args = {k: req.get(k) for k in ("lines", "search", "cursor", "limit")}
                req = {k: v for k, v in req.items() if k not in view_args}
            result = self.core.handle(req, trusted_ctx)
            if op == "work.read" and view_args is not None:
                result = self._as_read_view(result, view_args)
        except self.mod.CoreError as exc:
            return self.refused(exc)
        except Exception as exc:                                   # noqa: BLE001
            # 投递/成果/发布三层的拒绝同样是业务拒绝：它们带 `code`、`message`、`en`/`detail`，
            # 折成"内部错误"会把一个可复核的拒绝变成不可复核的故障。
            if exc.__class__.__name__ in ("DueRefused", "ResultRefused", "PublishRefused",
                                        "ArtifactReadRefused"):
                # 四家的分工不变：`ResultRefused` 带自己的机器码（forbidden/not_found/…）；
                # `PublishRefused` 与 `ArtifactReadRefused` 只有两种语言的一句话（没有机器码）——
                # 那是"这份文件不能发 / 这页读不了"，属于请求本身不成立，给 `invalid_request`，
                # 不冒充内部故障。读权拒绝永远轮不到这一条：它在 Core 的 `work.read` 里就
                # 已经是 `ResultRefused(forbidden)`；视图只接展示参数（越界页、坏 lines）。
                if not getattr(exc, "code", None):
                    exc.code = "invalid_request"
                return self.refused(exc)
            # 其余才是内部错误：不泄漏细节。
            return self.refused(self.mod.CoreError(
                "internal" if "internal" in self.mod.CODES else "busy",
                "内部错误：%s" % type(exc).__name__,
                en="internal error: %s" % type(exc).__name__))
        finally:
            if close_prepared is not None:
                close_prepared()
        return {"ok": True, "result": result}

    # ---------------------------------------------------------------- 三种帧
    @staticmethod
    def refused(exc, locale="zh"):
        """业务拒绝：码 + 一句人话 + 可复核的 detail（只带白名单里的键）。

        `CoreError` 之外，投递层的 `DueRefused`、成果层的 `ResultRefused`、发布层的
        `PublishRefused` 也是业务拒绝（各有自己的 code/message/en/detail），同样是 `ok:false`
        而不是"内部错误"。
        """
        text = getattr(exc, "text", None)
        message = text(locale) if callable(text) else (getattr(exc, "message", None) or str(exc))
        frame = {"ok": False, "code": getattr(exc, "code", "invalid_request"), "message": message}
        detail = getattr(exc, "detail", None) or {}
        if detail.get("reason_code"):
            frame["reason_code"] = detail["reason_code"]
        rest = {k: v for k, v in detail.items() if k != "reason_code"}
        if rest:
            frame["detail"] = rest
        return frame

    @staticmethod
    def unknown(retry_ref, message=None):
        """传输未知：**原样**带回 `retry_ref`，由调用方用它查同一笔动作的回执。"""
        return {"ok": False, "code": "unknown",
                "message": message or "结果未知：带同一个 retry_ref 重试，动作账本会给出是否已提交",
                "retry_ref": retry_ref}

