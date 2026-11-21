"""D1: a stopped session with nothing new to read still has work it can do.

Spec §1.4's last paragraph, as one module. The trigger is a native idle fact (`turn_end` /
`session idle`), and the question it asks is not "is there mail" but "is there anything this
person can act on right now". Those are different: mail is one way work arrives, and a responsible
person can be idle with an empty inbox and an unfinished Work they own. So this module arms a
`due(kind=idle_check)` row at native idle even when no letter was written, and the row is what
starts a round when its time comes.

**The actionable predicate is not defined here.** `actionable` is the caller's read of Core's
current "can this unit act" facts — the same predicate the UI and `work.todo` use, i.e. items whose
own `todo` is `do` or `review`. Re-deriving it here would be a second answer to "can I work now",
and the two would drift the first time the product renames a state. This module takes it as a
list of items and only decides what that means for the timer.

**A segment, not a stream.** §1.4: "到期项关联执行区间，发过提醒和看到窗口活跃都不算业务进展。实际
工作恢复后关闭这一段；下次停顿按新执行区间判断。" So:

* one row per execution segment (`idle_check:<binding>:<gen>`), and a new generation is a new
  segment — a re-appointed holder is not continuing the previous person's idle period;
* sending a reminder, and seeing the window active, are **not** progress: only `advance` (a real
  native fact) closes the segment;
* closing it means the row is `cleared`; the next stop opens a new one.

**Attempts are bounded and named.** "unknown 重试有界，不复投同尝试": each attempt carries the key
of the native attempt it started (`attempt_key`), the row remembers which keys it already used, and
a settle that names an already-used key answers `already_attempted` without starting anything. The
count is capped, so a session that keeps being unable to start cannot loop for ever.
"""
from __future__ import annotations

from delivery import due as due_mod

#: How many rounds one idle segment may ask for before it waits for a real fact instead.
MAX_ATTEMPTS = 3


def _subject(binding_id: str, generation: int) -> str:
    """这个段的 due subject：`<绑定>:<代次>`。拼法只有这一处，别处一律调它。"""
    return "%s:%s" % (binding_id, int(generation))


def get(conn, binding_id: str, generation: int):
    """This segment's row, or None. Read-only.

    代次换了就是新的段：把上一任的尝试历史带过来，会让新持有者的第一段空闲被判成
    "已经试过"——那正是这个模块要消除的沉默。
    """
    return due_mod.get(conn, due_mod.due_key("idle_check", _subject(binding_id, generation)))


def arm(conn, *, unit: str, binding_id: str, generation: int, due_at: float,
        actionable=(), now_ts: float | None = None):
    """A native idle happened: register or keep this segment's check.

    Called on **every** turn end, including ones with no new mail — that is the whole point of D1.
    The row's payload keeps `attempts` (the native attempt keys already started) across re-arms, so
    a stop that happens twice inside one segment does not get a second round for the same attempt.

    **A `cleared` row is a finished segment, and a finished segment's history is not this one's.**
    Re-arming it therefore starts the count again: without that, a person whose work ended after
    three attempts would never be asked again — the next piece of work arrives, the row is re-armed,
    `exhausted` still sees three used attempts, and the segment is silent for ever. The distinction
    is the row's state, not a timer: `armed` ⇒ same segment (carry on, that is the 32220768 fix);
    `cleared` ⇒ a new one.
    """
    row = get(conn, binding_id, generation)
    payload = (row or {}).get("payload") or {}
    same_segment = bool(row) and row.get("state") == "armed"
    # `attempts` 与 `asked_key` 都是**段内现场**，两者同进同退：段还在（`armed`）就照旧带过来，
    # 段已关（`cleared`）就都不带。只重置其中一个会造出"次数归零、却仍挂着上一次未回执的询问"
    # 这种半新半旧的段 —— 那一拍 `already_attempted` 会把新段的第一问吞掉。
    attempts = _attempts(conn, binding_id, generation) if same_segment else []
    asked = payload.get("asked_key") if same_segment else None
    return due_mod.arm(conn, "idle_check", _subject(binding_id, generation), due_at,
                       owner_ref=unit,
                       payload={"unit": unit, "actionable": len(actionable or ()),
                                "attempts": attempts,
                                # `asked_key`：有一次未结账的询问在等回执。turn_end 只是刷新到期
                                # 时刻，不清它 —— 清了等待就变成每一次都是新的，正是 32220768 的
                                # 75 轮。
                                "asked_key": asked},
                       binding_id=binding_id, binding_gen=int(generation), now_ts=now_ts)


