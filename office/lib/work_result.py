"""成果可回读 —— 一条纵切：交上去的字节能按**那一版**再读回来。

这一条纵切只有两件事，两头都以字节为准：

  · **写入**：把提交清单里每一项按内容寻址核一遍（`<store>/<sha256 hex>` 里真有这份字节，且它的
    sha256 逐位等于声明的版本），核不到的**不拒收**（协议原文：接受并记 unverified），但必须把
    结论记在**这一项**上 —— 自报的 SHA 不会因为"提交成功"就变成核实过。
  · **读出**：给定（提交，文件，版本）时，先判这个人读不读得到这件 Work，再判这一对
    ref@版本在不在那次提交里，最后从库里取出**这一份**字节并复核摘要。三件事各自给不同的拒绝，
    不合并成一个"读不到"。

`sub_ref` 是唯一入口：一次提交就是一组 (ref, version)；没有"文件路径"这一层，所以读回不依赖
任何人当时的工作树。页面后置 —— 这里只给可执行的事实，不做展示形状。

库的布局是 schema 级事实：一项成果的字节就放在 `<store>/<sha256 hex>`，没有第二份副本。
`r/artifact_read` 的读口用同一条规则（`load_object` / `digest_name`）；本模块不解版本令牌之外的
任何东西，也不猜缺失的字节。
"""
import hashlib
import json
import os
import re
import time

_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")
WRITE, DELETE = "write", "delete"
VERIFIED, MISSING, MISMATCH = "verified", "missing", "mismatch"


class ResultRefused(Exception):
    """成果读写被拒。`code` 是机器码，`message` 是给人看的一句话。"""

    def __init__(self, code, message, en=None, **detail):
        super().__init__(message)
        self.code, self.message, self.en, self.detail = code, message, en, detail


def require_text(value, what, *, limit=None):
    """"必须是字符串且非空"那一句（`_check_shape` 与 `_resolve_person` 共用这一份）。"""
    if not isinstance(value, str) or not value or (limit is not None and len(value) > limit):
        raise ResultRefused("invalid_request", "%s 必须是非空字符串" % what,
                            en="%s must be a non-empty string" % what, field=what)


def _positions_held(conn, actor):
    """这个会话此刻当前任职的岗位 —— **唯一**的取值处（读权、承办、可续问的都是这一份）。

    只认 `position_holders` 的现任（一岗一行，`holder` 就是当前事实）：没有"候选人"这一步。
    """
    return {r["position"] for r in
            conn.execute("SELECT position FROM position_holders WHERE holder=?", (actor,)).fetchall()
            if r["position"]}


def require_actor(actor):
    """受控身份那一句：这一族模块共用**一份**判据与一份话术（曾经五处各写一遍）。"""
    if not isinstance(actor, str) or not actor:
        raise ResultRefused("invalid_request", "缺少受控身份", en="a controlled actor is required")

def require_version(row, expected_version, work_ref=None):
    """乐观并发：读者手里的版本与库里不一致就拒（`stale`）。三处曾各写一遍同一条判据。"""
    if expected_version is None or expected_version == row["version"]:
        return
    ref = work_ref if work_ref is not None else row["work_ref"]
    raise ResultRefused("conflict", "%s 在你查看后有变化（你看到 v%s，当前 v%s）"
                        % (ref, expected_version, row["version"]),
                        en="%s changed after you looked (you saw v%s, now v%s)"
                           % (ref, expected_version, row["version"]),
                        work_ref=ref, expected=expected_version, current=row["version"],
                        state=row["state"], reason_code="stale")


def _sub_row(conn, sub_ref):
    """一次提交的行；没有就是 `not_found`（`authorize_read` 与 `read_by_path` 共用这一句）。"""
    sub = conn.execute("SELECT * FROM subs WHERE sub_ref=?", (sub_ref,)).fetchone()
    if sub is None:
        raise ResultRefused("not_found", "提交 %r 不存在" % sub_ref,
                            en="submission %r does not exist" % sub_ref, sub_ref=sub_ref)
    return sub


