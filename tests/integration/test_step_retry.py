"""The per-step retry (row G3, ADR-0076): a repeat under the same intent, on both paths.

What each test here is really about is the **record**. The loop change is a few lines; what an
explanation reads back months later is every attempt's number, tier and cause, and the halt that
names them. So the assertions are on events, on the halt's own text and on
``plan_steps.attempt`` — not merely on the terminal state.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select
from tests.fakes.harness import LoopHarness, open_harness, plan_document, step
from tests.fakes.loadcoach_app import FakeModel, ScriptedError, ScriptedGeneration

from promptcadence.config import load_settings
from promptcadence.domain.errors import ErrorCode
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopHarness]:
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "2")
    with open_harness(load_settings().settings) as harness:
        yield harness


@pytest.fixture
def patient_harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopHarness]:
    """``step_retries = 2``: two repeats, so a step can fail twice and still complete."""
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "2")
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__STEP_RETRIES", "2")
    with open_harness(load_settings().settings) as harness:
        yield harness


def _planned(harness: LoopHarness, *scripted: object) -> str:
    harness.script_plan(plan_document(step("s1")))
    harness.script(*scripted)
    return harness.submit_planned()


# --------------------------------------------------------------------------------------------
# A retryable failure is repeated, and the step completes
# --------------------------------------------------------------------------------------------


def test_a_step_that_fails_twice_completes_on_its_third_attempt(
    patient_harness: LoopHarness,
) -> None:
    """The whole row in one journey: two accidents, one answer, and a record that says so."""
    harness = patient_harness
    trajectory_id = _planned(
        harness,
        ScriptedError("ALL_CANDIDATES_FAILED"),
        ScriptedError("PROVIDER_TIMEOUT"),
        ScriptedGeneration(text="The notes describe three meetings."),
    )
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED

    assert harness.events(trajectory_id) == [
        "trajectory.created",
        "trajectory.claimed",
        "plan.drafted",
        "plan.approved",
        "intent.minted",
        "step.started",
        "turn.started",
        "step.retried",
        "turn.started",
        "step.retried",
        "turn.started",
        "budget.debited",
        "turn.completed",
        "step.completed",
        "trajectory.completed",
    ]

    first, second = harness.event_data(trajectory_id, "step.retried")
    assert first["attempt"] == 2 and second["attempt"] == 3
    assert first["failed_tier"] == second["failed_tier"] == "local_fast"
    assert "ALL_CANDIDATES_FAILED" in first["cause"]
    assert "PROVIDER_TIMEOUT" in second["cause"]
    assert first["error_code"] == second["error_code"] == ErrorCode.LOADCOACH_ERROR.value

    # Each attempt announced its own turn, and each event names the one that went unanswered.
    announced = [event["turn_id"] for event in harness.event_data(trajectory_id, "turn.started")]
    assert [first["failed_turn_id"], second["failed_turn_id"]] == announced[:2]

    with harness.database.read() as session:
        plan_step = session.execute(select(models.PlanStep)).scalar_one()
        intents = session.execute(select(models.ExecutionIntent)).scalars().all()
    assert plan_step.attempt == 3, "the counter is the planned path's summary of the events"
    assert plan_step.status == "committed"
    # ADR-0056: a repeat mints nothing and widens nothing.
    assert len(intents) == 1 and intents[0].revision == 1
    assert {event["intent_id"] for event in (first, second)} == {intents[0].intent_id}
    assert {event["intent_revision"] for event in (first, second)} == {1}
    # Only the turn that answered is debited; the planner's spend is on the plan row, not here.
    assert len(list(harness.budget.entries(run_id=trajectory_id))) == 1


def test_the_bypassed_loop_repeats_through_the_same_code_path(harness: LoopHarness) -> None:
    """Contract 1's requirement: the synthetic ``loop`` step repeats identically.

    The counter cannot follow it — there is no ``plan_steps`` row for a bypassed trajectory — which
    is exactly why the events are the history (ADR-0076 §4).
    """
    harness.script(ScriptedError("ALL_CANDIDATES_FAILED"), ScriptedGeneration(text="done"))
    trajectory_id = harness.submit_bypass()
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED

    (retried,) = harness.event_data(trajectory_id, "step.retried")
    assert retried["step_id"] == "loop"
    assert retried["attempt"] == 2
    assert retried["intent_revision"] == 1
    with harness.database.read() as session:
        assert session.execute(select(models.PlanStep)).scalars().all() == []


# --------------------------------------------------------------------------------------------
# The budget, and the halt that names every attempt
# --------------------------------------------------------------------------------------------


def test_the_budget_is_spent_and_the_halt_names_the_last_cause_and_every_attempt(
    harness: LoopHarness,
) -> None:
    """A halt saying only "attempt 2 failed" is the failure mode this row exists to prevent."""
    trajectory_id = _planned(
        harness,
        ScriptedError("ALL_CANDIDATES_FAILED"),
        ScriptedError("PROVIDER_TIMEOUT", message="the provider did not answer in time"),
    )
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.HALTED

    view = harness.service.get(trajectory_id)
    cause = view.halted_reason or ""
    assert view.error_code == ErrorCode.LOADCOACH_ERROR.value
    assert "step s1: 2 attempts failed" in cause
    assert "revision 1" in cause
    assert "step_retries = 1" in cause
    assert "attempt 1 (local_fast): " in cause and "ALL_CANDIDATES_FAILED" in cause
    assert "attempt 2 (local_fast): " in cause
    assert "the provider did not answer in time" in cause
    assert harness.events(trajectory_id)[-1] == "trajectory.halted"
    # One repeat was started, so one event — the halt is not itself an attempt.
    assert len(harness.event_data(trajectory_id, "step.retried")) == 1


def test_zero_retries_halts_on_the_first_failure_naming_that_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``step_retries = 0`` is one attempt and no repeat — the pre-G3 behaviour, asked for."""
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__STEP_RETRIES", "0")
    with open_harness(load_settings().settings) as harness:
        trajectory_id = _planned(harness, ScriptedError("ALL_CANDIDATES_FAILED"))
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.HALTED
        assert harness.event_data(trajectory_id, "step.retried") == []
        cause = harness.service.get(trajectory_id).halted_reason or ""
        assert "step s1: 1 attempts failed" in cause and "step_retries = 0" in cause