def clear(conn, *, binding_id: str, generation: int, now_ts: float | None = None):
    """Close this segment (§1.4 "实际工作恢复后关闭这一段"). The next stop opens a new one."""
    return due_mod.clear(conn, "idle_check", _subject(binding_id, generation), now_ts=now_ts)


def _attempts(conn, binding_id: str, generation: int) -> list:
    row = get(conn, binding_id, generation)
    if row is None:
        return []
    used = row["payload"].get("attempts")
    return list(used) if isinstance(used, list) else []


def settle(conn, *, unit: str, binding_id: str, generation: int, due_version: int,
           actionable: list, attempt_key: str | None = None, now_ts: float | None = None,
           retry_seconds: float = 30.0, max_attempts: int = MAX_ATTEMPTS) -> dict:
    """The segment's check came due: decide from Core's current facts, write in this transaction.

    `due_version` is the version read **before** the caller re-read Core's actionable facts, and it
    is what the write CASes on: an appointment or Work change that landed in between makes the
    reading stale, and acting on it would start a round for a responsibility that has moved.

    The answers, and none of them is a guess:

    * ``not_due`` / ``stale`` — nothing to do; nothing written.
    * ``clear`` — the unit has nothing actionable now: the segment closes. A person whose work is
      finished should not be woken to be told so.
    * ``already_attempted`` — this exact native attempt already started a round in this segment.
      **No second send**: "不复投同尝试". Re-sending the same attempt is how one idle period turns
      into a loop of identical rounds.
    * ``exhausted`` — the segment has used `max_attempts`. The row is pushed back by
      `retry_seconds` and stays in this queue: bounded retry, and a real `advance`/`clear` settles it
      earlier. Not a stall letter, and not a scan.
    * ``started`` — a round was started through `start_round(conn, row, attempt_key)`, whose return
      value is reported as `ref`. The attempt key is recorded in the same write, so a replay of this
      call answers `already_attempted` rather than starting twice.

    `advance` is deliberately not a decision here: "发过提醒和看到窗口活跃都不算业务进展", so only
    an explicit `clear` (real work resumed) or a new segment ends the episode. That is the opposite
    of a tool deadline, where progress is a native fact the host reports.
    """
    at = due_mod.now() if now_ts is None else float(now_ts)
    subject = _subject(binding_id, generation)
    row = due_mod.get(conn, due_mod.due_key("idle_check", subject))
    if row is None or row["state"] != "armed":
        return {"decision": "not_due", "version": None, "ref": None}
    if int(row["version"]) != int(due_version):
        return {"decision": "stale", "version": row["version"], "ref": None}
    if not actionable:
        cleared = clear(conn, binding_id=binding_id, generation=generation, now_ts=at)
        return {"decision": "clear", "version": (cleared or {}).get("version"), "ref": None}
    used = row["payload"].get("attempts")
    used = list(used) if isinstance(used, list) else []
    # Name each ask once. The persisted key survives timer revisions, while a reopened
    # row gets a new key so a previous segment's native start can never satisfy it.
    pending = row["payload"].get("asked_key")
    if attempt_key is None:
        attempt_key = pending or "%s#%d" % (subject, row["version"])
    if attempt_key in used:
        return {"decision": "already_attempted", "version": row["version"], "ref": None}
    if pending:
        return {"decision": "already_attempted", "version": row["version"], "ref": None,
                "asked_key": pending}
    if len(used) >= max(1, int(max_attempts)):
        deferred = due_mod.push_back(conn, "idle_check", subject, at + max(1.0, float(retry_seconds)),
                                     now_ts=at)
        return {"decision": "exhausted", "version": (deferred or {}).get("version"), "ref": None,
                "attempt_key": attempt_key}
    # **The attempt is not recorded here.** The round is a Core fact built by the host after this
    # transaction commits (Core's `_txn` owns its own transaction; there is no conn-only begin and
    # inventing one would be a second selection/authorization path). Recording the attempt now would
    # mean: if the host dies between commit and the round, the segment believes it tried, the budget
    # is spent, and the person is never asked again. So this pass only *asks*: the row is pushed back
    # by `retry_seconds` (bounded, still in this queue, still armed) and the attempt is recorded by
    # `record_attempt` once the host really started the round.
    deferred = due_mod.push_back(conn, "idle_check", subject, at + max(1.0, float(retry_seconds)),
                                now_ts=at)
    # 这一次询问要**在行上留痕**（`asked`/`asked_key`）：下一拍 `already_attempted` 靠它拦住
    # 同一次等待。留痕必须在 `push_back` **之后**写（`push_back` 只动 due_at/version，不碰
    # payload，所以顺序无关，但留痕本身不能省 —— 省了就是 32220768 的 75 轮）。
    payload = dict(row["payload"])
    payload["asked_key"] = attempt_key
    due_mod.set_payload(conn, "idle_check", subject, payload, now_ts=at)
    return {"decision": "round_pending", "version": (deferred or {}).get("version"), "ref": None,
            "attempt_key": attempt_key}


