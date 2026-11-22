"""岗位知识条目、可恢复的版本快照、通用摘要与派工匹配。"""
import json
import posixpath
from pathlib import Path

LIMIT = 6000                 # 每岗正文字数上限（渲染后的整版）
ENTRY_LIMIT = 400            # 每条上限
WHEN_MAX = 8                 # 每条最多几个 when
WHEN_ITEM_MAX = 120
DEFAULT_SECTION = "实践"
OPS = ("add", "replace", "remove", "restore")
HISTORY_SHOWN = 50           # 读口最多列几条快照索引

TEXT = json.loads((Path(__file__).with_name("position_knowledge_text.json")).read_text(encoding="utf-8"))


class Refused(Exception):
    """本模块的拒绝。`code` 取 Core 的拒绝类别，`reason_code` 给机器判，两句话来自文字文件。"""

    def __init__(self, code, key, **fields):
        t = TEXT["refusal"][key]
        say = lambda lang: t[lang].format(**{**fields, **({"what": TEXT["what"][fields["what"]][lang]}
                                                         if "what" in fields else {})})
        super().__init__(say("zh"))
        self.code, self.reason_code, self.fields = code, key, fields
        self.zh, self.en = say("zh"), say("en")


# ------------------------------------------------------------------ 渲染（整版正文是派生物）
def render(position, entries):
    """条目 → 整版正文（确定性：同一批条目永远同一份字节）。`id/why/when` 不进正文。"""
    order, by_sec = [], {}
    for e in entries or []:
        sec = (e.get("section") or "").strip() or DEFAULT_SECTION
        if sec not in by_sec:
            by_sec[sec] = []
            order.append(sec)
        by_sec[sec].append((e.get("text") or "").strip())
    lines = ["# " + TEXT["title"]["zh"].format(role=position)]
    for sec in order:
        lines += ["", "## " + sec]
        # 续行保持缩进，旧版本读取快照也能区分条目和子项。
        lines += ["- " + t.replace("\n", "\n  ") for t in by_sec[sec] if t]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ when：确定性匹配
def _path_segments(value):
    """路径规范化：去 `./` 与首尾 `/`、折叠重复分隔；含 `..` 或为空 ⇒ None。"""
    p = posixpath.normpath("/" + str(value).strip().replace("\\", "/")).strip("/")
    if not p or ".." in str(value).replace("\\", "/").split("/"):
        return None
    return tuple(p.split("/"))


def _is_path(item):
    return "/" in item or item.startswith(".")


def normalize_when(when):
    """`when` 入参 → 规范化列表。接受 None / 字符串 / 字符串列表；项目引用小写，路径去 `./`。"""
    if when is None:
        return []
    items = [when] if isinstance(when, str) else when
    if not isinstance(items, (list, tuple)) or len(items) > WHEN_MAX:
        raise Refused("invalid_request", "when_invalid", max=WHEN_MAX, why="count")
    out = []
    for it in items:
        if not isinstance(it, str) or not it.strip() or len(it.strip()) > WHEN_ITEM_MAX:
            raise Refused("invalid_request", "when_invalid", max=WHEN_MAX, why="item")
        it = it.strip()
        if _is_path(it):
            seg = _path_segments(it)
            if seg is None:
                raise Refused("invalid_request", "when_invalid", max=WHEN_MAX, why=it)
            it = "/".join(seg) if len(seg) > 1 else "./" + seg[0]
        else:
            it = it.lower()
        if it not in out:
            out.append(it)
    return out


def _hits(when, project_ref, paths):
    ref = (project_ref or "").strip().lower()
    segs = [s for s in (_path_segments(p) for p in (paths or ())) if s]
    for w in when:
        if _is_path(w):
            ws = _path_segments(w)
            if any(s[:len(ws)] == ws for s in segs):      # 按段前缀：office/lib 不命中 office/library
                return True
        elif ref and w == ref:
            return True
    return False


def select(conn, role, project_ref=None, paths=()):
    """派工时附带的条目：本岗现行条目里只取 `when` 命中者（按项目引用相等、路径按段前缀）。确定性，无模型。"""
    return [e for e in _live(conn, role) if e["when"] and _hits(e["when"], project_ref, paths)]


# ------------------------------------------------------------------ 读
def _entry(r):
    return {"id": r["entry_id"], "section": r["section"], "text": r["text"], "why": r["why"],
            "when": json.loads(r["when_json"]), "version": r["version"],
            "updated_by": r["updated_by"], "updated_at": r["updated_at"]}


def _live(conn, position):
    rows = conn.execute("SELECT * FROM knowledge WHERE position=? AND removed_at IS NULL", (position,)).fetchall()
    return sorted((_entry(r) for r in rows), key=lambda e: int(e["id"][1:]))


def _version(conn, position):
    r = conn.execute("SELECT MAX(ver) AS v FROM knowledge_snapshot WHERE position=?", (position,)).fetchone()
    return int(r["v"] or 0)


