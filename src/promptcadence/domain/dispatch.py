"""promptcadence.domain.dispatch — the ready-set rule, and the two events a step writes.

Lifecycle §8.4: the ``LoopController`` keeps a ready set — the steps whose dependencies have all
committed — and dispatches from it under ``max_concurrent_steps``. Raised above 1, concurrency is
granted only across **disjoint execution surfaces**: at most one local step in flight *ever*,
because LoadCoach's admission control and ADR-0038 make two concurrent local steps a queueing
fiction, plus up to ``max_concurrent_remote_steps`` remote ones. The rule is pure so it can be
walked as a matrix; the controller only asks it what to start next.

The DAG is always recorded, and this function is always consulted, even when the answer is one
step at a time — so the explanation can later show what *could* have run in parallel.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from baseaicore import ValidationError

from promptcadence.domain.events import EventType
from promptcadence.domain.plan import PlanStep
from promptcadence.domain.tiers import Tier

__all__ = ["StepCompleted", "StepStarted", "dispatchable"]


def dispatchable(
    ready: Sequence[PlanStep],
    *,
    in_flight: Sequence[Tier],
    tier_of: Mapping[str, Tier],
    max_concurrent_steps: int,
    max_concurrent_remote_steps: int,
) -> tuple[PlanStep, ...]:
    """Choose which ready steps may start now, given what is already in flight.

    Args:
        ready: The ready steps, in plan order. Only steps that hold a live intent belong here; a
            gated step with no intent yet is the controller's approval question, not a dispatch
            one.
        in_flight: The tier of every step currently running. Tiers, not names: the rule cares
            only about each one's execution surface, and a tier says which it is.
        tier_of: The tier each ready step would run on, keyed by ``step_id`` — the **intent's**
            approved tier, which on a redline is not the tier the plan declared.
        max_concurrent_steps: ``[execution] max_concurrent_steps``.
        max_concurrent_remote_steps: ``[execution] max_concurrent_remote_steps``.

    Returns:
        The steps to start, a subset of ``ready`` in plan order: never more than
        ``max_concurrent_steps`` in flight in total, never more than one local step in flight, and
        never more than ``max_concurrent_remote_steps`` remote ones. Empty when nothing may start.

    Raises:
        ValidationError: If a ready step names no tier in ``tier_of``, or a bound is below 1. A
            limit of zero would be a controller that dispatches nothing forever.
    """
    if max_concurrent_steps < 1 or max_concurrent_remote_steps < 1:
        message = "concurrency bounds start at 1; a bound of 0 dispatches nothing forever"
        raise ValidationError(message, details={"field": "max_concurrent_steps"})
    local_in_flight = sum(1 for tier in in_flight if not tier.is_remote)
    remote_in_flight = len(in_flight) - local_in_flight
    total = len(in_flight)
    chosen: list[PlanStep] = []
    for step in ready:
        if step.step_id not in tier_of:
            message = f"ready step {step.step_id!r} has no tier to dispatch on"
            raise ValidationError(message, details={"field": "tier_of", "step_id": step.step_id})
        if total >= max_concurrent_steps:
            break
        tier = tier_of[step.step_id]
        if tier.is_remote:
            if remote_in_flight >= max_concurrent_remote_steps:
                continue
            remote_in_flight += 1
        else:
            if local_in_flight >= 1:
                continue
            local_in_flight += 1
        total += 1
        chosen.append(step)
    return tuple(chosen)


@dataclass(frozen=True, slots=True)
class StepStarted:
    """``step.started`` — a step's first dispatch, in the write that opened its thread.

    Both paths emit it: the bypass loop is one synthetic step, ``loop``, and a record whose step
    events existed only on the planned path would make contract 1's diff branch on the mode.
    """

    event_type: ClassVar[EventType] = EventType.STEP_STARTED
    trajectory_id: str
    step_id: str
    thread_id: str
    intent_id: str
    intent_revision: int
    approved_tier: str
    depends_on: tuple[str, ...] = ()

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "step_id": self.step_id,
            "thread_id": self.thread_id,
            "intent_id": self.intent_id,
            "intent_revision": self.intent_revision,
            "approved_tier": self.approved_tier,
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True, slots=True)
class StepCompleted:
    """``step.completed`` — the step reached a declared finish, in the write that recorded it."""

    event_type: ClassVar[EventType] = EventType.STEP_COMPLETED
    trajectory_id: str
    step_id: str
    thread_id: str
    intent_id: str
    intent_revision: int
    turn_count: int
    final_turn_id: str

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "step_id": self.step_id,
            "thread_id": self.thread_id,
            "intent_id": self.intent_id,
            "intent_revision": self.intent_revision,
            "turn_count": self.turn_count,
            "final_turn_id": self.final_turn_id,
        }
