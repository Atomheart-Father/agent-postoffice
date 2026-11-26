"""`due`: every "judge this later" fact, as rows — one table, no polling.

Spec §4.2's table, and only that table. `storage` owns the DDL (`CREATE TABLE due ...` and
`due_next`); this module owns every business read and write of it, so there is exactly one place
that knows what a `due_key` looks like, what `armed`/`fired`/`cleared` mean, and what a version is
for. A second spelling of any of those would be a second answer to "is this due", which is the same
class of bug as two dispatchers.

**No Core import.** These functions take a SQLite connection and touch one table. That is what lets
the caller put a letter and its `due(batch)` in the *same* transaction (§1.3 "同次提交内按收件箱
登记或保留一条合批到期项"): the letter row and the deadline commit together or not at all, so a
crash between them cannot produce "mail that nobody will ever be told about" or "a deadline for
mail that was never written". It is also why this file can be exercised, reasoned about, and
changed without a database other than the one it is handed.

**Time is epoch seconds** (§3.1): `due_at` is `REAL`, compared with `<`/`>` against `time.time()`.
No parsed timestamps, no local time, no "today" — a deadline that means something different in
another timezone is not a deadline.

**Version is the concurrency token, not decoration** (§4.2 "更新则递增，到期结果比较读到的版本").
`fire()` *requires* the version the caller acted on. A firing that does not name its reading can
fire on a deadline that a progress event already pushed back, and the letter it sends then
describes a state that no longer holds.
"""
from __future__ import annotations

import json
import time
from urllib.parse import quote

#: Spec §4.2's vocabulary. A row outside it is refused rather than written: a typo'd kind is a
#: deadline nothing ever asks for, which looks exactly like "not due yet" for the rest of time.
KINDS = ("batch", "tool_hang", "wait_deadline", "idle_work", "address_expiry",
         "cooldown")

#: `armed` — waiting. `fired` — its time came and the judgement ran. `cleared` — the reason went
#: away before or after firing (§1.5 "恢复后清掉这条；以后再卡住会重新报一次").
STATES = ("armed", "fired", "cleared")


class DueRefused(Exception):
    """A write this module will not make, with a machine-readable reason.

    Not a Core error: nothing here reads or writes Core, so a caller that gets this has a bad
    argument or a stale version, not a business refusal from the record.
    """

    def __init__(self, code, message, **fields):
        self.code = code
        self.message = message
        self.fields = fields
        super().__init__("%s: %s" % (code, message))


def now() -> float:
    """The one clock. A caller that passes its own instant passes a float in epoch seconds."""
    return time.time()


# --------------------------------------------------------------------------- keys

def due_key(kind: str, subject: str) -> str:
    """`<kind>:<subject>` — the one spelling, so `batch:alice` cannot also be written `batch/alice`.

    `subject` is the **logical unit**, not a path: a personal mailbox (`alice`) and a position
    (`office.frontend`) are both addresses, and the row says which address must act. A position's
    mail waits for a legal incumbent; the row does not encode who that is now — that is Core's
    fact, read at firing time (§3 "岗位信以岗位为收件地址，等有合法现任才投递").
    """
    if kind not in KINDS:
        raise DueRefused("invalid_kind", "unknown due kind %r (one of %s)" % (kind, ", ".join(KINDS)))
    if not isinstance(subject, str) or not subject.strip():
        raise DueRefused("invalid_subject", "subject_ref must be a non-empty logical unit")
    if "\x00" in subject:
        raise DueRefused("invalid_subject", "subject_ref %r cannot carry a NUL" % (subject,))
    # Colons are allowed and expected: spec §4.2 spells a tool deadline
    # `tool_hang:<binding>:<turn>:<call>`. The kind is the prefix before the **first** colon, so a
    # subject that starts with `batch:` would collide with another kind's key — that is what the
    # caller would have built wrong, not what this function guesses.
    return "%s:%s" % (kind, subject)


# --------------------------------------------------------------------------- reads

