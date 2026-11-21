"""Work 的两个写动作：交付（publish）与判定（review），以及完成后的自动续。

这一层只决定**业务事实**：谁能动、动完 Work 变成什么、哪些下游因此被解开。字节怎么落库
（内容寻址、摘要、临时文件）归 `r/publish.py`；提交行怎么记、内容怎么核归 `work_result.py`；
"现在谁还能动手"归 `work_runnable.py`。这里不复制它们任何一条判据。

一次调用 = 一个业务动作，在**调用方的事务里**完成，返回可直接当结果用的事实。幂等键由调用方
给出（`act_key`），同一个键同参数重放返回首次的结果，异参数拒 `conflict` —— 用的是 storage 的
`action_replay` / `record_action`，不另写一份幂等账。

`release_dependents` 是"续"的那一段：一件 Work 完成时，把**所有前置都已满足**的下游挑出来。
它只给事实（哪些 Work 现在可以动了），发不发信是投递的事 —— Work 不写信。
"""
import hashlib
import json

from position_knowledge import TEXT as KNOWLEDGE_TEXT
from work_result import (ResultRefused, _work_row, record_result, digest_change_set, now_text,
                         change_set_of, require_actor, require_version, _positions_held)
from work_runnable import _holders, _open_dependencies

WORK_ACTIVE, WORK_SUBMITTED, WORK_DONE, WORK_CANCELLED = "Active", "Submitted", "Done", "Cancelled"
SUB_EFFECTIVE, SUB_ACCEPTED, SUB_RETURNED, SUB_REPLACED = "effective", "accepted", "returned", "replaced"
DECISIONS = ("accept", "return")


# --------------------------------------------------------------------------- facts

def _is_holder(conn, actor, row):
    """承办人是不是这个会话 —— 与可续判据同一个集合（本人 + 本人现任的岗位）。"""
    return row["assignee_ref"] in _holders(conn, actor)


def _scopes(row):
    """这件 Work 的交付范围：多范围列有值就用它，否则退回单个 `scope_ref`。

    NULL 表示"当时只有一个范围"，与"范围为空"不是同一件事，所以绝不返回空列表。
    """
    return json.loads(row["resource_scope_json"] or "null") or [row["scope_ref"]]


def _outside_scope(scope, change_set, scope_covers=None):
    """哪些成果落在交付范围之外：`change_set.ref` 自带交付范围前缀（`r/publish` 就这么拼），
    所以判据只有一条 —— ref 是否在 Work 的某个范围之下。★ 曾经还有第二个坐标（"登记根落在范围内"），
    而登记根是从 `scope[0]` 反查的 ⇒ 恒真、单范围时整条判据形同虚设，已删。`scope_covers` 给了就一并问。"""
    from work_artifact import scope_contains
    out = []
    for c in change_set:
        if not isinstance(c, dict):
            continue
        ref = c.get("ref") or ""
        if scope_covers is not None and scope_covers(scope, ref):
            continue
        if scope_contains(scope, ref):
            continue
        out.append(ref)
    return out


def _current_sub(conn, row):
    """桌上那一份：本期 effective 提交、且摘要仍等于它自己的 change_set（与读面同一组条件）。

    **不看 Work 状态**：免审 Work 交了阶段成果仍停在 Active，但那份确实在桌上（再交要写 `replaces`，
    撤回也要认得它）；"是否进入审核"由调用方另判。以前用 `state != Submitted` 当门，免审 Work
    于是永远看不见自己那份提交（能重复交付、撤不回来）。摘要被改坏的那份不算现行提交。"""
    for s in conn.execute("SELECT * FROM subs WHERE work_ref=? AND state=? ORDER BY seq",
                          (row["work_ref"], SUB_EFFECTIVE)).fetchall():
        if s["epoch_at_submit"] != row["epoch"]:
            continue
        if s["sub_digest"] != digest_change_set(change_set_of(s["change_set_json"])):
            continue
        return s
    return None


def _review_ref(work_ref, n):
    """审核引用：`R2-W1.1`（项目号 + Work 序号 + 轮次），与既有编号形状一致。"""
    prj, _, work = work_ref.rpartition("/")
    return "R%s-W%s.%d" % (prj.rsplit("-", 1)[-1], work[1:], n)