def _work_row(conn, work_ref, *, or_none=False):
    """一件 Work 的行；没有就是 `not_found`（曾经 `work_flow` 里也有一份一模一样的）。"""
    row = conn.execute("SELECT * FROM work WHERE work_ref=?", (work_ref,)).fetchone()
    if row is None and not or_none:
        raise ResultRefused("not_found", "Work %r 不存在" % work_ref,
                            en="Work %r does not exist" % work_ref, work_ref=work_ref)
    return row


def digest_change_set(change_set):
    """`sub_digest`：只对准确成果集合本身算摘要（与 Core 的 `digest_change_set` 逐字节相同）。

    同一份清单在两个地方算摘要就会有两个 `sub_digest`，而读面拿它判"这一份提交的摘要还等于
    它自己吗"—— 两处算法漂一点，一份正常提交就会被当成"摘要不符"而不再可审。
    """
    items = sorted((str(c["ref"]), str(c["exact_version"]), str(c["op"])) for c in change_set)
    encoded = json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def store_name(exact_version):
    """`sha256:<64 hex>` → 库里的对象名；任何别的形状都不猜。"""
    m = _DIGEST.fullmatch(exact_version) if isinstance(exact_version, str) else None
    if not m:
        raise ResultRefused("invalid_request", "exact_version 必须是 sha256:<64 hex>",
                            en="exact_version must be sha256:<64 hex>", field="exact_version")
    return m.group(1)


def change_set_of(raw):
    """`subs.change_set_json` → 变更项清单。**一处解析、一处兜坏数据。**

    坏数据当空清单：清单本来就是"这次提交改了哪些东西"，读不出来就只能当作没有可用项，
    让调用方按各自语义回答（读面报"不在提交里"、挑选器跳过它）。非 dict 元素一律丢。
    """
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return []
    return [c for c in items if isinstance(c, dict)] if isinstance(items, list) else []


def check_ref_shape(ref, *, what="ref"):
    """成果路径的形态：非空、无空白、**没有空段或 `..` 段**、不开头也不结尾于 `/`。

    这一条与 Core 的 `check_ref` 同一条规则（含"没有空段"），但那一份在 `office_core.py`
    （本模块不 import 它）——
    一份成果清单会被写进 `subs.change_set_json` 并被 `work.read` 按 ref 找回来，所以"这个 ref
    能不能安全地当路径用"必须在**写进来的那一刻**判，不能等到端点打开文件时才发现
    （那时库里已经存了一条指向工作区外的引用）。
    """
    require_text(ref, what, limit=200)
    if any(ch.isspace() for ch in ref):
        raise ResultRefused("invalid_request", "%s 不能含空白" % what,
                            en="%s must not contain whitespace" % what, field=what)
    if ref.startswith("/") or ref.endswith("/"):
        raise ResultRefused("invalid_request", "%s 不能以 / 开头或结尾" % what,
                            en="%s must not start or end with /" % what, field=what)
    if "" in ref.split("/") or ".." in ref.split("/"):
        raise ResultRefused("invalid_request", "%s 不能含 .. 段" % what,
                            en="%s must not contain a .. segment" % what, field=what)
    return ref


def _store_path(store_root, exact_version):
    return os.path.join(str(store_root), store_name(exact_version))


def read_version(store_root, exact_version):
    """库里这一版的字节；摘要求和令牌逐位相同，否则当作**没有这一份**。

    没有就是没有（`missing`），字节对不上就是坏了（`mismatch`）—— 两种事实分开，因为它们要
    人做的事不同：前者是"没交上来"，后者是"库里的东西不能信"。
    """
    path = _store_path(store_root, exact_version)
    want = store_name(exact_version)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return None, {"reason": MISSING, "detail": e.strerror or "unreadable"}
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        return None, {"reason": MISMATCH, "detail": got}
    return data, {"reason": None, "bytes": len(data)}


def verify_change_set(change_set, *, store_root):
    """每一项的核验结论（**记在条目上**，不是整批一个布尔）。

    删除项没有新内容可核（`delete_has_no_content`），这与"核不到"不是同一件事。
    """
    out = []
    for item in change_set:
        entry = dict(item)
        if entry.get("op") == DELETE:
            entry["content_verified"] = False
            entry["content_check"] = {"reason": "delete_has_no_content"}
        else:
            data, why = read_version(store_root, entry.get("exact_version"))
            entry["content_verified"] = data is not None
            entry["content_check"] = why
        out.append(entry)
    return out