def _row(conn, key):
    return conn.execute("SELECT * FROM due WHERE due_key=?", (key,)).fetchone()


def _out(row):
    """One row as a plain dict, payload parsed.

    `payload` stays a dict even when the column is empty, because every caller that fires a row
    reads `.payload.get(...)` and a None there would turn a real deadline into a crash at the worst
    moment. A malformed payload is reported as `{}` rather than raised: the deadline itself is
    still valid, and refusing to say when it is due would be worse than losing its annotations.
    """
    if row is None:
        return None
    keys = row.keys()
    payload = _payload(row)
    return {
        "due_key": row["due_key"],
        "kind": row["kind"],
        "subject_ref": row["subject_ref"],
        "owner_ref": row["owner_ref"],
        "due_at": row["due_at"],
        "state": row["state"],
        "version": row["version"],
        "fire_count": row["fire_count"],
        "last_fired_at": row["last_fired_at"],
        "binding_id": row["binding_id"] if "binding_id" in keys else None,
        "binding_gen": row["binding_gen"] if "binding_gen" in keys else None,
        "payload": payload,
        "updated_at": row["updated_at"],
    }


def _payload(row) -> dict:
    """One row's payload as a dict; a malformed value is `{}` rather than a raise.

    A lost annotation is cheaper than a deadline nobody can read, and every caller that fires a row
    does `.get(...)` on it.
    """
    try:
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def get(conn, key: str):
    """One row, or None. Read-only; no side effects, so it is safe inside a caller's read txn."""
    return _out(_row(conn, key))


def armed_rows(conn, *, at=None, kinds=None):
    """Yield armed facts in deadline order, optionally stopping at a time or restricting kinds."""
    where = ["state='armed'"]
    values = []
    if at is not None:
        where.append("due_at<=?")
        values.append(float(at))
    if kinds is not None:
        wanted = tuple(dict.fromkeys(k for k in kinds if isinstance(k, str) and k))
        if not wanted:
            return
        where.append("kind IN (%s)" % ",".join("?" for _ in wanted))
        values.extend(wanted)
    for row in conn.execute("SELECT * FROM due WHERE " + " AND ".join(where) + " ORDER BY due_at,due_key", values):
        yield _out(row)


def due_rows(conn, at: float | None = None, *, kinds=None) -> list:
    """Read the armed rows already due at `at` (the current time by default)."""
    return list(armed_rows(conn, at=now() if at is None else at, kinds=kinds))


# --------------------------------------------------------------------------- writes

def _insert(conn, key, kind, subject, due_at, owner, payload, binding_id, binding_gen, at):
    conn.execute(
        "INSERT INTO due(due_key, kind, subject_ref, owner_ref, due_at, state, version, fire_count,"
        " last_fired_at, binding_id, binding_gen, payload_json, updated_at)"
        " VALUES(?,?,?,?,?,'armed',1,0,NULL,?,?,?,?)",
        (key, kind, subject, owner, due_at, binding_id, binding_gen,
         json.dumps(payload or {}, ensure_ascii=False, sort_keys=True), at))
    return 1


