"""promptcadence.domain.tools — the two tool-call events, and the vocabulary they are written in.

Deliberately thin, and deliberately free of :mod:`toolyard`. Every rule about *how* a tool call is
authorized, contained and refused lives in that package (ADR-0053) and is not restated here; what
this module owns is the pair of events spec §17 names — ``tool.call.started`` and
``tool.call.completed`` — plus the one enum a consumer switches on to tell an executed call from a
refused one.

**Why the domain does not import ToolYard.** ``toolyard`` depends on ``httpx``, and
``.importlinter``'s domain-purity contract forbids ``httpx`` anywhere under
:mod:`promptcadence.domain`. That is not an accident to work around: the domain's job here is to
say what is *recorded*, in strings and numbers a database column and an SSE frame can hold, and a
``ToolResult`` is none of those things. The services layer owns the translation, in exactly one
place (:mod:`promptcadence.services.tools`), so the mapping from ToolYard's closed ``Reason`` set
onto what an operator reads is reviewable rather than scattered.

**Why neither body carries the arguments or the output.** Events are replayed over SSE, written to
logs and rendered in a browser, and a tool's arguments are model-authored text that may hold
whatever the model was handed — so the bodies carry digests. The plaintext lives in the
``tool_call_records`` row, under ``redact_args`` and under the retention sweep; the digest is what
survives both, and it is what links an event to the artifact a large output spilled to.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from promptcadence.domain.events import EventType

__all__ = ["ToolCallCompleted", "ToolCallStarted", "ToolOutcome"]


class ToolOutcome(StrEnum):
    """How a tool call ended, as this application records it.

    The same four values ToolYard's ``ToolStatus`` carries, restated here because the domain does
    not import that package and because the *record* is what a later phase reads: a budget debit
    (P5) and an explanation (P8) both switch on this, and neither should have to depend on a
    third-party enum's identity to do it. A test asserts the two sets agree, so a divergence is a
    failure rather than a silent mistranslation.

    None of these is an exception. A tool the model invented, an argument that failed the schema, a
    path that escaped its root and a handler that raised all arrive here as values — that is
    ADR-0053 decision 4, and the reason a refused call continues the trajectory rather than ending
    it.
    """

    OK = "ok"
    """The handler ran and produced output."""

    REFUSED = "refused"
    """A check said no and nothing ran. ``reason`` names the first check that failed."""

    FAILED = "failed"
    """The handler ran and broke — its own contract, or the world's."""

    TIMEOUT = "timeout"
    """The handler outlived its limit; its output is discarded rather than shown."""

    @property
    def executed(self) -> bool:
        """Report whether any side effect could have happened.

        Returns:
            ``True`` for :attr:`OK`, :attr:`FAILED` and :attr:`TIMEOUT` — the handler was entered
            in all three. ``False`` only for :attr:`REFUSED`, which is the one outcome that
            guarantees nothing ran. A consumer asking "did this touch the world" must not read
            "not OK" as "nothing happened": a timed-out ``write_file`` may have written.
        """
        return self is not ToolOutcome.REFUSED


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """``tool.call.started`` — one call is about to be dispatched to the executor.

    Written *before* the call, in its own transaction, for the same reason ``turn.started`` is: a
    process that dies mid-call leaves evidence that the call was attempted. Without it, a
    ``write_file`` that ran and then lost its process would be indistinguishable from one that
    never started.

    Attributes:
        trajectory_id: The trajectory.
        turn_id: The assistant turn whose ``tool_calls`` this answers.
        invocation_id: This application's id for the call, which is also the ``TOOL`` turn's
            ``tool_call_id`` and the executor's ``ToolContext.invocation_id``. A model never
            supplies it.
        tool_name: What the model asked for, already cleaned and capped by the executor's rules
            when it reaches the record. Present even when no such tool exists.
        args_sha256: The digest of the sanitized arguments. Always present, including under
            ``redact_args``, so an event and its row can be matched without either holding
            plaintext.
    """

    event_type: ClassVar[EventType] = EventType.TOOL_CALL_STARTED
    trajectory_id: str
    turn_id: str
    invocation_id: str
    tool_name: str
    args_sha256: str

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "turn_id": self.turn_id,
            "invocation_id": self.invocation_id,
            "tool_name": self.tool_name,
            "args_sha256": self.args_sha256,
        }


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    """``tool.call.completed`` — one call ended, whatever way it ended.

    Emitted for every outcome, refusals included. An event stream holding only successful calls
    would answer "what did this trajectory do" and not "what did it try", and the second question
    is the one a security review asks.

    Attributes:
        trajectory_id: The trajectory.
        turn_id: The assistant turn the call belongs to.
        invocation_id: The call.
        tool_name: What was asked for.
        outcome: How it ended.
        reason: ToolYard's machine-readable reason, or ``None`` for :attr:`ToolOutcome.OK`. A
            member of that package's closed set, carried as a string because the domain does not
            import it.
        duration_ms: Wall time, from a monotonic source.
        result_sha256: The digest of the handler's **full** output — not of what the model was
            shown, which is capped. This is the key an artifact is filed under.
        artifact_ref: The artifact the full output was spilled to, when it was too large to keep
            inline, or ``None``. Naming the hash rather than inlining a truncated body is the
            difference between a record that can be re-read and one that only looks complete.
        output_truncated: Whether the model saw a labelled prefix rather than the whole output.
    """

    event_type: ClassVar[EventType] = EventType.TOOL_CALL_COMPLETED
    trajectory_id: str
    turn_id: str
    invocation_id: str
    tool_name: str
    outcome: ToolOutcome
    reason: str | None
    duration_ms: int
    result_sha256: str
    artifact_ref: str | None = None
    output_truncated: bool = False

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "turn_id": self.turn_id,
            "invocation_id": self.invocation_id,
            "tool_name": self.tool_name,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
            "result_sha256": self.result_sha256,
            "artifact_ref": self.artifact_ref,
            "output_truncated": self.output_truncated,
        }
