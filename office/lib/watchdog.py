"""Watchdog decisions for observations from a verified native host binding.

The delivery worker owns the durable due row. It checks the row version and
applies the decision in the same transaction as any system letter.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ToolCall:
    binding_id: str
    generation: int
    turn_id: str
    native_id: str


@dataclass(frozen=True)
class ToolObservation:
    call: ToolCall
    state: Literal["running", "finished", "waiting", "background"]
    # The adapter advances this only for actual progress.
    progress_revision: int | None = None
    # Verified samples of the associated process tree, not inferred from missing output.
    process_tree_idle: bool | None = None
    process_tree_root: tuple[int, str] | None = None


ToolDecision = Literal["clear", "advance", "unknown", "notify", "already_notified"]


def tool_due_decision(
    call: ToolCall,
    *,
    progress_revision: int,
    fire_count: int,
    observation: ToolObservation | None,
) -> ToolDecision:
    """Decide after rereading the native tool at its deadline.

    The due worker rearms on progress, clearing the previous alert episode.
    An unknown observation can be reported as unknown without claiming a stall.
    """
    if observation is None or observation.call != call:
        return "unknown"
    if observation.state in ("finished", "waiting", "background"):
        return "clear"
    observed_progress = observation.progress_revision
    if (
        observation.state != "running"
        or observed_progress is None
        or observed_progress < progress_revision
    ):
        return "unknown"
    if observed_progress > progress_revision:
        return "advance"
    if observation.process_tree_idle is not True:
        return "unknown"
    return "already_notified" if fire_count else "notify"


@dataclass(frozen=True)
class QuotaDecision:
    blocked: bool
    notify: bool


def quota_decision(
    was_blocked: bool,
    *,
    status: int | None = None,
    limit_window: Literal["five_hour", "weekly"] | None = None,
    reason: str | None = None,
) -> QuotaDecision:
    """Recognize exhaustion and notify once per blocked episode."""
    exhausted = reason == "quota_exhausted" or status == 402 or (
        status == 429 and limit_window in ("five_hour", "weekly")
    )
    return QuotaDecision(
        blocked=was_blocked or exhausted,
        notify=exhausted and not was_blocked,
    )