def arm(conn, kind: str, subject: str, due_at: float, *, owner_ref=None, payload=None,
        binding_id=None, binding_gen=None, now_ts: float | None = None) -> dict:
    """Arm (or re-arm) one deadline; returns the row that is now armed, with its version.

    Two properties this has that "insert the row" does not:

    * **Re-arming within a binding generation keeps the history.** `fire_count` and `last_fired_at` survive, because §1.5 says
      a fault that comes back is *the same fault* — "同一故障再次触发：只更新次数和最近时间，不再
      发信". A count that reset on re-arm could not answer that question.
    * **A cleared row can come back.** `cleared` means the reason went away, not that the kind is
      retired: "恢复后清掉这条；以后再卡住会重新报一次". So `arm` on a cleared row re-arms it with a
      fresh version and the old count intact.

    The deadline is written as given; a caller that wants "now" passes `now()`.
    """
    at = now() if now_ts is None else float(now_ts)
    key = due_key(kind, subject)
    due_at = _finite(due_at, "due_at")
    existing = _row(conn, key)
    if existing is None:
        _insert(conn, key, kind, subject, due_at, owner_ref, payload, binding_id, binding_gen, at)
        return _out(_row(conn, key))
    version = int(existing["version"]) + 1
    same_generation = binding_gen is None or binding_gen == existing["binding_gen"]
    conn.execute(
        "UPDATE due SET due_at=?, state='armed', version=?, owner_ref=COALESCE(?, owner_ref),"
        " binding_id=COALESCE(?, binding_id), binding_gen=COALESCE(?, binding_gen),"
        " fire_count=?, last_fired_at=?, payload_json=?, updated_at=? WHERE due_key=?",
        (due_at, version, owner_ref, binding_id, binding_gen,
         existing["fire_count"] if same_generation else 0,
         existing["last_fired_at"] if same_generation else None,
         json.dumps(payload or {}, ensure_ascii=False, sort_keys=True), at, key))
    return _out(_row(conn, key))


def _armed(conn, kind, subject, due_at, now_ts):
    """(at, key, due_at, row) for an **armed** row, or four Nones. `push_back`/`advance` 的同一段。

    None 是"没有这条 / 它不在 armed"的诚实回答：被清掉的期限不能因为"有进展"就复活，
    已经判过的也不必再判 —— 只有 `arm()` 能把这两者带回来。
    """
    at = now() if now_ts is None else float(now_ts)
    key = due_key(kind, subject)
    due_at = _finite(due_at, "due_at")
    row = _row(conn, key)
    if row is None or row["state"] != "armed":
        return None, None, None, None
    return at, key, due_at, row


def push_back(conn, kind: str, subject: str, due_at: float, *, now_ts: float | None = None):
    """Move an **armed** deadline later (real progress). Returns the row, or None if not armed.

    None is the honest answer for both "no such row" and "not armed": a `cleared` deadline must not
    be revived by progress on a command that is no longer being watched, and a `fired` one has
    already had its judgement. Only `arm()` brings either back.
    """
    at, key, due_at, row = _armed(conn, kind, subject, due_at, now_ts)
    if row is None:
        return None
    version = int(row["version"]) + 1
    conn.execute("UPDATE due SET due_at=?, version=?, updated_at=? WHERE due_key=?",
                 (due_at, version, at, key))
    return _out(_row(conn, key))


def clear(conn, kind: str, subject: str, *, expected_version: int | None = None,
          now_ts: float | None = None):
    """Retire one deadline (`cleared`). Returns the row, or None when there was nothing to clear.

    Clearing an already-cleared row is a no-op rather than an error: several native events can
    legitimately say "that command is over" (finish, background, permission prompt), and the second
    one is not a bug. `fire_count` is left as it is so the next recurrence still knows it recurred.

    **`expected_version` 是"我清的是我读过的那一版"**（`fire` 的 CAS 同一条理由）。投递的案发点：
    帧写出之后同一单元又来了一封信，那一版被重新 arm（版本 +1）；旧帧回执若不带版本，就把**还没人
    送过**的新那一版一起清掉 —— 信还在箱里、到期项没了，再没有谁会为它写帧（真机复现：
    `batch:alice` 从 v3 被旧帧清成 v4，`late-1` 永远留在 `delivered`）。不传版本仍按旧语义清，
    因为"清"也用于与版本无关的终结动作（例如判定"这件事结束了"）。
    """
    at = now() if now_ts is None else float(now_ts)
    key = due_key(kind, subject)
    row = _row(conn, key)
    if row is None or row["state"] == "cleared":
        return None
    if expected_version is not None and int(row["version"]) != int(expected_version):
        return None                              # 有人动过这一行：这一版的结账不成立
    version = int(row["version"]) + 1
    conn.execute("UPDATE due SET state='cleared', version=?, updated_at=? WHERE due_key=?",
                 (version, at, key))
    return _out(_row(conn, key))