def now_text(now_ts=None):
    """库里的一切时刻只有一种写法 —— 与 Core 的 `_now()` 同一个本机格式。

    两种写法混存会让"先还是后"的判断在字符串比较下出错（`T` 与空格不同字节），而这张库里的
    时刻本来就是给人看、给按字符串排序用的。
    """
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(now_ts) if now_ts is not None else time.localtime())


def record_result(conn, actor, *, work_ref, change_set, store_root, epoch, act_key, note="",
                  final=False, now_ts=None):
    """写一次提交（`subs` 一行），返回这次提交的事实。

    `final`：1.1 的最终交付标记，原样写进 `subs.final`。收不收、会不会因此关闭 Work 是审核/终交
    那一步的事，不在这里决定。
    """
    require_actor(actor)
    if not isinstance(change_set, (list, tuple)) or not change_set:
        raise ResultRefused("invalid_request", "change_set 至少一条成果",
                            en="change_set needs at least one result")
    checked, seen = [], set()
    for item in change_set:
        if not isinstance(item, dict):
            raise ResultRefused("invalid_request", "change_set 每项须为 {ref, exact_version, op}",
                                en="each change_set item must be {ref, exact_version, op}")
        ref, op = item.get("ref"), item.get("op") or WRITE
        check_ref_shape(ref)
        if op not in (WRITE, DELETE):
            raise ResultRefused("invalid_request", "op 只能是 write 或 delete",
                                en="op must be write or delete", field="op")
        if ref in seen:
            raise ResultRefused("invalid_request", "change_set 重复成果 %r" % ref,
                                en="change_set repeats %r" % ref, field="ref")
        seen.add(ref)
        if op == DELETE:
            checked.append({"ref": ref, "exact_version": "tombstone", "op": DELETE})
        else:
            # 写项先过令牌形状：一个写不进库的名字不该走到核验那一步。
            store_name(item.get("exact_version"))
            checked.append({"ref": ref, "exact_version": item["exact_version"], "op": WRITE})
    verified = verify_change_set(checked, store_root=store_root)

    digest = digest_change_set(checked)
    n = conn.execute("SELECT COUNT(*) AS n FROM subs WHERE work_ref=?", (work_ref,)).fetchone()[0] + 1
    sub_ref = "%s/S%d" % (work_ref, n)
    conn.execute(
        "INSERT INTO subs(sub_ref, work_ref, seq, change_set_json, sub_digest, submitted_by,"
        " epoch_at_submit, state, created_at, act_key, note, final) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (sub_ref, work_ref, n,
         json.dumps(checked, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
         digest, actor, epoch, "effective", now_text(now_ts), act_key, note or "",
         1 if final else 0))
    return {"sub_ref": sub_ref, "work_ref": work_ref, "seq": n, "sub_digest": digest,
            "final": bool(final), "change_set": verified}


def may_read_work(conn, actor, row, prow, *, management=None, holders=None):
    """**唯一**的"这个人读不读得到这件 Work"。

    只在 `work_result` 里实现一次：它是这一族模块里最底层的一个（只 import 标准库），
    所以 `work_business` 的 `work.get`/`work.list` 与成果回读共用它，写不出第二份。

    · 共享项目：登记在册的员工都能读（含历史提交）；
    · 私密项目：承办、审核、或持本项目 `project.manage` 的人；承办/审核可以是**岗位地址**
      （岗位不持有会话、现任才持有），所以 `holders` 里的岗位也算 —— 否则"岗位派工给的活
      自己能交、却读不回自己的成果"；
    · `management` 必给：有没有本项目的 `project.manage`（Core 给，含有效期/撤销/scope 覆盖/岗位现任）。
      **不在这里再查一遍 `grants`**。发版、资源状态也问它，没有第二份。

    判据只看**现存事实**（`projects.visibility`、`work.assignee_ref`/`reviewer_ref`），不看历史。
    """
    if prow is None or row is None:
        return False
    if (prow["visibility"] or "shared") != "private":
        return True
    if holders is None:
        holders = _positions_held(conn, actor)
    if actor in (row["assignee_ref"], row["reviewer_ref"]):
        return True
    if row["assignee_ref"] in holders or row["reviewer_ref"] in holders:
        return True
    return management is not None and management(row["project_ref"])