def read(conn, position):
    """现行状态：条目 + 岗位版本 + 字数 + 最近的快照索引（正文要读用 `snapshot_text`）。"""
    entries = _live(conn, position)
    snaps = conn.execute("SELECT ver FROM knowledge_snapshot WHERE position=? ORDER BY ver DESC LIMIT ?",
                         (position, HISTORY_SHOWN)).fetchall()
    return {"position": position, "version": _version(conn, position), "entries": entries,
            "chars": len(render(position, entries)), "limit": LIMIT, "entry_limit": ENTRY_LIMIT,
            "snapshots": [s["ver"] for s in snaps]}


def _snapshot(conn, position, ver):
    r = conn.execute("SELECT text FROM knowledge_snapshot WHERE position=? AND ver=?", (position, ver)).fetchone()
    if r is None:
        raise Refused("not_found", "snapshot_not_found", ver=ver)
    raw = r["text"]
    marker = "# office-knowledge-metadata-v1 "
    body, sep, metadata = raw.rpartition("\n" + marker)
    if sep:
        try:
            entries = json.loads(metadata)
            if isinstance(entries, list):
                return {"text": body + "\n", "entries": entries}
        except (ValueError, TypeError):
            pass
    return {"text": raw}


def snapshot_text(conn, position, ver):
    return _snapshot(conn, position, ver)["text"]


# ------------------------------------------------------------------ 写
def _next_id(conn, position):
    rows = conn.execute("SELECT entry_id FROM knowledge WHERE position=?", (position,)).fetchall()
    return max([int(r["entry_id"][1:]) for r in rows] or [0]) + 1      # 含已删/已退役：永不复用


def _text(value, op):
    if not isinstance(value, str) or not value.strip():
        raise Refused("invalid_request", "text_required", op=op)
    return value.strip()


def _need_version(expected, op):
    if expected is None:
        raise Refused("invalid_request", "version_required", op=op)
    if isinstance(expected, bool) or not isinstance(expected, int):
        raise Refused("invalid_request", "field_type", field="expected_version")


def apply(conn, position, actor, now, **kw):
    """`_apply` 套一层保存点：拒绝时本次写入整体撤销（连接上是否另有事务都一样）。"""
    conn.execute("SAVEPOINT position_knowledge")
    try:
        out = _apply(conn, position, actor, now, **kw)
    except BaseException:
        conn.execute("ROLLBACK TO position_knowledge")
        conn.execute("RELEASE position_knowledge")
        raise
    conn.execute("RELEASE position_knowledge")
    return out


def _apply(conn, position, actor, now, *, op, id=None, text=None, section=None, why=None, when=None,
          expected_version=None, to_version=None):
    """在**调用方事务**里做一次写；返回结果事实。合并全由代码做，模型只给增量。

    · add：id 由本模块分配；不需要 expected_version。
    · replace / remove：`expected_version` = 这一条自己的版本（只校验这一条，别人改别条不冲突）。
    · restore：`to_version` = 岗位快照号，`expected_version` = 岗位当前快照号；旧 id 全部退役、新 id 继续递增。
    上限：操作后总长 > LIMIT 且比操作前长 ⇒ 拒（超限的岗位只能变短）；restore 越限放行；
    replace 超 ENTRY_LIMIT 但不比原条长 ⇒ 放行。不截断、不自动压缩。
    """
    if not isinstance(position, str) or not position.strip():
        raise Refused("invalid_request", "position_required")
    if op not in OPS:
        raise Refused("invalid_request", "op_unknown")
    for name, v in (("id", id), ("section", section), ("why", why)):
        if v is not None and not isinstance(v, str):
            raise Refused("invalid_request", "field_type", field=name)
    entries = _live(conn, position)
    before = len(render(position, entries))
    pos_ver = _version(conn, position)
    now_ver = pos_ver + 1
    changed = None
    if op == "restore":
        if to_version is None or isinstance(to_version, bool) or not isinstance(to_version, int):
            raise Refused("invalid_request", "to_version_required")
        _need_version(expected_version, op)
        if expected_version != pos_ver:
            raise Refused("stale", "stale", what="position", expected=expected_version,
                          current=pos_ver)
        parts = _snapshot(conn, position, to_version).get("entries")
        if not isinstance(parts, list):
            raise Refused("capability_missing", "snapshot_metadata_missing", ver=to_version)
        conn.execute("UPDATE knowledge SET removed_at=? WHERE position=? AND removed_at IS NULL", (now, position))
        nid = _next_id(conn, position)
        for i, part in enumerate(parts):
            conn.execute("INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                         (position, "K%d" % (nid + i), part["section"], part["text"], part["why"],
                          json.dumps(part["when"], ensure_ascii=False), 1, actor, now))
        after_entries = _live(conn, position)
    elif op == "add":
        body = _text(text, op)
        if len(body) > ENTRY_LIMIT:
            raise Refused("invalid_request", "entry_too_long", limit=ENTRY_LIMIT, chars=len(body), before=0)
        changed = "K%d" % _next_id(conn, position)
        conn.execute("INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                     (position, changed, (section or "").strip() or DEFAULT_SECTION, body,
                      (why or "").strip(), json.dumps(normalize_when(when), ensure_ascii=False), 1, actor, now))
        after_entries = _live(conn, position)
    else:
        if not id or not id.strip():
            raise Refused("invalid_request", "id_required", op=op)
        cur = next((e for e in entries if e["id"] == id), None)
        if cur is None:
            raise Refused("not_found", "entry_not_found", id=id)
        _need_version(expected_version, op)
        if expected_version != cur["version"]:
            raise Refused("stale", "stale", what="entry", expected=expected_version,
                          current=cur["version"])
        changed = id
        if op == "remove":
            conn.execute("UPDATE knowledge SET removed_at=? WHERE position=? AND entry_id=?", (now, position, id))
        else:
            body = _text(text, op)
            if len(body) > ENTRY_LIMIT and len(body) > len(cur["text"]):
                raise Refused("invalid_request", "entry_too_long", limit=ENTRY_LIMIT, chars=len(body),
                              before=len(cur["text"]))
            conn.execute(
                "UPDATE knowledge SET text=?, why=?, when_json=?, section=?, version=version+1, updated_by=?, "
                "updated_at=? WHERE position=? AND entry_id=?",
                (body, (why if why is not None else cur["why"]).strip(),
                 json.dumps(normalize_when(when) if when is not None else cur["when"], ensure_ascii=False),
                 (section or "").strip() or cur["section"], actor, now, position, id))
        after_entries = _live(conn, position)
    body_after = render(position, after_entries)
    after = len(body_after)
    if op != "restore" and after > LIMIT and after > before:        # 事务内：拒绝由调用方回滚
        oldest = sorted(after_entries, key=lambda e: (e["updated_at"], e["id"]))[:5]
        raise Refused("invalid_request", "over_limit", limit=LIMIT, before=before, chars=after,
                      oldest=[{"id": e["id"], "chars": len(e["text"]), "updated_at": e["updated_at"]} for e in oldest])
    metadata = [{k: e[k] for k in ("section", "text", "why", "when")} for e in after_entries]
    snapshot = body_after + "# office-knowledge-metadata-v1 " + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    conn.execute("INSERT INTO knowledge_snapshot VALUES(?,?,?)", (position, now_ver, snapshot))
    return {"ok": True, "position": position, "op": op, "id": changed, "version": now_ver,
            "entry_version": next((e["version"] for e in after_entries if e["id"] == changed), None),
            "chars": after, "limit": LIMIT, "entries": len(after_entries), "updated_by": actor, "updated_at": now}


