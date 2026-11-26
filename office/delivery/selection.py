"""Which letters leave a mailbox now: the delivery rule, read from the record instead of the disk.

The file-era selector answered this by scanning `<box>/inbox/*.md` and comparing file mtimes. That
world is gone: `letter.send` writes `letters` + `letter_states` in one transaction, and there is no
"letter file waiting to be delivered" left to scan. The *policy* did not change, though, and it is the
reason this module exists:

* a letter that asks for action waits out the batch window, then leaves in a bounded batch;
* receipts, notices and cc copies ride along with that batch (they never trigger one themselves);
* a second reminder for a letter that is already in an active round is suppressed — the person has
  been told, and telling them again is what turns a useful reminder into noise;
* "nothing to do" is not the same as "nothing there", so the answer names which it is (`hold_until`
  vs `reason`).

**Facts are read, policy is passed.** `window` and `batch` are the caller's policy values
(`RoundOperations._selection_timing` owns them); this module never invents a default, because a
default here would be a second policy silently disagreeing with the configured one.

**No round is created and nothing is written.** `round_begin` owns the round, the refs and the
freeze; this function only answers "what would go, and why not yet".
"""
from __future__ import annotations

import json
import time

#: Kinds in the order the old rule used them. `formal` and `alarm` gate the window; `receipt`,
#: `notice` and `cc` only ride. Kept as a tuple so a typo is caught here rather than becoming a
#: silently missing letter.
FORMAL_KINDS = ("formal", "alarm")
RIDE_KINDS = ("receipt", "notice", "cc")


def _meta(raw):
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _kind_of(row, meta, need):
    """One letter's kind for this box, from record facts only.

    `cc` is a property of the *copy* (`letter_states.category`), not of the letter; an alarm is a
    system letter whose `meta.system` says so; everything else splits on whether `need` asks for
    anything. That split is the only place the old rule's `receipt`/`notice` distinction still
    matters, and it is expressed as "rides along" rather than by naming every system kind — a new
    system kind should not need this function edited to be treated as a notice.
    """
    if (row["category"] or "to") == "cc":
        return "cc"
    system = str(meta.get("system") or "")
    if system == "alarm" or system.startswith("alarm."):
        return "alarm"
    from lib.mail import SYSTEM_MAIL_SENDER, informational_need
    return "receipt" if informational_need(need, system=row["from_actor"] == SYSTEM_MAIL_SENDER) else "formal"


