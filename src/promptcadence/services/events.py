"""promptcadence.services.events — every event goes through here, and never alone.

ADR-0044 as code: **a state change and the event announcing it are one write.** The rule has
three parts and this module is the only place that satisfies them, so a caller cannot follow it
by accident and cannot break it without bypassing this module:

1. :meth:`TrajectoryEventSink.write` yields one session and one writer. The caller changes the
   trajectory row on that session and appends the event through that writer; both commit in the
   same transaction or neither does. A caller that opens its own transaction and then emits is
   wrong even when it looks equivalent.
2. **Publish after commit, never inside the transaction.** Staged events reach the in-memory
   broker only once the transaction has committed, so a subscriber can never see an event whose
   row was rolled back. The store is the source of truth; the broker is a latency optimisation.
3. A poller reads the state first and the log second (:meth:`events` is what the CLI's ``wait``
   drains after it has seen a terminal state, never before).

The persisted half is the SSE contract too (API standards §8): a **gap-free, per-trajectory
sequence** starting at 1, allocated inside the writing transaction from the stored maximum, so a
reconnecting client's ``Last-Event-ID`` replays everything after it with no gap and no duplicate.
Sequences are allocated under the trajectory row's own concurrency discipline — every writer
that changes the row does so by compare-and-set on its state — so two writers for one trajectory
cannot both commit, and the unique ``(trajectory_id, sequence)`` constraint is the backstop.

LoadCoach's ``JobEventSink`` is the reference implementation ADR-0044 names; this is the same
shape over PromptCadence's ``events`` table, minus the live token frames PromptCadence does not
stream.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from baseaicore import new_id
from baseaicore.timeutil import to_rfc3339
from mirrorwall import Event, EventBroker
from sqlalchemy import func, select

from promptcadence.domain.events import EventBody, EventType
from promptcadence.infrastructure.db.models import Event as EventRow

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from contextlib import AbstractContextManager

    from mirrorwall import Subscription
    from sqlalchemy.orm import Session

    from promptcadence.services.database import Database

__all__ = [
    "TERMINAL_EVENTS",
    "EventWriter",
    "StoredEvent",
    "TrajectoryEventSink",
    "TrajectoryEventSource",
]

TERMINAL_EVENTS: Final[frozenset[str]] = frozenset(
    {
        EventType.TRAJECTORY_COMPLETED.value,
        EventType.TRAJECTORY_HALTED.value,
        EventType.TRAJECTORY_FAILED.value,
        EventType.TRAJECTORY_CANCELLED.value,
        EventType.PLAN_REJECTED.value,
    }
)
"""The events after which a trajectory's stream closes: one per terminal state (lifecycle §8.1).

