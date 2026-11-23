"""The one entry an idle scheduler calls: "these came due, what does delivery do about them".

`officed` waits until the earliest real `due_at` (or a commit event), then hands the rows that came
due to someone who knows what each kind *means*. That judgement is delivery's; this module is the
seam. It does not sleep, does not scan, does not schedule.

    actions = worker.drain(store, on_batch=…, on_tool=…, on_idle=…)

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

from delivery import due as due_mod
from delivery.coord import manager_boxes
from lib.watchdog import ToolObservation

#: How many rows one pass takes. A backlog is worked oldest-first across passes rather than in one
#: unbounded transaction: each row commits separately, and a long pass is a long period during which
#: a row can move under someone else.
DEFAULT_LIMIT = 64


def quota_blocked(conn, binding_id, generation) -> bool:
    """这条绑定**当前代次**的额度窗口是否已阻塞：只读事实，不判、不写。

    缺行/缺代次 = 不阻塞（"没有阻塞这件事"）；判据只在 `lib/watchdog.quota_decision`，不复制。
    """
    if binding_id is None or generation is None:
        return False                  # 行上没代次 ⇒ 查不到这段的事实（不是"已阻塞"）
    row = conn.execute("SELECT value_json FROM binding_state WHERE binding_id=? AND generation=?"
                       " AND key='quota'", (binding_id, int(generation))).fetchone()
    try:
        state = json.loads(row["value_json"]) if row is not None else {}
    except (TypeError, ValueError):
        state = {}
    return isinstance(state, dict) and state.get("blocked") is True


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
          on_idle: Handler | None = None, on_wait: Handler | None = None,
          on_model: Handler | None = None,
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
    handlers = {"batch": on_batch, "tool_hang": on_tool, "idle_check": on_idle,
                "wait_deadline": on_wait, "model_silent": on_model}
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

class ToolHandler(Handler):
    """A tool deadline: re-read the native call outside the lock, then settle inside the row's txn."""

    def __init__(self, *, observe=None, notify, rearm_seconds: float = 60.0,
                 retry_seconds: float = 30.0):
        #: `None` means "this process has no live native handle" — the daemon's normal case. It is
        #: not a missing feature: the host reports the tool through `host.event`, and those facts
        #: already are this row (`tool_start` arms it, `tool_progress` advances it, `tool_end` clears
        #: it). So the recorded progress **is** the observation, and asking the native runtime a
        #: second time would be a second answer to "did it move", which is how a real stall gets
        #: talked out of reporting.
        self.observe = observe
        self.notify = notify
        self.rearm_seconds = rearm_seconds
        self.retry_seconds = retry_seconds

    def gather(self, row, at, store):
        call = due_mod.tool_call_of(row)
        if self.observe is None:
            seen = (row["payload"] or {}).get("progress_revision")
            return ToolObservation(call=call, state="running",
                                    progress_revision=seen if isinstance(seen, int) else None)
        # A host that does have a live handle (the Pi adapter) reads it here, deliberately outside
        # any transaction: it can take seconds, and holding the write lock would stall every writer.
        return self.observe(call)

    def apply(self, conn, row, facts, at):
        return due_mod.settle_tool_deadline(
            conn, call=due_mod.tool_call_of(row), due_version=row["version"], observation=facts,
            now_ts=at, rearm_seconds=self.rearm_seconds, retry_seconds=self.retry_seconds,
            notify=self.notify)


