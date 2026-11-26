"""The one entry an idle scheduler calls: "these came due, what does delivery do about them".

`officed` waits until the earliest real `due_at` (or a commit event), then hands the rows that came
due to someone who knows what each kind *means*. That judgement is delivery's; this module is the
seam. It does not sleep, does not scan, does not schedule.

    actions = worker.drain(store, on_batch=…, on_tool=…)

## Two phases, and the order is the contract

A handler is split in two, because a due row's decision needs host facts and the host must never be
called while we hold the write lock:

* `gather(row, at, store)` — **outside any write transaction.** Native re-reads, Core reads, adapter
  calls. Returns opaque facts. This is where `tool_handler` calls `observe(call)`, which is a real
  call to a host process that can take seconds; doing it under `BEGIN IMMEDIATE` would block every
  other writer in the office for that whole time.
* `apply(conn, row, facts, at)` — **inside this row's own transaction.** Writes only: no native
  call, no network, no sleeping. Everything it does rolls back together.

The CAS version is read before `gather` and handed to `apply` unchanged, which is the same contract
as `due.settle_tool_deadline`: an observation describes the row at that version, and a decision taken
on it may only be written if the row has not moved since.

## One transaction per row

A row that fails must not take the others with it, and — the case that produced this shape — must not
leave a half-write behind. If a handler fires the row and then fails writing the system letter, the
*row* has to be back where it was: a `fired` deadline with no letter is a stall nobody was told about
and nobody will ever be told about, because the row no longer comes due. So each row commits on its
own; a failure rolls that row back completely and is reported in `failed`, and the remaining rows
still settle.

## Only kinds that have a handler are read

`due.due_rows(kinds=…)` filters to the kinds registered in this call. A row whose kind nobody handles
must not be returned: it is permanently overdue, so the scheduler would wake on it immediately, for
ever, and every pass would report it `unhandled` — a spin loop that looks like work. It stays armed
and simply is not read here; whoever registers that kind picks it up when they do.
"""
from __future__ import annotations

import hashlib
import json
import os

from delivery import due as due_mod
from delivery.coord import manager_boxes

#: How many rows one pass takes. A backlog is worked oldest-first across passes rather than in one
#: unbounded transaction: each row commits separately, and a long pass is a long period during which
#: a row can move under someone else.
DEFAULT_LIMIT = 64


def quota_bindings(conn, binding_id, generation):
    """Active bindings for the same actual native session, including its position addresses."""
    row = conn.execute("SELECT host_session_ref FROM bindings WHERE binding_id=? AND generation=?"
                       " AND state='active' AND revoked_at IS NULL", (binding_id, generation)).fetchone()
    if row is None:
        return []
    if not row["host_session_ref"]:
        return [(binding_id, generation)]
    return [(r["binding_id"], r["generation"]) for r in conn.execute(
        "SELECT binding_id,generation FROM bindings WHERE host_session_ref=? AND state='active' AND revoked_at IS NULL",
        (row["host_session_ref"],))]


def quota_state(conn, binding_id, generation):
    """The current actual session's blocked episodes, without inferring missing facts."""
    if binding_id is None or generation is None:
        return {"blocked": False}
    observed = []
    for peer in quota_bindings(conn, binding_id, int(generation)):
        row = conn.execute("SELECT value_json FROM binding_state WHERE binding_id=? AND generation=?"
                           " AND key='quota'", peer).fetchone()
        try:
            state = json.loads(row["value_json"]) if row is not None else {}
        except (TypeError, ValueError):
            continue
        if isinstance(state, dict) and state.get("blocked") is True:
            observed.append(state.get("observed_at"))
    timestamps = [float(t) for t in observed if type(t) in (int, float)]
    return {"blocked": bool(observed), "observed_at": max(timestamps) if timestamps else None}


def quota_blocked(conn, binding_id, generation) -> bool:
    return quota_state(conn, binding_id, generation)["blocked"]


class Handler:
    """A due kind's domain judgement, split so no host call happens under the write lock.

    Subclasses override `gather` and `apply`. The base `gather` answers None (a kind whose decision
    needs no outside fact), and `apply` must not be called with facts from a different reading.
    """

    def gather(self, row, at, store):
        """Facts for this row, read outside any write transaction. May do native/Core reads."""
        return None

    def apply(self, conn, row, facts, at):
        """Decide and write through `conn`. Returns a dict with `decision` and optional `action`."""
        raise NotImplementedError