def _bump(conn, row, op, payload, actor, now, act_key, sets):
    """版本 +1、写事件、返回新版本号。`epoch` 只在"换了评审期"时动，这里不动。

    `sets` 可以空：`references` 的变化在 `inputs`/`work_relations` 的行上，不在 `work` 的列上。
    """
    version = row["version"] + 1
    cols = dict(sets)
    cols["version"] = version
    conn.execute("UPDATE work SET %s WHERE work_ref=?" % ", ".join("%s=?" % k for k in cols),
                 (*cols.values(), row["work_ref"]))
    conn.execute(
        "INSERT INTO work_events(work_ref, op, payload_json, version_after, epoch_after, actor,"
        " acted_at, act_key) VALUES(?,?,?,?,?,?,?,?)",
        (row["work_ref"], op, json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")),
         version, row["epoch"], actor, now, act_key))
    return version


def _require_open(row, work_ref, what):
    """终结态那道门（`publish` 与 `update` 共用）：Done/Cancelled 不再改，只有 `references` 豁免。"""
    if row["state"] not in (WORK_ACTIVE, WORK_SUBMITTED):
        raise ResultRefused("conflict", "%s 已%s，不能再%s；后续修改请新建关联 Work"
                            % (work_ref, row["state"], what),
                            en="%s is already %s and cannot be changed any more; continue in a related "
                               "Work" % (work_ref, row["state"]),
                            work_ref=work_ref, state=row["state"], reason_code="work_closed")


def _require_current_sub(conn, row, sub_ref):
    """`sub_ref` 必须正是这件 Work 现在**待审**的那一份（两处同一句话、同一份事实）。"""
    current = _current_sub(conn, row)
    if current is None or current["sub_ref"] != sub_ref:
        raise ResultRefused("conflict", "%s 不是 %s 当前待审的提交" % (sub_ref, row["work_ref"]),
                            en="%s is not the current submission of %s" % (sub_ref, row["work_ref"]),
                            work_ref=row["work_ref"], sub_ref=sub_ref, reason_code="sub_not_current")
    return current


def release_dependents(conn, work_ref):
    """这件 Work 完成后，**所有前置都满足**的下游（`waits_for` 指向它）：`[{work_ref, assignee,
    reviewer, objective}]`。取消/未完成的前置算不满足，`removed_at` 的不算依赖。发信归投递。

    "还有没有开着的 `waits_for`"就是可续判据那一条（`work_runnable._open_waits`，批量）——
    这里不再自己写一遍同义的 SQL：两份只会一处放宽、一处没改。
    """
    downs = [r["work_ref"] for r in conn.execute(
        "SELECT DISTINCT work_ref FROM work_relations"
        " WHERE target_work_ref=? AND relation='waits_for' AND removed_at IS NULL",
        (work_ref,)).fetchall()]
    still_open = _open_dependencies(conn, downs)
    out = []
    for ref in downs:
        down = _work_row(conn, ref, or_none=True)
        if down is None or down["state"] in (WORK_DONE, WORK_CANCELLED) or ref in still_open:
            continue
        out.append({"work_ref": down["work_ref"], "assignee": down["assignee_ref"],
                    "reviewer": down["reviewer_ref"], "objective": down["objective"]})
    return out


# --------------------------------------------------------------------------- publish

def plan(conn, actor, *, work_ref, change_set, expected_version=None, replaces=None,
         scope_covers=None, now_ts=None):
    """交付前的**业务预检**（承办/状态/范围/版本/在途）：`publish` 与 Core 的"落字节之前"共用它
    —— 同一个函数调两次，不是两套规则。返回 `(row, pending, now)`，不成立就抛 `ResultRefused`。"""
    row = _work_row(conn, work_ref)
    if not _is_holder(conn, actor, row):
        raise ResultRefused("unauthorized_attempt",
                            "只有当前执行者（%s）可以交付 %s" % (row["assignee_ref"], work_ref),
                            en="Only the current assignee (%s) can publish %s"
                               % (row["assignee_ref"], work_ref),
                            work_ref=work_ref, reason_code="not_assignee", needs="assignee")
    _require_open(row, work_ref, "交付")
    scope = _scopes(row)
    out_of_scope = _outside_scope(scope, change_set, scope_covers)
    if out_of_scope:
        raise ResultRefused("out_of_scope",
                            "提交成果超出 Work 交付范围 %s：%s" % (scope, sorted(out_of_scope)),
                            en="These results are outside this Work's delivery scope %s: %s"
                               % (scope, sorted(out_of_scope)),
                            work_ref=work_ref, out_of_scope=sorted(out_of_scope))
    require_version(row, expected_version, work_ref)

    now = now_text(now_ts)
    pending = _current_sub(conn, row)
    if pending is not None:
        if replaces != pending["sub_ref"]:
            raise ResultRefused("conflict",
                                "已有待审提交 %s：替换要写 replaces=%s，或先撤回"
                                % (pending["sub_ref"], pending["sub_ref"]),
                                en="Submission %s is already awaiting review: pass replaces=%s, or withdraw it"
                                   % (pending["sub_ref"], pending["sub_ref"]),
                                work_ref=work_ref, current_sub=pending["sub_ref"],
                                reason_code="pending_submission")
        if pending["submitted_by"] != actor:
            raise ResultRefused("unauthorized_attempt", "只能替换本人的在途提交",
                                en="You can only replace your own submission in flight",
                                sub_ref=pending["sub_ref"], reason_code="not_submitter", needs="submitter")
    elif replaces:
        raise ResultRefused("conflict", "%s 不是当前待审提交（已被判定、撤回或替换）" % replaces,
                            en="%s is not the current submission (already judged, withdrawn or replaced)"
                               % replaces,
                            work_ref=work_ref, replaces=replaces, reason_code="not_current")

    return row, pending, now