class IdleHandler(Handler):
    """An idle segment: read Core's actionable facts outside, ask for the round inside.

    The round itself is built by the host **after** this transaction (Core's round entry owns its own
    transaction and lock); `apply` therefore only decides "this person can act and has attempts left".
    That is why the attempt is recorded by `delivery.idle.record_attempt` once the round exists —
    recording it here would spend the budget for a round that may never start.
    """

    def __init__(self, *, actionable, attempt_key=None, retry_seconds: float = 30.0,
                 max_attempts: int | None = None):
        self.actionable = actionable
        #: `None` = the segment counts its own attempts (`delivery.idle.settle` answers `#1`,
        #: `#2`… from its persisted counter). A caller-supplied key must be **stable across
        #: passes** for the same wait — a key that changes every pass defeats both
        #: `already_attempted` and the attempt cap, which is how a real session spun 75 rounds
        #: (each pass invented a fresh key from the row version that the settle itself had just
        #: bumped).
        self.attempt_key = attempt_key
        self.retry_seconds = retry_seconds
        self.max_attempts = max_attempts

    def gather(self, row, at, store):
        unit = row["payload"].get("unit") or row["owner_ref"]
        # Core read, outside the write lock; the predicate must be the one the UI and `work.todo`
        # use, so D1 cannot answer "can this person work" differently from their own page.
        return {"unit": unit, "actionable": self.actionable(unit),
                "attempt_key": self.attempt_key(row, at) if self.attempt_key else None}

    def apply(self, conn, row, facts, at):
        from delivery import idle as idle_mod
        kwargs = {"retry_seconds": self.retry_seconds}
        if row.get('_retry_pending'):
            kwargs['retry_pending'] = True
        if self.max_attempts is not None:
            kwargs["max_attempts"] = self.max_attempts
        if quota_blocked(conn, row["binding_id"], row["binding_gen"]):
            # 额度窗口没恢复 ⇒ **不续投**（"本窗口暂停续投"），否则就是"到期就重投、重投就失败"
            # 的死循环。行推后有界，不 fire、不清段；真实新执行后回到这一趟。
            due_mod.push_back(conn, "idle_check", row["subject_ref"], at + self.retry_seconds)
            return {"decision": "deferred", "reason": "quota_blocked"}
        answer = idle_mod.settle(
            conn, unit=facts["unit"], binding_id=row["binding_id"],
            generation=row["binding_gen"], due_version=row["version"],
            actionable=facts["actionable"], attempt_key=facts["attempt_key"],
            now_ts=at, **kwargs)
        # The attempt key in the answer is the one that counts: when the caller supplies none, the
        # segment names the ask itself (`<subject>#n`), and `facts["attempt_key"]` is `None` here —
        # forwarding it would put a keyless frame on the wire and break the host's receipt loop.
        attempt_key = answer.get("attempt_key") or facts["attempt_key"]
        if answer.get("decision") == "round_pending":
            # The wake is the caller's, after commit: a native turn cannot be rolled back, so it is
            # never requested from inside the write transaction. `due_key` is carried so the frame
            # can be traced back to the row that produced it; `text` is delivery's own continuation
            # sentence, because D1's wake is not a letter the host could render.
            answer["action"] = {"kind": "idle_started", "unit": facts["unit"],
                                "ref": answer.get("ref"),
                                "attempt_key": attempt_key,
                                "due_key": row["due_key"]}
        return answer


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


