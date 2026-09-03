"""Tests for promptcadence.domain.trajectory: every §8.2 row, every refusal, every guard.

The terminal-state property is written over the enum rather than over five named cases, so a sixth
terminal state added later is covered the day it appears.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from baseaicore import DataClassification, Money, ValidationError

from promptcadence.domain.errors import IllegalTransitionError
from promptcadence.domain.events import EventBody, EventType
from promptcadence.domain.trajectory import (
    TRANSITIONS,
    BudgetWindowWait,
    TrajectoryCancelled,
    TrajectoryClaimed,
    TrajectoryCompleted,
    TrajectoryCreated,
    TrajectoryDeclaration,
    TrajectoryFailed,
    TrajectoryHalted,
    TrajectoryRecovered,
    TrajectoryResumed,
    TrajectoryState,
    Transition,
    TransitionOutcome,
    WindowWait,
    approve_plan,
    cancel,
    claim_for_bypass,
    claim_for_planning,
    complete,
    create,
    deny_or_time_out_approval,
    fail,
    grant_approval,
    halt,
    halt_window_wait,
    park_for_window,
    reject_plan,
    request_approval,
    resume_from_window,
    transitions_from,
)

_S = TrajectoryState
_EDGE = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
_NON_TERMINAL = tuple(state for state in _S if not state.is_terminal)
_TERMINAL = tuple(state for state in _S if state.is_terminal)


def _wait(parked_from: TrajectoryState = _S.EXECUTING, days: int = 0) -> WindowWait:
    """A window park with a persisted edge and day count."""
    return WindowWait(parked_from=parked_from, next_edge_at=_EDGE, days_waited=days)


# --------------------------------------------------------------------------------------------
# The table, and the terminal-state property
# --------------------------------------------------------------------------------------------


def test_the_table_holds_exactly_the_seventeen_rows_of_lifecycle_eight_two() -> None:
    """T14, T15 and T16 expand over their source or target states; the labels are still 17."""
    labels = sorted({transition.label for transition in TRANSITIONS}, key=lambda t: int(t[1:]))
    assert labels == [f"T{index}" for index in range(1, 18)]


def test_every_transition_writes_at_least_one_event() -> None:
    """ADR-0044: a state change and its event are one write, so a silent row is a defect."""
    for transition in TRANSITIONS:
        assert transition.events
    with pytest.raises(ValidationError, match="writes no event"):
        Transition(
            label="T99", source=_S.QUEUED, target=_S.PLANNING, trigger="x", guard="-", events=()
        )


@pytest.mark.parametrize("state", _TERMINAL)
def test_terminal_states_are_absorbing(state: TrajectoryState) -> None:
    """The property, over the enum: a sixth terminal state is covered the day it is added."""
    assert transitions_from(state) == ()
    assert state.holds_lease is False


def test_the_table_cannot_hold_a_row_leaving_a_terminal_state() -> None:
    with pytest.raises(ValidationError, match="leaves terminal state"):
        Transition(
            label="T99",
            source=_S.COMPLETED,
            target=_S.QUEUED,
            trigger="x",
            guard="-",
            events=(EventType.TRAJECTORY_CREATED,),
        )


def test_only_planning_and_executing_hold_a_lease() -> None:
    """``awaiting_window`` parks for days; a lease held that long is a lease nobody is renewing."""
    assert {state for state in _S if state.holds_lease} == {_S.PLANNING, _S.EXECUTING}
    assert _S.AWAITING_WINDOW.holds_lease is False
    assert _S.AWAITING_APPROVAL.holds_lease is False


def test_every_non_terminal_state_can_be_cancelled() -> None:
    """T14 covers every non-terminal state, and the table lists one row per source."""
    sources = {t.source for t in TRANSITIONS if t.label == "T14"}
    assert sources == set(_NON_TERMINAL)


# --------------------------------------------------------------------------------------------
# One test per row
# --------------------------------------------------------------------------------------------


def test_t1_creates_a_queued_trajectory() -> None:
    outcome = create()
    assert outcome.state is _S.QUEUED
    assert outcome.events == (EventType.TRAJECTORY_CREATED,)


def test_t2_claims_for_planning_and_refuses_each_guard() -> None:
    outcome = claim_for_planning(_S.QUEUED, planning_enabled=True, lease_acquired=True)
    assert outcome.state is _S.PLANNING
    assert outcome.transition.label == "T2"
    with pytest.raises(IllegalTransitionError, match="Planning enabled"):
        claim_for_planning(_S.QUEUED, planning_enabled=False, lease_acquired=True)
    with pytest.raises(IllegalTransitionError, match="lease acquired"):
        claim_for_planning(_S.QUEUED, planning_enabled=True, lease_acquired=False)
    with pytest.raises(IllegalTransitionError, match="but the trajectory is executing"):
        claim_for_planning(_S.EXECUTING, planning_enabled=True, lease_acquired=True)


def test_t3_will_not_enter_executing_without_a_minted_default_intent() -> None:
    """ADR-0048's invariance is structural: no path reaches ``executing`` without an intent."""
    outcome = claim_for_bypass(
        _S.QUEUED, bypass_permitted=True, lease_acquired=True, default_intent_minted=True
    )
    assert outcome.state is _S.EXECUTING
    assert outcome.events == (EventType.TRAJECTORY_CLAIMED, EventType.INTENT_MINTED)
    with pytest.raises(IllegalTransitionError, match="default intent minted"):
        claim_for_bypass(
            _S.QUEUED, bypass_permitted=True, lease_acquired=True, default_intent_minted=False
        )
    with pytest.raises(IllegalTransitionError, match="Bypass permitted"):
        claim_for_bypass(
            _S.QUEUED, bypass_permitted=False, lease_acquired=True, default_intent_minted=True
        )


