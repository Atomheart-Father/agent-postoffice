"""Work 可续判据 —— 这个人此刻还有没有**能动手**的责任。

看门狗的「有活却闲着」（产品 §9）与续办入口问的是同一句话，所以只有这一份实现。规格给的
边界是三条，缺一不可：

  · **前置都已满足** —— 它 `waits_for` 的每一件都已 Done（被取消的不算满足，依赖不会自己解开）；
  · **没有在等任何人或任何结果** —— 没有 `waiting_json`，也没有一份已经交上去、正等着别人审的
    现行提交；
  · **确实有能执行的动作** —— 承办人可以提交，或者审核人可以审这一份。

只凭"有 Work 在进行、又没有显式声明等待"就下结论，会把"等着审核"错报成有活：那也正是审核人
缺席时最容易被误唤醒的一类。所以"等审核"在这里是一条独立的排除原因，而不是被当成有活。

调用面：交付模块在**已有的读事务**里调用 `runnable_work(conn, actor, unit)`，不新开事务、
不写任何行。返回的 `items` 就是现在能动手的那些；`skipped` 只报各类被滤掉几条 —— 排除原因要
可观察，但它不是第二份判据（分类与筛选在同一个循环里产生，不会互相漂）。

`unit` 是投递单元（个人箱或岗位地址），与 `letter.accept` / `round.begin` 同一份单元合同：
承办关系可能是写在岗位上的，那个岗位此刻的现任才是能动手的人。
"""
WORK_OPEN = ("Active", "Submitted")
WORK_DONE = "Done"
SUB_EFFECTIVE = "effective"

#: 被滤掉的原因码。值就是返回值里 `skipped` 的键，外部（看门狗、页面）不要另起词。
SKIP_CLOSED = "closed"                    # 已经 Done/Cancelled：不再有责任
SKIP_NOT_MINE = "not_current_holder"      # 承办/审核写的是别人或已换任
SKIP_DEPENDENCY = "dependency_open"       # 还有 waits_for 没完成
SKIP_WAITING = "waiting"                  # 明确写了等待说明
SKIP_WAITING_REVIEW = "waiting_for_review"  # 球在审核人手上
SKIP_SELF_REVIEW = "self_review"          # 自己交的，不能自己审
SKIP_UNIT = "unit_not_mine"               # 问的那个投递单元不是本人的任职

#: 当前持岗的取值处只有 `work_result._positions_held` 一处：读权、承办、可续问的是同一句。
from work_result import _positions_held  # noqa: E402,F401


def _holders(conn, actor):
    """这个会话此刻**替谁**承办：本人 + 本人现任的岗位。

    `unit` 不在这里 —— 它是"问哪个单元的队"，不是身份。把它当身份加进来，任何人只要在参数里
    写一个岗位地址就能领走那个岗位的全部待办，这正是受控入口不允许的自报身份。
    """
    return {actor} | _positions_held(conn, actor)


def _open_rows(conn, holders):
    """现在仍是开口状态、且承办或审核落在这几个地址上的 Work。"""
    marks = ",".join("?" * len(holders))
    args = tuple(sorted(holders))
    return conn.execute(
        "SELECT work_ref, project_ref, objective, state, version, epoch, assignee_ref,"
        "       reviewer_ref, review_mode, waiting_json, created_at"
        "  FROM work"
        " WHERE state IN (?, ?)"
        "   AND (assignee_ref IN (%s) OR reviewer_ref IN (%s))"
        " ORDER BY created_at, work_ref" % (marks, marks),
        (WORK_OPEN[0], WORK_OPEN[1]) + args + args).fetchall()


def _chunks(refs, size=200):
    """按 SQLite 变量上限切块（两处查询共用这一处切法）。"""
    for start in range(0, len(refs), size):
        chunk = refs[start:start + size]
        if chunk:
            yield chunk


def _open_dependencies(conn, work_refs):
    """这些 Work 还开着的 `waits_for`：`work_ref` → 尚未完成的目标清单。

    方向是单向的（后端规格 §1.4 依赖方向）：一行 `waits_for` 的意思是"这个 Work 在等那个 Work
    完成"。已 `removed_at` 的关系不算依赖 —— 关系撤掉就是不再等。
    """
    out = {}
    refs = list(work_refs)
    for chunk in _chunks(refs):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
                "SELECT w.work_ref, t.state AS target_state"
                "  FROM work_relations w"
                "  LEFT JOIN work t ON t.work_ref = w.target_work_ref"
                " WHERE w.relation = 'waits_for' AND w.removed_at IS NULL"
                "   AND w.work_ref IN (%s)" % marks, tuple(chunk)).fetchall():
            if r["target_state"] != WORK_DONE:
                out.setdefault(r["work_ref"], []).append(r["target_state"])
    return out


