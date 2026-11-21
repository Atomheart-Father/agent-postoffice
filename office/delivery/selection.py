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


def _informational(need):
    """Whether a `need` asks for nothing. Delegates to the one judge in `lib.mail`.

    Imported lazily and with a fallback: this module is also exercised without a Core, and a second
    copy of the Chinese phrasing test would be exactly the kind of drift the file-era code suffered.
    """
    try:
        from lib.mail import informational_need
        return informational_need(need or "")
    except Exception:                                  # noqa: BLE001 - unavailable judge, not "no need"
        return False


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
    return "receipt" if _informational(need) else "formal"


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


def candidates(conn, box, *, only=None, exclude=None, own_round=None):
    """This box's deliverable letters, oldest first, as plain dicts.

    Deliverable means: the state row is `delivered` (not archived/read-legacy), the copy has not been
    accepted (`accepted_at IS NULL` — acceptance is Core's fact and stays there), and the letter is
    not already in an active round. The last condition is the one that used to live in the file
    selector as a `.claims` scan; here it is the unique index `ux_round_ref_active` doing the work.

    `only`/`exclude` are the caller's ref filters (`round_begin(only_refs=, exclude_refs=)`), applied
    here rather than at the round so "what would go" and "what goes" see the same list.
    """
    from lib.mail import letter_fields

    rows = conn.execute(
        "SELECT s.letter_id, s.category, s.accepted_at, s.logical_address, l.created_at,"
        "       l.need, l.subject, l.meta_json, s.content_override_json"
        "  FROM letter_states s JOIN letters l ON l.letter_id = s.letter_id"
        " WHERE s.box = ? AND s.state = 'delivered' AND s.accepted_at IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM work w WHERE l.from_actor='postoffice'"
        " AND json_valid(l.meta_json) AND json_extract(l.meta_json,'$.system')"
        " IN ('work.assigned','work.returned','work.unblocked')"
        " AND w.work_ref=json_extract(l.meta_json,'$.work_ref') AND w.state IN ('Done','Cancelled'))"
        # No `ORDER BY l.created_at`: the column can hold both text and epoch rows during the
        # transition, and SQLite sorts numbers before text, so a mixed table would silently reorder
        # the queue (measured: the epoch row jumped ahead of an older text row). Ordering happens in
        # Python on the parsed value instead, which is the only shape this module reasons about.
        " ORDER BY s.letter_id", (box,)).fetchall()
    keep = set(only) if only is not None else None
    drop = set(exclude or ())
    active = {row[0] for row in conn.execute(
        "SELECT ref FROM round_ref WHERE box = ? AND ref_state = 'active'"
        " AND token IS NOT ?", (box, own_round)).fetchall()}
    unknown = {row[0] for row in conn.execute(
        "SELECT rr.ref FROM round_ref rr JOIN round r ON r.token=rr.token"
        " WHERE rr.box=? AND r.state='unknown' AND r.channel='codex' AND NOT EXISTS"
        " (SELECT 1 FROM round_ref newer WHERE newer.box=rr.box AND newer.ref=rr.ref"
        "  AND newer.attempt>rr.attempt)", (box,)).fetchall()}
    out = []
    for row in rows:
        ref = row["letter_id"]
        if keep is not None and ref not in keep:
            continue
        if ref in drop or ref in active or ref in unknown:
            continue
        meta = _meta(row["meta_json"])
        fields = letter_fields(row, row, ("subject", "need"))
        out.append({"ref": ref, "kind": _kind_of(row, meta, fields["need"]), "created_at": _epoch(row["created_at"]),
                    "subject": fields["subject"], "logical_address": row["logical_address"],
                    "meta": meta})
    # Unparseable rows last, then oldest first, then by ref so the answer is stable across passes.
    out.sort(key=lambda item: (item["created_at"] is None,
                               item["created_at"] if item["created_at"] is not None else 0.0,
                               item["ref"]))
    return out


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
    formal = [item for item in items if item["kind"] in FORMAL_KINDS]
    ride = [item for item in items if item["kind"] in ("receipt", "notice")]
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
    in_round = _formal_in_round(conn, box)
    if in_round:
        return {"deliver": [], "carry": [], "hold_until": None, "reason": "unread_reminder",
                "unread": True, "in_round": in_round}
    if cc:
        return _with_bad({"deliver": [], "carry": [], "hold_until": None, "reason": "cc_wait",
                          "unread": False}, bad)
    return _with_bad({"deliver": [], "carry": [], "hold_until": None, "reason": "receipts_wait",
                      "unread": False}, bad)


def _formal_in_round(conn, box):
    """Refs of action-asking letters already in an **active** round for this box, oldest first.

    This is what the file era spelled `mailbox_unread_on` / `_mailbox_mark_reminded`: a reminder that
    was handed to a round and not yet accepted must not be handed again. In the record it is one
    indexed question — active refs are exactly the letters somebody is already being told about — so
    there is no `.seen` file and no per-box marker to keep in step.

    A round on a letter that has since been accepted or archived is not a pending reminder, so both
    are excluded: the question is "is somebody still being told", not "was there ever a round".
    """
    rows = conn.execute(
        "SELECT r.ref FROM round_ref r JOIN letter_states s"
        "  ON s.letter_id = r.ref AND s.box = r.box"
        " WHERE r.box = ? AND r.ref_state = 'active' AND s.state = 'delivered'"
        "   AND s.accepted_at IS NULL"
        " ORDER BY r.rowid", (box,)).fetchall()
    return [row[0] for row in rows]


def _with_bad(answer, bad):
    """Attach `bad_created_at` only when there is something to report.

    An always-present empty list trains readers to ignore it; a key that appears only when a row is
    broken is a fact a log line can carry without a "0" that means nothing.
    """
    if bad:
        answer["bad_created_at"] = bad
    return answer
