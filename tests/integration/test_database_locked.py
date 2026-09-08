"""Degradation: SQLite database locked (graceful-degradation.md, row "Database locked (SQLite)").

Documented behaviour: "Retry with backoff to busy_timeout, then E" — the same property FreeWeight's
``tests/integration/test_transactions.py`` and WeightsDB's own
``test_sqlite_busy_timeout_raises_storage_busy`` prove. This is a genuine lock, not a simulated
fault: a real second connection holds a real write transaction open, and
``TrajectoryService.submit`` — an ordinary write, not a special case — is asked to reach through it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.conftest import budget_and_estimator
from weightsdb import MigrationRunner, StorageBusy, create_engine_for

from promptcadence.config import load_settings
from promptcadence.services.database import MIGRATIONS_LOCATION, Database
from promptcadence.services.events import TrajectoryEventSink
from promptcadence.services.trajectories import TrajectoryService, TrajectorySubmission

_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def test_a_locked_database_refuses_a_new_trajectory_and_creates_nothing(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'locked.sqlite3'}"
    settings = load_settings().settings

    # Ready the schema before anyone holds a lock — migrating is itself a write.
    setup_engine = create_engine_for(url)
    MigrationRunner(setup_engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    setup_engine.dispose()

    # A second connection over the same file, holding a real write transaction open — the
    # locking engine's own machinery, not a fault this test injects.
    holder_engine = create_engine_for(url)
    holder = holder_engine.connect()
    holder.execute(text("CREATE TABLE _lock_holder (id INTEGER PRIMARY KEY)"))
    holder.execute(text("INSERT INTO _lock_holder (id) VALUES (1)"))  # opens BEGIN IMMEDIATE

    # A short busy_timeout so the test proves the same property WeightsDB's own unit test does,
    # without waiting out the application's real (much longer) default.
    contender_engine = create_engine_for(url, sqlite_busy_timeout_ms=100)
    database = Database(contender_engine)
    ticks = iter(range(100_000))
    clock = lambda: _NOW + timedelta(milliseconds=next(ticks))  # noqa: E731 — a tiny local clock
    sink = TrajectoryEventSink(database, clock=clock)
    budget, _estimator = budget_and_estimator(database, settings, clock=clock)
    service = TrajectoryService(database, sink, settings, budget=budget, clock=clock)
    try:
        with pytest.raises(StorageBusy) as excinfo:
            service.submit(TrajectorySubmission(task="never gets in", bypass_planning=True))
        assert excinfo.value.details["busy_timeout_ms"] == 100
    finally:
        holder.rollback()
        holder.close()
        holder_engine.dispose()
        contender_engine.dispose()

    # The database is exactly as it was before the contended attempt.
    readback_engine = create_engine_for(url)
    with readback_engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM trajectories")).scalar_one()
    assert count == 0
    readback_engine.dispose()