def test_attempts_and_turns_draw_on_one_envelope_so_max_turns_still_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generous retry budget cannot outrun ``max_turns_per_step`` (ADR-0076 §5).

    And the two stay distinguishable in the record: this halt is ``STEP_LIMIT_EXCEEDED`` with the
    max_turns wording, not the retry budget's.
    """
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__STEP_RETRIES", "5")
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__MAX_TURNS_PER_STEP", "2")
    with open_harness(load_settings().settings) as harness:
        trajectory_id = _planned(
            harness,
            ScriptedError("ALL_CANDIDATES_FAILED"),
            ScriptedError("ALL_CANDIDATES_FAILED"),
            ScriptedError("ALL_CANDIDATES_FAILED"),
        )
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.HALTED
        view = harness.service.get(trajectory_id)
        assert view.error_code == ErrorCode.STEP_LIMIT_EXCEEDED.value
        assert "max_turns (2) is spent" in (view.halted_reason or "")
        assert len(harness.event_data(trajectory_id, "step.retried")) == 2


# --------------------------------------------------------------------------------------------
# What is never repeated
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("VALIDATION_ERROR", ErrorCode.LOADCOACH_ERROR),
        ("CONTEXT_LIMIT_EXCEEDED", ErrorCode.COMPACTION_FAILED),
        ("CAPABILITY_UNSUPPORTED", ErrorCode.LOADCOACH_ERROR),
        ("FORBIDDEN", ErrorCode.LOADCOACH_ERROR),
    ],
)
def test_a_deterministic_refusal_is_never_repeated(
    harness: LoopHarness, code: str, expected: ErrorCode
) -> None:
    """The identical request gets the identical answer, so a repeat spends a turn to learn nothing.

    ``VALIDATION_ERROR`` is the one G2 made reachable (`G2_HANDOFF.md` §3.3): a request LoadCoach
    cannot construct fails the job cleanly rather than hanging. Cleanly refused is still refused.
    """
    trajectory_id = _planned(harness, ScriptedError(code))
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.HALTED
    assert harness.service.get(trajectory_id).error_code == expected.value
    assert harness.event_data(trajectory_id, "step.retried") == []


def test_a_deviation_halt_is_never_repeated(harness: LoopHarness) -> None:
    """A violation is a statement about what executed; repeating it would launder the finding."""
    harness.fake.model = FakeModel(
        canonical_id="openai_compatible/gpt@sha256:" + "b" * 64, provider_kind="ollama"
    )
    trajectory_id = harness.submit_bypass()
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.HALTED
    view = harness.service.get(trajectory_id)
    assert view.error_code == ErrorCode.DEVIATION_HALTED.value
    assert "tier_violation" in (view.halted_reason or "")
    assert harness.event_data(trajectory_id, "step.retried") == []


def test_a_tier_that_cannot_serve_escalates_rather_than_repeating(harness: LoopHarness) -> None:
    """ADR-0076 §3's other half: ``NO_ELIGIBLE_MODEL`` keeps its own mechanism, untouched.

    It is not an attempt that went wrong — it is a statement about which tiers can serve — so it
    produces a ``tier_escalation`` deviation and never a ``step.retried``.
    """
    harness.script(ScriptedError("NO_ELIGIBLE_MODEL", details={"candidates": []}))
    trajectory_id = harness.submit_bypass()
    assert harness.claim_and_run(trajectory_id) is not TrajectoryState.COMPLETED
    assert harness.event_data(trajectory_id, "step.retried") == []
    (deviation,) = harness.event_data(trajectory_id, "deviation.detected")
    assert deviation["category"] == "tier_escalation"


def test_a_repeat_stops_the_tier_ladder_rather_than_falling_through_it(
    harness: LoopHarness,
) -> None:
    """A repeat before an escalation means a transient failure never moves the step's tier.

    Falling to the next permitted tier here would be an escalation's move made without an
    escalation's record: the step would run somewhere it reached because something flaked.
    """
    harness.script(ScriptedError("ALL_CANDIDATES_FAILED"), ScriptedGeneration(text="done"))
    trajectory_id = harness.submit_bypass()
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
    tiers = [event["tier"] for event in harness.event_data(trajectory_id, "turn.started")]
    assert tiers == ["local_fast", "local_fast"]
    assert harness.event_data(trajectory_id, "deviation.detected") == []