def record_attempt(conn, *, binding_id: str | None = None, generation: int | None = None,
                   attempt_key: str, due_key: str | None = None,
                   now_ts: float | None = None, next_due_at: float | None = None):
    """Record that the host really started this segment's round — **after** it exists.

    This is the other half of `settle`'s `round_pending`: the attempt budget may only count attempts
    that happened. Called by the host once its round is real, so a crash before it leaves the budget
    untouched and the next pass asks again; a crash after it leaves one attempt counted, which is the
    truth.

    `next_due_at` is when the segment should next look — a real native turn end (`arm`) or, when the
    host cannot say, `now + retry`. Passing no time leaves the row as it is and only records history.
    """
    at = due_mod.now() if now_ts is None else float(now_ts)
    # `due_key` first when the caller has it: the action already carries the row's own identity, and
    # recomputing it from `(binding_id, generation)` has a silent failure mode — a row whose
    # `binding_gen` is NULL reads back as 0, the recomputed key points at no row, and the attempt is
    # quietly never recorded (so the segment keeps asking for ever). The key came from the row, so
    # using it is not a guess; using a re-derivation is.
    if due_key:
        key = due_key
        row = due_mod.get(conn, key)
        if row is None or row["kind"] != "idle_check":
            return None
        subject = key.split(":", 1)[1]
        binding_id = row["binding_id"] if row["binding_id"] is not None else binding_id
        generation = row["binding_gen"] if row["binding_gen"] is not None else generation
    else:
        if not isinstance(binding_id, str) or not binding_id:
            return None
        if generation is None:
            return None
        subject = _subject(binding_id, generation)
        row = due_mod.get(conn, due_mod.due_key("idle_check", subject))
        if row is None:
            return None
    used = row["payload"].get("attempts")
    used = list(used) if isinstance(used, list) else []
    if row["state"] != "armed" or row["payload"].get("asked_key") != attempt_key:
        return row if attempt_key in used else None
    used.append(attempt_key)
    payload = dict(row["payload"])
    payload["attempts"] = used
    # 这一问**结了账**：清掉"等待回执"的现场，段内计数让位给 attempts（下一次是新的一问）。
    payload.pop("asked", None)
    payload.pop("asked_key", None)
    due_mod.set_payload(conn, "idle_check", subject, payload, now_ts=at)
    if next_due_at is not None:
        due_mod.arm(conn, "idle_check", subject, float(next_due_at),
                    owner_ref=row["owner_ref"], payload=payload,
                    binding_id=binding_id, binding_gen=int(generation), now_ts=at)
    return due_mod.get(conn, due_mod.due_key("idle_check", subject))