def _pending_subs(conn, rows):
    """每一件 Work 现在**交上去等着**的那一份：`work_ref` → (sub_ref, submitted_by)。

    判据与读面 `_current_sub` 同一组事实：本期（`epoch_at_submit == work.epoch`）的 effective
    提交。摘要是否仍等于自己的 change_set 在这里不再核一遍 —— 那是读面回答"这一份该不该审"的
    判据；可续判据只问"球在谁手上"，而一份摘要被改坏的提交仍然占着"等审"这个位置，把它当成
    没人接手会更糟（会误唤醒承办人）。两处因此不共用这个细节，但都只从 `subs` 取事实。
    """
    out = {}
    refs = [r["work_ref"] for r in rows]
    for chunk in _chunks(refs):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
                "SELECT s.work_ref, s.sub_ref, s.submitted_by"
                "  FROM subs s JOIN work w ON w.work_ref = s.work_ref"
                " WHERE s.state = ? AND s.epoch_at_submit = w.epoch"
                "   AND s.work_ref IN (%s)"
                " ORDER BY s.seq" % marks, (SUB_EFFECTIVE,) + tuple(chunk)).fetchall():
            out[r["work_ref"]] = (r["sub_ref"], r["submitted_by"])
    return out


def same_submitter(conn, work_ref, sub_ref, submitted_by, actor, person):
    if submitted_by == person:
        return True
    row = conn.execute("SELECT json_extract(payload_json,'$.submitted_person') FROM work_events"
                       " WHERE work_ref=? AND op='work.publish'"
                       " AND json_extract(payload_json,'$.sub_ref')=? LIMIT 1", (work_ref, sub_ref)).fetchone()
    if row and row[0]:
        return row[0] == person
    if submitted_by == actor:
        return True
    if conn.execute("SELECT 1 FROM position_holders WHERE position=?", (submitted_by,)).fetchone():
        return None
    return False


def runnable_work(conn, actor, unit=None, *, now_ts=None, person=None):
    """现在就能动手的 Work（看门狗与续办入口的唯一判据）。

    `conn` 必须是调用方**已有的**读事务/连接；`actor` 是受控入口给出的身份，不从正文自报；
    `unit` 可选（个人箱或岗位地址）。`now_ts` 只用于回填返回值里的时刻，不参与判据 ——
    到期项（`due`）才是时间的判据，这条路不自己算时间。
    """
    if not isinstance(actor, str) or not actor:
        raise ValueError("runnable_work needs a controlled actor")
    if unit is not None:
        if not isinstance(unit, str) or not unit:
            raise ValueError("runnable_work takes a non-empty unit or None")
        # 只能替本人或本人现任的岗位回答（与 `letter.accept` 的单元合同同一口径）。
        if unit != actor and unit not in _positions_held(conn, actor):
            return {"actor": actor, "unit": unit, "count": 0, "items": [],
                    "skipped": {SKIP_UNIT: 1}, "at": now_ts}
    holders = {unit} if unit is not None else _holders(conn, actor)
    rows = _open_rows(conn, holders)
    deps = _open_dependencies(conn, [r["work_ref"] for r in rows])
    pend = _pending_subs(conn, rows)

    skipped = {}
    items = []
    for row in rows:
        ref = row["work_ref"]

        def skip(code):
            skipped[code] = skipped.get(code, 0) + 1

        if row["state"] not in WORK_OPEN:
            skip(SKIP_CLOSED)
            continue
        # 依赖先判：它是不靠人的事实，和"谁该动"无关。
        if deps.get(ref):
            skip(SKIP_DEPENDENCY)
            continue
        # 明确写了等待（`waiting_json` 有内容）就照它办。读不懂的 json 也仍然算等待：
        # 宁可少报一次有活，也不误唤醒一个正在等的人。
        if row["waiting_json"]:
            skip(SKIP_WAITING)
            continue

        mine = row["assignee_ref"] in holders
        reviewing = row["reviewer_ref"] in holders
        sub = pend.get(ref)
        action = None
        if row["state"] == WORK_OPEN[1]:            # Submitted
            if sub is None:
                # 在审状态却没有现行提交：事实对不上，不猜"该谁动手"。
                skip(SKIP_NOT_MINE)
                continue
            if reviewing and same_submitter(conn, ref, *sub, actor, person or actor) is False:
                action = "review"
            else:
                # 承办人在等审核人的结论（含"自己交的不能自己审"）。
                skip(SKIP_SELF_REVIEW if reviewing else SKIP_WAITING_REVIEW)
                continue
        else:                                        # Active
            if not mine:
                skip(SKIP_NOT_MINE)
                continue
            action = "publish"

        item = {"work_ref": ref, "action": action, "state": row["state"],
                "project_ref": row["project_ref"], "objective": row["objective"],
                "version": row["version"], "assignee": row["assignee_ref"],
                "reviewer": row["reviewer_ref"]}
        if action == "review":
            item["sub_ref"] = sub[0]
        items.append(item)

    return {"actor": actor, "unit": unit, "count": len(items), "items": items,
            "skipped": skipped, "at": now_ts}
