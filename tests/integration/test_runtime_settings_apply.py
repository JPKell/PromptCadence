"""A change written through the settings table reaches the running worker (spec §12).

The registry is only half of a runtime-changeable setting; the other half is that something
re-reads it. These tests prove the second half at the lease-reap cadence — the analogue of
LoadCoach's flags cadence — with the clock injected, so "within one cadence" is asserted rather
than waited for.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from tests.fakes.harness import LoopHarness, open_harness

from promptcadence.config import Settings, load_settings
from promptcadence.services.settings import read_runtime_settings, write_runtime_settings
from promptcadence.services.worker import TrajectoryWorker

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def harness() -> Iterator[LoopHarness]:
    with open_harness(load_settings().settings) as built:
        yield built


def _worker(harness: LoopHarness, settings: Settings) -> TrajectoryWorker:
    return TrajectoryWorker(
        database=harness.database,
        sink=harness.sink,
        loadcoach=harness.loadcoach,
        settings=settings,
        tools=harness.tools,
        budget=harness.budget,
        egress=harness.egress,
        clock=harness.clock,
        poll_interval_seconds=0.01,
    )


def test_a_refresh_applies_every_key_to_the_settings_the_process_shares(
    harness: LoopHarness,
) -> None:
    settings = load_settings().settings
    worker = _worker(harness, settings)
    controller = harness.controller()
    write_runtime_settings(
        harness.database,
        {
            "execution.step_retries": 3,
            "execution.max_turns_per_step": 12,
            "compaction.threshold": 0.5,
            "storage.content_retention_hours": 1,
            "planning.corrective_retries": 4,
        },
        settings=settings,
        now=_NOW,
    )
    assert settings.execution.step_retries == 1, "not applied until the worker reads them"

    effective = worker.refresh_runtime_settings(controller)

    assert effective["execution.step_retries"] == 3
    assert settings.execution.step_retries == 3
    assert settings.execution.max_turns_per_step == 12
    assert settings.compaction.threshold == 0.5
    assert settings.storage.content_retention_hours == 1
    assert controller.planner.corrective_retries == 4, "the one holder that cached its number"
    assert controller.planner.max_attempts == 5


def test_the_fallback_is_the_configured_value_not_the_one_already_applied(
    harness: LoopHarness,
) -> None:
    """A second refresh over an unreadable row restores configuration, not the last write."""
    settings = load_settings().settings
    worker = _worker(harness, settings)
    write_runtime_settings(
        harness.database, {"execution.step_retries": 6}, settings=settings, now=_NOW
    )
    assert worker.refresh_runtime_settings()["execution.step_retries"] == 6
    with harness.database.write() as session:
        session.execute(
            text(
                "UPDATE settings SET value_json = '\"nonsense\"' "
                "WHERE key = 'execution.step_retries'"
            )
        )
    assert worker.refresh_runtime_settings()["execution.step_retries"] == 1
    assert settings.execution.step_retries == 1


def test_the_running_worker_applies_a_write_within_one_reap_cadence(harness: LoopHarness) -> None:
    settings = load_settings().settings
    worker = _worker(harness, settings)
    worker.start()
    try:
        write_runtime_settings(
            harness.database, {"execution.step_retries": 5}, settings=settings, now=_NOW
        )
        assert settings.execution.step_retries == 1, "the reap has not come round yet"
        harness.clock.advance(timedelta(seconds=settings.execution.lease_seconds + 1))
        # ponytail: the fake clock already reflects the reap instant, so what remains is real
        # wall-clock scheduling latency for the background thread's next `poll_interval_seconds`
        # (0.01s) wake-up — normally a handful of milliseconds. This failed once at 10s under a
        # parallel LoadCoach coverage run saturating the machine's CPU; 30s (3000 poll intervals)
        # gives real OS scheduling delay headroom without weakening the one thing this test
        # proves — that a write reaches the worker within one reap cadence, not one wall-clock
        # instant. Raise further only if CI keeps flaking under contention.
        deadline = time.monotonic() + 30
        while settings.execution.step_retries != 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert settings.execution.step_retries == 5, "one reap cadence, one applied setting"
    finally:
        worker.stop()


def test_the_sweep_runs_on_the_retention_the_operator_set(harness: LoopHarness) -> None:
    """``storage.content_retention_hours`` is read through the applied value, not the file's."""
    settings = load_settings().settings
    worker = _worker(harness, settings)
    write_runtime_settings(
        harness.database, {"storage.content_retention_hours": 0}, settings=settings, now=_NOW
    )
    worker.refresh_runtime_settings()
    assert settings.storage.content_retention_hours == 0
    assert (
        read_runtime_settings(harness.database, settings=settings)[
            "storage.content_retention_hours"
        ]
        == 0
    )
    outcome = worker.sweep_retention(harness.controller(), _NOW)
    assert outcome is not None, "the sweep ran, and it ran on the stored retention"
