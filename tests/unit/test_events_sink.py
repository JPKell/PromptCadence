"""Tests for promptcadence.services.events: ADR-0044 asserted, not merely honoured.

The crash-between test holds the window open deliberately: the state change is written, the
event append fails, and the invariant — neither is observable — is asserted directly rather than
waited for on a slow runner (ADR-0044, "the rule is testable without a race").
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from sqlalchemy import select
from weightsdb import MigrationRunner
from weightsdb.testing import temporary_sqlite

from promptcadence.domain.events import EventType
from promptcadence.domain.trajectory import (
    TrajectoryCompleted,
    TrajectoryCreated,
    TrajectoryState,
)
from promptcadence.infrastructure.db import models
from promptcadence.services.database import MIGRATIONS_LOCATION, Database
from promptcadence.services.events import TERMINAL_EVENTS, TrajectoryEventSink

_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
_ID = "01TRAJECTORY0000000000000A"


@pytest.fixture
def database() -> Iterator[Database]:
    with temporary_sqlite() as engine:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        yield Database(engine)


@pytest.fixture
def sink(database: Database) -> TrajectoryEventSink:
    return TrajectoryEventSink(database, clock=lambda: _NOW)


def _created() -> TrajectoryCreated:
    return TrajectoryCreated(
        trajectory_id=_ID,
        classification=__import__("baseaicore").DataClassification.INTERNAL,
        tool_allowlist=("read_file",),
        token_budget=1000,
        bypass_planning=True,
    )


def _queue(sink: TrajectoryEventSink) -> None:
    with sink.write() as (session, events):
        session.add(
            models.Trajectory(
                id=_ID,
                task="t",
                data_classification="internal",
                status="queued",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        events.append(_ID, _created(), now=_NOW)


def _status(database: Database) -> str:
    with database.read() as session:
        row = session.get(models.Trajectory, _ID)
        assert row is not None
        return str(row.status)


def test_a_state_change_and_its_event_commit_together(
    sink: TrajectoryEventSink, database: Database
) -> None:
    _queue(sink)
    assert _status(database) == "queued"
    (event,) = sink.events(_ID)
    assert (event.sequence, event.event_type) == (1, "trajectory.created")
    assert event.data["trajectory_id"] == _ID


def test_sequences_are_dense_per_trajectory_starting_at_one(sink: TrajectoryEventSink) -> None:
    _queue(sink)
    with sink.write() as (_, events):
        events.append(_ID, TrajectoryCompleted(trajectory_id=_ID, step_count=1, turn_count=2))
        events.append(_ID, TrajectoryCompleted(trajectory_id=_ID, step_count=1, turn_count=3))
    assert [e.sequence for e in sink.events(_ID)] == [1, 2, 3]
    assert [e.sequence for e in sink.events(_ID, after_sequence=2)] == [3]


class _Exploding:
    """An event body whose canonical form fails — the crash between the two writes."""

    event_type: ClassVar[EventType] = EventType.TRAJECTORY_COMPLETED
    trajectory_id: str = _ID

    def as_canonical(self) -> dict[str, Any]:
        message = "crashed between the state change and its event"
        raise RuntimeError(message)


def test_a_crash_between_the_state_change_and_its_event_leaves_neither(
    sink: TrajectoryEventSink, database: Database
) -> None:
    """ADR-0044's crash-between test: the row was changed, the event failed, nothing is visible."""
    _queue(sink)
    seen: list[Any] = []
    with sink.broker.subscribe(stream_id=_ID) as subscription, pytest.raises(RuntimeError):
        with sink.write() as (session, events):
            row = session.get(models.Trajectory, _ID)
            assert row is not None
            row.status = TrajectoryState.COMPLETED.value
            session.flush()  # the state change has reached the database inside the transaction
            events.append(_ID, _Exploding(), now=_NOW)
        seen.append(subscription.poll())
    assert _status(database) == "queued"
    with database.read() as session:
        rows = (
            session.execute(select(models.Event).where(models.Event.trajectory_id == _ID))
            .scalars()
            .all()
        )
    assert [row.event_type for row in rows] == ["trajectory.created"]
    assert subscription.poll() is None, "nothing was published for the rolled-back write"


def test_publication_happens_only_after_commit(sink: TrajectoryEventSink) -> None:
    _queue(sink)
    with sink.broker.subscribe(stream_id=_ID) as subscription:
        with sink.write() as (session, events):
            row = session.get(models.Trajectory, _ID)
            assert row is not None
            row.status = TrajectoryState.COMPLETED.value
            events.append(_ID, TrajectoryCompleted(trajectory_id=_ID, step_count=1, turn_count=1))
            assert subscription.poll() is None, "not visible inside the transaction"
        published = subscription.poll()
    assert published is not None
    assert published.type == "trajectory.completed"
    assert published.sequence == 2
    assert published.payload["entity"] == {"kind": "trajectory", "id": _ID}


def test_replay_reads_bounded_batches_in_order(sink: TrajectoryEventSink) -> None:
    _queue(sink)
    with sink.write() as (_, events):
        for turn_count in range(2, 6):
            events.append(
                _ID, TrajectoryCompleted(trajectory_id=_ID, step_count=1, turn_count=turn_count)
            )
    source = sink.source(_ID)
    batch = source.replay(stream_id=_ID, after_sequence=2, limit=2)
    assert [e.sequence for e in batch] == [3, 4]
    assert TERMINAL_EVENTS >= {
        "trajectory.completed",
        "trajectory.halted",
        "trajectory.failed",
        "trajectory.cancelled",
        "plan.rejected",
    }
