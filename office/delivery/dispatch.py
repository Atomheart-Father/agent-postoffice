"""Post-commit dispatch: turn a settled due row's action into one wake frame on disk.

`worker` decides *what* should happen; this module only tells the host, strictly **after** the commit
— a native wake cannot be rolled back, so it must never sit inside the transaction that justifies it.

## 帧与领取链

一帧一个动作，原子写进单元自己的 `wake/`：

    <box_dir>/wake/<sortable>-<kind>.json        # 帧
    <box_dir>/wake/<sortable>-<kind>.json.tmp    # 永不可见：`os.replace` 覆盖过去

**取帧不删、回执才删**：`round.await` 读帧（读几次都是同一帧），`wake.ack` 在原生起轮之后才删。
所以"读完就删"不是这一层的消费者契约 —— 那等于还没起轮就先回执（真机结论见
`tests/w439_d1_wake_claim_test.py`）。

**为什么是文件不是 socket 调用**：daemon 与宿主是两个进程，帧必须在宿主重启期间也活着（提交了
却没人可叫的到期项不能悄悄消失）；而端点 socket 是*宿主 → Core* 方向，往里推帧就得给 daemon
编一个监听侧身份。

## 两种形状

`batch_round` — 单元合批到期、投递层已写出这一批的正文：

    {"v": 1, "kind": "batch_round", "unit": ..., "due_key": ..., "at": ...,
     "refs": [...], "due_version": <这一帧对应的到期行版本>, "text": <批次正文>}

`text` 由投递层写一次（`delivery.round.batch_text`，用这一轮刚冻住的 refs），帧只搬运它：宿主把
原文送下去，不再渲染第二遍。

`due_version` 是回执照的锚：帧写出之后那封信可能又被别的写入 arm 了一次（同一单元、同一个到期键，
版本 +1），此时旧帧的回执只能清它自己那一版，不能把"还没送出去的那一版"一起清掉。

`idle_started` — 停下来的会话没有新信可读、但还有能动手的活：

    {"v": 1, "kind": "idle_started", "unit": ..., "attempt_key": <原生尝试>,
     "text": <续工提示>, "due_key": ..., "at": ...}

D1 的提示是投递层自己的句子，不是信，所以 `text` 必须随帧带。
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


def write_frame(box_dir, frame: dict) -> str:
    """Write one frame atomically into `box_dir/wake/`; return its path.

    **一个到期键只有一帧。** 同一个 `due_key` 的新快照 `os.replace` 掉旧的那一帧：两帧并存就是
    让宿主自己挑"哪一帧算数"，而它只能按文件名挑**最旧**的那一帧，于是拿过期快照去 `round.begin`，
    与那一刻重新冻的选择不等集 —— `frame_refs_vs_frozen` 就是这么来的（案发同一条 due_key 两帧，
    refs 一旧一新）。旧的不是历史，是同一个期限的过期快照。
    """
    box_dir = str(box_dir)
    wake_dir = os.path.join(box_dir, "wake")
    os.makedirs(wake_dir, exist_ok=True)
    stem = "%s-%s" % (frame["kind"], _safe(frame.get("due_key") or ""))
    path = os.path.join(wake_dir, stem + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(frame, fh, ensure_ascii=False, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def wake_host(*, box_dir_for):
    """Build the `on_action` callable the daemon (officed) expects.

    `box_dir_for(unit)` 就是投递层唯一那句"这个单元住哪儿"（岗位在 `positions/<name>/`，个人箱在
    `<home>/<name>/`），这里不立第二套。写帧出错原样抛给 daemon 按动作记，不失败整趟。
    """
    def dispatch(action: dict) -> str:
        frame = frame_for(action)
        return write_frame(box_dir_for(frame["unit"]), frame)
    return dispatch
