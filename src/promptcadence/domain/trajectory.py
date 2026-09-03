"""promptcadence.domain.trajectory — the states, the T1-T17 table, and the guards behind them.

Lifecycle §8.2 is normative and this module is its transcription, with one property added that
prose cannot carry: **no transition exists that the table does not list.** Every legal move is a
named function whose guard column is implemented rather than summarised, and everything else is
:class:`~promptcadence.domain.errors.IllegalTransitionError`.

Terminal states are absorbing. That is asserted here (``__post_init__`` refuses a transition out
of one) and again as a property test over the enum rather than over five hand-written cases, so a
sixth terminal state added later is covered the day it appears rather than the day someone
remembers.

Two subtleties worth stating where the code is:

* **``awaiting_window`` holds no lease** and its clock is a persisted value, not process state
  (§8.3). A parked trajectory survives a restart with its parked-from state, its next UTC-day edge
  and its day count intact, which is the whole reason those three are fields of
  :class:`WindowWait` rather than variables in a worker.
* **Turn-loop activity is not a transition.** Turns, tool calls, debits, egress verdicts and
  compactions all happen inside ``executing``; ``executing`` is one state, not many, and adding a
  ``running_tool`` state would multiply the recovery table for no governance gain.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Final

from baseaicore import DataClassification, Money, ValidationError

from promptcadence.domain.errors import IllegalTransitionError
from promptcadence.domain.events import EventType

__all__ = [
    "TRANSITIONS",
    "BudgetWindowWait",
    "TrajectoryCancelled",
    "TrajectoryClaimed",
    "TrajectoryCompleted",
    "TrajectoryCreated",
    "TrajectoryDeclaration",
    "TrajectoryFailed",
    "TrajectoryHalted",
    "TrajectoryRecovered",
    "TrajectoryResumed",
    "TrajectoryState",
    "Transition",
    "TransitionOutcome",
    "WindowWait",
    "approve_plan",
    "cancel",
    "claim_for_bypass",
    "claim_for_planning",
    "complete",
    "create",
    "deny_or_time_out_approval",
    "fail",
    "grant_approval",
    "halt",
    "halt_window_wait",
    "park_for_window",
    "reject_plan",
    "request_approval",
    "resume_from_window",
    "transitions_from",
]


class TrajectoryState(StrEnum):
    """Every state a trajectory can be in (lifecycle §8.1).

    ``is_terminal`` and ``holds_lease`` are properties rather than lookup tables kept elsewhere,
    so a new member cannot be added without answering both questions at the point of declaration.
    """

    QUEUED = "queued"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_WINDOW = "awaiting_window"
    EXECUTING = "executing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    HALTED = "halted"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether the state is absorbing: no transition leaves it, ever."""
        return self in _TERMINAL

    @property
    def holds_lease(self) -> bool:
        """Whether a worker holds a lease in this state, and therefore whether recovery applies.

        Only ``planning`` and ``executing``. ``awaiting_window`` deliberately does not: a
        trajectory parked for up to ``window_wait_max_days`` holding a lease would either need a
        keeper renewing it for days or would expire into recovery every minute (§8.3).
        """
        return self in _LEASE_HOLDING


_TERMINAL: Final[frozenset[TrajectoryState]] = frozenset(
    {
        TrajectoryState.COMPLETED,
        TrajectoryState.REJECTED,
        TrajectoryState.HALTED,
        TrajectoryState.FAILED,
        TrajectoryState.CANCELLED,
    }
)
_LEASE_HOLDING: Final[frozenset[TrajectoryState]] = frozenset(
    {TrajectoryState.PLANNING, TrajectoryState.EXECUTING}
)
_PARKABLE: Final[frozenset[TrajectoryState]] = frozenset(
    {TrajectoryState.PLANNING, TrajectoryState.EXECUTING}
)