def fire(conn, kind: str, subject: str, *, expected_version: int, now_ts: float | None = None,
         payload: dict | None = None):
    """Record that one deadline's judgement ran. **Requires the version the caller acted on.**

    A supplied payload records the observation under the same version as the fired deadline.

    Returns the fired row, or None when the write must not happen — a different version (something
    changed the deadline between the read and this call), not armed, or gone. None is not an error:
    it is precisely the race this token exists to lose safely.

    `fire_count` increments here and `last_fired_at` moves, which is what "同一故障再次触发：只更新
    次数和最近时间，不再发信" reads from — the caller sends a letter only when the count went from
    0 to 1, and a re-arm of the same fault keeps counting.
    """
    at = now() if now_ts is None else float(now_ts)
    key = due_key(kind, subject)
    if not isinstance(expected_version, int):
        raise DueRefused("invalid_version", "expected_version must be an int")
    row = _row(conn, key)
    if row is None or row["state"] != "armed" or int(row["version"]) != expected_version:
        return None
    version = expected_version + 1
    conn.execute(
        "UPDATE due SET state='fired', version=?, fire_count=fire_count+1, last_fired_at=?,"
        " updated_at=?, payload_json=COALESCE(?,payload_json) WHERE due_key=? AND version=?",
        (version, at, at, None if payload is None else json.dumps(payload, ensure_ascii=False, sort_keys=True),
         key, expected_version))
    return _out(_row(conn, key))


def tool_alert_version(fact):
    """Return the deadline and version named by a current-format tool alert."""
    if isinstance(fact, str) and fact.startswith('tool:tool_hang:'):
        key, _, version = fact[5:].rpartition('#')
        if version.isascii() and version.isdecimal():
            return key, version
    return None


def tool_call_of(row):
    """Reconstruct only a recorded native identity and check it against the storage key."""
    from lib.watchdog import ToolCall
    binding_id, generation = row['binding_id'], row['binding_gen']
    turn, native = row['payload'].get('turn_id'), row['payload'].get('native_id')
    if type(generation) is not int or any(not isinstance(value, str) or not value for value in (binding_id, turn, native)):
        raise DueRefused('invalid_key', 'tool deadline has no recorded native identity')
    call = ToolCall(binding_id, generation, turn, native)
    if tool_key(call) != row['due_key']:
        raise DueRefused('invalid_key', 'tool identity does not match its deadline')
    return call


def retire_tools(conn, binding, ts, *, turn=None):
    """Retire frontend monitoring for exact recorded calls, without claiming process exit."""
    rows = conn.execute("SELECT * FROM due WHERE kind='tool_hang' AND binding_id=? AND binding_gen=?"
                        " AND state IN ('armed','fired') AND json_valid(payload_json)", binding).fetchall()
    retired = 0
    for raw in rows:
        row = _out(raw)
        payload = row['payload']
        started = payload.get('tool_started')
        if (type(started) not in (int, float) or started > ts or not payload.get('turn_id')
                or (turn is not None and payload['turn_id'] != turn)):
            continue
        retired += bool(clear(conn, 'tool_hang', row['subject_ref'], expected_version=row['version'], now_ts=ts))
    return retired


def set_payload(conn, kind: str, subject: str, payload: dict, *, now_ts: float | None = None):
    """Replace one row's payload without touching its deadline, state, or version semantics.

    Only for annotations a caller must persist next to a row whose *time* is not changing — an idle
    segment's attempt keys after a round started. It still bumps `version`, because a reader that
    cached the row should notice it changed. Returns the row, or None when there is no such row.
    """
    at = now() if now_ts is None else float(now_ts)
    key = due_key(kind, subject)
    row = _row(conn, key)
    if row is None:
        return None
    version = int(row["version"]) + 1
    conn.execute("UPDATE due SET version=?, payload_json=?, updated_at=? WHERE due_key=?",
                 (version, json.dumps(payload or {}, ensure_ascii=False, sort_keys=True), at, key))
    return _out(_row(conn, key))