def test_t4_auto_approval_requires_resolved_steps_and_minted_intents() -> None:
    outcome = approve_plan(_S.PLANNING, all_steps_resolved=True, intents_minted=3)
    assert outcome.state is _S.EXECUTING
    assert outcome.events == (EventType.PLAN_APPROVED, EventType.INTENT_MINTED)
    with pytest.raises(IllegalTransitionError, match="approved/redlined"):
        approve_plan(_S.PLANNING, all_steps_resolved=False, intents_minted=3)
    with pytest.raises(IllegalTransitionError, match="intents minted"):
        approve_plan(_S.PLANNING, all_steps_resolved=True, intents_minted=0)


def test_t5_and_t10_park_on_a_request_that_must_exist() -> None:
    """A trajectory parked with no request is one nobody can release (ADR-0049 rule 6)."""
    from_planning = request_approval(_S.PLANNING, request_created=True)
    assert from_planning.state is _S.AWAITING_APPROVAL
    assert from_planning.transition.label == "T5"
    from_executing = request_approval(_S.EXECUTING, request_created=True)
    assert from_executing.transition.label == "T10"
    with pytest.raises(IllegalTransitionError, match="approval_request created"):
        request_approval(_S.PLANNING, request_created=False)
    with pytest.raises(IllegalTransitionError, match="planning or executing"):
        request_approval(_S.QUEUED, request_created=True)


def test_t6_rejects_from_planning_only() -> None:
    assert reject_plan(_S.PLANNING).state is _S.REJECTED
    with pytest.raises(IllegalTransitionError):
        reject_plan(_S.EXECUTING)


def test_t7_and_t13_record_a_cause() -> None:
    """Every halt and every failure names its cause; an empty one is refused, not stored."""
    assert fail(_S.PLANNING, cause="draft failed after 2 correctives").transition.label == "T7"
    assert fail(_S.EXECUTING, cause="loadcoach gone").transition.label == "T13"
    with pytest.raises(IllegalTransitionError, match="Cause recorded"):
        fail(_S.EXECUTING, cause="   ")
    with pytest.raises(IllegalTransitionError, match="planning or executing"):
        fail(_S.QUEUED, cause="x")


