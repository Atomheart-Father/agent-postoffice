"""Write one wake frame per unit and due key after the due transaction commits.

A batch carries the text and refs selected by delivery. Its receipt checks
the due version. Idle wake frames carry the attempt key. Atomic replacement
keeps one frame per key. The host reads without removing it and acknowledges
only after native start.
"""

from __future__ import annotations

import json
import os
import re
import time

from delivery.round import MESSAGES

#: Frame format. Bumped only if a field changes meaning; the consumer refuses what it cannot read
#: rather than guessing, because a guessed wake is a wrong turn.
FRAME_VERSION = 1

def _safe(name: str) -> str:
    """A filesystem-safe spelling of a due key. Only the name changes; the frame keeps the original."""
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", name)[:120] or "frame"


def frame_for(action: dict, *, at: float | None = None) -> dict:
    """The one frame shape, built from a `worker` action. Unknown action kinds are refused.

    Refusing rather than writing a frame with a missing kind is deliberate: a consumer that receives
    something it does not understand cannot act, and a frame nobody acts on looks exactly like a
    delivery that was made.
    """
    kind = action.get("kind")
    if kind not in ("batch_round", "idle_started"):
        raise ValueError("unknown wake action kind: %r" % (kind,))
    frame = {"v": FRAME_VERSION, "kind": kind, "unit": action.get("unit"),
             "due_key": action.get("due_key"), "at": float(at if at is not None else time.time())}
    if kind == "batch_round":
        body = action.get("text")
        if not isinstance(body, str) or not body:
            # 没有正文的批次帧不可执行，而"没人能执行的帧"与"已经投递过"长得一样 ⇒ 拒写，
            # 不写 `text: null` 让宿主去猜（那是第二个答案的入口）。
            raise ValueError("batch_round frame needs the body delivery wrote")
        frame.update({"refs": list(action.get("refs") or ()),
                      # 正文由投递层写（`delivery.round.batch_text`），帧只搬运它。
                      "text": body,
                      # 这一帧对应的是哪一版到期行。回执按它做 CAS：帧写出之后又被重新 arm
                      # （又来一封信）时，旧帧的回执不能把新那一版一起清掉。
                      "due_version": action.get("due_version")})
    else:
        frame.update({"text": action.get("text") or MESSAGES["idle"],
                      "attempt_key": action.get("attempt_key")})
    return frame


def frame_path(box_dir, kind, due_key):
    return os.path.join(str(box_dir), 'wake', '%s-%s.json' % (kind, _safe(due_key or '')))


def write_frame(box_dir, frame: dict) -> str:
    """Atomically replace the previous frame for this due key."""
    box_dir = str(box_dir)
    wake_dir = os.path.join(box_dir, "wake")
    os.makedirs(wake_dir, exist_ok=True)
    path = frame_path(box_dir, frame["kind"], frame.get("due_key"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(frame, fh, ensure_ascii=False, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def wake_host(*, box_dir_for):
    """Use the daemon's mailbox directory resolver for both reads and writes."""
    def dispatch(action: dict) -> str:
        frame = frame_for(action)
        return write_frame(box_dir_for(frame["unit"]), frame)
    dispatch.frame_path = lambda unit, kind, key: frame_path(box_dir_for(unit), kind, key)
    return dispatch
