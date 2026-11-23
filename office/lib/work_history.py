"""Work 的完整履历：事件流水、已决定的审核轮次、材料与关系的每一次变化。

三件东西都只有**一个事实来源**，这里只做投影，不另存状态：

  · **事件**（`work_events`）—— 每一次受控动作写一条，payload 里就是那一次的原样字段
    （`sub_ref` / `review_ref` / `change_set` / `reason` / `evidence` / `from` / `to`）。这是
    "谁、几点、做了什么、引用什么"的唯一出处。
  · **审核轮次**（`reviews`）—— 每一轮是一条已作出的决定。它和事件不是两份：事件说"那一步
    发生了什么"，轮次说"这一份提交被怎么判的"。履历要回答后者，所以按提交归一次组。
  · **材料与关系**（`inputs` / `work_relations`）—— 当前还在的那些由 `work_business.get` 给；
    这里额外给"哪些被改过、被谁、在什么时候"，因为它只写在事件 payload 里，读当前行是看不到的。

`history=true` 才给这份完整履历。默认的 `work.get` 只给现状 —— 履历随历史线性增长，
而日常处理靠的是当前状态、准确提交和最近一次退回（§7 第 13 条：看清最新决定、按引用找回原文）。

`todo` 不在这里：它就是"现在能动手的 Work"，唯一实现是 `work_runnable.runnable_work`。
同一个人在两处得到不同答案，比少一个读口糟得多。
"""
import json

#: 事件 op → 履历里的一句话（不解析 payload 的措辞，只按 op 选词）。
#: 词表以外的一律 `work.event`：宁可少描述一句，也不为未知 op 编一个说法。
EVENT_ZH = {
    "work.create": "建 Work 并派工",
    "work.assign": "改派",
    "work.reviewer": "改审核安排",
    "work.edit": "改约定",
    "work.references": "补材料",
    "work.wait": "写等待说明",
    "work.continue": "解除等待",
    "work.cancel": "取消",
    "work.publish": "交付",
    "work.submit": "交付",
    "work.withdraw": "撤回提交",
    "work.review": "审核决定",
}
EVENT_EN = {
    "work.create": "created and assigned this Work",
    "work.assign": "reassigned",
    "work.reviewer": "changed the review arrangement",
    "work.edit": "edited the goal",
    "work.references": "added material",
    "work.wait": "declared a wait",
    "work.continue": "cleared the wait",
    "work.cancel": "cancelled",
    "work.publish": "delivered",
    "work.submit": "delivered",
    "work.withdraw": "withdrew a submission",
    "work.review": "decided a submission",
}