def replay_key(work_ref, change_set, note, replaces, final):
    """`work.publish` 的幂等键：Core 也用它问"这一批是不是回放"——回放不该被预检拦下。"""
    return {"work_ref": work_ref, "change_set": change_set, "note": note,
            "replaces": replaces, "final": bool(final)}


def publish(storage, conn, actor, *, work_ref, change_set, store_root, act_key, note="",
            replaces=None, expected_version=None, final=False, now_ts=None, scope_covers=None):
    """交付一件 Work 的成果。返回这次提交的事实（含哪些下游因此被解开）。

    在**调用方的事务里**执行；不自己开事务、不提交。
    """
    require_actor(actor)
    note = note or ""
    replay, digest = storage.action_replay(conn, actor, "work.publish", act_key,
                                          replay_key(work_ref, change_set, note, replaces, final))
    if replay is not None:
        return replay

    row, pending, now = plan(conn, actor, work_ref=work_ref, change_set=change_set,
                             expected_version=expected_version, replaces=replaces,
                             scope_covers=scope_covers, now_ts=now_ts)
    if pending is not None and replaces:                 # 预检只读，替换在这里落（一次）
        conn.execute("UPDATE subs SET state=? WHERE sub_ref=?", (SUB_REPLACED, replaces))

    recorded = record_result(conn, actor, work_ref=work_ref, change_set=change_set,
                             store_root=store_root, epoch=row["epoch"], act_key=act_key,
                             note=note, final=final, now_ts=now_ts)
    verified = [c["ref"] for c in recorded["change_set"] if c.get("content_verified")]
    unverified = [{"ref": c["ref"], "reason": (c.get("content_check") or {}).get("reason")}
                  for c in recorded["change_set"] if not c.get("content_verified")]

    # 免审的终交直接完成（规格 §1.2）；需审的停在 Submitted 等判定。阶段交付两样都不做。
    completed = bool(final) and (row["review_mode"] or "none") == "none"
    sets = {"state": WORK_DONE, "completion_basis": "final_unaudited"} if completed \
        else {"state": WORK_SUBMITTED if row["review_mode"] not in (None, "", "none") else row["state"]}
    payload = {"sub_ref": recorded["sub_ref"], "change_set": recorded["change_set"],
               "note": note, "final": bool(final), "replaces": replaces}
    version = _bump(conn, row, "work.publish", payload, actor, now, act_key, sets)
    result = {"ok": True, "work_ref": work_ref, "sub_ref": recorded["sub_ref"],
              "sub_digest": recorded["sub_digest"], "change_set": recorded["change_set"],
              "version": version, "state": sets["state"], "final": bool(final),
              "content_verified": verified, "content_unverified": unverified,
              "replaced": replaces, "completion_basis": sets.get("completion_basis"),
              "unblocked": release_dependents(conn, work_ref) if completed else []}
    storage.record_action(conn, actor, "work.publish", act_key, digest, result, now_ts)
    return result


# --------------------------------------------------------------------------- review

def review_experience(conn, row):
    """已有退回记录才建议沉淀经验，个人名下仅在岗位唯一时可定位。"""
    if not conn.execute("SELECT 1 FROM reviews WHERE work_ref=? AND epoch=? AND decision='return' LIMIT 1",
                        (row["work_ref"], row["epoch"])).fetchone():
        return None
    assignee = row["assignee_ref"]
    if conn.execute("SELECT 1 FROM position_holders WHERE position=?", (assignee,)).fetchone():
        position = assignee
    else:
        positions = _positions_held(conn, assignee)
        if len(positions) != 1:
            return None
        position = positions.pop()
    return {"position": position, "hint": KNOWLEDGE_TEXT["review_followup"]}


