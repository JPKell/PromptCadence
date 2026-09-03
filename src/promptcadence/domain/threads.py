"""promptcadence.domain.threads — a conversation thread, built as if it were a package.

Spec §10 records the **ThreadRack rejection**: a thread/turn package would have exactly one
consumer, and the suite's extraction rule refuses an extraction with fewer than two
([ADR-0011](../../../docs/adr/0011-shared-package-boundaries.md)). What it does *not* refuse is
building the types as though the package existed, so that extraction later is a move rather than a
rewrite. That is this module's whole constraint, and it is one no test can catch by accident:

    **No PromptCadence vocabulary appears in any type here.** Not ``trajectory_id``, not ``tier``,
    not ``intent_id``, not ``step_id``. A thread has an ``owner_id`` because the host names what
    owns a thread; it does not know what a trajectory is.

The pressure against that rule is constant and arrives one convenient field at a time, so the
escape valve is explicit and typed rather than ad hoc: :class:`Turn` is **generic over its
provenance**. A host attaches whatever provenance it needs as a single parameterised field, and
the host's own type carries the host's vocabulary. PromptCadence uses
``Turn[promptcadence.domain.intent.TurnProvenance]``; a host with nothing to attach uses
``Turn[None]``.

That generic parameter is doing governance work as well as layering work. ``provenance`` has no
default, so **a turn cannot be constructed without one**, and PromptCadence's provenance type
cannot be constructed without an :class:`~promptcadence.domain.intent.ExecutionIntent`. Spec §11's
contract 1 — no turn executes without an intent — is therefore a property of the constructors
rather than a rule an implementer must remember (ADR-0056 §2).

The ``turns`` **table** is allowed to carry ``tier``, ``intent_id`` and ``trajectory_id`` columns:
a store is the host's, and mapping a generic type onto a host-shaped row is exactly what a store
is for. It is the domain type that must stay extractable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from baseaicore import TokenUsage, ValidationError

__all__ = [
    "FinishReason",
    "Thread",
    "ThreadSnapshot",
    "ThreadStore",
    "Turn",
    "TurnRole",
    "build_snapshot",
]


class TurnRole(StrEnum):
    """Who produced a turn. The four roles every chat-shaped protocol agrees on."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(StrEnum):
    """Why a model stopped, as *declared* by the provider — never inferred from the text.

    ``STOP`` is the only member that may be read as success (spec §11 contract 6). ``LENGTH`` and
    ``ERROR`` are handled explicitly, and the absence of a finish reason is ``None`` rather than a
    member: there is deliberately no ``UNKNOWN`` that could be mistaken for a completion.
    """

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Thread:
    """One conversation, owned by something the thread does not name.

    Attributes:
        thread_id: Stable identifier, assigned by the host.
        owner_id: The host's identifier for whatever the thread belongs to. Opaque here; it is a
            trajectory in PromptCadence and would be something else in a second consumer.
        created_at: When the thread was opened, timezone-aware.

    Raises:
        ValidationError: If any identifier is empty, or ``created_at`` is naive. A naive timestamp
            in a record that must stay reconstructable is a defect, not a nuisance.
    """

    thread_id: str
    owner_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        """Refuse an empty identifier or a naive timestamp."""
        _require_id(self.thread_id, "thread_id")
        _require_id(self.owner_id, "owner_id")
        _require_aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class Turn[ProvenanceT]:
    """One entry in a thread, with whatever provenance the host attaches to it.

    ``provenance`` is positional and has no default on purpose: the host's decision about what
    every turn must carry is enforced by the constructor rather than by review.

    Attributes:
        turn_id: Stable identifier, assigned by the host.
        thread_id: The thread this turn belongs to.
        sequence: Position within the thread, dense and 1-based. Unique per thread.
        role: Who produced the turn.
        provenance: The host's per-turn record. ``None`` for a host that attaches nothing.
        content: The turn's text, when the host retains it. ``None`` after a retention sweep has
            scrubbed it — which is not the same as an empty turn, and is why this is nullable.
        content_sha256: Digest of the original content, retained when the content is not, so a
            scrubbed transcript still proves what it contained.
        model_canonical_id: The execution subject that produced an assistant turn, verbatim from
            the provider-facing layer. ``None`` for turns no model produced.
        finish_reason: The provider's declared stop reason, or ``None`` when it declared none.
        usage: Token counts as reported. Absent classes are ``UNSUPPORTED``, never zero
            (ADR-0016), which is what lets a later ledger tell "not reported" from "none used".
        tool_call_id: For a ``TOOL`` turn, the call it answers, so a call and its result are never
            separated by a compaction that reads only this field.

    Raises:
        ValidationError: If an identifier is empty, ``sequence`` is below 1, a ``TOOL`` turn names
            no ``tool_call_id``, or a non-``TOOL`` turn names one.
    """

    turn_id: str
    thread_id: str
    sequence: int
    role: TurnRole
    provenance: ProvenanceT
    content: str | None = field(default=None, kw_only=True)
    content_sha256: str | None = field(default=None, kw_only=True)
    model_canonical_id: str | None = field(default=None, kw_only=True)
    finish_reason: FinishReason | None = field(default=None, kw_only=True)
    usage: TokenUsage | None = field(default=None, kw_only=True)
    tool_call_id: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Refuse an empty identifier, a non-positive sequence or a mismatched tool link."""
        _require_id(self.turn_id, "turn_id")
        _require_id(self.thread_id, "thread_id")
        if self.sequence < 1:
            message = f"turn sequence must be 1 or greater, got {self.sequence}"
            raise ValidationError(message, details={"field": "sequence", "value": self.sequence})
        if self.role is TurnRole.TOOL and self.tool_call_id is None:
            message = "a tool turn must name the tool_call_id it answers"
            raise ValidationError(message, details={"field": "tool_call_id", "role": self.role})
        if self.role is not TurnRole.TOOL and self.tool_call_id is not None:
            message = f"only a tool turn may name a tool_call_id, got role {self.role}"
            raise ValidationError(message, details={"field": "tool_call_id", "role": self.role})


@dataclass(frozen=True, slots=True)
class ThreadSnapshot[ProvenanceT]:
    """The turns of a thread as of one instant — the view a model is sent, never a deletion.

    A snapshot is what compaction operates on: the store keeps every original turn, and what
    changes is the snapshot (lifecycle §7). Building one therefore never removes a row.

    Attributes:
        thread_id: The thread this is a view of.
        turns: The turns, in ascending ``sequence`` order.
        taken_at: When the view was taken, timezone-aware.

    Raises:
        ValidationError: If a turn belongs to another thread, or the turns are not in strictly
            ascending sequence order. Out-of-order turns would send a model a transcript that
            never happened.
    """

    thread_id: str
    turns: tuple[Turn[ProvenanceT], ...]
    taken_at: datetime

    def __post_init__(self) -> None:
        """Refuse a foreign turn, a duplicate sequence or an out-of-order transcript."""
        _require_id(self.thread_id, "thread_id")
        _require_aware(self.taken_at, "taken_at")
        previous = 0
        for turn in self.turns:
            if turn.thread_id != self.thread_id:
                message = (
                    f"turn {turn.turn_id} belongs to thread {turn.thread_id}, not {self.thread_id}"
                )
                raise ValidationError(message, details={"field": "turns", "turn_id": turn.turn_id})
            if turn.sequence <= previous:
                message = (
                    f"turns must ascend by sequence; {turn.sequence} follows {previous} "
                    f"at turn {turn.turn_id}"
                )
                raise ValidationError(message, details={"field": "turns", "turn_id": turn.turn_id})
            previous = turn.sequence


class ThreadStore[ProvenanceT](Protocol):
    """Persistence for threads and their turns. The port; every implementation is a host's.

    Deliberately narrow. A store reads and appends; it does not update a turn and it does not
    delete one, because a transcript that can be rewritten cannot be the authoritative record the
    explanation is composed from (spec §11 contract 2). Compaction changes a
    :class:`ThreadSnapshot`, never the rows behind it.
    """

    def create_thread(self, thread: Thread) -> None:
        """Persist a new thread.

        Raises:
            ConflictError: If ``thread.thread_id`` already exists.
        """
        ...

    def get_thread(self, thread_id: str) -> Thread | None:
        """Return the thread, or ``None`` when no thread has that identifier."""
        ...

    def next_sequence(self, thread_id: str) -> int:
        """Return the sequence the next appended turn must carry: 1 for an empty thread."""
        ...

    def append_turn(self, turn: Turn[ProvenanceT]) -> None:
        """Append one turn.

        Raises:
            ConflictError: If the thread already holds that ``sequence``, or the turn id exists.
            NotFoundError: If the thread does not exist.
        """
        ...

    def turns(self, thread_id: str) -> Sequence[Turn[ProvenanceT]]:
        """Return every turn of the thread in ascending ``sequence`` order."""
        ...

    def snapshot(self, thread_id: str, *, taken_at: datetime) -> ThreadSnapshot[ProvenanceT]:
        """Return every turn of the thread as one ordered view."""
        ...


def build_snapshot[P](
    thread_id: str, turns: Iterable[Turn[P]], *, taken_at: datetime
) -> ThreadSnapshot[P]:
    """Order an iterable of turns by sequence and wrap them as a snapshot.

    Provided so every store produces snapshots the same way, rather than each sorting for itself
    and one of them forgetting to.

    Args:
        thread_id: The thread the turns belong to.
        turns: The turns, in any order.
        taken_at: When the view is being taken, timezone-aware.

    Returns:
        A :class:`ThreadSnapshot` whose turns ascend by sequence.

    Raises:
        ValidationError: If a turn belongs to another thread, or two turns share a sequence.
    """
    ordered = tuple(sorted(turns, key=lambda turn: turn.sequence))
    return ThreadSnapshot(thread_id=thread_id, turns=ordered, taken_at=taken_at)


def _require_id(value: str, field_name: str) -> None:
    """Refuse an empty or whitespace-only identifier."""
    if not value.strip():
        message = f"{field_name} must not be empty"
        raise ValidationError(message, details={"field": field_name})


def _require_aware(value: datetime, field_name: str) -> None:
    """Refuse a naive timestamp; every recorded instant carries its offset."""
    if value.tzinfo is None or value.utcoffset() is None:
        message = f"{field_name} must be timezone-aware"
        raise ValidationError(message, details={"field": field_name})