def _finite(value, name):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DueRefused("invalid_due_at", "%s must be a number of epoch seconds" % name) from None
    if value != value or value in (float("inf"), float("-inf")):
        raise DueRefused("invalid_due_at", "%s must be finite" % name)
    return value


# --------------------------------------------------------------------------- batch

def arm_batch(conn, unit: str, due_at: float, *, owner_ref=None, payload=None,
              now_ts: float | None = None) -> dict:
    """Register or keep **one** batch deadline for one logical unit (§1.3).

    One row per unit, not one per letter: a mailbox that receives ten letters inside the window
    still has a single deadline, because the thing being decided is "does this unit have mail to be
    told about now", and that is a property of the unit's inbox, not of any one letter. Re-arming
    therefore does not multiply rows — it moves the same row's time and bumps its version, which is
    also what makes "同次提交内…保留一条" true on the second and tenth letter.

    `unit` is the logical address: a personal box or a position. Which live session may consume it
    is not recorded here — a position's row waits for a legal incumbent, and that is Core's fact,
    read when the row fires (§3).
    """
    return arm(conn, "batch", unit, due_at, owner_ref=owner_ref, payload=payload, now_ts=now_ts)


def clear_batch(conn, unit: str, *, expected_version: int | None = None,
                now_ts: float | None = None):
    """Retire one unit's batch deadline — what a delivered-and-settled round leaves behind.

    `expected_version` 见 `clear`：回执要说明"我清的是我读到的那一版"，否则帧写出之后新到的那封信
    会被一起清掉。
    """
    return clear(conn, "batch", unit, expected_version=expected_version, now_ts=now_ts)


# --------------------------------------------------------------------------- tool deadlines

def tool_key(call) -> str:
    """`tool_hang:<binding>:<turn>:<call>` — spec §4.2's spelling, one per native attempt.

    The three parts are the native identity, not an invented one: two tools in the same turn are
    two deadlines, and the same tool id under a different binding is a different call. That is what
    lets the deadline be re-read against the host later (`tool_due_decision` compares the whole
    `ToolCall`), instead of matching "some call in this turn" and reporting a stall about the wrong
    command.
    """
    return due_key("tool_hang", ':'.join(quote(value, safe='') for value in
                                          (call.binding_id, call.turn_id, call.native_id)))


def arm_tool(conn, call, due_at: float, *, progress_revision: int = 0, owner_ref=None,
             observation=None, now_ts: float | None = None) -> dict:
    """Arm a tool deadline whose payload carries the progress revision the caller has seen.

    `progress_revision` is stored with the row because that is what the decision compares against
    the host's current one: without it, "no progress since we armed" could only be guessed from
    time, and a command that prints repeatedly would look stuck (§1.5 "重复输出不算进度").
    """
    if not isinstance(progress_revision, int) or progress_revision < 0:
        raise DueRefused("invalid_progress", "progress_revision must be a non-negative int")
    # The binding and its generation are written on the row as well as in the key: a native call id
    # is only unique **within one binding generation**, so a row whose generation no longer matches
    # the connection must not be fired against that connection's call of the same name.
    return arm(conn, "tool_hang", tool_key(call).split(':', 1)[1],
               due_at, owner_ref=owner_ref,
               payload={**(observation or {}), "progress_revision": progress_revision,
                        "turn_id": call.turn_id, "native_id": call.native_id},
               binding_id=call.binding_id, binding_gen=int(call.generation), now_ts=now_ts)