def review(storage, conn, actor, *, work_ref, sub_ref, decision, reason=None, evidence=None,
           act_key, now_ts=None):
    """对当前那一份提交作出判断（accept / return）。返回判定后的事实。

    accept **最终版**让 Work 完成并把下游解开；accept 中间版（`subs.final=0`）只把那一份记为通过，
    Work 回到 Active 等下一份（规格 §6 W1：中途提交和审核不关闭 Work）；return 同样回 Active。
    在**调用方的事务里**执行。
    """
    require_actor(actor)
    if decision not in DECISIONS:
        raise ResultRefused("invalid_request", "decision 必须是 accept 或 return",
                            en="decision must be accept or return", field="decision")
    if decision == "return" and not (reason or "").strip():
        raise ResultRefused("invalid_request", "退回必须写清楚问题（reason）",
                            en="A return must state the problem", field="reason")
    params = {"work_ref": work_ref, "sub_ref": sub_ref, "decision": decision,
              "reason": reason, "evidence": evidence}
    replay, digest = storage.action_replay(conn, actor, "work.review", act_key, params)
    if replay is not None:
        return replay

    row = _work_row(conn, work_ref)
    if row["state"] != WORK_SUBMITTED:
        raise ResultRefused("conflict", "%s 当前不在待审状态（%s），没有可判定的提交"
                            % (work_ref, row["state"]),
                            en="%s is %s, not awaiting review" % (work_ref, row["state"]),
                            work_ref=work_ref, state=row["state"], reason_code="not_submitted")
    current = _require_current_sub(conn, row, sub_ref)
    if current["submitted_by"] == actor:
        raise ResultRefused("unauthorized_attempt", "不能审核自己提交的内容",
                            en="You cannot review your own submission",
                            sub_ref=sub_ref, reason_code="self_review", needs="reviewer")
    if row["reviewer_ref"] not in _holders(conn, actor):
        raise ResultRefused("unauthorized_attempt",
                            "只有当前审核人（%s）可以判定 %s" % (row["reviewer_ref"], work_ref),
                            en="Only the current reviewer (%s) can judge %s"
                               % (row["reviewer_ref"], work_ref),
                            work_ref=work_ref, reason_code="not_reviewer", needs="reviewer")

    now = now_text(now_ts)
    accepted = decision == "accept"
    closes = accepted and bool(current["final"])      # 只有"最终版通过"才结束 Work
    # 审核**自己**那一行：`reviews` 是"这一份提交被怎么判的"的唯一出处。履历的轮次、退回意见、
    # 依据都从它读；只改 `subs.state` 会让"审过几轮"没有事实，读面只能靠事件去猜。
    n = conn.execute("SELECT COUNT(*) AS n FROM reviews WHERE work_ref=?", (work_ref,)).fetchone()[0] + 1
    review_ref = _review_ref(work_ref, n)
    conn.execute(
        "INSERT INTO reviews(review_ref, work_ref, sub_ref, decision, sub_digest, epoch,"
        " reviewer_ref, grant_ref, grant_version, evidence_json, evidence_hash,"
        " decided_by, decided_at, act_key, reason)"
        " VALUES(?,?,?,?,?,?,?,'',0,?,?,?,?,?,?)",
        (review_ref, work_ref, sub_ref, decision, current["sub_digest"],
         row["epoch"], actor,
         json.dumps(evidence or [], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
         hashlib.sha256(json.dumps(evidence or [], ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")).hexdigest(),
         actor, now, act_key, reason or ""))
    conn.execute("UPDATE subs SET state=? WHERE sub_ref=?",
                 (SUB_ACCEPTED if accepted else SUB_RETURNED, sub_ref))
    sets = {"state": WORK_DONE, "completion_basis": "review_accept"} if closes \
        else {"state": WORK_ACTIVE}
    payload = {"sub_ref": sub_ref, "decision": decision, "reason": reason or "",
               "evidence": evidence or []}
    version = _bump(conn, row, "work.review", payload, actor, now, act_key, sets)
    result = {"ok": True, "work_ref": work_ref, "sub_ref": sub_ref, "review_ref": review_ref,
              "decision": decision, "version": version, "state": sets["state"],
              "completion_basis": sets.get("completion_basis"),
              "unblocked": release_dependents(conn, work_ref) if closes else []}
    if closes and (experience := review_experience(conn, row)):
        result["position_experience"] = experience
    storage.record_action(conn, actor, "work.review", act_key, digest, result, now_ts)
    return result