class ModelSilentHandler(Handler):
    """「模型无动静」到期：唯一信号是**当前 attempt 的真实增量**（真 Pi 取证 `5e05476a`）。

    窗口与到期项归 Core（`round.started` 挂、`model_progress` 推后、`turn_end` 清，与 `tool_hang`
    同一张 `due` 表、同一个 drain）。这里只做**到点那一刻的判断**，全部在这一个写事务里：

    1. **due 版本先核**：行被推后或清过 ⇒ 这次观察整体作废，零写入；
    2. **活绑定先核**（一次查询拿齐 actor/代次/状态）：绑定已撤销、或 payload 里的代次已不是
       当前代次 ⇒ 这是**旧代次的到期项**，用既有终结动作清掉、**零信**。旧代次的
       `native_attempts` 仍然查得到，所以"查得到事实"本身不能当成"这件事还活着"；
    2b. **本次服务是否承载这个绑定**（`carried`，由 daemon 用自己的供给/原生事实回答）：确定不承载
       ⇒ 那一刻没有原生观测，静默与否**未知** ⇒ 不发信、也不清行（缺席不是撤权），等重新握手或
       既有事件再启窗口；
    3. **事实读活代次那一条**：`turn_closed_at` 不是 None ⇒ 那回合已结束 ⇒ 同样走终结动作
       清掉（不是推后：已结束的回合不会再产出，反复推后只会让一条永久废弃的行每窗醒来一次）；
       尝试在活代次里查不到（被 `NATIVE_ATTEMPTS_KEEP` 修剪掉的老尝试）⇒ 也是永久不可判 ⇒ 清掉；
       `now - last_activity_ts < 窗口` ⇒ 它刚动过（推后没落盘也一样）⇒ 推后并对齐到
       `last + 窗口`（**只有这一种**推后，因为只有它有"还会再动"的余地）；
    4. **确有工具在运行** ⇒ 推后一窗，工具走自己的通道；
    5. **额度耗尽** ⇒ 推后一窗；
    6. **明确等待** ⇒ 推后一窗；
    7. 以上都不成立 ⇒ 升级**一封**，`fact:silent` 去重 ⇒ 同一段只此一封。

    终结动作就是 `due.clear`（与 `turn_end` 同一句）：清不是 fire —— 没有静默这件事发生。
    """

    def __init__(self, *, core, window: float | None = None, carried=None):
        """`window` 由 Core 的 `MODEL_SILENT_SECONDS` 给（产品 §17 初值 900），不在这里放数字。

        缺省从 `core` 取同一个来源；取不到就**当场抛** —— 静默退回一个本地默认值，等于给
        "静默"重新定义一次，而这个数字只该有一处定义。

        `carried`：**本次服务是否承载这个绑定**的只读判据（`callable(binding_id) -> True/False/None`），
        由 daemon 用它自己的原生/供给事实回答。返回值按三态读，不含糊：

        * `True` —— 本服务承载它，照常判断；
        * `False` —— 本服务**确定不承载**：那一刻没有任何原生观测，模型静默与否**未知** ⇒
          不发信、也不清行（缺席不是撤权），把行留在 armed，等重新握手或既有事件再启窗口；
        * `None` —— 供给不是权威全量（比如这份服务没有装载一份完整清单）：同样未知 ⇒ 不发信、
          不清能正常运行的行。

        不给这个判据（`None`）= 本次没有这类知识，按现状判断 —— 缺一个接缝不是证据。
        """
        self.carried = carried
        self.core = core
        if window is None:
            window = getattr(core, "MODEL_SILENT_SECONDS", None)
            if window is None:
                raise ValueError(
                    "Core 没有 MODEL_SILENT_SECONDS：窗口归 Core，delivery 不另定一个")
        self.window = float(window)

    def apply(self, conn, row, facts, at):
        payload = row["payload"] or {}
        attempt = payload.get("attempt_id")
        binding = payload.get("binding_id")

        def close(reason):
            """既有终结动作：`due.clear`（清不是 fire）。返回决定词供对账。"""
            due_mod.clear(conn, "model_silent", row["subject_ref"], now_ts=at)
            return {"decision": "moot", "reason": reason}

        def defer(reason, until=None):
            moved = due_mod.push_back(conn, "model_silent", row["subject_ref"],
                                      at + self.window if until is None else until, now_ts=at)
            return {"decision": "deferred", "reason": reason,
                    "version": (moved or {}).get("version")}

        # 1. 版本先核（不重算键：行自己就是身份）
        cur = due_mod.get(conn, row["due_key"])
        if cur is None or int(cur["version"]) != int(row["version"]):
            return {"decision": "superseded"}
        if not (isinstance(attempt, str) and attempt) or not (isinstance(binding, str) and binding):
            return close("no_attempt")

        # 2. 活绑定（一次查询拿齐 actor/代次/状态；原来是两处各查一次）
        live = conn.execute(
            "SELECT actor, generation, state, revoked_at FROM bindings WHERE binding_id=?",
            (binding,)).fetchone()
        if live is None or live["state"] != "active" or live["revoked_at"] is not None:
            return close("binding_gone")             # 撤销/不存在的绑定：旧代次不再判
        gen = int(live["generation"])
        claimed = payload.get("generation")
        if claimed is not None and int(claimed) != gen:
            # payload 说 1、当前代次 3：这一行是**旧代次**留下的。`native_attempts` 按代次存，
            # 旧代次查得到 ≠ 这件事还活着 —— 清掉、零信，而不是拿历史事实替新窗口下结论。
            return close("generation_moved")
        who = live["actor"]

        # 2b. 本次服务承载它吗？（只读判据，不新建在线状态表、不轮询）
        #     数据库说它 active，与"这台服务正在观测它"是两件事：供给里已经没有它时，我们
        #     根本没有原生观测 —— 既不能说它静默，也不能把"这台服务没带它"读成撤权。
        if self.carried is not None:
            seen_by_service = self.carried(binding)
            if seen_by_service is not True:
                # 不发信、**不清**（缺席 ≠ 撤权）：行留在 armed，等重新握手或既有事件再启窗口。
                return defer("not_carried" if seen_by_service is False else "carried_unknown")

        # 3. 活代次那一条尝试
        seen_row = conn.execute(
            "SELECT value_json FROM binding_state WHERE binding_id=? AND generation=?"
            " AND key='native_attempts'", (binding, gen)).fetchone()
        try:
            seen = json.loads(seen_row["value_json"]) if seen_row is not None else {}
        except (TypeError, ValueError):
            seen = {}
        entry = (seen or {}).get(attempt)
        if not isinstance(entry, dict):
            return close("attempt_unknown")          # 活代次里没有它：永久不可判
        # 同一代次里已经有**更新**的尝试 ⇒ 这是旧窗口：宿主已经往前走了，它的"静默"不再说明
        # 任何正在发生的事（实测：重新承载后旧行会误报一封）。清掉、零信；新窗口自己判。
        if any(isinstance(v, dict) and (v.get("ts") or 0) > (entry.get("ts") or 0)
               for k, v in (seen or {}).items() if k != attempt):
            return close("superseded_by_newer_attempt")
        if entry.get("turn_closed_at") is not None:
            return close("turn_closed")              # 回合已结束：终结动作，不是推后
        last = entry.get("last_activity_ts")
        if last is None:
            last = entry.get("ts")
        try:
            last = float(last)
        except (TypeError, ValueError):
            last = None
        if last is not None and (at - last) < self.window:
            return defer("recent_activity", until=last + self.window)

        # 4. 工具在跑？（同一绑定的 tool_hang 行还 armed；工具走自己的通道）
        if conn.execute("SELECT 1 FROM due WHERE kind='tool_hang' AND state='armed'"
                        " AND binding_id=? LIMIT 1", (binding,)).fetchone() is not None:
            return defer("tool_running")

        # 5. 额度耗尽？（与续投同一个事实读：`quota_blocked` 只有一个实现）
        if quota_blocked(conn, binding, gen):
            return defer("quota_blocked")

        works, other = self.core._notified_work_of_attempt(conn, attempt, binding, gen)
        if works and not other and all(conn.execute(
                "SELECT 1 FROM work WHERE work_ref=? AND state='Active'"
                " AND waiting_json != '' AND (assignee_ref=? OR assignee_ref IN"
                " (SELECT position FROM position_holders WHERE holder=?))",
                (work, who, who)).fetchone() for work in works):
            return defer("work_waiting")
        work = works[0] if len(works) == 1 else None

        # 7. 都没有：升级一封。收件人按 §9 尽量给"该 Work 的负责经理"，归属不确定就如实说不确定。
        target, basis = self._escalation_target(conn, actor=who, work=work, now_ts=at)
        escalate = "model-silent:%s:%s:silent" % (binding, attempt)
        because = "" if basis == "work_manager" else "\n（Work 归属无法唯一确定，先报直接责任人。）"
        sent = self.core.system_letter(
            letter_id="silent-" + hashlib.sha256(escalate.encode("utf-8")).hexdigest()[:16],
            boxes=[target],
            subject="模型长时间没有产出：%s" % attempt,
            body=("这一次原生尝试已 %d 分钟没有 assistant 增量，也不是在等工具、没有额度阻塞、"
                  "没有明确等待。\n先用 office_work_get 看一下现状：若这件事卡在你手里，直接继续"
                  "或改派；若你认为刚才其实在动，忽略这封即可 —— 同一段只报一次。%s"
                  % (int(self.window // 60), because)),
            need="确认是否继续或改派", kind="model.silent", fact_key=escalate,
            conn=conn, now_ts=at)
        fired = due_mod.fire(conn, "model_silent", row["subject_ref"],
                             expected_version=row["version"], now_ts=at)
        return {"decision": "escalated", "letter": sent.get("letter_id"),
                "recipient": target, "basis": basis, "fired": fired is not None}


    def _escalation_target(self, conn, *, actor, work=None, now_ts=None):
        """能确定唯一 Work 和一位当前项目经理时，向该经理升级。"""
        row = conn.execute("SELECT project_ref FROM work WHERE work_ref=?", (work,)).fetchone() if work else None
        if row is None:
            return actor, "unknown"
        boxes = [box for box in manager_boxes(self.core, conn, row["project_ref"], now_ts)
                 if actor not in {box, *self.core._live_holders(conn, box)}]
        return (boxes[0], "work_manager") if len(boxes) == 1 else (actor, "unknown")


class BatchHandler(Handler):
    """A unit's batch deadline: find the current legal readers, then start its round inside."""

    def __init__(self, *, consumers, begin_round, retry_seconds: float = 30.0):
        self.consumers = consumers
        self.begin_round = begin_round
        self.retry_seconds = retry_seconds

    def gather(self, row, at, store):
        unit = row["subject_ref"]
        with store.transaction(read_only=True) as conn:
            # Core's appointment facts, read outside the write lock so a slow read does not hold it.
            return {"unit": unit, "consumers": list(self.consumers(conn, unit))}

    def apply(self, conn, row, facts, at):
        if not facts["consumers"]:
            # §3: a position's mail waits for a legal incumbent. Pushing the row back keeps it in
            # this queue without inventing a round or sending to nobody.
            moved = due_mod.push_back(conn, "batch", facts["unit"],
                                      at + max(1.0, float(self.retry_seconds)), now_ts=at)
            return {"decision": "no_incumbent", "version": (moved or {}).get("version")}
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
