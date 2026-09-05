"""Gate C: ready-set dispatch over the plan DAG, the concurrency rule, and multi-step recovery."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from tests.fakes.harness import LoopHarness, open_harness, plan_document, step
from tests.fakes.loadcoach_app import ScriptedGeneration, held_generation

from promptcadence.config import load_settings
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models
from promptcadence.services.worker import recover


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopHarness]:
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "2")
    with open_harness(load_settings().settings) as harness:
        yield harness


_DIAMOND = plan_document(
    step("s1", tools=[]),
    step("s2", tools=[]),
    step("s3", depends_on=["s1", "s2"], tools=[]),
)


def test_a_diamond_dag_runs_in_ready_set_order_with_the_dag_recorded(
    harness: LoopHarness,
) -> None:
    harness.script_plan(_DIAMOND)
    harness.script(
        ScriptedGeneration(text="s1 result"),
        ScriptedGeneration(text="s2 result"),
        ScriptedGeneration(text="s3 result"),
    )
    trajectory_id = harness.submit_planned()
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
    started = harness.event_data(trajectory_id, "step.started")
    assert [(s["step_id"], s["depends_on"]) for s in started] == [
        ("s1", []),
        ("s2", []),
        ("s3", ["s1", "s2"]),
    ]
    completed = harness.event_data(trajectory_id, "step.completed")
    assert [c["step_id"] for c in completed] == ["s1", "s2", "s3"]
    (done,) = harness.event_data(trajectory_id, "trajectory.completed")
    assert done["step_count"] == 3
    with harness.database.read() as session:
        rows = (
            session.execute(select(models.PlanStep).order_by(models.PlanStep.sequence))
            .scalars()
            .all()
        )
        threads = session.execute(select(models.Thread)).scalars().all()
    assert [(row.step_id, row.depends_on_json, row.status) for row in rows] == [
        ("s1", [], "committed"),
        ("s2", [], "committed"),
        ("s3", ["s1", "s2"], "committed"),
    ], "the DAG is recorded even though execution was serial"
    assert sorted(thread.step_id for thread in threads) == ["s1", "s2", "s3"]
    turns = harness.service.turns(trajectory_id)
    s3_framing = next(t for t in turns if t.step_id == "s3" and t.prompt_id == "step.execute")
    assert "s1: s1 result" in (s3_framing.turn.content or "")
    assert "s2: s2 result" in (s3_framing.turn.content or "")
    assert harness.service.get(trajectory_id).lease_owner is None


def test_two_ready_local_steps_stay_serial_under_a_raised_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The concurrency rule, on the fake: one local step in flight ever (ADR-0038)."""
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__MAX_CONCURRENT_STEPS", "2")
    with open_harness(load_settings().settings) as harness:
        harness.script_plan(_DIAMOND)
        harness.script(
            ScriptedGeneration(text="s1"),
            ScriptedGeneration(text="s2"),
            ScriptedGeneration(text="s3"),
        )
        trajectory_id = harness.submit_planned()
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
        types = harness.events(trajectory_id)
        started = [i for i, t in enumerate(types) if t == "step.started"]
        completed = [i for i, t in enumerate(types) if t == "step.completed"]
        assert started[1] > completed[0], "s2 started only after s1 committed"
        assert started[2] > completed[1]


def test_a_takeover_mid_step_resumes_at_that_step_without_a_duplicate_thread(
    harness: LoopHarness,
) -> None:
    """Multi-step reconciliation: s1 committed, s2 in flight when the worker dies."""
    held, hold = held_generation(text="never seen")
    harness.script_plan(
        plan_document(step("s1", tools=[]), step("s2", depends_on=["s1"], tools=[]))
    )
    harness.script(ScriptedGeneration(text="s1 result"), held)
    trajectory_id = harness.submit_planned()
    stalled = harness.controller("host:1/0")
    assert stalled.claim(trajectory_id) is TrajectoryState.PLANNING
    result: list[TrajectoryState] = []
    thread = threading.Thread(target=lambda: result.append(stalled.run(trajectory_id)))
    thread.start()
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while not harness.fake.in_flight() and datetime.now(UTC) < deadline:
        threading.Event().wait(0.01)
    (orphan,) = harness.fake.in_flight()

    recoverer = harness.controller("host:2/0")
    summary = recover(
        recoverer, harness.database, owner_prefix="host:2", now=harness.clock(), only_expired=False
    )
    assert summary.resumed == (trajectory_id,)
    hold.set()
    thread.join(timeout=5)
    assert result == [TrajectoryState.EXECUTING], "fenced: the stalled worker committed nothing"

    harness.script(ScriptedGeneration(text="s2 result"))
    assert recoverer.run(trajectory_id) is TrajectoryState.COMPLETED
    with harness.database.read() as session:
        threads = session.execute(select(models.Thread)).scalars().all()
    assert sorted(t.step_id for t in threads) == ["s1", "s2"], "no duplicate thread for s2"
    assert [s["step_id"] for s in harness.event_data(trajectory_id, "step.started")] == ["s1", "s2"]
    recovered = harness.event_data(trajectory_id, "trajectory.recovered")
    assert recovered and recovered[0]["outcome"].startswith("cancelled_in_flight_job:")
    assert harness.fake.jobs[orphan.job_id].state == "cancelled"
    s2_turns = [t for t in harness.service.turns(trajectory_id) if t.step_id == "s2"]
    assert [t.turn.role.value for t in s2_turns] == ["user", "user", "assistant"]