def advance_tool(conn, row, due_at, *, progress_revision, now_ts=None):
    """Record real progress and start a fresh alert segment on the same deadline."""
    if row['state'] not in ('armed', 'fired'):
        return None
    at = now() if now_ts is None else float(now_ts)
    payload = dict(row['payload'], progress_revision=progress_revision)
    conn.execute("UPDATE due SET due_at=?, state='armed', version=version+1, fire_count=0, payload_json=?, updated_at=?"
                 " WHERE due_key=?", (_finite(due_at, "due_at"), json.dumps(payload, ensure_ascii=False, sort_keys=True),
                                       at, row['due_key']))
    return get(conn, row['due_key'])


def settle_tool_deadline(conn, *, call, due_version: int, observation,
                         now_ts: float | None = None, rearm_seconds: float = 60.0,
                         retry_seconds: float = 30.0, notify=None) -> dict:
    """One tool deadline, after its time came: decide from native facts, then write in **this** txn.

    ``due_version`` is the version the caller read **before** it went and re-read the native tool.
    That ordering is the point: the observation describes the host at that reading, so it may only
    be applied to that version. Re-reading `version` inside this transaction would CAS against a
    newer row and let an observation taken earlier overwrite a progress event that arrived in
    between — the exact "act on a stale snapshot" the token exists to prevent. A mismatch writes
    nothing and reports ``stale``: the caller has a newer fact to look at.

    ``progress_revision`` compared by `tool_due_decision` is the value **persisted in the row**, not
    one handed in now. The row is what we armed with ("no progress since this revision"), so a
    caller-supplied revision would silently redefine the question after the fact.

    `lib/watchdog.tool_due_decision` stays the judgement; this function only supplies its facts and
    applies its answer:

    * ``notify`` — fire with `due_version` as CAS, then `notify(conn, row)` writes the system letter
      on this same connection. Only the CAS winner reaches the letter, and the letter's `fact_key`
      makes a replay harmless: §1.5's "一封系统信", once per alerted segment.
    * ``already_notified`` — fire, bumping `fire_count`, no second letter (§1.5 "只更新次数和最近
      时间，不再发信").
    * ``advance`` — real progress: push back and end the alerted segment (`fire_count = 0`), so a
      later stall speaks again.
    * ``clear`` — finished / background / waiting for permission: retire the row.
    * ``unknown`` — **no letter, and no overdue row left behind.** The observation cannot tell us
      anything, so it must not be recorded as a stall; but an armed row with a past `due_at` would
      make the scheduler wake on it immediately, for ever, which is a spin loop wearing the name
      "no polling". So the row is pushed back by `retry_seconds` and stays in this same queue: the
      next wake is bounded, a real new observation or a reconnect settles it earlier, and nothing
      scans.

    Nothing here sends mail itself: `notify` is the caller's writer, called inside this transaction
    so a failure in either half rolls back both.
    """
    from lib.watchdog import tool_due_decision

    at = now() if now_ts is None else float(now_ts)
    key = tool_key(call)
    subject = key.split(":", 1)[1]
    row = get(conn, key)
    if row is None or row["state"] != "armed":
        return {"decision": "not_due", "version": None, "fired": None, "letter_id": None}
    if int(row["version"]) != int(due_version):
        return {"decision": "stale", "version": row["version"], "fired": None, "letter_id": None}
    # A native call id is unique **within one binding generation**. If the row was armed by another
    # binding, or by an earlier generation of this one, the id may have been reused by a different
    # call: settling it against this connection would either excuse a real stall or accuse an
    # innocent command. Neither fact is recoverable here, so the answer is `binding_changed` with
    # nothing written; the caller re-arms from its own current observation.
    row_binding = row["binding_id"]
    row_gen = row["binding_gen"]
    if row_binding is not None and row_binding != call.binding_id:
        return {"decision": "binding_changed", "version": row["version"], "fired": None,
                "letter_id": None}
    if row_gen is not None and int(row_gen) != int(call.generation):
        return {"decision": "binding_changed", "version": row["version"], "fired": None,
                "letter_id": None}
    progress_revision = row["payload"].get("progress_revision", 0)
    if not isinstance(progress_revision, int):
        progress_revision = 0
    decision = tool_due_decision(
        call,
        progress_revision=progress_revision,
        fire_count=row["fire_count"],
        observation=observation)
    if decision == "clear":
        cleared = clear(conn, "tool_hang", subject, now_ts=at)
        return {"decision": decision, "version": (cleared or {}).get("version"),
                "fired": None, "letter_id": None}
    if decision == "advance":
        # The new revision goes into the row **in this statement**: without it the next deadline
        # compares the host's same revision against the old baseline and advances for ever, so the
        # alert that should follow a real stall never fires.
        moved = advance_tool(conn, row, at + max(1.0, float(rearm_seconds)),
                        progress_revision=observation.progress_revision, now_ts=at)
        return {"decision": decision, "version": (moved or {}).get("version"),
                "fired": None, "letter_id": None}
    if decision == "unknown":
        deferred = push_back(conn, "tool_hang", subject, at + max(1.0, float(retry_seconds)),
                             now_ts=at)
        return {"decision": decision, "version": (deferred or {}).get("version"),
                "fired": None, "letter_id": None}
    payload = None
    if decision == 'notify':
        payload = row['payload'].copy()
        payload.pop('tool_pid', None)
        payload.pop('tool_birth', None)
        if observation.process_tree_root:
            payload['tool_pid'], payload['tool_birth'] = observation.process_tree_root
    fired = fire(conn, "tool_hang", subject, expected_version=due_version, now_ts=at, payload=payload)
    if fired is None:
        return {"decision": "raced", "version": due_version, "fired": None, "letter_id": None}
    letter_id = None
    if decision == "notify" and notify is not None:
        letter_id = notify(conn, fired)
    return {"decision": decision, "version": fired["version"], "fired": fired,
            "letter_id": letter_id}