@dataclass(frozen=True, slots=True)
class TrajectoryDeclaration:
    """What the caller declared when submitting the trajectory — the outer envelope of everything.

    Every :class:`~promptcadence.domain.intent.ExecutionIntent` is bounded by this: an intent's
    ``max_classification`` is at or below ``classification``, its ``approved_tools`` is a subset of
    ``tool_allowlist``, and its budgets are slices of these. The allowlist in particular is the
    **caller's**, not the model's, which is why a tool outside it is never re-approvable
    (lifecycle §5).

    Attributes:
        trajectory_id: The trajectory this declares.
        classification: The caller's declaration, defaulting to ``CONFIDENTIAL`` at the API edge
            (ADR-0046 rule 3). Nothing here defaults it: a default chosen twice is a default that
            can disagree with itself.
        tool_allowlist: Every tool the caller permits, for every step.
        token_budget: The trajectory's token ceiling — the universal brake (ADR-0030).
        money_budget: Its money ceiling, or ``None`` when only tokens bind. Never ``Money.zero()``
            as a stand-in for "no ceiling": zero is a ceiling that refuses everything.
        max_turns: ``execution.max_steps`` — what the bypass path takes as its default intent's
            ``max_turns`` (ADR-0056 §2).
        project: The ``[budget.projects.<name>]`` this work is tagged with, or ``None``.

    Raises:
        ValidationError: If the id is empty, or either budget is not positive.
    """

    trajectory_id: str
    classification: DataClassification
    tool_allowlist: frozenset[str]
    token_budget: int
    money_budget: Money | None = field(default=None, kw_only=True)
    max_turns: int = field(default=20, kw_only=True)
    project: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Refuse an unidentified trajectory or a budget that could never permit a turn."""
        if not self.trajectory_id.strip():
            message = "trajectory_id must not be empty"
            raise ValidationError(message, details={"field": "trajectory_id"})
        if self.token_budget < 1:
            message = f"token_budget must be positive, got {self.token_budget}"
            raise ValidationError(message, details={"field": "token_budget"})
        if self.max_turns < 1:
            message = f"max_turns must be positive, got {self.max_turns}"
            raise ValidationError(message, details={"field": "max_turns"})
        if self.money_budget is not None and self.money_budget.nanos <= 0:
            message = (
                "money_budget must be positive when set; a zero ceiling refuses everything, and "
                "'no money ceiling' is None (ADR-0016)"
            )
            raise ValidationError(message, details={"field": "money_budget"})


@dataclass(frozen=True, slots=True)
class WindowWait:
    """The persisted clock of an ``awaiting_window`` park (lifecycle §8.1, §8.3).

    Attributes:
        parked_from: The state to return to when the day rolls. Only ``planning`` or ``executing``
            can park, so only those can be returned to — T16's target is data, not a guess.
        next_edge_at: The next UTC-day edge, persisted rather than computed from process start, so
            a restart resumes on the same edge.
        days_waited: How many day edges have already passed without the ceiling admitting.

    Raises:
        ValidationError: If ``parked_from`` is not a parkable state, ``next_edge_at`` is naive, or
            ``days_waited`` is negative.
    """

    parked_from: TrajectoryState
    next_edge_at: datetime
    days_waited: int = 0

    def __post_init__(self) -> None:
        """Refuse a park from a state that cannot park, or a clock that cannot be trusted."""
        if self.parked_from not in _PARKABLE:
            message = (
                f"only {sorted(s.value for s in _PARKABLE)} can park in awaiting_window, "
                f"got {self.parked_from.value}"
            )
            raise ValidationError(message, details={"field": "parked_from"})
        if self.next_edge_at.tzinfo is None or self.next_edge_at.utcoffset() is None:
            message = "next_edge_at must be timezone-aware; the window edge is a UTC-day boundary"
            raise ValidationError(message, details={"field": "next_edge_at"})
        if self.days_waited < 0:
            message = f"days_waited must not be negative, got {self.days_waited}"
            raise ValidationError(message, details={"field": "days_waited"})


@dataclass(frozen=True, slots=True)
class Transition:
    """One row of lifecycle §8.2.

    Attributes:
        label: The row's label, ``"T1"`` through ``"T17"``.
        source: The state moved from, or ``None`` for T1, which creates.
        target: The state moved to.
        trigger: What causes it, in the table's own words.
        guard: The guard column, in the table's own words. The *implementation* is the function
            that returns this row; this string is what the record and the documentation show.
        events: The events written in the same transaction (ADR-0044), in order.

    Raises:
        ValidationError: If the source is terminal. Terminal states are absorbing, and a table row
            leaving one would be a lie the property test could only find afterwards.
    """

    label: str
    source: TrajectoryState | None
    target: TrajectoryState
    trigger: str
    guard: str
    events: tuple[EventType, ...]

    def __post_init__(self) -> None:
        """Refuse a row that leaves a terminal state."""
        if self.source is not None and self.source.is_terminal:
            message = f"{self.label} leaves terminal state {self.source.value}"
            raise ValidationError(message, details={"field": "source", "label": self.label})
        if not self.events:
            message = f"{self.label} writes no event; a state change and its event are one write"
            raise ValidationError(message, details={"field": "events", "label": self.label})


@dataclass(frozen=True, slots=True)
class TransitionOutcome:
    """The result of a permitted transition: the new state, its row, and what to write with it."""

    state: TrajectoryState
    transition: Transition

    @property
    def events(self) -> tuple[EventType, ...]:
        """The events that commit in the same write as the state change (ADR-0044)."""
        return self.transition.events


_S = TrajectoryState
_E = EventType

TRANSITIONS: Final[tuple[Transition, ...]] = (
    Transition(
        label="T1",
        source=None,
        target=_S.QUEUED,
        trigger="POST /trajectories",
        guard="Request validates (classification, tools subset of registry, caps)",
        events=(_E.TRAJECTORY_CREATED,),
    ),
    Transition(
        label="T2",
        source=_S.QUEUED,
        target=_S.PLANNING,
        trigger="Worker claim",
        guard="Planning enabled for this trajectory; lease acquired",
        events=(_E.TRAJECTORY_CLAIMED,),
    ),
    Transition(
        label="T3",
        source=_S.QUEUED,
        target=_S.EXECUTING,
        trigger="Worker claim, bypass",
        guard="Bypass permitted; lease acquired; default intent minted",
        events=(_E.TRAJECTORY_CLAIMED, _E.INTENT_MINTED),
    ),
    Transition(
        label="T4",
        source=_S.PLANNING,
        target=_S.EXECUTING,
        trigger="Auto approval",
        guard="Every step approved/redlined; intents minted",
        events=(_E.PLAN_APPROVED, _E.INTENT_MINTED),
    ),
    Transition(
        label="T5",
        source=_S.PLANNING,
        target=_S.AWAITING_APPROVAL,
        trigger="Manual mode, or a hybrid-gated step with no ungated work ready",
        guard="approval_request created",
        events=(_E.APPROVAL_REQUESTED,),
    ),
    Transition(
        label="T6",
        source=_S.PLANNING,
        target=_S.REJECTED,
        trigger="Trajectory-level verdict rejected",
        guard="-",
        events=(_E.PLAN_REJECTED,),
    ),
    Transition(
        label="T7",
        source=_S.PLANNING,
        target=_S.FAILED,
        trigger="Plan draft failed after the corrective budget",
        guard="-",
        events=(_E.TRAJECTORY_FAILED,),
    ),
    Transition(
        label="T8",
        source=_S.AWAITING_APPROVAL,
        target=_S.EXECUTING,
        trigger="Operator/API approve",
        guard="approve scope; request pending; intents (re-)minted",
        events=(_E.APPROVAL_GRANTED, _E.INTENT_MINTED),
    ),
    Transition(
        label="T9",
        source=_S.AWAITING_APPROVAL,
        target=_S.HALTED,
        trigger="Deny, or request_timeout_hours elapsed",
        guard="-",
        events=(_E.APPROVAL_DENIED, _E.TRAJECTORY_HALTED),
    ),
    Transition(
        label="T10",
        source=_S.EXECUTING,
        target=_S.AWAITING_APPROVAL,
        trigger=(
            "A hybrid-gated step becomes ready; a drift needs scoped re-approval; a ceiling raise "
            "is requested"
        ),
        guard="approval_request created; in-flight turns finish first",
        events=(_E.APPROVAL_REQUESTED,),
    ),
    Transition(
        label="T11",
        source=_S.EXECUTING,
        target=_S.COMPLETED,
        trigger="All steps terminal-success / declared finish",
        guard="-",
        events=(_E.TRAJECTORY_COMPLETED,),
    ),
    Transition(
        label="T12",
        source=_S.EXECUTING,
        target=_S.HALTED,
        trigger=(
            "tier_violation; re-approval denied; deviation limit; budget exhaustion (halt "
            "policy); egress denial with no permitted tier"
        ),
        guard="Cause recorded",
        events=(_E.TRAJECTORY_HALTED,),
    ),
    Transition(
        label="T13",
        source=_S.EXECUTING,
        target=_S.FAILED,
        trigger="Unrecoverable error",
        guard="Cause recorded",
        events=(_E.TRAJECTORY_FAILED,),
    ),
    *(
        Transition(
            label="T14",
            source=source,
            target=_S.CANCELLED,
            trigger="POST /cancel or CLI",
            guard=(
                "From executing: honoured at the next turn boundary; any in-flight LoadCoach job "
                "cancelled"
            ),
            events=(_E.TRAJECTORY_CANCELLED,),
        )
        for source in (
            _S.QUEUED,
            _S.PLANNING,
            _S.AWAITING_APPROVAL,
            _S.AWAITING_WINDOW,
            _S.EXECUTING,
        )
    ),
    *(
        Transition(
            label="T15",
            source=source,
            target=_S.AWAITING_WINDOW,
            trigger=(
                "The per-day ceiling would be exceeded by the plan or by the next step, and "
                'on_daily_exhausted = "window"'
            ),
            guard=(
                "Parked-from state and the next UTC-day edge persisted; in-flight turns finish "
                "first; lease released"
            ),
            events=(_E.BUDGET_WINDOW_WAIT,),
        )
        for source in (_S.PLANNING, _S.EXECUTING)
    ),
    *(
        Transition(
            label="T16",
            source=_S.AWAITING_WINDOW,
            target=target,
            trigger="The UTC day rolls",
            guard=(
                "The per-day ceiling now admits the plan or step; window_wait_max_days not "
                "exceeded; lease re-acquired"
            ),
            events=(_E.TRAJECTORY_RESUMED,),
        )
        for target in (_S.PLANNING, _S.EXECUTING)
    ),
    Transition(
        label="T17",
        source=_S.AWAITING_WINDOW,
        target=_S.HALTED,
        trigger="window_wait_max_days elapsed - the ceiling still refused at that many day edges",
        guard="Cause recorded",
        events=(_E.TRAJECTORY_HALTED,),
    ),
)
"""Lifecycle §8.2, in full. T14, T15 and T16 expand to one row per source or target state."""

_BY_LABEL: Final[Mapping[str, tuple[Transition, ...]]] = {
    label: tuple(t for t in TRANSITIONS if t.label == label)
    for label in dict.fromkeys(t.label for t in TRANSITIONS)
}


def transitions_from(state: TrajectoryState) -> tuple[Transition, ...]:
    """Return every transition the table permits out of a state.

    Args:
        state: The current state.

    Returns:
        The permitted rows, empty for every terminal state — which is the property test's subject
        and the reason it can be written over the enum rather than over five named cases.
    """
    return tuple(t for t in TRANSITIONS if t.source is state)


def _row(label: str, *, source: TrajectoryState | None, target: TrajectoryState) -> Transition:
    """Return the one table row with that label, source and target."""
    for transition in _BY_LABEL[label]:
        if transition.source is source and transition.target is target:
            return transition
    message = f"no transition {label} from {source} to {target}"
    raise IllegalTransitionError(message, details={"label": label})


def _require(current: TrajectoryState, expected: TrajectoryState, label: str) -> None:
    """Refuse a transition attempted from the wrong state."""
    if current is not expected:
        message = f"{label} moves from {expected.value}, but the trajectory is {current.value}" + (
            " (terminal)" if current.is_terminal else ""
        )
        raise IllegalTransitionError(
            message, details={"label": label, "from": current.value, "expected": expected.value}
        )


def _guard(passed: bool, label: str, guard: str, current: TrajectoryState) -> None:
    """Refuse a transition whose guard did not hold, naming the guard rather than the row."""
    if not passed:
        message = f"{label} refused: {guard}"
        raise IllegalTransitionError(
            message, details={"label": label, "from": current.value, "guard": guard}
        )


def create() -> TransitionOutcome:
    """T1 - accept a validated request and queue it.

    Returns:
        The outcome placing the trajectory in ``queued`` and writing ``trajectory.created``.
    """
    return TransitionOutcome(
        state=TrajectoryState.QUEUED, transition=_row("T1", source=None, target=_S.QUEUED)
    )


def claim_for_planning(
    current: TrajectoryState, *, planning_enabled: bool, lease_acquired: bool
) -> TransitionOutcome:
    """T2 - a worker claims a queued trajectory for planning.

    Args:
        current: The current state; must be ``queued``.
        planning_enabled: Whether planning is enabled for this trajectory, after the config
            default and any permitted per-request override.
        lease_acquired: Whether the worker actually holds the lease. Passed rather than assumed:
            claiming without the lease is how two workers plan the same trajectory.

    Returns:
        The outcome moving to ``planning`` and writing ``trajectory.claimed``.

    Raises:
        IllegalTransitionError: If the trajectory is not ``queued``, planning is disabled for it
            (T3 is the row for that), or the lease was not acquired.
    """
    _require(current, _S.QUEUED, "T2")
    _guard(planning_enabled, "T2", "Planning enabled for this trajectory", current)
    _guard(lease_acquired, "T2", "lease acquired", current)
    return TransitionOutcome(
        state=_S.PLANNING, transition=_row("T2", source=_S.QUEUED, target=_S.PLANNING)
    )


def claim_for_bypass(
    current: TrajectoryState,
    *,
    bypass_permitted: bool,
    lease_acquired: bool,
    default_intent_minted: bool,
) -> TransitionOutcome:
    """T3 - a worker claims a queued trajectory in bypass mode and begins executing.

    ``default_intent_minted`` is a guard rather than a courtesy: ADR-0048's invariance is
    structural only if there is no path into ``executing`` without an intent, and this is that
    path's half of the promise. The other half is that a turn cannot be constructed without one
    (:class:`~promptcadence.domain.intent.TurnProvenance`).

    Args:
        current: The current state; must be ``queued``.
        bypass_permitted: Whether the bypass is permitted for this trajectory.
        lease_acquired: Whether the worker holds the lease.
        default_intent_minted: Whether the default ``ExecutionIntent`` has been minted.

    Returns:
        The outcome moving to ``executing`` and writing ``trajectory.claimed`` plus
        ``intent.minted`` in the same transaction.

    Raises:
        IllegalTransitionError: If the trajectory is not ``queued``, the bypass is not permitted,
            the lease was not acquired, or no default intent was minted.
    """
    _require(current, _S.QUEUED, "T3")
    _guard(bypass_permitted, "T3", "Bypass permitted", current)
    _guard(lease_acquired, "T3", "lease acquired", current)
    _guard(default_intent_minted, "T3", "default intent minted", current)
    return TransitionOutcome(
        state=_S.EXECUTING, transition=_row("T3", source=_S.QUEUED, target=_S.EXECUTING)
    )


def approve_plan(
    current: TrajectoryState, *, all_steps_resolved: bool, intents_minted: int
) -> TransitionOutcome:
    """T4 - every step was approved or redlined automatically, and its intent minted.

    Args:
        current: The current state; must be ``planning``.
        all_steps_resolved: Whether every step's verdict is ``approved`` or ``redlined``.
        intents_minted: How many intents were minted. Must be at least one: an approved plan that
            minted nothing would move into ``executing`` with no envelope for any turn.

    Returns:
        The outcome moving to ``executing`` and writing ``plan.approved`` with ``intent.minted``
        once per approved step.

    Raises:
        IllegalTransitionError: If the trajectory is not ``planning``, a step is unresolved, or no
            intent was minted.
    """
    _require(current, _S.PLANNING, "T4")
    _guard(all_steps_resolved, "T4", "Every step approved/redlined", current)
    _guard(intents_minted >= 1, "T4", "intents minted", current)
    return TransitionOutcome(
        state=_S.EXECUTING, transition=_row("T4", source=_S.PLANNING, target=_S.EXECUTING)
    )


def request_approval(current: TrajectoryState, *, request_created: bool) -> TransitionOutcome:
    """T5 and T10 - park on exactly one pending approval request.

    One function for two rows because the *state* answer is identical and the difference is which
    row the record shows: from ``planning`` it is a plan-level or manual-mode hold (T5), from
    ``executing`` it is a gated step, a scoped re-approval or a ceiling raise (T10).

    Args:
        current: The current state; must be ``planning`` or ``executing``.
        request_created: Whether the ``approval_request`` row exists. A trajectory parked with no
            request is one nobody can release (ADR-0049 rule 6).

    Returns:
        The outcome moving to ``awaiting_approval`` and writing ``approval.requested``.

    Raises:
        IllegalTransitionError: If the state is neither ``planning`` nor ``executing``, or no
            request was created.
    """
    if current not in {_S.PLANNING, _S.EXECUTING}:
        message = f"approval may be requested from planning or executing, not {current.value}" + (
            " (terminal)" if current.is_terminal else ""
        )
        raise IllegalTransitionError(message, details={"from": current.value})
    label = "T5" if current is _S.PLANNING else "T10"
    _guard(request_created, label, "approval_request created", current)
    return TransitionOutcome(
        state=_S.AWAITING_APPROVAL,
        transition=_row(label, source=current, target=_S.AWAITING_APPROVAL),
    )


def reject_plan(current: TrajectoryState) -> TransitionOutcome:
    """T6 - the trajectory-level verdict was ``rejected``; nothing executed.

    Args:
        current: The current state; must be ``planning``.

    Returns:
        The outcome moving to ``rejected`` and writing ``plan.rejected``.

    Raises:
        IllegalTransitionError: If the trajectory is not ``planning``.
    """
    _require(current, _S.PLANNING, "T6")
    return TransitionOutcome(
        state=_S.REJECTED, transition=_row("T6", source=_S.PLANNING, target=_S.REJECTED)
    )


def grant_approval(
    current: TrajectoryState,
    *,
    approver_has_scope: bool,
    request_pending: bool,
    intents_minted: int,
) -> TransitionOutcome:
    """T8 - an operator or API grant releases the hold, minting the intents it authorises.

    Args:
        current: The current state; must be ``awaiting_approval``.
        approver_has_scope: Whether the approving identity holds the ``approve`` scope. Separate
            from ``write`` so the identity that submits work cannot approve its own egress
            (ADR-0049 rule 2).
        request_pending: Whether the request is still pending; resolution is idempotent per
            request, so a second grant must not move the trajectory again.
        intents_minted: How many intents the grant minted. Approval's output *is* the minting
            (ADR-0049 rule 5), so a grant that mints nothing has authorised nothing.

    Returns:
        The outcome moving to ``executing`` and writing ``approval.granted`` with
        ``intent.minted``.

    Raises:
        IllegalTransitionError: If the trajectory is not ``awaiting_approval``, the approver lacks
            the scope, the request is not pending, or nothing was minted.
    """
    _require(current, _S.AWAITING_APPROVAL, "T8")
    _guard(approver_has_scope, "T8", "approve scope", current)
    _guard(request_pending, "T8", "request pending", current)
    _guard(intents_minted >= 1, "T8", "intents (re-)minted", current)
    return TransitionOutcome(
        state=_S.EXECUTING, transition=_row("T8", source=_S.AWAITING_APPROVAL, target=_S.EXECUTING)
    )


def deny_or_time_out_approval(current: TrajectoryState) -> TransitionOutcome:
    """T9 - the request was denied, or ``request_timeout_hours`` elapsed.

    A timeout is never a grant (ADR-0049 rule 4), so both endings share one row and one target.

    Args:
        current: The current state; must be ``awaiting_approval``.

    Returns:
        The outcome moving to ``halted`` and writing ``approval.denied`` with
        ``trajectory.halted``.

    Raises:
        IllegalTransitionError: If the trajectory is not ``awaiting_approval``.
    """
    _require(current, _S.AWAITING_APPROVAL, "T9")
    return TransitionOutcome(
        state=_S.HALTED, transition=_row("T9", source=_S.AWAITING_APPROVAL, target=_S.HALTED)
    )


def complete(current: TrajectoryState, *, all_steps_succeeded: bool) -> TransitionOutcome:
    """T11 - every step reached declared success, or the bypass loop declared finish.

    Args:
        current: The current state; must be ``executing``.
        all_steps_succeeded: Whether every step reached a *declared* success. A model never
            decides control flow: this comes from a declared ``finish_reason`` or a
            schema-validated result, never from the text saying it is done (spec §11 contract 6).

    Returns:
        The outcome moving to ``completed`` and writing ``trajectory.completed``.

    Raises:
        IllegalTransitionError: If the trajectory is not ``executing``, or a step has not
            succeeded.
    """
    _require(current, _S.EXECUTING, "T11")
    _guard(all_steps_succeeded, "T11", "All steps terminal-success / declared finish", current)
    return TransitionOutcome(
        state=_S.COMPLETED, transition=_row("T11", source=_S.EXECUTING, target=_S.COMPLETED)
    )


def halt(current: TrajectoryState, *, cause: str) -> TransitionOutcome:
    """T12 - governance stopped the trajectory.

    Args:
        current: The current state; must be ``executing``.
        cause: Why it halted, recorded verbatim and printed verbatim by
            ``promptcadence trajectory show``. Every halt names its cause (spec §13); an empty
            cause is refused rather than stored, because "halted" with no reason is the failure
            that makes a governance record useless.

    Returns:
        The outcome moving to ``halted`` and writing ``trajectory.halted``.

    Raises:
        IllegalTransitionError: If the trajectory is not ``executing``, or the cause is empty.
    """
    _require(current, _S.EXECUTING, "T12")
    _guard(bool(cause.strip()), "T12", "Cause recorded", current)
    return TransitionOutcome(
        state=_S.HALTED, transition=_row("T12", source=_S.EXECUTING, target=_S.HALTED)
    )


def fail(current: TrajectoryState, *, cause: str) -> TransitionOutcome:
    """T7 and T13 - an unrecoverable error stopped the trajectory.

    From ``planning`` this is a draft that failed after the corrective budget (T7); from
    ``executing`` it is any unrecoverable error (T13).

    Args:
        current: The current state; must be ``planning`` or ``executing``.
        cause: Why it failed, recorded on the row.

    Returns:
        The outcome moving to ``failed`` and writing ``trajectory.failed``.

    Raises:
        IllegalTransitionError: If the state is neither ``planning`` nor ``executing``, or the
            cause is empty.
    """
    if current not in {_S.PLANNING, _S.EXECUTING}:
        message = f"failure is recorded from planning or executing, not {current.value}" + (
            " (terminal)" if current.is_terminal else ""
        )
        raise IllegalTransitionError(message, details={"from": current.value})
    label = "T7" if current is _S.PLANNING else "T13"
    _guard(bool(cause.strip()), label, "Cause recorded", current)
    return TransitionOutcome(
        state=_S.FAILED, transition=_row(label, source=current, target=_S.FAILED)
    )


def cancel(current: TrajectoryState, *, at_turn_boundary: bool = True) -> TransitionOutcome:
    """T14 - the caller cancelled a non-terminal trajectory.

    Args:
        current: The current state; must be non-terminal.
        at_turn_boundary: Whether a cancel from ``executing`` is being honoured at a turn
            boundary. Cancelling mid-turn would leave a committed LoadCoach job with no turn row
            to reconcile it against, which is exactly the orphan recovery exists to avoid.

    Returns:
        The outcome moving to ``cancelled`` and writing ``trajectory.cancelled``.

    Raises:
        IllegalTransitionError: If the trajectory is already terminal — a completed trajectory is
            not cancellable, and a service maps this onto ``TRAJECTORY_NOT_CANCELLABLE`` — or a
            cancel from ``executing`` is not at a turn boundary.
    """
    if current.is_terminal:
        message = f"a {current.value} trajectory is terminal and cannot be cancelled"
        raise IllegalTransitionError(message, details={"label": "T14", "from": current.value})
    if current is _S.EXECUTING:
        _guard(at_turn_boundary, "T14", "honoured at the next turn boundary", current)
    return TransitionOutcome(
        state=_S.CANCELLED, transition=_row("T14", source=current, target=_S.CANCELLED)
    )


def park_for_window(
    current: TrajectoryState, *, wait: WindowWait, lease_released: bool, turns_settled: bool
) -> TransitionOutcome:
    """T15 - the per-day ceiling refused, and ``on_daily_exhausted = "window"`` parks rather than
    halts.

    Args:
        current: The current state; must be ``planning`` or ``executing``.
        wait: The persisted clock. Its ``parked_from`` must be the state being left, so the record
            says where T16 will return to rather than inferring it later.
        lease_released: Whether the lease has been released. ``awaiting_window`` holds none (§8.1);
            parking while still holding one would keep a worker slot for days.
        turns_settled: Whether in-flight turns have finished. A park mid-turn would strand the
            same in-flight work a cancel would.

    Returns:
        The outcome moving to ``awaiting_window`` and writing ``budget.window_wait``.

    Raises:
        IllegalTransitionError: If the state cannot park, ``wait.parked_from`` disagrees with it,
            the lease was not released, or turns are still in flight.
    """
    if current not in _PARKABLE:
        message = f"only planning or executing park in awaiting_window, not {current.value}" + (
            " (terminal)" if current.is_terminal else ""
        )
        raise IllegalTransitionError(message, details={"label": "T15", "from": current.value})
    _guard(wait.parked_from is current, "T15", "Parked-from state persisted", current)
    _guard(turns_settled, "T15", "in-flight turns finish first", current)
    _guard(lease_released, "T15", "lease released", current)
    return TransitionOutcome(
        state=_S.AWAITING_WINDOW,
        transition=_row("T15", source=current, target=_S.AWAITING_WINDOW),
    )


def resume_from_window(
    current: TrajectoryState,
    *,
    wait: WindowWait,
    ceiling_admits: bool,
    window_wait_max_days: int,
    lease_acquired: bool,
) -> TransitionOutcome:
    """T16 - the UTC day rolled and the per-day ceiling now admits the work.

    Args:
        current: The current state; must be ``awaiting_window``.
        wait: The persisted clock, whose ``parked_from`` is the state returned to.
        ceiling_admits: Whether the per-day ceiling now admits the plan or the next step.
        window_wait_max_days: ``budget.window_wait_max_days``.
        lease_acquired: Whether the worker re-acquired the lease.

    Returns:
        The outcome moving back to ``wait.parked_from`` and writing ``trajectory.resumed``.

    Raises:
        IllegalTransitionError: If the trajectory is not ``awaiting_window``, the ceiling still
            refuses, the wait has run past ``window_wait_max_days`` (T17 is that row), or the
            lease was not re-acquired.
    """
    _require(current, _S.AWAITING_WINDOW, "T16")
    _guard(ceiling_admits, "T16", "The per-day ceiling now admits the plan or step", current)
    _guard(
        wait.days_waited < window_wait_max_days,
        "T16",
        "window_wait_max_days not exceeded",
        current,
    )
    _guard(lease_acquired, "T16", "lease re-acquired", current)
    return TransitionOutcome(
        state=wait.parked_from,
        transition=_row("T16", source=_S.AWAITING_WINDOW, target=wait.parked_from),
    )


def halt_window_wait(
    current: TrajectoryState, *, wait: WindowWait, window_wait_max_days: int, cause: str
) -> TransitionOutcome:
    """T17 - the ceiling still refused after ``window_wait_max_days`` day edges.

    Args:
        current: The current state; must be ``awaiting_window``.
        wait: The persisted clock.
        window_wait_max_days: ``budget.window_wait_max_days``.
        cause: Why it halted, recorded on the row.

    Returns:
        The outcome moving to ``halted`` and writing ``trajectory.halted``.

    Raises:
        IllegalTransitionError: If the trajectory is not ``awaiting_window``, the wait has not yet
            reached the limit — waiting is not over, and halting early would discard work the
            next day edge would have released — or the cause is empty.
    """
    _require(current, _S.AWAITING_WINDOW, "T17")
    _guard(
        wait.days_waited >= window_wait_max_days,
        "T17",
        "window_wait_max_days elapsed",
        current,
    )
    _guard(bool(cause.strip()), "T17", "Cause recorded", current)
    return TransitionOutcome(
        state=_S.HALTED, transition=_row("T17", source=_S.AWAITING_WINDOW, target=_S.HALTED)
    )


@dataclass(frozen=True, slots=True)
class TrajectoryCreated:
    """``trajectory.created`` - T1. Ids, caps and the declaration; never the task text."""

    event_type: ClassVar[EventType] = EventType.TRAJECTORY_CREATED
    trajectory_id: str
    classification: DataClassification
    tool_allowlist: tuple[str, ...]
    token_budget: int
    bypass_planning: bool
    project: str | None = None

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "classification": self.classification.value,
            "tool_allowlist": list(self.tool_allowlist),
            "token_budget": self.token_budget,
            "bypass_planning": self.bypass_planning,
            "project": self.project,
        }


@dataclass(frozen=True, slots=True)
class TrajectoryClaimed:
    """``trajectory.claimed`` - T2 and T3. Which worker took it, and into which state."""

    event_type: ClassVar[EventType] = EventType.TRAJECTORY_CLAIMED
    trajectory_id: str
    state: TrajectoryState
    worker_id: str
    lease_expires_at: datetime

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "state": self.state.value,
            "worker_id": self.worker_id,
            "lease_expires_at": self.lease_expires_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class TrajectoryCompleted:
    """``trajectory.completed`` - T11. Counts, not content."""

    event_type: ClassVar[EventType] = EventType.TRAJECTORY_COMPLETED
    trajectory_id: str
    step_count: int
    turn_count: int

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "step_count": self.step_count,
            "turn_count": self.turn_count,
        }


@dataclass(frozen=True, slots=True)
class TrajectoryHalted:
    """``trajectory.halted`` - T9, T12 and T17. Every halt names its cause (spec §13)."""

    event_type: ClassVar[EventType] = EventType.TRAJECTORY_HALTED
    trajectory_id: str
    cause: str
    error_code: str | None = None

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "cause": self.cause,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class TrajectoryFailed:
    """``trajectory.failed`` - T7 and T13."""

    event_type: ClassVar[EventType] = EventType.TRAJECTORY_FAILED
    trajectory_id: str
    cause: str
    error_code: str | None = None

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "cause": self.cause,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class TrajectoryCancelled:
    """``trajectory.cancelled`` - T14."""

    event_type: ClassVar[EventType] = EventType.TRAJECTORY_CANCELLED
    trajectory_id: str
    cancelled_from: TrajectoryState

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "cancelled_from": self.cancelled_from.value,
        }


@dataclass(frozen=True, slots=True)
class BudgetWindowWait:
    """``budget.window_wait`` - T15. The park's whole clock, so a reader needs no other row."""

    event_type: ClassVar[EventType] = EventType.BUDGET_WINDOW_WAIT
    trajectory_id: str
    parked_from: TrajectoryState
    next_edge_at: datetime
    days_waited: int
    window_wait_max_days: int

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "parked_from": self.parked_from.value,
            "next_edge_at": self.next_edge_at.isoformat(),
            "days_waited": self.days_waited,
            "window_wait_max_days": self.window_wait_max_days,
        }


@dataclass(frozen=True, slots=True)
class TrajectoryResumed:
    """``trajectory.resumed`` - T16."""

    event_type: ClassVar[EventType] = EventType.TRAJECTORY_RESUMED
    trajectory_id: str
    resumed_to: TrajectoryState
    days_waited: int

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "resumed_to": self.resumed_to.value,
            "days_waited": self.days_waited,
        }


@dataclass(frozen=True, slots=True)
class TrajectoryRecovered:
    """``trajectory.recovered`` - lifecycle §8.3's recovery edges, exercised in Phase 3."""

    event_type: ClassVar[EventType] = EventType.TRAJECTORY_RECOVERED
    trajectory_id: str
    recovered_from: TrajectoryState
    outcome: str

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "recovered_from": self.recovered_from.value,
            "outcome": self.outcome,
        }
