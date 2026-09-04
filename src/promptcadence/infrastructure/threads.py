"""promptcadence.infrastructure.threads — the SQLAlchemy ThreadStore, mapping generic onto host.

The development plan's Phase 2 list puts ``SqlThreadStore`` in ``domain/threads.py``. It cannot
live there: ``.importlinter``'s ``domain-purity`` contract forbids ``sqlalchemy`` inside
``promptcadence.domain``, and the workspace ``CLAUDE.md`` states the same rule for every
application in the suite. The resolution is one-directional — the ``Protocol`` and the value
objects stay in ``domain``, the implementation moves here — and the contract is never weakened to
keep a file layout. The amendment this proposes to the development plan is recorded in
``C4_HANDOFF.md``.

This is also where the two shapes of a turn meet. The domain's :class:`Turn` is package-shaped and
carries a *generic* provenance; the ``turns`` **table** carries PromptCadence's own columns —
``trajectory_id``, ``tier``, ``intent_id``, ``intent_revision``. Mapping one onto the other is
precisely what a store is for, and doing it here is what keeps the extraction of a thread package
a move rather than a rewrite (spec §10, the ThreadRack rejection).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baseaicore import ConflictError, NotFoundError, TokenUsage, is_supported
from sqlalchemy import func, select

from promptcadence.domain.intent import TurnProvenance
from promptcadence.domain.threads import (
    FinishReason,
    Thread,
    ThreadSnapshot,
    Turn,
    TurnRole,
    build_snapshot,
)
from promptcadence.infrastructure.db import models

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.orm import Session, sessionmaker

__all__ = ["SqlThreadStore", "thread_row", "turn_row"]


class SqlThreadStore:
    """A :class:`~promptcadence.domain.threads.ThreadStore` over PromptCadence's own tables.

    Satisfies ``ThreadStore[TurnProvenance]`` structurally; no inheritance, because the port is a
    ``Protocol`` and a second implementation (an in-memory one for Phase 3's tests) must be able to
    satisfy it without importing anything from here.

    Reads and appends only. There is no update and no delete: a transcript that can be rewritten
    cannot be the authoritative record the explanation is composed from (spec §11 contract 2), and
    compaction changes a snapshot rather than the rows behind it (lifecycle §7).
    """

    __slots__ = ("_sessions",)

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """Bind the store to a session factory.

        Args:
            sessions: The factory from :attr:`promptcadence.services.database.Database.sessions`.
                Injected rather than constructed so tests bind a temporary database without
                patching anything.
        """
        self._sessions = sessions

    def create_thread(self, thread: Thread) -> None:
        """Persist a new thread.

        Args:
            thread: The thread. ``owner_id`` is written to ``threads.trajectory_id`` — the generic
                name meets the host's here, which is the whole point of the split.

        Raises:
            ConflictError: If a thread with that id already exists.
        """
        with self._sessions.begin() as session:
            if session.get(models.Thread, thread.thread_id) is not None:
                message = f"thread {thread.thread_id} already exists"
                raise ConflictError(message, details={"thread_id": thread.thread_id})
            session.add(
                models.Thread(
                    id=thread.thread_id,
                    trajectory_id=thread.owner_id,
                    created_at=thread.created_at,
                )
            )

    def get_thread(self, thread_id: str) -> Thread | None:
        """Return the thread, or ``None`` when no thread has that identifier."""
        with self._sessions() as session:
            row = session.get(models.Thread, thread_id)
            if row is None:
                return None
            return Thread(thread_id=row.id, owner_id=row.trajectory_id, created_at=row.created_at)

    def next_sequence(self, thread_id: str) -> int:
        """Return the sequence the next appended turn must carry: 1 for an empty thread."""
        with self._sessions() as session:
            highest = session.execute(
                select(func.max(models.Turn.sequence)).where(models.Turn.thread_id == thread_id)
            ).scalar_one_or_none()
            return 1 if highest is None else int(highest) + 1

    def append_turn(self, turn: Turn[TurnProvenance]) -> None:
        """Append one turn, with the envelope it ran under.

        The turn's provenance cannot have been built without an
        :class:`~promptcadence.domain.intent.ExecutionIntent`
        (:class:`~promptcadence.domain.intent.TurnProvenance` takes one as an ``InitVar``), so
        there is no way to reach this method with a turn that ran ungoverned. That is contract 1
        holding at the storage boundary as well as at the construction one.

        Args:
            turn: The turn to append.

        Raises:
            NotFoundError: If the thread does not exist.
            ConflictError: If the thread already holds that sequence, or the turn id exists.
        """
        with self._sessions.begin() as session:
            if session.get(models.Thread, turn.thread_id) is None:
                message = f"thread {turn.thread_id} does not exist"
                raise NotFoundError(message, details={"thread_id": turn.thread_id})
            if session.get(models.Turn, turn.turn_id) is not None:
                message = f"turn {turn.turn_id} already exists"
                raise ConflictError(message, details={"turn_id": turn.turn_id})
            taken = session.execute(
                select(models.Turn.id).where(
                    models.Turn.thread_id == turn.thread_id,
                    models.Turn.sequence == turn.sequence,
                )
            ).scalar_one_or_none()
            if taken is not None:
                message = (
                    f"thread {turn.thread_id} already holds sequence {turn.sequence} (turn {taken})"
                )
                raise ConflictError(
                    message,
                    details={"thread_id": turn.thread_id, "sequence": turn.sequence},
                )
            session.add(_to_row(turn))

    def turns(self, thread_id: str) -> Sequence[Turn[TurnProvenance]]:
        """Return every turn of the thread in ascending ``sequence`` order."""
        with self._sessions() as session:
            rows = session.execute(
                select(models.Turn)
                .where(models.Turn.thread_id == thread_id)
                .order_by(models.Turn.sequence)
            ).scalars()
            return [_from_row(row) for row in rows]

    def snapshot(self, thread_id: str, *, taken_at: datetime) -> ThreadSnapshot[TurnProvenance]:
        """Return every turn of the thread as one ordered view.

        Args:
            thread_id: The thread.
            taken_at: When the view is being taken, timezone-aware. Injected rather than read from
                the clock here: a snapshot appears in the record, and a store that stamped its own
                time would make the record depend on when it was read.

        Returns:
            The snapshot.
        """
        return build_snapshot(thread_id, self.turns(thread_id), taken_at=taken_at)


def thread_row(thread: Thread) -> models.Thread:
    """Map a domain thread onto its row, for a caller composing it into a larger transaction.

    The loop opens a thread in the same write as the claim that starts it (T3) and the intent it
    mints — one transaction, one event set (ADR-0044) — which :meth:`SqlThreadStore.create_thread`
    cannot join because it owns its own session. This is the mapping without the session.
    """
    return models.Thread(
        id=thread.thread_id, trajectory_id=thread.owner_id, created_at=thread.created_at
    )


def turn_row(
    turn: Turn[TurnProvenance],
    *,
    loadcoach_job_id: str | None = None,
    loadcoach_ms: float | None = None,
    overhead_ms: float | None = None,
) -> models.Turn:
    """Map a domain turn onto the ``turns`` row, with the host-only columns the row carries.

    Args:
        turn: The package-shaped turn.
        loadcoach_job_id: The LoadCoach job that produced an assistant turn — the reference
            recovery reconciles against and the explanation links to.
        loadcoach_ms: LoadCoach's own ``total_ms`` for the turn.
        overhead_ms: PromptCadence's time around the call, reported separately (spec §15).

    Returns:
        The row, ready to be added to a session the caller owns.
    """
    row = _to_row(turn)
    row.loadcoach_job_id = loadcoach_job_id
    row.loadcoach_ms = loadcoach_ms
    row.overhead_ms = overhead_ms
    return row


def _to_row(turn: Turn[TurnProvenance]) -> models.Turn:
    """Map a domain turn onto the ``turns`` row, where PromptCadence's own columns live."""
    usage = turn.usage
    return models.Turn(
        id=turn.turn_id,
        thread_id=turn.thread_id,
        trajectory_id=turn.provenance.trajectory_id,
        sequence=turn.sequence,
        role=turn.role.value,
        tier=turn.provenance.tier,
        intent_id=turn.provenance.intent_id,
        intent_revision=turn.provenance.intent_revision,
        model_canonical_id=turn.model_canonical_id,
        content_text=turn.content,
        content_hash=turn.content_sha256,
        finish_reason=turn.finish_reason.value if turn.finish_reason is not None else None,
        tool_call_id=turn.tool_call_id,
        input_tokens=_counted(usage, "input_tokens"),
        output_tokens=_counted(usage, "output_tokens"),
        cache_write_tokens=_counted(usage, "cache_write_tokens"),
        cache_read_tokens=_counted(usage, "cache_read_tokens"),
    )


def _counted(usage: TokenUsage | None, field_name: str) -> int | None:
    """Return a reported token count, or ``None`` when the provider did not report it.

    ``UNSUPPORTED`` becomes ``NULL``, never ``0``: an unreported class is not a class that used
    nothing (ADR-0016), and storing zero would let LoadLedger total a floor as though it were
    complete (ADR-0069).
    """
    if usage is None:
        return None
    value = getattr(usage, field_name)
    return int(value) if is_supported(value) else None


def _from_row(row: models.Turn) -> Turn[TurnProvenance]:
    """Map a ``turns`` row back onto the domain turn, reconstructing its provenance.

    Uses :meth:`~promptcadence.domain.intent.TurnProvenance.rehydrate`, the one path that builds
    provenance without an :class:`~promptcadence.domain.intent.ExecutionIntent` in hand — because
    the row itself is the evidence that an envelope existed when the turn ran, and re-reading the
    intent would report whichever revision is current rather than the one that governed it. That
    method is asserted to be called from this package and nowhere else.
    """
    if row.intent_id is None or row.intent_revision is None:
        message = (
            f"turn {row.id} has no (intent_id, revision); every executed turn records the "
            "envelope it ran under (ADR-0056 §3)"
        )
        raise NotFoundError(message, details={"turn_id": row.id})
    provenance = TurnProvenance.rehydrate(
        trajectory_id=row.trajectory_id,
        tier=row.tier or "",
        intent_id=row.intent_id,
        intent_revision=row.intent_revision,
    )
    return Turn(
        row.id,
        row.thread_id,
        row.sequence,
        TurnRole(row.role),
        provenance,
        content=row.content_text,
        content_sha256=row.content_hash,
        model_canonical_id=row.model_canonical_id,
        finish_reason=FinishReason(row.finish_reason) if row.finish_reason else None,
        usage=_usage_of(row),
        tool_call_id=row.tool_call_id,
    )


def _usage_of(row: models.Turn) -> TokenUsage | None:
    """Rebuild the reported token usage, leaving unreported classes ``UNSUPPORTED``."""
    counts = {
        name: getattr(row, name)
        for name in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")
    }
    if all(value is None for value in counts.values()):
        return None
    return TokenUsage(**{name: value for name, value in counts.items() if value is not None})
