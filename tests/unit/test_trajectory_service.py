"""Tests for promptcadence.services.trajectories: T1's refusals and record, T14's two halves."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from baseaicore import DataClassification, Money, ValidationError
from weightsdb import MigrationRunner
from weightsdb.testing import temporary_sqlite

from promptcadence.config import load_settings
from promptcadence.domain.errors import (
    ProjectUnknownError,
    TierNotConfiguredError,
    ToolNotFoundError,
    TrajectoryNotCancellableError,
    TrajectoryNotFoundError,
)
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models
from promptcadence.services.database import MIGRATIONS_LOCATION, Database
from promptcadence.services.events import TrajectoryEventSink
from promptcadence.services.trajectories import TrajectoryService, TrajectorySubmission

_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def database() -> Iterator[Database]:
    with temporary_sqlite() as engine:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        yield Database(engine)


@pytest.fixture
def service(database: Database, tmp_path: object) -> TrajectoryService:
    settings = load_settings().settings
    ticks = iter(range(10_000))
    clock = lambda: _NOW + timedelta(seconds=next(ticks))  # noqa: E731 — a test clock
    return TrajectoryService(
        database, TrajectoryEventSink(database, clock=clock), settings, clock=clock
    )


def test_submit_records_the_whole_envelope_and_the_created_event(
    service: TrajectoryService, database: Database
) -> None:
    view = service.submit(TrajectorySubmission(task="summarize ./notes", bypass_planning=True))
    assert view.state is TrajectoryState.QUEUED
    assert view.classification is DataClassification.CONFIDENTIAL  # the safe default
    assert view.bypass_planning is True
    assert view.tools == ("read_file", "list_dir", "write_file", "run_command", "http_fetch")
    assert view.token_budget == 2_000_000
    assert view.money_budget == Money(currency="USD", nanos=5_000_000_000)
    assert view.tier_snapshot_id is not None and view.tier_snapshot_id.startswith("sha256:")
    assert view.approval_policy_version is not None
    (event,) = service.events(view.trajectory_id)
    assert event.event_type == "trajectory.created"
    assert event.data["bypass_planning"] is True
    with database.read() as session:
        assert session.get(models.TierSnapshot, view.tier_snapshot_id) is not None


def test_two_submissions_share_one_content_addressed_snapshot(
    service: TrajectoryService, database: Database
) -> None:
    a = service.submit(TrajectorySubmission(task="a"))
    b = service.submit(TrajectorySubmission(task="b"))
    assert a.tier_snapshot_id == b.tier_snapshot_id
    with database.read() as session:
        assert session.query(models.TierSnapshot).count() == 1


def test_the_configured_default_decides_the_bypass_when_the_request_is_silent(
    service: TrajectoryService,
) -> None:
    assert service.submit(TrajectorySubmission(task="a")).bypass_planning is False


def test_submit_refuses_what_it_must(service: TrajectoryService) -> None:
    with pytest.raises(ValidationError, match="task"):
        service.submit(TrajectorySubmission(task="   "))
    with pytest.raises(ProjectUnknownError):
        service.submit(TrajectorySubmission(task="a", project="research"))
    with pytest.raises(ToolNotFoundError) as tool:
        service.submit(TrajectorySubmission(task="a", tools=("read_file", "teleport")))
    assert tool.value.details["tools"] == ["teleport"]
    with pytest.raises(TierNotConfiguredError):
        service.submit(TrajectorySubmission(task="a", tier="gpt_9"))
    with pytest.raises(ValidationError, match="max_turns"):
        service.submit(TrajectorySubmission(task="a", max_turns=999))
    with pytest.raises(ValidationError, match="budget.tokens"):
        service.submit(TrajectorySubmission(task="a", token_budget=0))


def test_a_refused_override_is_a_refusal_not_a_silent_ignore(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROMPTCADENCE_PLANNING__ALLOW_REQUEST_OVERRIDE", "false")
    settings = load_settings().settings
    service = TrajectoryService(database, TrajectoryEventSink(database), settings)
    with pytest.raises(ValidationError, match="allow_request_override"):
        service.submit(TrajectorySubmission(task="a", bypass_planning=True))


def test_get_resolve_and_list(service: TrajectoryService) -> None:
    views = [service.submit(TrajectorySubmission(task=f"t{i}")) for i in range(3)]
    assert service.get(views[0].trajectory_id).task == "t0"
    with pytest.raises(TrajectoryNotFoundError):
        service.get("01ABSENT000000000000000000")
    assert service.resolve(views[1].trajectory_id[:10]).trajectory_id == views[1].trajectory_id
    with pytest.raises(ValidationError, match="ambiguous"):
        service.resolve("01")
    page, cursor = service.list(limit=2)
    assert [v.task for v in page] == ["t2", "t1"]
    assert cursor is not None
    rest, end = service.list(limit=2, cursor=cursor)
    assert [v.task for v in rest] == ["t0"]
    assert end is None
    queued, _ = service.list(state=TrajectoryState.QUEUED)
    assert len(queued) == 3
    assert service.turns(views[0].trajectory_id) == []


def test_cancel_a_queued_trajectory_at_once_in_one_write(service: TrajectoryService) -> None:
    view = service.submit(TrajectorySubmission(task="a"))
    cancelled = service.cancel(view.trajectory_id)
    assert cancelled.state is TrajectoryState.CANCELLED
    assert cancelled.completed_at is not None
    assert [e.event_type for e in service.events(view.trajectory_id)] == [
        "trajectory.created",
        "trajectory.cancelled",
    ]
    with pytest.raises(TrajectoryNotCancellableError):
        service.cancel(view.trajectory_id)


def test_cancel_a_leased_trajectory_only_asks(
    service: TrajectoryService, database: Database
) -> None:
    view = service.submit(TrajectorySubmission(task="a"))
    with database.write() as session:
        row = session.get(models.Trajectory, view.trajectory_id)
        assert row is not None
        row.status = TrajectoryState.EXECUTING.value
        row.lease_owner = "host:1/0"
    asked = service.cancel(view.trajectory_id)
    assert asked.state is TrajectoryState.EXECUTING
    assert asked.cancel_requested is True
    assert [e.event_type for e in service.events(view.trajectory_id)] == ["trajectory.created"]
    assert service.cancel(view.trajectory_id).cancel_requested is True  # idempotent
    with pytest.raises(TrajectoryNotFoundError):
        service.cancel("01ABSENT000000000000000000")


def test_the_view_document_carries_the_cause_verbatim(service: TrajectoryService) -> None:
    view = service.submit(TrajectorySubmission(task="a", classification=DataClassification.PUBLIC))
    document = view.as_json()
    assert document["state"] == "queued"
    assert document["data_classification"] == "public"
    assert document["cause"] is None
    assert document["budget"]["money"] == {"currency": "USD", "nanos": 5_000_000_000}