def drain(store, *, on_batch: Handler | None = None, on_tool: Handler | None = None,
          on_wait: Handler | None = None, on_idle_work: Handler | None = None,
          now_ts: float | None = None, limit: int = DEFAULT_LIMIT, rows=None) -> dict:
    """Settle the rows that are due **right now**, one transaction each; return post-commit actions.

    `store` is a `storage.Storage` (anything with `transaction()`); this module never opens its own
    database, so the caller keeps one write discipline.

    Returns `{"handled", "actions", "skipped", "failed", "more"}`:

    * `handled` — one entry per row that settled, with its key, decision and version after the write;
    * `actions` — handler actions in row order, to be performed **after** the transactions commit;
    * `skipped` — rows that came due but whose kind has no handler here (they stay armed and are not
      read again by this call, so they cannot drive a loop);
    * `failed` — `(key, error)` for a row whose handler raised; that row's writes were rolled back;
    * `more` — how many further rows were due beyond `limit`, so a caller can tell a backlog from an
      empty table without counting.
    """
    at = due_mod.now() if now_ts is None else float(now_ts)
    handlers = {"batch": on_batch, "tool_hang": on_tool, "wait_deadline": on_wait, "idle_work": on_idle_work}
    registered = [kind for kind, h in handlers.items() if h is not None]
    out = {"handled": [], "actions": [], "skipped": [], "failed": [], "more": 0}
    if not registered:
        # Nothing to do and nothing to read: an empty answer is honest, and it keeps an idle
        # scheduler from waking on rows whose meaning nobody has registered yet.
        return out
    with store.transaction(read_only=True) as conn:
        due_now = due_mod.due_rows(conn, at, kinds=registered) if rows is None else rows
        total = len(due_now)
        selected = due_now[:max(1, int(limit))]
        # Versions are read here, before any gather: a handler's observation describes the row at
        # this version, and `apply` CASes on exactly it.
        readings = [dict(row) for row in selected]
    out["more"] = max(0, total - len(selected))
    for row in readings:
        handler = handlers[row["kind"]]
        try:
            facts = handler.gather(row, at, store)
        except Exception as exc:                      # noqa: BLE001
            # A host read that failed is not a decision. The row keeps its deadline and this pass
            # says why; nothing was written.
            out["failed"].append((row["due_key"], "gather: %s: %s" % (type(exc).__name__, exc)))
            continue
        answer = _apply_one(store, handler, row, facts, at, out)
        if answer is not None and answer.get("action") is not None:
            out["actions"].append(answer["action"])
    return out


def _apply_one(store, handler, row, facts, at, out):
    """One row, one transaction: apply, record the post-version, and roll the row back on failure."""
    try:
        with store.transaction() as conn:
            answer = handler.apply(conn, row, facts, at)
            if not isinstance(answer, dict):
                raise TypeError("handler returned %r" % (type(answer).__name__,))
            after = due_mod.get(conn, row["due_key"])
        if answer.get("decision") != "stale":
            out["handled"].append({"due_key": row["due_key"], "kind": row["kind"],
                                   "decision": answer.get("decision"),
                                   "version": (after or {}).get("version")})
        return answer
    except Exception as exc:                          # noqa: BLE001
        # The transaction above has already rolled back — including whatever the handler wrote
        # before it raised. That matters most when a handler fires a row and then fails to write the
        # system letter: committing the fire alone would leave a stall nobody is told about, and the
        # row would never come due again. The row stays exactly where it was.
        out["failed"].append((row["due_key"], "%s: %s" % (type(exc).__name__, exc)))
        return None


# --------------------------------------------------------------------------- handlers

def _process_status(pid, expected):
    from delivery.process_observer import birth
    if type(pid) is not int or not expected:
        return 'unknown'
    observed = birth(pid)
    if observed:
        return 'alive' if observed == expected else 'dead'
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 'dead'
    except (PermissionError, OSError):
        pass
    return 'unknown'


