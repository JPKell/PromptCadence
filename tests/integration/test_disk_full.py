"""Degradation: disk full mid-turn (graceful-degradation.md, row "Disk full").

Documented behaviour: "Turn aborted at the next boundary; committed turns and their events
survive, because a state change and the event announcing it are one write (ADR-0044)." Every
state change ``LoopController`` makes goes through ``TrajectoryEventSink.write()``
(``services/events.py``), one ``database.write()`` transaction per boundary. Rather than filling a
real disk or patching ``os.write`` globally, this attaches a SQLAlchemy cursor-execute hook to the
harness's own engine — the seam the application already owns — that raises
``sqlite3.OperationalError("database or disk is full")`` the moment a turn row is about to be
inserted. ``run()`` has no generic exception handler around a turn boundary, so the failure
propagates to the caller (the worker thread, in production) exactly as an uncaught write failure
would; what this proves is that nothing partial lands first.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from tests.conftest import budget_and_estimator, egress_for
from tests.fakes.loadcoach_app import FakeLoadCoach, build_fake_app, shipped_profiles
from toolyard import TieredSandbox
from weightsdb import MigrationRunner, create_engine_for

from promptcadence.config import Settings, load_settings
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.loadcoach import LoadCoachClient
from promptcadence.services.database import MIGRATIONS_LOCATION, Database
from promptcadence.services.events import TrajectoryEventSink
from promptcadence.services.loop import LoopController
from promptcadence.services.tools import ToolPlant
from promptcadence.services.trajectories import TrajectoryService, TrajectorySubmission

_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class _Harness:
    """The minimal set of collaborators one turn needs, over a caller-supplied database."""

    def __init__(self, settings: Settings, database: Database, fake: FakeLoadCoach) -> None:
        self.database = database
        self.fake = fake
        ticks = iter(range(100_000))
        self.clock = lambda: _NOW + timedelta(milliseconds=next(ticks))
        self.sink = TrajectoryEventSink(database, clock=self.clock)
        self.budget, self.estimator = budget_and_estimator(database, settings, clock=self.clock)
        self.egress = egress_for(database, clock=self.clock)
        self.service = TrajectoryService(
            database, self.sink, settings, budget=self.budget, clock=self.clock
        )
        self.loadcoach = LoadCoachClient(
            TestClient(build_fake_app(fake), base_url="http://loadcoach.test")
        )
        self.tools = ToolPlant(settings, sandbox=TieredSandbox(which=lambda _name: None))
        self.settings = settings

    def controller(self, owner: str = "host:1/0") -> LoopController:
        return LoopController(
            budget=self.budget,
            estimator=self.estimator,
            egress=self.egress,
            database=self.database,
            sink=self.sink,
            loadcoach=self.loadcoach,
            settings=self.settings,
            owner=owner,
            clock=self.clock,
            tools=self.tools,
        )

    def submit(self) -> str:
        submission = TrajectorySubmission(task="summarize ./notes", bypass_planning=True)
        return self.service.submit(submission).trajectory_id


def _fail_once_on_turn_insert(fired: dict[str, bool]) -> Any:
    def hook(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if fired["value"] or "INTO turns" not in statement:
            return
        fired["value"] = True
        raise sqlite3.OperationalError("database or disk is full")

    return hook


@pytest.fixture
def harness(tmp_path: Any) -> Iterator[_Harness]:
    settings = load_settings().settings
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles("tools.agent.local_fast", "tools.agent.local_large"))
    url = f"sqlite:///{tmp_path / 'promptcadence.sqlite3'}"
    engine = create_engine_for(url)
    MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    try:
        yield _Harness(settings, Database(engine), fake)
    finally:
        engine.dispose()


def test_a_disk_full_turn_write_leaves_nothing_partial_and_other_trajectories_untouched(
    harness: _Harness,
) -> None:
    # Trajectory A runs to completion normally, before any fault exists.
    healthy_id = harness.submit()
    controller_a = harness.controller("host:1/0")
    assert controller_a.claim(healthy_id) is TrajectoryState.EXECUTING
    assert controller_a.run(healthy_id) is TrajectoryState.COMPLETED
    healthy_turns_before = harness.service.turns(healthy_id)
    assert len(healthy_turns_before) > 0

    # Trajectory B is claimed, then the disk fills for exactly the one write that would record
    # its turn.
    failing_id = harness.submit()
    controller_b = harness.controller("host:1/1")
    assert controller_b.claim(failing_id) is TrajectoryState.EXECUTING
    event.listen(
        harness.database.engine,
        "before_cursor_execute",
        _fail_once_on_turn_insert({"value": False}),
    )
    with pytest.raises(sqlite3.OperationalError):
        controller_b.run(failing_id)

    # Nothing partial: B is exactly as `claim` left it, and it has no turn at all.
    view = harness.service.get(failing_id)
    assert view.state is TrajectoryState.EXECUTING
    assert harness.service.turns(failing_id) == []

    # A's already-committed turn is untouched by B's failure.
    assert harness.service.turns(healthy_id) == healthy_turns_before

    # The fault was for one write, not a standing condition: B can still be run to completion.
    assert controller_b.run(failing_id) is TrajectoryState.COMPLETED
