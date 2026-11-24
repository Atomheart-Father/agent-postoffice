"""§D3 经理统筹·第一段：一处阻塞，在阻塞事实的事务里，变成负责经理的一封信。

「统筹待办」不是一张新表，也不是一条新投递链：**阻塞事实发生的那一个事务**里，从 Work
（谁在等、等什么、谁能解环）把待办算出来，用既有 `system_letter` 写给负责经理，
`fact_key` 去重；`need` 非空 ⇒ `_arm_batch_due` 走**既有**的 batch 合批。没有定时扫描：
事件是「承办人声明等待」，不是时钟；没有待办表：信本身就是待办记录，回读用
`fact_key`；没有另起投递：信一落地与所有别的可行动信同一条合批、同一个宿主。

调用点是 `work.update(action="wait")` 那个事务里、行写完之后的**同一事务**（正式接线在
runtime 的写区，一句调用）。本模块收的是完整行为与判据：

* **一处阻塞只通知一次。**「一处」= 一个等待段：从没在等到在等的那一次跃迁上发信；
  段内再声明（改口、换理由）是同一处，不再发。`continue` 关段后再等是新的一处：
  新版本 ⇒ 新 `fact_key` ⇒ 经理再收一封。
* **声明者就是负责经理时不发。** 事实已经在该统筹的人手里；给他写一封信是自我通知。
* **解环人从既有事实读。** 开着的 `waits_for` 前置的承办人是能解环的人；声明里的
  `on`（等待对象）原文照登。信给事实，不替经理裁决。

第二段是 `respond`（`coord.respond` 的行为本体，op 接线归 runtime）：经理拿着
`work.blocked` 信的 letter_id 回到**同一个等待段**——过期通知不碰新段；选「继续等」
必须带未来 deadline，记 `due(kind=wait_deadline)`；到期由 `worker.WaitDeadlineHandler`
重读，仍受阻且仍是那个段才升级一封，已解除/旧段静默。
"""
from __future__ import annotations

import hashlib
import json
import time

from delivery import due as due_mod
from lib.work_runnable import WORK_DONE


def block_facts(conn, work_ref):
    """从 Work 和等待关系读取当前阻塞事实。"""
    w = conn.execute("SELECT * FROM work WHERE work_ref=?", (work_ref,)).fetchone()
    if w is None:
        return None
    try:
        waiting = json.loads(w["waiting_json"]) if w["waiting_json"] else None
    except ValueError:
        waiting = None
    breakers = []
    for t in conn.execute(
            "SELECT t.work_ref AS tref, t.assignee_ref AS tassignee, t.objective AS tobj,"
            "       t.state AS tstate"
            "  FROM work_relations w JOIN work t ON t.work_ref = w.target_work_ref"
            "  WHERE w.work_ref=? AND w.relation='waits_for' AND w.removed_at IS NULL"
            "  ORDER BY t.work_ref",
            (work_ref,)).fetchall():
        if t["tstate"] == WORK_DONE:
            continue                     # 已交付的前置解不了也不卡这个阻塞
        breakers.append({"work_ref": t["tref"], "assignee": t["tassignee"],
                         "objective": t["tobj"], "state": t["tstate"]})
    return {"work_ref": work_ref, "project_ref": w["project_ref"], "assignee": w["assignee_ref"],
            "objective": (w["objective"] or "").strip(),
            "version": int(w["version"] or 0),
            "waiting": waiting if isinstance(waiting, dict) else None,
            "breakers": breakers}


def manager_boxes(core, conn, project_ref, now_ts):
    return sorted({row["subject_ref"] for row in core._project_grants(conn, project_ref, now_ts)})


def notify_blocked(core, conn, work_ref, *, was_waiting, now_ts=None):
    """一处阻塞 → 负责经理收唯一一封统筹信。**在调用方的事务里写**，不自己开、不提交。

    `was_waiting`＝这次声明之前是否已在等待；返回 `{sent, reason}`（没发时给原因，不静默）。
    幂等三道闸：调用方的 `act_key`、这里的段内跃迁判据、以及 `fact_key` 让同一事实只落一封。
    """
    facts = block_facts(conn, work_ref)
    if facts is None or not facts["waiting"]:
        return {"sent": None, "reason": "not_waiting"}
    if was_waiting:
        # 同一段内改口：段首键是第一次声明时写下的那个，这里**不重写** —— 重写等于给
        # 每次改口换段身份，"还是这个段吗"就永远答不上来。
        return {"sent": None, "reason": "same_block"}
    # 段首键（A 裁定：段身份沿用现有等待状态，键只放在 waiting_json 里，不建表）。
    # 「还是这个段吗」要能从**行上**回答：`coord.respond` 与到期重读都拿它对，
    # 别处没有这个事实。信即使不发（下面两条不发原因），键也要写 —— 段身份不随
    # 通知策略走。
    fact = "work-wait:%s:%d" % (work_ref, facts["version"])
    stamped = dict(facts["waiting"])
    stamped["key"] = fact
    conn.execute("UPDATE work SET waiting_json=? WHERE work_ref=?",
                 (json.dumps(stamped, ensure_ascii=False), work_ref))
    boxes = manager_boxes(core, conn, facts["project_ref"], now_ts)
    if not boxes:
        return {"sent": None, "reason": "no_manager"}
    assignee = facts["assignee"]
    declarers = {assignee, *core._live_holders(conn, assignee)}
    boxes = [box for box in boxes if declarers.isdisjoint({box, *core._live_holders(conn, box)})]
    if not boxes:
        return {"sent": None, "reason": "declarer_is_manager"}
    waiting = facts["waiting"]
    reason = (waiting.get("reason") or "").strip()
    on = waiting.get("on")
    on = on.strip() if isinstance(on, str) else None
    breakers = facts["breakers"]
    if breakers:
        who = "、".join(
            ("%s（%s%s）" % (b["assignee"] or "无承办人", b["work_ref"],
                             "，已取消需重排" if b["state"] == "Cancelled" else ""))
            for b in breakers)
    elif on:
        who = on
    else:
        who = "没有登记可解环的人 —— 按理由安排"
    body = ("%s（%s）被承办人 %s 声明阻塞：\n"
           "等待理由：%s\n等待对象：%s\n可解环的人：%s\n"
           "先读 office_work_get 了解现状，再决定改派（work.update assign）、催办（给解环人写信）"
           "或取消（work.update cancel）。"
           % (work_ref, facts["objective"] or "目标见 Work 本身", facts["assignee"],
              reason or "（承办人未写）", on or "（未写）", who))
    sent = core.system_letter(
        letter_id="wait-" + hashlib.sha256(fact.encode("utf-8")).hexdigest()[:16],
        boxes=boxes,
        subject="阻塞待统筹：%s" % work_ref,
        body=body,
        need="统筹解除这件 Work 的阻塞",
        kind="work.blocked", fact_key=fact, work_ref=work_ref, conn=conn, now_ts=now_ts)
    return {"sent": sent, "fact_key": fact}