def fired_tool_alerts(conn, box, *, round_token=None):
    """Current, unaccepted tool alerts in one box; caller owns the read transaction."""
    from lib.mail import SYSTEM_MAIL_SENDER
    fact = "CASE WHEN json_valid(l.meta_json) THEN json_extract(l.meta_json,'$.fact_key') END"
    system = "CASE WHEN json_valid(l.meta_json) THEN json_extract(l.meta_json,'$.system') END"
    if round_token is None:
        rows = conn.execute(
            "SELECT DISTINCT d.* FROM due d INDEXED BY due_state_due_at CROSS JOIN letters l INDEXED BY ix_letters_system_fact "
            "CROSS JOIN letter_states s WHERE d.state='fired' AND d.kind='tool_hang' "
            "AND l.from_actor=? AND " + fact + "='tool:' || d.due_key || '#' || d.version "
            "AND " + system + "='tool.due' AND s.letter_id=l.letter_id AND s.box=? "
            "AND s.state='delivered' AND s.accepted_at IS NULL", (SYSTEM_MAIL_SENDER, box)).fetchall()
        return [due_mod._out(row) for row in rows]
    refs = conn.execute(
        "SELECT " + fact + " FROM round_ref rr INDEXED BY sqlite_autoindex_round_ref_1 CROSS JOIN letters l CROSS JOIN letter_states s "
        "WHERE rr.token=? AND rr.box=? AND l.letter_id=rr.ref AND l.from_actor=? "
        "AND " + system + "='tool.due' AND s.letter_id=l.letter_id AND s.box=rr.box "
        "AND s.state='delivered' AND s.accepted_at IS NULL", (round_token, box, SYSTEM_MAIL_SENDER)).fetchall()
    rows = {}
    for (value,) in refs:
        identity = due_mod.tool_alert_version(value)
        if identity:
            key, version = identity
            row = due_mod.get(conn, key)
            if row and row['kind'] == 'tool_hang' and row['state'] == 'fired' and str(row['version']) == version:
                rows[key] = row
    return list(rows.values())


def dead_tool_alerts(rows):
    """Observe exact process identities outside a transaction, once per distinct identity."""
    observed, dead = {}, []
    for row in rows:
        payload = row['payload']
        identities = ((payload.get('process_pid'), payload.get('process_birth')),
                      (payload.get('tool_pid'), payload.get('tool_birth')))
        for identity in identities:
            pid, birth = identity
            if type(pid) is not int or pid <= 0 or not isinstance(birth, str) or not birth:
                continue
            if identity not in observed:
                observed[identity] = _process_status(pid, birth)
            if observed[identity] == 'dead':
                dead.append(row)
                break
    return dead


def retire_dead_tool_alerts(conn, rows, at=None):
    """CAS-retire observed dead alerts inside the caller's existing write transaction."""
    retired = 0
    for row in rows:
        current = due_mod.get(conn, row['due_key'])
        if (current and current['state'] == 'fired' and current['version'] == row['version']
                and current['binding_id'] == row['binding_id'] and current['binding_gen'] == row['binding_gen']
                and current['payload'] == row['payload']):
            retired += bool(due_mod.clear(conn, row['kind'], row['subject_ref'],
                                          expected_version=row['version'], now_ts=at))
    return retired


class IdleWorkHandler(Handler):
    def __init__(self, core):
        self.core = core

    def gather(self, row, at, store):
        with store.transaction(read_only=True) as conn:
            data = self.core._monitor_state(conn, (row['binding_id'], row['binding_gen']))
        return _process_status(data.get('process_pid'), data.get('process_birth'))

    def apply(self, conn, row, facts, at):
        current = due_mod.get(conn, row['due_key'])
        if not current or current['state'] != 'armed' or current['version'] != row['version']:
            return {'decision': 'stale'}
        active = conn.execute("SELECT actor FROM bindings WHERE binding_id=? AND generation=? AND state='active' AND revoked_at IS NULL",
                              (row['binding_id'], row['binding_gen'])).fetchone()
        if not active or facts == 'dead':
            due_mod.clear(conn, 'idle_work', row['subject_ref'], expected_version=row['version'], now_ts=at)
            return {'decision': 'retired'}
        if facts != 'alive':
            payload = dict(current['payload'])
            attempts = min(int(payload.get('unknown_checks', 0)) + 1, 5)
            payload['unknown_checks'] = attempts
            due_mod.set_payload(conn, 'idle_work', row['subject_ref'], payload, now_ts=at)
            due_mod.push_back(conn, 'idle_work', row['subject_ref'], at + min(300 * 2 ** (attempts - 1), 3600), now_ts=at)
            return {'decision': 'unknown'}
        return self.core._monitor_idle_due(conn, row, active['actor'], at)