def batch_units_for(recipients, *, touched=None, informational=False) -> list:
    """The logical units that need a `due(batch)` after one letter, in the caller's order (§1.3).

    The judgement is narrow on purpose:

    * **cc copies do not**: "抄送、FYI、回执、广播只落箱，不登记到期项" — a copy is read if the
      reader looks, and waking a host for it would turn every cc into a delivery.
    * **purely informational `need` does not**: same sentence; the caller passes `informational`
      from the one existing judge of "this asks for nothing" (`lib.mail.informational_need`), rather
      than this function growing a second opinion about Chinese phrasing.
    * **only units that actually received a new state row do** (`touched`): a replayed write adds no
      inbox, so it must not move a deadline already armed for another letter.

    The unit is `logical_address` when given, else the box: a position is an address, not a path.
    Who may consume it *now* is not decided here — that is Core's appointment fact, read when the
    row fires. Recording today's holder in the row would turn an appointment into a delivery claim.
    """
    units, seen = [], set()
    for r in recipients or ():
        if not isinstance(r, dict):
            continue
        if (r.get("category") or "to") == "cc":
            continue
        box = r.get("box")
        if not isinstance(box, str) or not box:
            continue
        if touched is not None and box not in touched:
            continue
        unit = r.get("logical_address") or box
        if not isinstance(unit, str) or not unit or unit in seen:
            continue
        seen.add(unit)
        units.append(unit)
    if informational:
        return []
    return units


def arm_batch_for_recipients(conn, recipients, *, touched=None, informational=False,
                             now_ts: float | None = None, owner_ref=None) -> list:
    """`batch_units_for` + `arm_batch`, so the letter writer states its intent in one call.

    `batch_units_for` 已按单元去重（同一箱收到两遍是一封信，不是一个期限）。

    The deadline is `now`, not "now + window": the window is Core's decision inside `round.begin`
    (which answers `hold`/`deferred`), and a second copy of it here would be a second answer to
    "may this go out yet".
    """
    at = now() if now_ts is None else float(now_ts)
    units = batch_units_for(recipients, touched=touched, informational=informational)
    return [arm_batch(conn, unit, at, owner_ref=owner_ref, now_ts=at) for unit in units]