# ------------------------------------------------------------------ 上下文合同
def duties_of(spec):
    """从该岗位在组织配置里的登记（`aliases.<role>`）取职责 `role.duties[]`。"""
    role = spec.get("role") if isinstance(spec, dict) else None
    role = role if isinstance(role, dict) else {}
    return [d.strip() for d in (role.get("duties") or []) if isinstance(d, str) and d.strip()]


def summary(conn, position, spec):
    """送进 `office.context` 的**本岗**摘要：职责 + 通用条目（`when` 为空），总字数不超过 `LIMIT`。

    只读本岗自己的行；别的岗位、历史、快照都不碰。装不下的按序截断并记 `omitted`（读口取全）；
    有 `when` 的条目只数个数（`scoped`），随派工送达。
    """
    duties = duties_of(spec)
    entries = _live(conn, position)
    general = [e for e in entries if not e["when"]]
    shown, used = [], 0
    for e in general:
        n = len(e["text"])
        if used + n > LIMIT:
            break
        shown.append({"id": e["id"], "section": e["section"], "text": e["text"]})
        used += n
    return {"duties": duties,
            "knowledge": {"entries": shown, "chars": used, "limit": LIMIT,
                          "omitted": len(general) - len(shown), "scoped": len(entries) - len(general)}}


def navigation_lines(positions, zh=True):
    """`navigation` 里的职责与经验行（同一份事实的正文渲染，文字来自文字文件）。"""
    lang = "zh" if zh else "en"
    nav, out = TEXT["navigation"], []
    sep = "；" if zh else "; "
    for p in positions:
        if p.get("duties"):
            out.append(nav["duties"][lang].format(role=p["role"], duties=sep.join(p["duties"])))
        k = p.get("knowledge") or {}
        if k.get("entries"):
            out.append(nav["knowledge"][lang].format(role=p["role"], count=len(k["entries"]),
                                                      chars=k["chars"], limit=k["limit"]))
            out += ["- " + e["text"].replace("\n", " ") for e in k["entries"]]
        if k.get("omitted"):
            out.append(nav["omitted"][lang].format(n=k["omitted"]))
        if k.get("scoped"):
            out.append(nav["scoped"][lang].format(n=k["scoped"]))
    return out


def dispatch_lines(conn, roles, project_ref=None, paths=()):
    """派工通知里附带的经验行：受派人每个岗位里 `when` 命中这件 Work 的条目（没有就是空列表；派工信是中文）。"""
    out = []
    for role in roles:
        hit = select(conn, role, project_ref, paths)
        if hit:
            out.append(TEXT["dispatch"].format(role=role))
            out += ["- " + e["text"].replace("\n", " ") for e in hit]
    return out