class ToolHandler(Handler):
    """A tool deadline: re-read the native call outside the lock, then settle inside the row's txn."""

    def __init__(self, *, observe=None, notify, rearm_seconds: float = 60.0,
                 retry_seconds: float = 30.0):
        #: No native handle means the current tool state is unknown, not an observed running call.
        self.observe = observe
        self.notify = notify
        self.rearm_seconds = rearm_seconds
        self.retry_seconds = retry_seconds

    def gather(self, row, at, store):
        try:
            call = due_mod.tool_call_of(row)
        except due_mod.DueRefused:
            return (None, None, 'unknown')
        # A host that does have a live handle (the Pi adapter) reads it here, deliberately outside
        # any transaction: it can take seconds, and holding the write lock would stall every writer.
        payload = row.get('payload') or {}
        status = _process_status(payload.get('process_pid'), payload.get('process_birth'))
        return (call, self.observe(call) if self.observe is not None and status != 'dead' else None, status)

    def apply(self, conn, row, facts, at):
        call, observation, status = facts
        active = conn.execute("SELECT 1 FROM bindings WHERE binding_id=? AND generation=? AND state='active' AND revoked_at IS NULL",
                              (row['binding_id'], row['binding_gen'])).fetchone()
        if call is None or not active or status == 'dead':
            current = due_mod.get(conn, row['due_key'])
            if not current or current['state'] != 'armed' or current['version'] != row['version']:
                return {'decision': 'stale'}
            due_mod.clear(conn, row['kind'], row['subject_ref'], expected_version=row['version'], now_ts=at)
            return {'decision': 'retired'}
        return due_mod.settle_tool_deadline(
            conn, call=call, due_version=row["version"], observation=observation,
            now_ts=at, rearm_seconds=self.rearm_seconds, retry_seconds=self.retry_seconds,
            notify=self.notify)


class WaitDeadlineHandler(Handler):
    """「继续等到 X」到期：段、owner、due 版本的最终判断**全部在这一个写事务里**。

    不做事务外 gather：这里的判断读的是本机库（毫秒级），而在锁外先判再进锁写，会留一个
    「已恢复仍发」的窗口 —— 判断与升级必须在同一把写锁下分先后。判断次序也是契约：

    1. **due 版本先核**：行在读后被重新 arm 过（经理换了截止时间）⇒ 那次观察作废，
       零写入 —— 过期观察不碰新版本；
    2. 仍是那个段且仍受阻（Active + 行上段首键 == payload 的 fact）⇒ 升级一封，
       `fact + ":escalated"` 去重 ⇒ 同一段只此一封，重拍重烧也不多；
    3. 已解除或段翻篇 ⇒ 静默清行（清不是 fire：等的事没发生，行还得能被新一段重新 arm）。
    """

    def __init__(self, *, core):
        self.core = core

    def apply(self, conn, row, facts, at):
        work_ref = row["subject_ref"]
        key = due_mod.due_key("wait_deadline", work_ref)
        cur = due_mod.get(conn, key)
        if cur is None or int(cur["version"]) != int(row["version"]):
            return {"decision": "superseded", "work_ref": work_ref}
        w = conn.execute(
            "SELECT state, waiting_json, project_ref FROM work WHERE work_ref=?",
            (work_ref,)).fetchone()
        try:
            waiting = json.loads(w["waiting_json"]) if (w is not None and w["waiting_json"]) else None
        except ValueError:
            waiting = None
        same = (w is not None and w["state"] == "Active" and isinstance(waiting, dict)
                and waiting.get("key") == (row["payload"] or {}).get("fact"))
        if not same:
            due_mod.clear(conn, "wait_deadline", work_ref, now_ts=at)
            return {"decision": "moot", "work_ref": work_ref}
        boxes = manager_boxes(self.core, conn, w["project_ref"], at)
        if not boxes:
            due_mod.clear(conn, "wait_deadline", work_ref, now_ts=at)
            return {"decision": "moot", "work_ref": work_ref}
        fact = (row["payload"] or {}).get("fact")
        escalate = "%s:escalated" % fact
        sent = self.core.system_letter(
            letter_id="waitup-" + hashlib.sha256(escalate.encode("utf-8")).hexdigest()[:16],
            boxes=boxes,
            subject="阻塞仍未解除：%s" % work_ref,
            body=("%s 的等待已到截止时间，重读后它仍被阻塞（等待理由仍在 Work 上）。\n"
                  "同一段只升级这一次。先读 office_work_get，再决定改派（work.update assign）、"
                  "催办（给解环人写信）或取消（work.update cancel）。" % work_ref),
            need="决定改派、催办或取消",
            kind="work.blocked", fact_key=escalate, work_ref=work_ref, conn=conn, now_ts=at)
        fired = due_mod.fire(conn, "wait_deadline", work_ref,
                             expected_version=row["version"], now_ts=at)
        return {"decision": "escalated", "work_ref": work_ref,
                "letter": sent.get("letter_id"), "fired": fired is not None}


