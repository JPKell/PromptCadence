"""The ready-set dispatch rule (lifecycle §8.4): disjoint surfaces, one local step ever."""

from __future__ import annotations

import pytest
from baseaicore import DataClassification, ValidationError

from promptcadence.domain.dispatch import StepRetried, dispatchable
from promptcadence.domain.plan import PlanStep
from promptcadence.domain.tiers import TierSnapshot


def _step(step_id: str, tier: str) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description="x",
        depends_on=(),
        tools=(),
        tier=tier,
        data_classification=DataClassification.INTERNAL,
        expected_turns=1,
    )


def _tier_of(snapshot: TierSnapshot, steps: list[PlanStep]) -> dict[str, object]:
    return {step.step_id: snapshot.require(step.tier) for step in steps}


def test_serial_dispatch_picks_the_first_ready_step(tier_snapshot: TierSnapshot) -> None:
    steps = [_step("a", "local_fast"), _step("b", "local_large")]
    chosen = dispatchable(
        steps,
        in_flight=(),
        tier_of=_tier_of(tier_snapshot, steps),  # type: ignore[arg-type]
        max_concurrent_steps=1,
        max_concurrent_remote_steps=2,
    )
    assert [step.step_id for step in chosen] == ["a"]


def test_two_local_steps_never_run_together_whatever_the_cap(tier_snapshot: TierSnapshot) -> None:
    """ADR-0038: two concurrent local steps are a queueing fiction."""
    steps = [_step("a", "local_fast"), _step("b", "local_large")]
    chosen = dispatchable(
        steps,
        in_flight=(),
        tier_of=_tier_of(tier_snapshot, steps),  # type: ignore[arg-type]
        max_concurrent_steps=4,
        max_concurrent_remote_steps=2,
    )
    assert [step.step_id for step in chosen] == ["a"]
    nothing = dispatchable(
        [steps[1]],
        in_flight=(tier_snapshot.require("local_fast"),),
        tier_of=_tier_of(tier_snapshot, steps),  # type: ignore[arg-type]
        max_concurrent_steps=4,
        max_concurrent_remote_steps=2,
    )
    assert nothing == ()


def test_a_local_and_a_remote_step_may_run_together(tier_snapshot: TierSnapshot) -> None:
    steps = [_step("a", "local_fast"), _step("b", "remote_cheap"), _step("c", "remote_frontier")]
    chosen = dispatchable(
        steps,
        in_flight=(),
        tier_of=_tier_of(tier_snapshot, steps),  # type: ignore[arg-type]
        max_concurrent_steps=3,
        max_concurrent_remote_steps=2,
    )
    assert [step.step_id for step in chosen] == ["a", "b", "c"]
    capped = dispatchable(
        steps,
        in_flight=(),
        tier_of=_tier_of(tier_snapshot, steps),  # type: ignore[arg-type]
        max_concurrent_steps=3,
        max_concurrent_remote_steps=1,
    )
    assert [step.step_id for step in capped] == ["a", "b"]
    total_capped = dispatchable(
        steps,
        in_flight=(),
        tier_of=_tier_of(tier_snapshot, steps),  # type: ignore[arg-type]
        max_concurrent_steps=2,
        max_concurrent_remote_steps=2,
    )
    assert [step.step_id for step in total_capped] == ["a", "b"]


def test_in_flight_remote_steps_count_against_the_remote_cap(tier_snapshot: TierSnapshot) -> None:
    steps = [_step("b", "remote_cheap"), _step("d", "local_fast")]
    chosen = dispatchable(
        steps,
        in_flight=(tier_snapshot.require("remote_frontier"), tier_snapshot.require("remote_cheap")),
        tier_of=_tier_of(tier_snapshot, steps),  # type: ignore[arg-type]
        max_concurrent_steps=4,
        max_concurrent_remote_steps=2,
    )
    assert [step.step_id for step in chosen] == ["d"], (
        "the remote cap is full; the local slot is free"
    )


def test_a_bound_below_one_and_an_unknown_tier_are_refused(tier_snapshot: TierSnapshot) -> None:
    steps = [_step("a", "local_fast")]
    with pytest.raises(ValidationError):
        dispatchable(
            steps,
            in_flight=(),
            tier_of=_tier_of(tier_snapshot, steps),  # type: ignore[arg-type]
            max_concurrent_steps=0,
            max_concurrent_remote_steps=2,
        )
    with pytest.raises(ValidationError):
        dispatchable(
            steps,
            in_flight=(),
            tier_of={},
            max_concurrent_steps=1,
            max_concurrent_remote_steps=2,
        )


def test_step_retried_canonical_payload_is_exactly_this() -> None:
    """The golden an explanation reads a repeat back from (ADR-0076).

    ``plan_steps.attempt`` counts and only on the planned path; these events are the history, so
    every fact the halt's text and the explanation need — which attempt, which turn was announced
    and never answered, on which tier, and why — has to be here and nowhere else.
    """
    body = StepRetried(
        trajectory_id="tr1",
        step_id="s1",
        thread_id="th1",
        intent_id="in1",
        intent_revision=1,
        attempt=2,
        failed_turn_id="tu1",
        failed_tier="local_fast",
        cause="LoadCoach refused /api/v1/generate with ALL_CANDIDATES_FAILED",
        error_code="LOADCOACH_ERROR",
    )
    assert body.as_canonical() == {
        "trajectory_id": "tr1",
        "step_id": "s1",
        "thread_id": "th1",
        "intent_id": "in1",
        "intent_revision": 1,
        "attempt": 2,
        "failed_turn_id": "tu1",
        "failed_tier": "local_fast",
        "cause": "LoadCoach refused /api/v1/generate with ALL_CANDIDATES_FAILED",
        "error_code": "LOADCOACH_ERROR",
    }