def test_t8_requires_the_approve_scope_a_pending_request_and_a_minting() -> None:
    """Approval's output *is* the minting, so a grant that mints nothing authorised nothing."""
    outcome = grant_approval(
        _S.AWAITING_APPROVAL, approver_has_scope=True, request_pending=True, intents_minted=1
    )
    assert outcome.state is _S.EXECUTING
    assert outcome.events == (EventType.APPROVAL_GRANTED, EventType.INTENT_MINTED)
    with pytest.raises(IllegalTransitionError, match="approve scope"):
        grant_approval(
            _S.AWAITING_APPROVAL,
            approver_has_scope=False,
            request_pending=True,
            intents_minted=1,
        )
    with pytest.raises(IllegalTransitionError, match="request pending"):
        grant_approval(
            _S.AWAITING_APPROVAL,
            approver_has_scope=True,
            request_pending=False,
            intents_minted=1,
        )
    with pytest.raises(IllegalTransitionError, match="intents"):
        grant_approval(
            _S.AWAITING_APPROVAL,
            approver_has_scope=True,
            request_pending=True,
            intents_minted=0,
        )


def test_t9_treats_a_denial_and_a_timeout_alike() -> None:
    """A timeout is never a grant (ADR-0049 rule 4), so both endings share one row."""
    outcome = deny_or_time_out_approval(_S.AWAITING_APPROVAL)
    assert outcome.state is _S.HALTED
    assert outcome.events == (EventType.APPROVAL_DENIED, EventType.TRAJECTORY_HALTED)
    with pytest.raises(IllegalTransitionError):
        deny_or_time_out_approval(_S.EXECUTING)


def test_t11_completes_only_on_a_declared_success() -> None:
    assert complete(_S.EXECUTING, all_steps_succeeded=True).state is _S.COMPLETED
    with pytest.raises(IllegalTransitionError, match="declared finish"):
        complete(_S.EXECUTING, all_steps_succeeded=False)


def test_t12_halts_with_a_cause() -> None:
    assert halt(_S.EXECUTING, cause="tier_violation").state is _S.HALTED
    with pytest.raises(IllegalTransitionError, match="Cause recorded"):
        halt(_S.EXECUTING, cause="")


def test_t14_cancels_any_non_terminal_state_at_a_turn_boundary() -> None:
    for state in _NON_TERMINAL:
        assert cancel(state).state is _S.CANCELLED
    with pytest.raises(IllegalTransitionError, match="next turn boundary"):
        cancel(_S.EXECUTING, at_turn_boundary=False)


@pytest.mark.parametrize("state", _TERMINAL)
def test_a_terminal_trajectory_is_not_cancellable(state: TrajectoryState) -> None:
    with pytest.raises(IllegalTransitionError, match="terminal and cannot be cancelled"):
        cancel(state)


def test_t15_parks_only_after_the_lease_is_released_and_turns_have_settled() -> None:
    outcome = park_for_window(_S.EXECUTING, wait=_wait(), lease_released=True, turns_settled=True)
    assert outcome.state is _S.AWAITING_WINDOW
    assert outcome.events == (EventType.BUDGET_WINDOW_WAIT,)
    assert (
        park_for_window(
            _S.PLANNING, wait=_wait(_S.PLANNING), lease_released=True, turns_settled=True
        ).state
        is _S.AWAITING_WINDOW
    )
    with pytest.raises(IllegalTransitionError, match="lease released"):
        park_for_window(_S.EXECUTING, wait=_wait(), lease_released=False, turns_settled=True)
    with pytest.raises(IllegalTransitionError, match="in-flight turns"):
        park_for_window(_S.EXECUTING, wait=_wait(), lease_released=True, turns_settled=False)
    with pytest.raises(IllegalTransitionError, match="Parked-from state persisted"):
        park_for_window(
            _S.EXECUTING, wait=_wait(_S.PLANNING), lease_released=True, turns_settled=True
        )
    with pytest.raises(IllegalTransitionError, match="only planning or executing park"):
        park_for_window(_S.QUEUED, wait=_wait(), lease_released=True, turns_settled=True)


def test_t16_returns_to_the_state_it_parked_from() -> None:
    """The target is data, not a guess: it was persisted when the trajectory parked."""
    for parked_from in (_S.PLANNING, _S.EXECUTING):
        outcome = resume_from_window(
            _S.AWAITING_WINDOW,
            wait=_wait(parked_from),
            ceiling_admits=True,
            window_wait_max_days=3,
            lease_acquired=True,
        )
        assert outcome.state is parked_from
        assert outcome.events == (EventType.TRAJECTORY_RESUMED,)