def require_read_work(conn, actor, row, prow, *, management=None, holders=None):
    """读权那道门：判据只有 `may_read_work` 一处，这句话也只有一处。

    **一件 Work 读得到 = 它的成果读得到**：展示层（`work.get`/`work.list`）与字节层
    （`authorize_read`/`read_by_path`）问的是同一句，不给两套话术 —— 曾经一边说"读不到这件
    Work"、一边说"读不到这件 Work 的成果"，同一次拒绝在日志里长得像两件事。

    `read_by_path` 必须在本门**之后**才判"这一项是什么"：否则没读权的人能从 `forbidden` 与
    `not_found` 的差别里探测一份自己读不到的提交里有哪些文件。
    """
    if not may_read_work(conn, actor, row, prow, management=management, holders=holders):
        raise ResultRefused("forbidden", "读不到这件 Work",
                            en="this Work is not readable by you", work_ref=row["work_ref"])


def _housing(conn, sub):
    """这份提交所属的 Work 行与项目行（两处都先要这两行；取法只此一处）。"""
    row = _work_row(conn, sub["work_ref"], or_none=True)
    if row is None:
        raise ResultRefused("not_found", "提交 %r 不属于任何 Work" % sub["sub_ref"],
                            en="submission %r belongs to no Work" % sub["sub_ref"],
                            sub_ref=sub["sub_ref"])
    return row, conn.execute("SELECT * FROM projects WHERE project_ref=?",
                             (row["project_ref"],)).fetchone()


def authorize_read(conn, actor, *, sub_ref, ref, exact_version, _facts=None, management=None,
                   holders=None):
    """批不批这一次读：提交存在、这一对在提交里、这个人读得到这件 Work。

    **判据只有这一份**：`read_result` 调它拿事实，`lib/read_access` 那个入口名也委托到这里。
    批了就能读、读得到就是批过 —— 两处各写一遍必然漂（一处放宽、另一处没改，就出现
    "能读却读不到 / 读不到却能读"）。这里**不读字节**：判权和取字节是两件事，各自可单独用。
    `_facts` 是**同模块内**的已取事实（sub/work/prow/changes，`read_by_path` 定位时顺手带出）；
    判据本身一字不差 —— 传事实只是不把同一行查三遍，判权仍然在这里发生。
    """
    require_actor(actor)
    # 形状先判：一个连令牌都不是的东西不该被报成"不在提交里"（那是两件不同的事）。
    store_name(exact_version)
    if _facts is not None:
        sub, row, prow, changes = (_facts.get("sub"), _facts.get("work"),
                                   _facts.get("prow"), _facts.get("changes"))
    else:
        sub = _sub_row(conn, sub_ref)
        row, prow = _housing(conn, sub)
        changes = change_set_of(sub["change_set_json"])
    require_read_work(conn, actor, row, prow, management=management, holders=holders)
    hit = next((c for c in changes if c.get("ref") == ref
                and c.get("exact_version") == exact_version and c.get("op") != DELETE), None)
    if hit is None:
        raise ResultRefused("not_found", "%r@%r 不在提交 %r 里" % (ref, exact_version, sub_ref),
                            en="%r@%r is not in submission %r" % (ref, exact_version, sub_ref),
                            work_ref=row["work_ref"], sub_ref=sub_ref)
    return {"work_ref": row["work_ref"], "sub_ref": sub_ref, "ref": ref,
            "exact_version": exact_version, "project_ref": row["project_ref"]}


def read_result(conn, actor, *, sub_ref, ref, exact_version, store_root, _facts=None,
                management=None, holders=None):
    """按（提交，文件，版本）把**那一份字节**读回来：先 `authorize_read`，再取字节并复核摘要。

    五类拒绝分开：`invalid_request`（令牌形状）、`not_found`（没有这次提交/这一对不在提交里）、
    `forbidden`（读不到这件 Work）、`mismatch`（库里的字节不是声明的这一版）。
    """
    facts = authorize_read(conn, actor, sub_ref=sub_ref, ref=ref, exact_version=exact_version,
                           _facts=_facts, management=management, holders=holders)
    data, why = read_version(store_root, exact_version)
    if data is None:
        raise ResultRefused("mismatch", "库里这一份字节与声明的版本不一致",
                            en="the stored bytes for this version do not match",
                            work_ref=facts["work_ref"], sub_ref=sub_ref, **why)
    return {**facts, "bytes": data, "size": len(data), "content_verified": True}