def respond(core, conn, actor, *, letter_id, choice, reason=None, deadline=None, now_ts=None):
    """`coord.respond` 的行为本体（op 接线归 runtime）：从 `work.blocked` 通知回到同一个等待段。

    `item` 是那封通知的 letter_id：凭信上 `meta.fact_key`（段首键）认段，不解析主题、不猜目标。
    处理人须持有本项目当前有效的管理授权。

    * `wait`：deadline 必须在未来，记 `due(wait_deadline, subject=work)`，payload 带段首键与信。
    * `handle` / `delegate`：清掉挂着的 `wait_deadline`（不再催）；`delegate` 另给这件 Work 的
      承办人一封行动信 —— 责任人取自现有事实，不另立收件人字段。
    * 过期通知：段首键对不上 ⇒ `stale_block`，**零写入**（旧段的通知不改新段）。
    """
    at = float(now_ts) if now_ts is not None else time.time()
    row = conn.execute("SELECT meta_json FROM letters WHERE letter_id=?", (letter_id,)).fetchone()
    if row is None:
        return {"refused": "no_letter"}
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except ValueError:
        meta = {}
    fact = meta.get("fact_key") if isinstance(meta, dict) else None
    if not isinstance(fact, str) or meta.get("system") != "work.blocked":
        return {"refused": "not_a_blocking_letter"}
    fact = fact.removesuffix(":escalated")
    parts = fact.split(":")
    if len(parts) != 3 or parts[0] != "work-wait":
        return {"refused": "not_a_blocking_letter"}
    work_ref = parts[1]
    facts = block_facts(conn, work_ref)
    if facts is None:
        return {"refused": "stale_block"}
    if not core._can_manage_project(conn, actor, facts["project_ref"], now_ts):
        return {"refused": "not_project_manager"}
    waiting = facts["waiting"] or {}
    if waiting.get("key") != fact:
        return {"refused": "stale_block"}
    if choice not in ("handle", "delegate", "wait"):
        return {"refused": "bad_choice"}
    who = facts["assignee"]
    if choice == "delegate" and not (isinstance(who, str) and who.strip() and who != actor):
        return {"refused": "no_delegate_target"}      # 没有可委派的责任人（零写入）
    if choice != "wait":
        cleared = due_mod.clear(conn, "wait_deadline", work_ref, now_ts=at)
        out = {"decision": choice, "work_ref": work_ref, "cleared_deadline": bool(cleared)}
        if choice != "delegate":
            return out
        sent = core.system_letter(
            letter_id="deleg-" + hashlib.sha256((fact + ":delegated").encode("utf-8")).hexdigest()[:16],
            boxes=[who], subject="经理把一处阻塞交给你：%s" % work_ref,
            body=("经理 %s 不自行接手 %s，把它交回承办人：\n等待理由：%s\n等待对象：%s\n"
                  "先用 office_work_get 读现状，再决定催办、改派或声明你在等什么。"
                  % (actor, work_ref, (waiting.get("reason") or "（未写）").strip(),
                     (waiting.get("on") or "（未写）").strip())),
            need="接手处理这件 Work 的阻塞", kind="work.delegated",
            fact_key=fact + ":delegated", work_ref=work_ref, conn=conn, now_ts=at)
        return {**out, "sent": sent}
    if not isinstance(deadline, (int, float)) or float(deadline) <= at:
        return {"refused": "deadline_past"}
    armed = due_mod.arm(conn, "wait_deadline", work_ref, float(deadline),
                        payload={"fact": fact, "letter": letter_id, "manager": actor},
                        now_ts=at)
    return {"decision": "wait", "work_ref": work_ref, "fact_key": fact,
            "due": armed}