def test_t16_refuses_each_of_its_guards() -> None:
    with pytest.raises(IllegalTransitionError, match="now admits"):
        resume_from_window(
            _S.AWAITING_WINDOW,
            wait=_wait(),
            ceiling_admits=False,
            window_wait_max_days=3,
            lease_acquired=True,
        )
    with pytest.raises(IllegalTransitionError, match="window_wait_max_days not exceeded"):
        resume_from_window(
            _S.AWAITING_WINDOW,
            wait=_wait(days=3),
            ceiling_admits=True,
            window_wait_max_days=3,
            lease_acquired=True,
        )
    with pytest.raises(IllegalTransitionError, match="lease re-acquired"):
        resume_from_window(
            _S.AWAITING_WINDOW,
            wait=_wait(),
            ceiling_admits=True,
            window_wait_max_days=3,
            lease_acquired=False,
        )
    with pytest.raises(IllegalTransitionError, match="T16 moves from awaiting_window"):
        resume_from_window(
            _S.EXECUTING,
            wait=_wait(),
            ceiling_admits=True,
            window_wait_max_days=3,
            lease_acquired=True,
        )


def test_t17_halts_only_once_the_wait_has_actually_run_out() -> None:
    """Halting early would discard work the next day edge would have released."""
    outcome = halt_window_wait(
        _S.AWAITING_WINDOW, wait=_wait(days=3), window_wait_max_days=3, cause="daily ceiling"
    )
    assert outcome.state is _S.HALTED
    with pytest.raises(IllegalTransitionError, match="window_wait_max_days elapsed"):
        halt_window_wait(_S.AWAITING_WINDOW, wait=_wait(days=2), window_wait_max_days=3, cause="x")
    with pytest.raises(IllegalTransitionError, match="Cause recorded"):
        halt_window_wait(_S.AWAITING_WINDOW, wait=_wait(days=3), window_wait_max_days=3, cause=" ")


@pytest.mark.parametrize(
    ("mover", "state"),
    [
        (mover, state)
        for mover, legal in (
            ("claim_for_planning", {_S.QUEUED}),
            ("claim_for_bypass", {_S.QUEUED}),
            ("approve_plan", {_S.PLANNING}),
            ("reject_plan", {_S.PLANNING}),
            ("complete", {_S.EXECUTING}),
            ("halt", {_S.EXECUTING}),
            ("grant_approval", {_S.AWAITING_APPROVAL}),
            ("deny_or_time_out_approval", {_S.AWAITING_APPROVAL}),
            ("resume_from_window", {_S.AWAITING_WINDOW}),
            ("halt_window_wait", {_S.AWAITING_WINDOW}),
        )
        for state in _S
        if state not in legal
    ],
)
def test_every_transition_not_in_the_table_is_refused(mover: str, state: TrajectoryState) -> None:
    """The completeness claim: an unlisted move is an error, not an undefined behaviour."""
    by_mover: dict[str, dict[str, Any]] = {
        "claim_for_planning": {"planning_enabled": True, "lease_acquired": True},
        "claim_for_bypass": {
            "bypass_permitted": True,
            "lease_acquired": True,
            "default_intent_minted": True,
        },
        "approve_plan": {"all_steps_resolved": True, "intents_minted": 1},
        "reject_plan": {},
        "complete": {"all_steps_succeeded": True},
        "halt": {"cause": "x"},
        "grant_approval": {
            "approver_has_scope": True,
            "request_pending": True,
            "intents_minted": 1,
        },
        "deny_or_time_out_approval": {},
        "resume_from_window": {
            "wait": _wait(),
            "ceiling_admits": True,
            "window_wait_max_days": 3,
            "lease_acquired": True,
        },
        "halt_window_wait": {"wait": _wait(days=3), "window_wait_max_days": 3, "cause": "x"},
    }
    arguments = by_mover[mover]
    function: Callable[..., TransitionOutcome] = globals()[mover]
    with pytest.raises(IllegalTransitionError):
        function(state, **arguments)