#: 挑一份提交的三种问法。**一个选择器**，差异写成显式参数 —— 三处各写一遍就是三个能漂的答案：
#: `table`      = 桌上那一份（先 effective 再 accepted，工具不给 sub_ref 时读它）
#: `final`      = Done 的指向（最后一份 accepted，没有就是最后一份 effective；历史 33 件只有后者）
#: `latest`     = 最近一份（不限状态；履历与成果链要看全部版本时用）
PICK_TABLE, PICK_FINAL, PICK_LATEST = "table", "final", "latest"


def pick_sub(conn, work_ref, *, how=PICK_TABLE, row=None):
    """从这件 Work 的提交里挑一份 —— **唯一**的选择判据。

    `how` 的三个值见上面常数。`row` 可传已取到的 Work 行（调用方为判权已经查过，不重复查）。
    挑不到就是 None（"没有这一份"是合法答案，不抛）。

    三条规则本来各有道理，不该合并成一条：
      · 桌上那一份**先 effective**：一份被退回的提交仍然要能被替换/撤回，所以它不是"最终"；
      · Done 的指向**先 accepted**：终态看的是被采纳的那一份，而 33 件历史 Done 只有 effective；
      · 履历/成果链要的是**全部版本**，所以它挑的是最近那一份（不限状态）。
    三个问法共用一个"取最近"规则，差在优先级。
    """
    if row is None:
        row = _work_row(conn, work_ref)
    if how == PICK_FINAL:
        rows = conn.execute("SELECT * FROM subs WHERE work_ref=? AND state IN (?,?)"
                            " ORDER BY seq DESC", (work_ref, "effective", "accepted")).fetchall()
        for s in rows:
            if s["state"] == "accepted":
                return s
        return rows[0] if rows else None
    if how == PICK_LATEST:
        return conn.execute("SELECT * FROM subs WHERE work_ref=? ORDER BY seq DESC LIMIT 1",
                            (work_ref,)).fetchone()
    for state in ("effective", "accepted"):
        hit = conn.execute("SELECT * FROM subs WHERE work_ref=? AND state=? ORDER BY seq DESC LIMIT 1",
                           (work_ref, state)).fetchone()
        if hit is not None:
            return hit
    return None


def _match_path(changes, path):
    """`path` → 提交里那一项（**含删除记录**）。完整 ref 优先；没完全相同的才按路径段收尾找。

    三条边界都是拿两仓夹具实测出来的：

      · **完整 ref 优先**：两个登记根有重叠时（`docs/a.md` 与 `x/docs/a.md` 在同一份提交里），
        一个完整 ref 曾经既命中"完全相同"、又命中"后缀相同"，被当成歧义拒掉 —— 而它压根不歧义。
      · **只脆 `./` 前缀，不脆点**：`lstrip("./")` 是按**字符集**剔的，会把 `.env` 剔成 `env`，
        于是根目录下的 dotfile 永远读不到（它要的是 `/.env` 这个收尾，真名里的点被吃掉了）。
      · **后缀命中必须唯一**：`a.md` 同时命中两份才是**真**歧义，这时返回 None，由调用方要求写完整 ref。
    """
    want = path or ""
    while want.startswith("./"):        # 只去掉真正的 `./` 前缀（可以叠几层）
        want = want[2:]
    want = want.lstrip("/")            # ref 不以 `/` 开头；开头的 `/` 从来不是名字的一部分
    if not want:
        return None
    exact = [c for c in changes if isinstance(c, dict) and c.get("ref") == want]
    if exact:
        return exact[0]
    tail = [c for c in changes
            if isinstance(c, dict) and str(c.get("ref", "")).endswith("/" + want)]
    return tail[0] if len(tail) == 1 else None


def _previous_sub_with(conn, sub, ref):
    """同一件 Work 里、比这份更早、且把这个 `ref` 当**写入**的那一份 —— "旧版仍可读"的指向。"""
    for r in conn.execute("SELECT sub_ref, change_set_json FROM subs WHERE work_ref=? AND seq<? "
                          "ORDER BY seq DESC", (sub["work_ref"], sub["seq"])).fetchall():
        for c in change_set_of(r["change_set_json"]):
            if isinstance(c, dict) and c.get("ref") == ref and (c.get("op") or WRITE) != DELETE:
                return r["sub_ref"]
    return None