def _epoch(value):
    """`letters.created_at` → epoch 秒；两种形状都认，**认不出就是 None，不是 0**。

    列声明 REAL，写入方（`lib/mail._now`）写的是本机文本时间 `YYYY-mm-dd HH:MM:SS` —— 全树的时间列
    都是这一个形状，`letter.read` 给出去的也是它。所以这里按"库里实际存的"解析，并把这段当作
    **过渡**：写入侧统一（列声明改成 TEXT）后，这个函数就该删掉。

    `0.0` 是错的答案：时间认不出时窗口判不了，当成 1970 就让这封信**永远最急**、插到所有正常信前面
    —— 坏数据不该获得最高优先级。调用方拿 `None` 把该行排除出 `deliver` 并**点名**报出，这样它既不
    被丢、也不会插队。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return time.mktime(time.strptime(value, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            pass
        # 列一旦是 TEXT 亲和，写进来的**数值**会被 SQLite 转成 `"1795075967.55"` —— 第三种形状。
        # 认它不是因为"大概对"，而是因为它就是同一个瞬间的另一种写法；认不出才答 None。
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _item(row, origins):
    from lib.mail import letter_fields

    ref = row["letter_id"]
    meta = _meta(row["meta_json"])
    fields = letter_fields(row, row, ("subject", "need"))
    return {"ref": ref, "kind": _kind_of(row, meta, fields["need"]), "created_at": _epoch(row["created_at"]),
            "subject": fields["subject"], "logical_address": row["logical_address"],
            "meta": meta, "alert_origin": origins.get(ref)}


def candidates(conn, box, *, only=None, exclude=None, own_round=None):
    """This box's deliverable letters, oldest first, as plain dicts.

    Deliverable means: the state row is `delivered` (not archived/read-legacy), the copy has not been
    accepted (`accepted_at IS NULL` — acceptance is Core's fact and stays there), and the letter is
    not already in an active round. The last condition is the one that used to live in the file
    selector as a `.claims` scan; here it is the unique index `ux_round_ref_active` doing the work.

    `only`/`exclude` are the caller's ref filters (`round_begin(only_refs=, exclude_refs=)`), applied
    here rather than at the round so "what would go" and "what goes" see the same list.
    """
    keep = set(only) if only is not None else None
    if keep == set():
        return []
    rows = conn.execute(
        "SELECT l.letter_id, l.from_actor, l.created_at, l.subject, l.need, l.meta_json, s.state, s.state_reason,"
        " s.accepted_at, s.category, s.logical_address, s.content_override_json"
        " FROM letter_states s JOIN letters l ON l.letter_id=s.letter_id"
        " WHERE s.box=? AND s.state='delivered' AND s.accepted_at IS NULL" +
        (" AND s.letter_id IN (SELECT value FROM json_each(?))" if keep is not None else "") +
        # No `ORDER BY l.created_at`: the column can hold both text and epoch rows during the
        # transition, and SQLite sorts numbers before text, so a mixed table would silently reorder
        # the queue (measured: the epoch row jumped ahead of an older text row). Ordering happens in
        # Python on the parsed value instead, which is the only shape this module reasons about.
        " ORDER BY s.letter_id", (box,) if keep is None else (box, json.dumps(sorted(keep)))).fetchall()
    drop = set(exclude or ()) | set(_reserved_refs(conn, box, own_round))
    rows = [row for row in rows if row["letter_id"] not in drop]
    if not rows:
        return []
    from office_core import Core
    facts = Core._mail_facts(conn, rows, box=box)
    out = []
    for row in rows:
        life = Core._letter_lifecycle(row, row, facts=facts, box=box)
        if life["terminal"] and (life["letter_class"] == "work_notice" or life["terminal_reason"] == "alert_obsolete"):
            continue
        out.append(_item(row, facts["_alert_origins"]))
    # Unparseable rows last, then oldest first, then by ref so the answer is stable across passes.
    out.sort(key=lambda item: (item["created_at"] is None,
                               item["created_at"] if item["created_at"] is not None else 0.0,
                               item["ref"]))
    return out



def _formal_and_passive(conn, box, items):
    formal = [item for item in items if item["kind"] in FORMAL_KINDS]
    if not any(item["alert_origin"] for item in formal):
        return formal, set()
    sessions = {row[0] for row in conn.execute(
        "SELECT host_session_ref FROM bindings WHERE actor=? AND state='active'"
        " AND revoked_at IS NULL AND host_session_ref<>''", (box,))}
    primary, peer, passive = [], [], set()
    for item in formal:
        origin = item["alert_origin"]
        if origin not in sessions:
            primary.append(item)
        elif len(sessions) == 1:
            passive.add(item["ref"])
        else:
            # A peer can be woken for an alert about another session. Leave room for independent
            # business in the shared batch so the affected session can receive its own real mail.
            peer.append(item)
    return primary + peer, passive


def automatic_pending_count(conn, box):
    """Current formal mail that can still be selected by the normal scheduler."""
    from delivery import due
    batch = due.get(conn, due.due_key("batch", box))
    if not batch or batch["state"] != "armed":
        return 0
    formal, _ = _formal_and_passive(conn, box, candidates(conn, box))
    return sum(item["created_at"] is not None for item in formal)


def pending_delivery_count(conn, box):
    """Existing waiting and in-flight count; retained for existing consumers."""
    return automatic_pending_count(conn, box) + len(_reserved_refs(conn, box, include_unknown=False))


def unresolved_refs(conn, box, *, reserved=None, normal=None):
    """Reserved sends without acceptance, including authorized but still in-flight."""
    reserved = set(_reserved_refs(conn, box)) if reserved is None else reserved
    normal = set(_reserved_refs(conn, box, include_unknown=False)) if normal is None else normal
    unknown = reserved - normal
    authorized = conn.execute(
        "SELECT DISTINCT rr.ref FROM round_ref rr JOIN round r ON r.token=rr.token"
        " JOIN letter_states s ON (s.box,s.letter_id)=(rr.box,rr.ref)"
        " WHERE rr.box=? AND rr.ref_state='active' AND s.state='delivered' AND s.accepted_at IS NULL"
        " AND r.state='begun' AND r.send_id<>'-'", (box,))
    return unknown | {row[0] for row in authorized}


def delivery_projection(conn, box):
    """Keep the existing count while separating selectable work from uncertain sends."""
    automatic = automatic_pending_count(conn, box)
    reserved = set(_reserved_refs(conn, box))
    normal = set(_reserved_refs(conn, box, include_unknown=False))
    unresolved = unresolved_refs(conn, box, reserved=reserved, normal=normal)
    letters = sorted(unresolved)
    return {"pending": automatic + len(normal), "automatic": automatic,
            "in_flight": len(normal - unresolved),
            "unresolved": {"count": len(letters), "letters": letters[:20], "has_more": len(letters) > 20}}


def own_alert_only(conn, session, refs, box, *, items=None):
    """A session may read its own alert, but that alert alone must not start it."""
    refs = refs or []
    if not refs or not session:
        return False
    if items is None:
        from office_core import Core

        rows = conn.execute("SELECT l.*,s.category,s.content_override_json,s.logical_address"
                            " FROM letters l JOIN letter_states s ON s.letter_id=l.letter_id"
                            " WHERE s.box=? AND s.letter_id IN (SELECT value FROM json_each(?))",
                            (box, json.dumps(sorted(set(refs))))).fetchall()
        if len(rows) != len(set(refs)):
            return False
        origins = Core._mail_facts(conn, rows, box=box)["_alert_origins"]
        items = [_item(row, origins) for row in rows]
    formal = [item for item in items if item["kind"] in FORMAL_KINDS]
    return bool(formal and all(item["alert_origin"] == session for item in formal))



def select(conn, box, *, window, batch, now=None, only=None, exclude=None):
    """`{deliver, carry, hold_until, reason, unread, would_deliver}` — the one selection answer.

    The vocabulary is the file rule's, so an operator reading a log sees the same words:
    `window_elapsed` (something leaves), `window` (something is waiting), `unread_reminder` (a letter
    is already in an active round and still unaccepted, so this pass must not remind again),
    `cc_wait` (only copies), `receipts_wait` (only things that ride).

    `batch` bounds both halves independently: a mailbox with more formal letters than the batch sends
    the oldest `batch`, and the rest keep their `hold_until` so the next pass does not starve them.
    """
    at = time.time() if now is None else float(now)
    window = max(0.0, float(window))
    batch = max(0, int(batch))
    items = candidates(conn, box, only=only, exclude=exclude)
    # A row whose `created_at` cannot be read as a time cannot be given a window, so it must not be
    # sent on a guess. It is named in `bad_created_at` — visible in the log and to whoever fixes the
    # row — instead of being sorted to 1970 and pushed to the front of the queue.
    bad = [item["ref"] for item in items if item["created_at"] is None]
    items = [item for item in items if item["created_at"] is not None]
    formal, passive = _formal_and_passive(conn, box, items)
    ride = [item for item in items if item["kind"] in ("receipt", "notice")]
    ride.extend(item for item in items if item["ref"] in passive)
    cc = [item for item in items if item["kind"] == "cc"]

    due = [item for item in formal if at - item["created_at"] >= window]
    if due:
        depart = [item["ref"] for item in formal][:batch]
        if not depart:
            # `batch=0` means "no formal letter in this pass" (the policy's own words); the letters
            # keep their window and the next pass with a real batch takes them.
            return {"deliver": [], "carry": [], "hold_until": min(i["created_at"] for i in formal) + window,
                    "reason": "window", "unread": False}
        # Leftovers behind the batch are **not** held by the window — the window has passed, they are
        # simply next in line. Saying `hold_until: <their oldest + window>` would make the caller sleep
        # a full window for a letter that is ready now; so they are named as `rest` and the caller
        # decides whether to come straight back (it can) instead of guessing from an empty answer.
        return _with_bad({"deliver": depart, "carry": [item["ref"] for item in ride][:batch],
                          "hold_until": None, "reason": "window_elapsed", "unread": False,
                          "rest": [item["ref"] for item in formal][batch:]}, bad)
    if formal:
        return {"deliver": [], "carry": [], "reason": "window", "unread": False,
                "hold_until": min(item["created_at"] for item in formal) + window}
    # A letter that is already in an active round is excluded from `candidates` (that is the unique
    # index doing the old `.claims` scan), so it can never appear in `deliver`. Without this branch an
    # operator would be told "cc_wait" or "receipts_wait" for a mailbox whose real state is "already
    # reminded, waiting for the round" — a wrong explanation is worse than no explanation, because it
    # sends the next person looking for copies that are not there.
    in_round = _reserved_refs(conn, box)
    if in_round:
        return {"deliver": [], "carry": [], "hold_until": None, "reason": "unread_reminder",
                "unread": True, "in_round": in_round}
    if cc:
        return _with_bad({"deliver": [], "carry": [], "hold_until": None, "reason": "cc_wait",
                          "unread": False}, bad)
    return _with_bad({"deliver": [], "carry": [], "hold_until": None, "reason": "receipts_wait",
                      "unread": False}, bad)


def _reserved_refs(conn, box, own_round=None, *, include_unknown=True):
    """Unaccepted handoffs that still reserve the original letter."""
    rows = conn.execute(
        "SELECT rr.ref FROM round_ref rr JOIN round r ON r.token=rr.token"
        " JOIN letter_states s ON (s.box,s.letter_id)=(rr.box,rr.ref)"
        " WHERE rr.box=? AND rr.token IS NOT ? AND s.state='delivered' AND s.accepted_at IS NULL"
        " AND (? OR r.state<>'unknown')"
        " AND rr.ref_state='active'"
        " ORDER BY rr.rowid", (box, own_round, include_unknown))
    return list(dict.fromkeys(row[0] for row in rows))


def _with_bad(answer, bad):
    """Attach `bad_created_at` only when there is something to report.

    An always-present empty list trains readers to ignore it; a key that appears only when a row is
    broken is a fact a log line can carry without a "0" that means nothing.
    """
    if bad:
        answer["bad_created_at"] = bad
    return answer
