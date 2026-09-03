"""promptcadence.domain.turns — what one turn declared, and the two events the loop writes.

Spec §11 contract 6, the advance contract, in one pure function: **a turn completes only on a
declared** ``finish_reason`` **of** ``STOP``, **or on a schema-validated structured result.**
``LENGTH``, ``ERROR`` and *absence* are handled explicitly and never read as success. The reason
this is a domain function rather than an ``if`` in the loop is that it is the quiet failure of the
whole harness: a truncated answer that flows onward as a completed turn looks exactly like a
finished trajectory, and nothing downstream can tell. So the decision is one place, golden-tested,
with every input it may consider named in its signature and nothing else reachable.

A model never decides control flow. Nothing here reads the text; ``finish_reason`` is what the
provider *declared*, ``schema_validated`` is what LoadCoach's validator *checked*, and neither is
the model saying it is done.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from promptcadence.domain.errors import ErrorCode
from promptcadence.domain.events import EventType
from promptcadence.domain.threads import FinishReason

__all__ = [
    "FinishDecision",
    "FinishOutcome",
    "TurnCompleted",
    "TurnStarted",
    "decide_finish",
]


class FinishOutcome(StrEnum):
    """What the loop does after one turn, given only what was declared."""

    COMPLETE = "complete"
    """The turn is the trajectory's declared finish: ``STOP``, or a schema-validated result."""

    CONTINUE = "continue"
    """The provider declared it wants tools (``TOOL_CALLS``); the loop's next act is Phase 4's."""

    HALT = "halt"
    """No declared success. ``LENGTH``, ``ERROR``, a cancelled or filtered generation, an
    ``unknown`` reason, or no reason at all — each names its cause and none continues silently."""


@dataclass(frozen=True, slots=True)
class FinishDecision:
    """The decision, with the cause every halt must name (spec §13).

    Attributes:
        outcome: What to do.
        cause: Why, in words ``promptcadence trajectory show`` prints verbatim. Present on every
            outcome so a completion is as explicable as a halt.
        error_code: The spec §13 code behind a halt; ``None`` otherwise.
    """

    outcome: FinishOutcome
    cause: str
    error_code: ErrorCode | None = None


def decide_finish(
    *,
    finish_reason: FinishReason | None,
    schema_validated: bool,
    tool_calls_requested: int,
    undeclared_reason: str | None = None,
) -> FinishDecision:
    """Decide whether one turn is a declared finish, a tool round trip, or a halt.

    Args:
        finish_reason: The provider's declared stop reason as LoadCoach forwarded it, or ``None``
            when the response carried none. ``None`` is *absence*, which contract 6 lists beside
            ``LENGTH`` and ``ERROR`` as handled explicitly — it is not a reason and it is not a
            success.
        schema_validated: Whether LoadCoach performed a JSON-Schema check on the output and it
            passed. A length or regex check is not a schema check and does not count: passing
            ``max_output_chars`` says nothing about whether the answer is complete. A declared
            ``LENGTH`` or ``ERROR`` wins over a passed schema check — the provider said the
            answer was cut off, and a document that happens to validate is still not the answer.
        tool_calls_requested: How many tool calls the response carried.
        undeclared_reason: A reason string the response carried that is not a
            :class:`~promptcadence.domain.threads.FinishReason` member (``"content_filter"``,
            ``"cancelled"``, ``"unknown"``), recorded in the cause. Only meaningful with
            ``finish_reason=None``.

    Returns:
        :attr:`FinishOutcome.COMPLETE` for ``STOP`` or a schema-validated result;
        :attr:`FinishOutcome.CONTINUE` for ``TOOL_CALLS`` (or tool calls with no declared reason);
        :attr:`FinishOutcome.HALT` for everything else, with the cause named.

    Refuses nothing: every combination of inputs has an answer, because a turn the loop cannot
    decide is a turn that would be decided by whoever wrote the fallthrough.
    """
    if finish_reason is FinishReason.STOP:
        return FinishDecision(FinishOutcome.COMPLETE, "the provider declared finish_reason=stop")
    if finish_reason is FinishReason.LENGTH:
        return FinishDecision(
            FinishOutcome.HALT,
            "the provider declared finish_reason=length: the answer was truncated at the output "
            "limit and a truncated answer is never read as success (spec §11 contract 6)",
            ErrorCode.LOADCOACH_ERROR,
        )
    if finish_reason is FinishReason.ERROR:
        return FinishDecision(
            FinishOutcome.HALT,
            "the provider declared finish_reason=error: generation ended in a provider error",
            ErrorCode.LOADCOACH_ERROR,
        )
    if schema_validated:
        return FinishDecision(
            FinishOutcome.COMPLETE, "LoadCoach validated the output against the profile's schema"
        )
    if finish_reason is FinishReason.TOOL_CALLS or (
        finish_reason is None and tool_calls_requested > 0
    ):
        return FinishDecision(
            FinishOutcome.CONTINUE, f"the provider requested {tool_calls_requested} tool call(s)"
        )
    if undeclared_reason is not None:
        return FinishDecision(
            FinishOutcome.HALT,
            f"the provider declared finish_reason={undeclared_reason!r}, which is not a success "
            "and not a tool request",
            ErrorCode.LOADCOACH_ERROR,
        )
    return FinishDecision(
        FinishOutcome.HALT,
        "LoadCoach's response declared no finish_reason and performed no schema validation; a "
        "turn cannot complete on an undeclared finish (spec §11 contract 6). LoadCoach renders "
        "the provider's finish_reason at output.finish_reason since 846348b; an older "
        "LoadCoach renders none — see D2_HANDOFF.2.md",
        ErrorCode.LOADCOACH_ERROR,
    )


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """``turn.started`` — written before the LoadCoach call, in its own transaction.

    This is the event recovery reads. Its ``turn_id`` is also the ``idempotency_key`` the
    ``/generate`` request carries, so a worker that dies mid-call leaves behind exactly enough to
    find the job it started: a ``turn.started`` with no matching turn row names the key, and
    LoadCoach's ``GET /jobs?source=promptcadence`` lists the job holding it (lifecycle §8.3).
    """

    event_type: ClassVar[EventType] = EventType.TURN_STARTED
    trajectory_id: str
    turn_id: str
    sequence: int
    tier: str
    task_profile: str
    intent_id: str
    intent_revision: int

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "tier": self.tier,
            "task_profile": self.task_profile,
            "intent_id": self.intent_id,
            "intent_revision": self.intent_revision,
        }


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """``turn.completed`` — written in the same transaction as the turn row (ADR-0044).

    Numbers, ids and the declared reason; never the text. ``loadcoach_ms`` and ``overhead_ms``
    are reported separately, as spec §15 requires of every turn.
    """

    event_type: ClassVar[EventType] = EventType.TURN_COMPLETED
    trajectory_id: str
    turn_id: str
    sequence: int
    tier: str
    model_canonical_id: str
    loadcoach_job_id: str
    finish_reason: str | None
    schema_validated: bool
    input_tokens: int | None
    output_tokens: int | None
    loadcoach_ms: int | None
    overhead_ms: int
    decision: FinishOutcome

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "tier": self.tier,
            "model_canonical_id": self.model_canonical_id,
            "loadcoach_job_id": self.loadcoach_job_id,
            "finish_reason": self.finish_reason,
            "schema_validated": self.schema_validated,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "loadcoach_ms": self.loadcoach_ms,
            "overhead_ms": self.overhead_ms,
            "decision": self.decision.value,
        }