def _deleted_in_submission(conn, sub, ref):
    """要读的那一项是**这一版的删除记录**：这一版没有字节，但这不是"没有这项成果"。

    `reason_code` 是给线路分辨用的（`refused()` 会把它提到顶层）：真的没有这一项时不带它，
    所以"删了"与"根本没有"在机器码这一层也分得开。
    """
    prev = _previous_sub_with(conn, sub, ref)
    hint_zh = "；更早的版本在 %r 里仍可读" % prev if prev else ""
    hint_en = "; the earlier version is still readable in %r" % prev if prev else ""
    raise ResultRefused(
        "not_found",
        "%r 在提交 %r 里是**删除记录**：这一版没有字节%s" % (ref, sub["sub_ref"], hint_zh),
        en="%r is a deletion record in submission %r: there are no bytes for this version%s"
           % (ref, sub["sub_ref"], hint_en),
        sub_ref=sub["sub_ref"], path=ref, ref=ref,
        reason_code="deleted_in_submission", previous_sub_ref=prev)


def read_by_path(conn, actor, *, work_ref=None, sub_ref=None, path=None, store_root,
                 management=None, holders=None):
    """工具那一层的读法：Work（+可选提交、+提交里的路径）→ 那一份字节。

    与 `read_result` 同一条判据、同一份授权，只是多两步解析：哪一份提交、提交里哪一项。
    `path` 不唯一（多个范围下同名文件）时**不替调用方挑**，要求写完整 ref。

    **判权先于"这一项是什么"**：先 `_require_read`，再解析路径与项。否则没读权的人能靠回答
    分清"在提交里"与"根本没有"，进而探测一份读不到的提交里有哪些文件。

    三种"读不到"分开：这一项是**删除记录**（没有字节，旧版仍可读，`reason_code` 点名）/
    这一项**真的不在**这份提交里 / 这次提交或这件 Work **不存在**。
    """
    if sub_ref is None:
        if work_ref is None:
            raise ResultRefused("invalid_request", "需要 work_ref 或 sub_ref",
                                en="work_ref or sub_ref is required", field="work_ref")
        sub = pick_sub(conn, work_ref)
        if sub is None:
            raise ResultRefused("not_found",
                                "%s 还没有待审或最终的提交（要读历史提交请给 sub_ref）" % work_ref,
                                en="%s has no pending or final submission (pass sub_ref to read an "
                                   "earlier one)" % work_ref, work_ref=work_ref)
    else:
        sub = _sub_row(conn, sub_ref)
    # 一次定位、一次判读：同一行 sub 只查一次，change_set 只解析一次；判权与字节核验
    # 仍是 `_require_read`/`read_result` 那一份 —— 这里把定位顺手带出的事实递过去，
    # 不是把判据搬过来。
    changes = change_set_of(sub["change_set_json"])
    row, prow = _housing(conn, sub)
    require_read_work(conn, actor, row, prow, management=management, holders=holders)  # 先判权
    if path is None:
        if len(changes) != 1:
            raise ResultRefused("invalid_request", "这次提交有多个文件，需要 path 选一个",
                                en="This submission holds several files; pass path to choose one",
                                sub_ref=sub["sub_ref"], field="path")
        path = changes[0].get("ref")
    hit = _match_path(changes, path)
    if hit is None:
        raise ResultRefused("not_found", "%r 不在提交 %r 里（或用完整 ref）" % (path, sub["sub_ref"]),
                            en="%r is not in submission %r (or the path is ambiguous; use the full ref)"
                               % (path, sub["sub_ref"]), sub_ref=sub["sub_ref"], path=path)
    if (hit.get("op") or WRITE) == DELETE:
        _deleted_in_submission(conn, sub, hit["ref"])
    facts = {"sub": sub, "work": row, "prow": prow, "changes": changes}
    return read_result(conn, actor, sub_ref=sub["sub_ref"], ref=hit["ref"],
                       exact_version=hit["exact_version"], store_root=store_root, _facts=facts,
                       management=management, holders=holders)