class BatchHandler(Handler):
    """A unit's batch deadline: find the current legal readers, then start its round inside."""

    def __init__(self, *, consumers, begin_round, can_wait=None, retry_seconds: float = 30.0):
        self.consumers = consumers
        self.begin_round = begin_round
        self.can_wait = can_wait
        self.retry_seconds = retry_seconds

    def gather(self, row, at, store):
        unit = row["subject_ref"]
        with store.transaction(read_only=True) as conn:
            consumers = list(self.consumers(conn, unit))
            alerts = fired_tool_alerts(conn, unit) if consumers else []
        return {"unit": unit, "consumers": consumers, "dead_alerts": dead_tool_alerts(alerts)}

    def apply(self, conn, row, facts, at):
        current = due_mod.get(conn, row["due_key"])
        if not current or current["state"] != "armed" or current["version"] != row["version"]:
            return {"decision": "stale"}
        if not facts["consumers"]:
            if self.can_wait is not None and not self.can_wait(conn, facts["unit"]):
                # Keep the letter readable and stop scheduling an address that has been retired.
                cleared = due_mod.clear_batch(conn, facts["unit"], expected_version=row["version"], now_ts=at)
                return {"decision": "manual", "version": cleared["version"]}
            # §3: a position's mail waits for a legal incumbent. Pushing the row back keeps it in
            # this queue without inventing a round or sending to nobody.
            moved = due_mod.push_back(conn, "batch", facts["unit"],
                                      at + max(1.0, float(self.retry_seconds)), now_ts=at)
            return {"decision": "no_incumbent", "version": (moved or {}).get("version")}
        retire_dead_tool_alerts(conn, facts.get("dead_alerts", ()), at)
        started = self.begin_round(conn, row, facts["consumers"])
        plan = started if isinstance(started, dict) else {}
        reason = plan.get("reason")
        if reason in ("manual", "cc_wait", "receipts_wait") and not plan.get("bad_created_at"):
            cleared = due_mod.clear_batch(conn, facts["unit"], expected_version=row["version"], now_ts=at)
            return {"decision": reason, "version": (cleared or {}).get("version")}
        hold_until = plan.get("hold_until")
        # **Not cleared here.** The round is built by the host after this commit, and a crash between
        # the two must not consume the deadline: the row is pushed back by `retry_seconds` and stays
        # armed, so the next pass asks again. A second attempt is harmless because Core's selection
        # excludes refs that are already active — the host then finds `created=0` and clears.
        moved = due_mod.push_back(conn, "batch", facts["unit"],
                                  hold_until if hold_until is not None and hold_until > at
                                  else at + max(1.0, float(self.retry_seconds)), now_ts=at)
        # 这一轮冻结的全部 ref，顺序由供给方给（`officed.begin_round` = `deliver + carry`，正是
        # `round_ref` 的 INSERT 顺序）。这里**不再拆拼一次**：两处各自形成就会一处放宽、一处没改。
        refs = list(plan.get("refs") or ())
        if not refs:
            # 这一轮没有被冻结的信（例如这段时间里它们已经被别人拉进别的轮）。**不发帧**：
            # 一帧没有正文的 `batch_round` 宿主无法执行，而"没人能执行的帧"与"已经投递过"
            # 长得一模一样。行照上面推回去、仍是 armed，下一拍再问一次。
            return {"decision": "no_refs", "version": (moved or {}).get("version")}
        # 正文在这里写（同一个事务、同一份冻结的 refs）：投递层是它唯一的作者，宿主只负责送。
        from delivery import round as round_mod
        text = round_mod.batch_text(conn, facts["unit"], refs)
        return {"decision": "round_pending", "version": (moved or {}).get("version"),
                "text": text,
                "action": {"kind": "batch_round", "unit": facts["unit"], "text": text,
                           "refs": refs, "due_key": row["due_key"],
                           # **推后之后**的那一版：这才是"这一帧写出来时行是什么样"。用 `apply`
                           # 之前读到的版本会立刻差一版（`push_back` 自己就把版本 +1 了），
                           # 于是每一帧的回执都 CAS 失败——那比不 CAS 更糟。
                           "due_version": (moved or {}).get("version")}}