# --------------------------------------------------------------------------------------------
# The window clock and the declaration
# --------------------------------------------------------------------------------------------


def test_a_window_wait_refuses_a_state_that_cannot_park_or_a_clock_it_cannot_trust() -> None:
    with pytest.raises(ValidationError, match="can park in awaiting_window"):
        WindowWait(parked_from=_S.QUEUED, next_edge_at=_EDGE)
    with pytest.raises(ValidationError, match="timezone-aware"):
        WindowWait(parked_from=_S.EXECUTING, next_edge_at=datetime(2026, 9, 3))  # noqa: DTZ001
    with pytest.raises(ValidationError, match="days_waited"):
        WindowWait(parked_from=_S.EXECUTING, next_edge_at=_EDGE, days_waited=-1)


def test_the_declaration_refuses_a_budget_that_could_never_permit_a_turn(
    declaration: TrajectoryDeclaration,
) -> None:
    with pytest.raises(ValidationError, match="trajectory_id"):
        dataclasses.replace(declaration, trajectory_id="  ")
    with pytest.raises(ValidationError, match="token_budget"):
        dataclasses.replace(declaration, token_budget=0)
    with pytest.raises(ValidationError, match="max_turns"):
        dataclasses.replace(declaration, max_turns=0)
    with pytest.raises(ValidationError, match="money_budget"):
        dataclasses.replace(declaration, money_budget=Money(currency="USD", nanos=0))


def test_the_declaration_does_not_default_the_classification() -> None:
    """ADR-0046's default lives at the API edge; a default chosen twice can disagree with itself."""
    field = TrajectoryDeclaration.__dataclass_fields__["classification"]
    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING


# --------------------------------------------------------------------------------------------
# Event bodies
# --------------------------------------------------------------------------------------------


def test_the_trajectory_event_bodies_carry_ids_categories_and_numbers() -> None:
    bodies: list[EventBody] = [
        TrajectoryCreated(
            trajectory_id="tr1",
            classification=DataClassification.INTERNAL,
            tool_allowlist=("read_file",),
            token_budget=100,
            bypass_planning=True,
        ),
        TrajectoryClaimed(
            trajectory_id="tr1", state=_S.EXECUTING, worker_id="w1", lease_expires_at=_EDGE
        ),
        TrajectoryCompleted(trajectory_id="tr1", step_count=2, turn_count=5),
        TrajectoryHalted(trajectory_id="tr1", cause="tier_violation"),
        TrajectoryFailed(trajectory_id="tr1", cause="crash"),
        TrajectoryCancelled(trajectory_id="tr1", cancelled_from=_S.EXECUTING),
        BudgetWindowWait(
            trajectory_id="tr1",
            parked_from=_S.EXECUTING,
            next_edge_at=_EDGE,
            days_waited=1,
            window_wait_max_days=3,
        ),
        TrajectoryResumed(trajectory_id="tr1", resumed_to=_S.EXECUTING, days_waited=1),
        TrajectoryRecovered(trajectory_id="tr1", recovered_from=_S.EXECUTING, outcome="resumed"),
    ]
    for body in bodies:
        assert isinstance(body.event_type, EventType)
        assert body.as_canonical()["trajectory_id"] == "tr1"


def test_the_window_wait_body_carries_the_whole_clock() -> None:
    """A reader must not need a second row to know when the trajectory will try again."""
    body = BudgetWindowWait(
        trajectory_id="tr1",
        parked_from=_S.PLANNING,
        next_edge_at=_EDGE,
        days_waited=2,
        window_wait_max_days=3,
    )
    canonical = body.as_canonical()
    assert canonical["parked_from"] == "planning"
    assert canonical["next_edge_at"] == _EDGE.isoformat()
    assert canonical["days_waited"] == 2
    assert canonical["window_wait_max_days"] == 3


def test_the_transition_table_has_no_duplicate_source_target_pairs_per_label() -> None:
    """A label with two identical edges would make ``_row`` ambiguous rather than wrong."""
    seen = {(t.label, t.source, t.target) for t in TRANSITIONS}
    assert len(seen) == len(TRANSITIONS)