``plan.rejected`` is here because ``rejected`` is terminal, even though nothing in Phase 3 can
write it — a stream that stayed open after it would wait for a producer with nothing left to say.
"""


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """One persisted event, as the CLI and the API read it back."""

    event_id: str
    trajectory_id: str
    sequence: int
    event_type: str
    timestamp: datetime
    data: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        """Return the mapping the API and ``--json`` output carry."""
        return {
            "event_id": self.event_id,
            "trajectory_id": self.trajectory_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "timestamp": to_rfc3339(self.timestamp),
            "data": dict(self.data),
        }

    def as_stream_event(self) -> Event:
        """Return the MirrorWall event: the API standards §8 payload under the envelope."""
        return Event(
            sequence=self.sequence,
            type=self.event_type,
            payload={
                "event_id": self.event_id,
                "entity": {"kind": "trajectory", "id": self.trajectory_id},
                "timestamp": to_rfc3339(self.timestamp),
                "message": None,
                "data": dict(self.data),
            },
        )


@dataclass
class EventWriter:
    """Stages events inside one write transaction; the sink publishes them after commit."""

    session: Session
    sink: TrajectoryEventSink
    staged: list[StoredEvent] = field(default_factory=list)

    def append(
        self, trajectory_id: str, body: EventBody, *, now: datetime | None = None
    ) -> StoredEvent:
        """Persist one event with the trajectory's next sequence and stage it for publication.

        Args:
            trajectory_id: The trajectory the event belongs to.
            body: The event body. Its ``as_canonical`` is what the row stores and the frame
                carries — ids, categories and numbers, never content (``domain.events``).
            now: The event's timestamp; the sink's clock when omitted.

        Returns:
            The event as it will be persisted and published.
        """
        stamp = now if now is not None else self.sink.clock()
        # Flush the state change first: the event row references the trajectory row, and a
        # previously staged event in this same transaction must be visible to the sequence read.
        self.session.flush()
        sequence = self.sink.next_sequence(self.session, trajectory_id)
        data = body.as_canonical()
        row = EventRow(
            id=new_id(),
            trajectory_id=trajectory_id,
            sequence=sequence,
            event_type=body.event_type.value,
            timestamp=stamp,
            data_json=data,
        )
        self.session.add(row)
        stored = StoredEvent(
            event_id=row.id,
            trajectory_id=trajectory_id,
            sequence=sequence,
            event_type=body.event_type.value,
            timestamp=stamp,
            data=data,
        )
        self.staged.append(stored)
        return stored


class TrajectoryEventSink:
    """Where every trajectory event goes: the table first, then the broker. One per process."""

    __slots__ = ("_database", "broker", "clock")

    def __init__(
        self,
        database: Database,
        *,
        broker: EventBroker | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind the sink to a database handle.

        Args:
            database: The application's handle; every write is one of its transactions.
            broker: The in-memory fan-out; a fresh one when ``None``.
            clock: The event timestamp source, injected for determinism in tests.
        """
        self._database = database
        self.broker = broker if broker is not None else EventBroker()
        self.clock = clock if clock is not None else _utc_now

    @contextmanager
    def write(self) -> Iterator[tuple[Session, EventWriter]]:
        """One write transaction whose staged events are published once it has committed.

        Yields:
            The session, on which the caller changes state, and the writer, through which it
            appends the events describing that change. Both commit together; on any exception
            both roll back and nothing is published.
        """
        with self._database.write() as session:
            writer = EventWriter(session=session, sink=self)
            yield session, writer
        for stored in writer.staged:
            self.broker.publish(stored.trajectory_id, stored.as_stream_event())

    def next_sequence(self, session: Session, trajectory_id: str) -> int:
        """Allocate the trajectory's next sequence: one above the stored maximum, 1 when empty.

        Read inside the writing transaction, so under SQLite's ``BEGIN IMMEDIATE`` and under a
        compare-and-set on the trajectory row no two committed events share a number.
        """
        stored = session.execute(
            select(func.coalesce(func.max(EventRow.sequence), 0)).where(
                EventRow.trajectory_id == trajectory_id
            )
        ).scalar_one()
        return int(stored) + 1

    def events(self, trajectory_id: str, *, after_sequence: int = 0) -> Sequence[StoredEvent]:
        """Read every persisted event after ``after_sequence``, ascending.

        The poller's read (ADR-0044 rule 3): call it *after* checking the trajectory's state,
        and once more after the state is terminal, and no terminal event can be missed.
        """
        with self._database.read() as session:
            rows = (
                session.execute(
                    select(EventRow)
                    .where(
                        EventRow.trajectory_id == trajectory_id,
                        EventRow.sequence > after_sequence,
                    )
                    .order_by(EventRow.sequence)
                )
                .scalars()
                .all()
            )
            return [_stored_of(row) for row in rows]

    def replay(self, trajectory_id: str, *, after_sequence: int, limit: int) -> Sequence[Event]:
        """Read up to ``limit`` persisted events after ``after_sequence`` as stream events."""
        with self._database.read() as session:
            rows = (
                session.execute(
                    select(EventRow)
                    .where(
                        EventRow.trajectory_id == trajectory_id,
                        EventRow.sequence > after_sequence,
                    )
                    .order_by(EventRow.sequence)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [_stored_of(row).as_stream_event() for row in rows]

    def source(self, trajectory_id: str) -> TrajectoryEventSource:
        """Build the MirrorWall event source for one trajectory's SSE stream."""
        return TrajectoryEventSource(self, trajectory_id)


class TrajectoryEventSource:
    """A MirrorWall ``EventSource`` over one trajectory: replay from rows, live from the broker."""

    __slots__ = ("_sink", "trajectory_id")

    def __init__(self, sink: TrajectoryEventSink, trajectory_id: str) -> None:
        """Bind to one trajectory."""
        self._sink = sink
        self.trajectory_id = trajectory_id

    def replay(self, *, stream_id: str, after_sequence: int, limit: int) -> Sequence[Event]:
        """Persisted events after ``after_sequence``, in bounded batches."""
        return self._sink.replay(stream_id, after_sequence=after_sequence, limit=limit)

    def subscribe(self, *, stream_id: str) -> AbstractContextManager[Subscription]:
        """Open a live subscription on the broker."""
        return self._sink.broker.subscribe(stream_id=stream_id)


def _stored_of(row: EventRow) -> StoredEvent:
    return StoredEvent(
        event_id=row.id,
        trajectory_id=row.trajectory_id,
        sequence=row.sequence,
        event_type=row.event_type,
        timestamp=row.timestamp,
        data=dict(row.data_json) if isinstance(row.data_json, dict) else {},
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