def _payload(raw):
    """事件 payload → dict。读不懂就是空 dict（一条坏行不该让整段履历失败）。"""
    try:
        got = json.loads(raw or "{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def events(conn, work_ref, *, loc="zh"):
    """这件 Work 的每一次受控动作，按发生顺序。

    每一步给：序号、时刻、谁、op、一句话、以及那一步真正指向的东西（`ref` / `review_ref`）。
    **原文不动**：`payload` 只给这一次的字段名，不给整段 JSON（要原文去 `work.events`，
    那是它自己的受控读口）。少给一份副本，就少一处会漂的说法。
    """
    words = EVENT_ZH if loc == "zh" else EVENT_EN
    out = []
    for e in conn.execute("SELECT seq, op, payload_json, version_after, epoch_after, actor, acted_at"
                          " FROM work_events WHERE work_ref=? ORDER BY seq", (work_ref,)).fetchall():
        p = _payload(e["payload_json"])
        op = e["op"]
        step = {"seq": e["seq"], "at": e["acted_at"], "who": e["actor"], "op": op,
                "what": words.get(op, words.get("work.event", op)),
                "version_after": e["version_after"]}
        if op == "work.create":
            step["assignee"] = p.get("assignee")
            step["reviewer"] = p.get("reviewer")
        elif op == "work.assign":
            step["from"], step["to"] = p.get("from"), p.get("to")
            if p.get("scope"):
                step["scope"] = p["scope"]
        elif op == "work.reviewer":
            # `to` 为空且 `mode == "none"` 是**取消审核**，不是"换成空"——同一个 op 两种语义，
            # 判据只用 Core 自己记的字段。
            step["mode"] = p.get("mode")
            if p.get("mode") == "none":
                step["what"] = "取消审核" if loc == "zh" else "removed the reviewer"
            else:
                step["to"] = p.get("to")
        elif op in ("work.publish", "work.submit", "work.withdraw"):
            step["sub_ref"] = p.get("sub_ref")
            if p.get("final"):
                step["final"] = True
            if p.get("replaces"):
                step["replaces"] = p["replaces"]
            if p.get("completion_basis"):
                step["completion_basis"] = p["completion_basis"]
        elif op == "work.review":
            step["sub_ref"] = p.get("sub_ref")
            step["decision"] = p.get("decision")
            step["reason"] = p.get("reason") or ""
            if p.get("evidence"):
                step["evidence"] = p["evidence"]
        elif op in ("work.wait", "work.continue"):
            step["waiting"] = p.get("waiting")
        elif op == "work.references":
            step["added"] = p.get("added") or []
            step["removed"] = p.get("removed") or []
        elif op == "work.cancel":
            step["reason"] = p.get("reason") or ""
        out.append(step)
    return out


def review_view(r):
    """一次审核轮次的形状 —— **只有这一处**（履历里的轮次与 `work.get` 的退回意见共用）。"""
    return {"review_ref": r["review_ref"], "sub_ref": r["sub_ref"], "decision": r["decision"],
            "reviewer": r["reviewer_ref"], "decided_at": r["decided_at"],
            "reason": r["reason"] or ""}


def review_rounds(conn, work_ref):
    """已作出的审核轮次：每一份提交被怎么判的（可以为空 = 还没审过）。

    `state` 三态分开说：`accepted` / `returned` / `pending`（交上去了还没判）。免审的 Work
    没有轮次，这是"免审"而不是"尚未审核"—— 由调用方按 `review_mode` 区分，不在这里猜。
    """
    rounds = []
    for r in conn.execute("SELECT * FROM reviews WHERE work_ref=? ORDER BY rowid",
                          (work_ref,)).fetchall():
        rounds.append(dict(review_view(r), evidence=_evidence(r)))
    decided = {round_["sub_ref"] for round_ in rounds}
    pending = [s["sub_ref"] for s in conn.execute(
        "SELECT sub_ref FROM subs WHERE work_ref=? AND state='effective' ORDER BY seq",
        (work_ref,)).fetchall() if s["sub_ref"] not in decided]
    return {"rounds": rounds, "pending": pending,
            "count": len(rounds), "has_pending": bool(pending)}


def _evidence(r):
    """审核依据：是不是存在这一列、有没有内容，都照实给。"""
    try:
        raw = r["evidence_json"]
    except (IndexError, KeyError):
        return []
    if not raw:
        return []
    try:
        got = json.loads(raw)
    except ValueError:
        return []
    return got if isinstance(got, list) else []


def material_changes(conn, work_ref, *, live_only=False):
    """材料引用的现役与被撤下的两列；`live_only=True` 由 SQL 只取未撤的（默认读走这条）。"""
    sql = ("SELECT * FROM inputs WHERE work_ref=?" + (" AND removed_at IS NULL" if live_only else "")
           + " ORDER BY seq")
    live, removed = [], []
    for i in conn.execute(sql, (work_ref,)).fetchall():
        item = {"seq": i["seq"], "resource": i["ref"], "kind": i["kind"],
                "version": i["exact_version"] or None, "sub": i["sub_ref"],
                "note": i["note"] or "", "recorded_by": i["recorded_by"],
                "recorded_at": i["acted_at"]}
        if i["removed_at"]:
            item["removed_at"] = i["removed_at"]
        (removed if i["removed_at"] else live).append(item)
    return {"live": live, "removed": removed}


def _relation_item(r, *, toward=False):
    """一条关系的形状 —— **只有这一处**；`toward` 是"谁指向我"那一向。"""
    item = {"relation_id": r["id"], "work": r["work_ref"] if toward else r["target_work_ref"],
            "relation": r["relation"]}
    if not toward:
        item["note"] = r["note"] or ""
    if r["removed_at"]:
        item["removed_at"] = r["removed_at"]
    return item


def relation_changes(conn, work_ref, *, live_only=False, reverse=True, may_read=None):
    """关系的两向（撤掉的靠 `removed_at` 辨认）；`live_only` 只取未撤，`reverse=False` 不查反向。

    `may_read(work_ref)`：**两个方向**的对端读权判据，由**调用方**在同一事务里给（本模块不写
    第二套 SQL）。给了就丢掉读不到的对端 —— 否则一个只读得到公开 Work 的人，能从关系行里读到
    私密项目那一端的 `work_ref`（他对那件 Work 本身是 `forbidden`）。
    """
    cond = " AND removed_at IS NULL" if live_only else ""
    mine, toward_me = [], []
    for r in conn.execute("SELECT * FROM work_relations WHERE work_ref=?" + cond + " ORDER BY id",
                          (work_ref,)).fetchall():
        # **两个方向同一份判据**：`mine` 这半边也带对端 `work_ref`，"我指向一个读不到的私密
        # Work"同样把它的编号交出去。曾经只过滤 `toward_me`，于是 `relations` 那一列是敞开的。
        if may_read is not None and not may_read(r["target_work_ref"]):
            continue
        mine.append(_relation_item(r))
    if reverse:
        for r in conn.execute("SELECT * FROM work_relations WHERE target_work_ref=?" + cond
                              + " ORDER BY id", (work_ref,)).fetchall():
            if may_read is not None and not may_read(r["work_ref"]):
                continue
            toward_me.append(_relation_item(r, toward=True))

    return {"mine": mine, "toward_me": toward_me}


SPLIT_CACHE_INPUT = frozenset({"claude", "opencode", "pi"})


def usage_sum(rows):
    """一组用量行的合计（履历卡与 `usage.read` 共用的唯一一份加法）。

    入参是已记录的完整输入，缓存读是其中一项。旧记录未采集的缓存写不补猜。
    token 三格都是**一格缺则合计缺**（`None`）：宿主没报的不能当 0 加起来；
    `estimated_events` = 其中 `attribution="estimated"` 的条数（分母仍是 `events`）。
    """
    out = {"events": 0, "estimated_events": 0, "tokens": {"in": None, "out": None, "cache": None}}
    for r in rows:
        first = out["events"] == 0
        out["events"] += 1
        out["estimated_events"] += (r["attribution"] or "") == "estimated"
        tokens = out["tokens"]
        for key in tokens:
            value = r["tokens_" + key]
            if key == "in" and value is not None and r["harness"] in SPLIT_CACHE_INPUT:
                value += r["tokens_cache"] or 0
            tokens[key] = value if first else (
                None if tokens[key] is None or value is None else tokens[key] + value)
    return out


def usage(conn, work_ref):
    """本件的用量卡：承办与审核各自归拢，不在 Work 或人之间平分（调用方已判过读权）。

    `covered=False` = 本件一条记录都没有（未覆盖，不是用了 0）；`role_in_work` 归不到承办/审核的行
    不挑一个角安上，逐条单列进 `unattributable`；别的 Work 与 `work_ref` 为空的未归属用量不进本卡
    （后者在 `usage.read` 里单列）。`total` = 各行之和。
    """
    rows = conn.execute("SELECT * FROM usage WHERE work_ref=? ORDER BY at, entry_id", (work_ref,)).fetchall()
    by = {k: [r for r in rows if r["role_in_work"] == k] for k in ("assignee", "reviewer")}
    def role_usage(items):
        if not items:
            return None
        models = {}
        for row in items:
            models.setdefault(row["model"] or None, []).append(row)
        return {**usage_sum(items), "by_model": [
            {"model": model, **usage_sum(group)} for model, group in models.items()]}

    return {"work_ref": work_ref, "covered": bool(rows),
            "assignee": role_usage(by["assignee"]), "reviewer": role_usage(by["reviewer"]),
            "unattributable": [{**usage_sum([r]), "actor": r["actor"], "role_in_work": r["role_in_work"]}
                               for r in rows if r["role_in_work"] not in by],
            "total": usage_sum(rows) if rows else None}


def history(conn, work_ref, *, loc="zh", may_read=None):
    """完整履历：事件流 + 已决定的审核轮次 + 材料/关系的全部变化 + 用量。

    每一块各自按自己的表取一次，不做交叉推导（"事件里有几条 review"不等于"审了几轮"——
    撤回、替换、改派也会写事件；判"审了几轮"只有 `reviews` 算数）。
    """
    return {"events": events(conn, work_ref, loc=loc),
            "reviews": review_rounds(conn, work_ref),
            "materials": material_changes(conn, work_ref),
            "relations": relation_changes(conn, work_ref, may_read=may_read),
            "usage": usage(conn, work_ref),
            "released": [dict(r) for r in conn.execute(
                "SELECT release_ref, version, source_commit, at FROM releases WHERE EXISTS"
                " (SELECT 1 FROM json_each(work_refs_json) WHERE value=?) ORDER BY rowid", (work_ref,))]}
