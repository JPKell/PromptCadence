"""promptcadence.domain.events — the closed event vocabulary, and the rule every body obeys.

Spec §17 lists the event types PromptCadence emits; :class:`EventType` is that list and nothing
else. A state change and its event are **one write** (ADR-0044), so the emitting transition for
each type is lifecycle §8.2's table, and this phase owns the *shapes* while Phase 3 owns the
write.

One rule governs every body, and it is the reason bodies are typed value objects rather than free
dictionaries:

    **An event body carries ids, categories, numbers and timestamps — never prompt text, never
    model output, never a tool argument.** Events are replayed over SSE, written to logs and
    rendered in a browser; a body that carried the transcript would put a confidential trajectory's
    content on every one of those surfaces, and would do it for the trajectory the operator was
    most careful about.

Bodies live with the code that mints them — :class:`~promptcadence.domain.intent.IntentMinted` in
``intent.py``, :class:`~promptcadence.domain.deviation.DeviationDetected` in ``deviation.py``, the
trajectory bodies in ``trajectory.py``, the approval bodies in ``policy.py``. That keeps the shape
beside the decision it announces, and it keeps this module a leaf with no domain imports, so no
other module has to route around it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

__all__ = ["EventBody", "EventType"]


class EventType(StrEnum):
    """Every event PromptCadence emits (spec §17), and no other.

    An event this application can produce that is not a member is a defect in the specification to
    close with an amendment, not a string to invent at the emit site — the SSE consumer, the
    explanation renderer and the operator console all switch on this vocabulary.
    """

    TRAJECTORY_CREATED = "trajectory.created"
    TRAJECTORY_CLAIMED = "trajectory.claimed"
    PLAN_DRAFTED = "plan.drafted"
    PLAN_APPROVED = "plan.approved"
    PLAN_REJECTED = "plan.rejected"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_DENIED = "approval.denied"
    INTENT_MINTED = "intent.minted"
    STEP_STARTED = "step.started"
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_COMPLETED = "tool.call.completed"
    CONTEXT_COMPACTED = "context.compacted"
    BUDGET_DEBITED = "budget.debited"
    BUDGET_WINDOW_WAIT = "budget.window_wait"
    EGRESS_EVALUATED = "egress.evaluated"
    DEVIATION_DETECTED = "deviation.detected"
    STEP_RETRIED = "step.retried"
    STEP_COMPLETED = "step.completed"
    TRAJECTORY_COMPLETED = "trajectory.completed"
    TRAJECTORY_RESUMED = "trajectory.resumed"
    TRAJECTORY_HALTED = "trajectory.halted"
    TRAJECTORY_FAILED = "trajectory.failed"
    TRAJECTORY_CANCELLED = "trajectory.cancelled"
    TRAJECTORY_RECOVERED = "trajectory.recovered"


@runtime_checkable
class EventBody(Protocol):
    """What every event body provides: its type, and one canonical mapping form.

    ``as_canonical`` is what the ``events.data_json`` column stores and what an SSE frame carries,
    so it must be JSON-serializable, byte-stable for equal bodies, and free of content — see this
    module's docstring for the rule, and ``tests/unit/test_domain_events.py`` for the guard that
    enforces it over every body in the package.
    """

    event_type: ClassVar[EventType]

    def as_canonical(self) -> dict[str, Any]:
        """Return the body as the mapping that is persisted and streamed."""
        ...
