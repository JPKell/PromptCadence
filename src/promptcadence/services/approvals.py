"""promptcadence.services.approvals — approval in three modes, and minting as its output.

ADR-0049 rule 5: approval's output is not a verdict on a document, it is the minting of one
immutable :class:`~promptcadence.domain.intent.ExecutionIntent` per approved step, and the three
modes differ only in **who** authorises the minting. This service is where that becomes rows:

* :meth:`ApprovalService.decide_plan` runs P2's pure
  :func:`~promptcadence.domain.policy.evaluate_plan` over a validated plan, records the verdict
  (``plan_approvals``, with the policy version **derived** from
  :attr:`~promptcadence.domain.policy.ApprovalPolicy.version`), and then does what the mode says:
  ``auto`` mints every step (T4); ``manual`` holds the whole plan on one request (T5); ``hybrid``
  mints the ungated steps and leaves the gated ones for the moment they become ready (T4 now, T10
  later — the loop raises that request), or T5 when nothing ungated can start.

* :meth:`ApprovalService.grant` is T8 in every shape a request can take: a held plan, a gated
  step, the bypass path's gated default (whose grant **supersedes** revision 1 with a revision 2
  minted by the approver, so the record holds both the gated envelope and the granted one), a
  scoped re-approval (a superseding revision widened by exactly what the drift asked for), and a
  ceiling raise. Grants are idempotent per request and require the ``approve`` scope, which the
  web layer establishes and this service records as the minting authority.
* :meth:`ApprovalService.deny` and :meth:`ApprovalService.expire` are T9. A timeout is never a
  grant (ADR-0049 rule 4); the clock is the persisted ``expires_at``, read by the worker on every
  pass, so it survives a restart.

A trajectory parks on **exactly one** pending request (ADR-0049 rule 6): every request here is
created in the write that moves the trajectory to ``awaiting_approval``, and a second pending one
for the same trajectory is refused before it can be written.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, cast

from baseaicore import DataClassification, Money, ValidationError, new_id
from baseaicore.timeutil import to_rfc3339
from sqlalchemy import select, update

from promptcadence.domain.deviation import Deviation, DeviationCategory
from promptcadence.domain.errors import (
    ApprovalInvalidStateError,
    ErrorCode,
    TrajectoryNotFoundError,
)
from promptcadence.domain.intent import (
    BYPASS_STEP_ID,
    ExecutionIntent,
    IntentMinted,
    MintedBy,
    MintKind,
    supersede,
)
from promptcadence.domain.plan import Plan, PlanStep
from promptcadence.domain.policy import (
    ApprovalDenied,
    ApprovalGranted,
    ApprovalMode,
    ApprovalRequested,
    PlanApproved,
    PlanRejected,
    PlanVerdict,
    StepEstimate,
    StepOutcome,
    TrajectoryOutcome,
    VerdictReason,
    evaluate_plan,
    gate_reason,
)
from promptcadence.domain.trajectory import (
    TrajectoryHalted,
    TrajectoryState,
    deny_or_time_out_approval,
    grant_approval,
)
from promptcadence.infrastructure.db import models
from promptcadence.services.governance import GovernanceContext, load_context
from promptcadence.services.intents import (
    intent_row,
    live_intents,
    mint_step_intent,
    rebuild_intents,
    recorded_plan,
)
from promptcadence.services.views import view_of

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sqlalchemy import CursorResult
    from sqlalchemy.orm import Session

    from promptcadence.config import Settings
    from promptcadence.services.budget import BudgetService
    from promptcadence.services.database import Database
    from promptcadence.services.estimates import StepEstimator
    from promptcadence.services.events import EventWriter, TrajectoryEventSink
    from promptcadence.services.views import TrajectoryView

__all__ = [
    "ApprovalKind",
    "ApprovalRequestView",
    "ApprovalService",
    "Approver",
    "BudgetRaise",
    "GrantOutcome",
    "PlanDecision",
    "ReapprovalAsk",
    "RequestStatus",
]


class ApprovalKind(StrEnum):
    """What a pending request is asking, and therefore what its grant mints."""

    PLAN = "plan"
    """The whole plan, held in manual mode (T5). A grant mints every step."""

    GATED_STEP = "gated_step"
    """One hybrid-gated step that became ready (T5 or T10). A grant mints that step."""

    BYPASS_GATE = "bypass_gate"
    """The bypass default intent's gate fired at minting (T3 then T10). A grant supersedes it."""

    REAPPROVAL = "reapproval"
    """A drift with the ``scoped_reapproval`` disposition (T10). A grant supersedes, widened."""

    CEILING_RAISE = "ceiling_raise"
    """A ceiling refused the next step under ``on_exhausted = "approval"`` (T10)."""


class RequestStatus(StrEnum):
    """The four states of an ``approval_requests`` row."""

    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"


_RESOLVED: Final = frozenset({RequestStatus.GRANTED, RequestStatus.DENIED, RequestStatus.EXPIRED})


@dataclass(frozen=True, slots=True)
class Approver:
    """The identity a grant or denial is recorded under.

    Attributes:
        token_id: The token row's id, or ``"loopback"`` for the open install — what
            ``minted_by`` records as ``approver:<token_id>``.
        name: The token's name, for the event and the log.
    """

    token_id: str
    name: str


@dataclass(frozen=True, slots=True)
class BudgetRaise:
    """A new per-trajectory ceiling, offered with a grant of a ``ceiling_raise`` request.

    Attributes:
        token_ceiling: The new token ceiling, or ``None`` to keep the current one.
        money_ceiling: The new money ceiling, or ``None`` to keep the current one.
    """

    token_ceiling: int | None = None
    money_ceiling: Money | None = None

    def __post_init__(self) -> None:
        """Refuse a raise that raises nothing, or a non-positive ceiling."""
        if self.token_ceiling is None and self.money_ceiling is None:
            message = "a ceiling raise must name a new token or money ceiling"
            raise ValidationError(message, details={"field": "budget"})
        if self.token_ceiling is not None and self.token_ceiling < 1:
            message = "the new token ceiling must be positive"
            raise ValidationError(message, details={"field": "budget.tokens"})
        if self.money_ceiling is not None and self.money_ceiling.nanos <= 0:
            message = "the new money ceiling must be positive"
            raise ValidationError(message, details={"field": "budget.money"})


@dataclass(frozen=True, slots=True)
class ReapprovalAsk:
    """What a drift asked for, recorded on the request so the grant widens exactly that.

    Built by the loop from the :class:`~promptcadence.domain.deviation.Deviation` at the moment
    it parks (T10) and read back by :meth:`ApprovalService.grant`. One shape for every category,
    with the fields that category does not use left at their defaults.

    Attributes:
        category: The deviation's category.
        step_id: The drifted step — the **only** step the re-approval is scoped to.
        intent_id: The envelope the drift contradicted.
        intent_revision: Its revision at the time.
        tools: For ``undeclared_tool``, the allowlisted tools the intent did not cover.
        next_tier: For ``tier_escalation``, the next admitting tier in the escalation order.
        extend_turns: For ``turn_overrun``, how many turns a grant adds.
        tokens_spent: For ``budget_overrun``, what the step had spent.
        token_slice: For ``budget_overrun``, the slice a grant adds above what was spent.
        money_spent: For ``budget_overrun``, when priced.
        observed_classification: For ``classification_exceeded``, what came back.
    """

    category: DeviationCategory
    step_id: str
    intent_id: str
    intent_revision: int
    tools: tuple[str, ...] = ()
    next_tier: str | None = None
    extend_turns: int | None = None
    tokens_spent: int | None = None
    token_slice: int | None = None
    money_spent: Money | None = None
    observed_classification: DataClassification | None = None

    @classmethod
    def of(
        cls, deviation: Deviation, *, next_tier: str | None, intent: ExecutionIntent
    ) -> ReapprovalAsk:
        """Build the ask from a drift, against the envelope it drifted from."""
        return cls(
            category=deviation.category,
            step_id=intent.step_id,
            intent_id=intent.intent_id,
            intent_revision=intent.revision,
            tools=deviation.tools,
            next_tier=next_tier,
            extend_turns=intent.max_turns if deviation.category is _TURN_OVERRUN else None,
            tokens_spent=deviation.tokens_spent,
            token_slice=intent.token_budget if deviation.category is _BUDGET_OVERRUN else None,
            money_spent=deviation.money_spent,
            observed_classification=deviation.observed_classification,
        )

    def as_json(self) -> dict[str, Any]:
        """The ``detail_json`` form."""
        return {
            "category": self.category.value,
            "step_id": self.step_id,
            "intent_id": self.intent_id,
            "intent_revision": self.intent_revision,
            "tools": list(self.tools),
            "next_tier": self.next_tier,
            "extend_turns": self.extend_turns,
            "tokens_spent": self.tokens_spent,
            "token_slice": self.token_slice,
            "money_spent": self.money_spent.as_canonical() if self.money_spent else None,
            "observed_classification": (
                self.observed_classification.value if self.observed_classification else None
            ),
        }

    @classmethod
    def from_json(cls, document: Mapping[str, Any]) -> ReapprovalAsk:
        """Rebuild the ask from ``detail_json``."""
        money = document.get("money_spent")
        observed = document.get("observed_classification")
        return cls(
            category=DeviationCategory(str(document["category"])),
            step_id=str(document["step_id"]),
            intent_id=str(document["intent_id"]),
            intent_revision=int(document["intent_revision"]),
            tools=tuple(str(tool) for tool in document.get("tools", [])),
            next_tier=document.get("next_tier"),
            extend_turns=document.get("extend_turns"),
            tokens_spent=document.get("tokens_spent"),
            token_slice=document.get("token_slice"),
            money_spent=(
                Money(currency=str(money["currency"]), nanos=int(money["nanos"]))
                if isinstance(money, Mapping)
                else None
            ),
            observed_classification=DataClassification(str(observed)) if observed else None,
        )


_TURN_OVERRUN: Final = DeviationCategory.TURN_OVERRUN
_BUDGET_OVERRUN: Final = DeviationCategory.BUDGET_OVERRUN


@dataclass(frozen=True, slots=True)
class ApprovalRequestView:
    """One ``approval_requests`` row as the API and CLI show it."""

    request_id: str
    trajectory_id: str
    kind: ApprovalKind
    status: RequestStatus
    reason: str
    step_ids: tuple[str, ...]
    detail: Mapping[str, Any] | None
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None
    approver_token_id: str | None
    resolution_reason: str | None

    def age_seconds(self, now: datetime) -> float:
        """How long the request has been waiting, as of ``now``."""
        end = self.resolved_at if self.resolved_at is not None else now
        return max((end - self.created_at).total_seconds(), 0.0)

    def as_json(self, *, now: datetime) -> dict[str, Any]:
        """The API document, with its age as of ``now``."""
        return {
            "request_id": self.request_id,
            "trajectory_id": self.trajectory_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "reason": self.reason,
            "step_ids": list(self.step_ids),
            "detail": dict(self.detail) if self.detail is not None else None,
            "created_at": to_rfc3339(self.created_at),
            "expires_at": to_rfc3339(self.expires_at),
            "resolved_at": to_rfc3339(self.resolved_at) if self.resolved_at else None,
            "approver_token_id": self.approver_token_id,
            "resolution_reason": self.resolution_reason,
            "age_seconds": round(self.age_seconds(now), 3),
        }


@dataclass(frozen=True, slots=True)
class PlanDecision:
    """What :meth:`ApprovalService.decide_plan` decided, for the loop to move the row by.

    Attributes:
        state: ``executing`` (T4), ``awaiting_approval`` (T5) or ``rejected`` (T6).
        verdict: The plan verdict, as recorded.
        minted: The intents minted in this write (T4), in plan order.
        request_id: The request created (T5), or ``None``.
        cause: The verbatim reason for the row (T6's rejection, T5's hold), or ``None``.
        error_code: ``PLAN_REJECTED`` on T6, else ``None``.
    """

    state: TrajectoryState
    verdict: PlanVerdict
    minted: tuple[ExecutionIntent, ...] = ()
    request_id: str | None = None
    cause: str | None = None
    error_code: ErrorCode | None = None


@dataclass(frozen=True, slots=True)
class GrantOutcome:
    """What a grant did."""

    request: ApprovalRequestView
    minted: tuple[ExecutionIntent, ...]
    state: TrajectoryState
    already_resolved: bool = False


@dataclass(frozen=True, slots=True)
class _Widening:
    """The supersession a grant performs, as keyword arguments to ``supersede``."""

    fields: dict[str, Any] = field(default_factory=dict)


class ApprovalService:
    """Verdicts, requests, grants, denials and expiries. One instance per process."""

    __slots__ = (
        "_budget",
        "_clock",
        "_database",
        "_estimator",
        "_ids",
        "_remote_provider",
        "_settings",
        "_sink",
    )

    def __init__(
        self,
        database: Database,
        sink: TrajectoryEventSink,
        settings: Settings,
        *,
        estimator: StepEstimator,
        budget: BudgetService,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str] = new_id,
        loadcoach_has_remote_provider: bool = False,
    ) -> None:
        """Bind the service to the process's handles.

        Args:
            database: The application's database handle.
            sink: The event sink every write goes through.
            settings: The validated configuration.
            estimator: The layered step estimator the verdict's estimates come from.
            budget: The ceilings, for the headroom a plan is judged against.
            clock: The instant source, injected for determinism.
            id_factory: The id source for requests and intents.
            loadcoach_has_remote_provider: See
                :func:`promptcadence.services.governance.tier_policy_of`.
        """
        self._database = database
        self._sink = sink
        self._settings = settings
        self._estimator = estimator
        self._budget = budget
        self._clock = clock
        self._ids = id_factory
        self._remote_provider = loadcoach_has_remote_provider

    # ---- the plan verdict, on the loop's session ---------------------------------------------

    def decide_plan(
        self,
        session: Session,
        events: EventWriter,
        *,
        ctx: GovernanceContext,
        plan: Plan,
        plan_id: str,
        now: datetime,
    ) -> PlanDecision:
        """Render and record the verdict, then mint or hold per the mode.

        Runs on the **loop's** session so the verdict, the intents, the request and the
        transition commit as one write (ADR-0044); the loop performs the trajectory row's
        compare-and-set from the returned state, because only it holds the lease.

        Args:
            session: The loop's session.
            events: The loop's event writer.
            ctx: The trajectory's governance context.
            plan: The validated plan.
            plan_id: The ``plans`` row the verdict is over.
            now: The instant.

        Returns:
            The decision.

        Raises:
            ValidationError: A step's intent could not be minted — a plan that validated cannot
                reach this, so it is a wiring error rather than a governance event.
        """
        view, policy = ctx.view, ctx.approval_policy
        estimates = {step.step_id: self._estimate(step) for step in plan.steps}
        headroom = self._budget.position(view).headroom
        verdict = evaluate_plan(
            plan,
            declaration=ctx.declaration,
            tier_policy=ctx.tier_policy,
            policy=policy,
            estimates=estimates,
            headroom=headroom,
        )
        session.add(
            models.PlanApproval(
                id=self._ids(),
                trajectory_id=view.trajectory_id,
                plan_id=plan_id,
                outcome=verdict.outcome.value,
                approval_policy_version=verdict.approval_policy_version,
                verdict_json=verdict.as_canonical(),
                created_at=now,
            )
        )
        if verdict.outcome is TrajectoryOutcome.REJECTED:
            reasons = tuple((step.step_id, step.reason) for step in verdict.steps)
            events.append(
                view.trajectory_id,
                PlanRejected(
                    trajectory_id=view.trajectory_id,
                    reasons=reasons,
                    approval_policy_version=verdict.approval_policy_version,
                ),
                now=now,
            )
            return PlanDecision(
                state=TrajectoryState.REJECTED,
                verdict=verdict,
                cause=_rejection_cause(verdict),
                error_code=ErrorCode.PLAN_REJECTED,
            )

        mintable = [
            step
            for step in plan.steps
            if not verdict.verdict_for(step.step_id).requires_human_approval
        ]
        if policy.mode is ApprovalMode.MANUAL or not mintable:
            held = plan.steps if policy.mode is ApprovalMode.MANUAL else _first_ready_gated(plan)
            kind = (
                ApprovalKind.PLAN if policy.mode is ApprovalMode.MANUAL else ApprovalKind.GATED_STEP
            )
            first = verdict.verdict_for(held[0].step_id)
            reason = (
                VerdictReason.MANUAL_MODE
                if policy.mode is ApprovalMode.MANUAL
                else (
                    first.reason
                    if first.gate is None
                    else gate_reason(first.gate, mode=policy.mode)
                )
            )
            request_id = self.request(
                session,
                events,
                view=view,
                kind=kind,
                reason=reason,
                step_ids=tuple(step.step_id for step in held),
                detail=None,
                now=now,
            )
            cause = (
                f"the plan is held for a person: approval.mode is {policy.mode.value} "
                f"(request {request_id}, {len(held)} step(s))"
            )
            return PlanDecision(
                state=TrajectoryState.AWAITING_APPROVAL,
                verdict=verdict,
                request_id=request_id,
                cause=cause,
            )

        minted = tuple(
            self._mint_step(
                ctx,
                step,
                verdict,
                minted_by=MintedBy(MintKind.POLICY),
                now=now,
                approval_request_id=None,
            )
            for step in mintable
        )
        for intent in minted:
            session.add(intent_row(intent))
        events.append(
            view.trajectory_id,
            PlanApproved(
                trajectory_id=view.trajectory_id,
                step_count=len(plan.steps),
                redlined_count=sum(
                    1 for step in verdict.steps if step.outcome is StepOutcome.REDLINED
                ),
                approval_policy_version=verdict.approval_policy_version,
            ),
            now=now,
        )
        for intent in minted:
            events.append(view.trajectory_id, IntentMinted.of(intent), now=now)
        return PlanDecision(state=TrajectoryState.EXECUTING, verdict=verdict, minted=minted)

    def _estimate(self, step: PlanStep) -> StepEstimate:
        estimate, _ = self._estimator.estimate(tier=step.tier)
        return estimate

    def _mint_step(
        self,
        ctx: GovernanceContext,
        step: PlanStep,
        verdict: PlanVerdict,
        *,
        minted_by: MintedBy,
        now: datetime,
        approval_request_id: str | None,
    ) -> ExecutionIntent:
        return mint_step_intent(
            intent_id=self._ids(),
            declaration=ctx.declaration,
            step=step,
            verdict=verdict.verdict_for(step.step_id),
            tier_policy=ctx.tier_policy,
            policy=ctx.approval_policy,
            minted_by=minted_by,
            minted_at=now,
            max_turns=self._settings.execution.max_turns_per_step,
            approval_request_id=approval_request_id,
        )

    # ---- requests --------------------------------------------------------------------------

    def request(
        self,
        session: Session,
        events: EventWriter,
        *,
        view: TrajectoryView,
        kind: ApprovalKind,
        reason: VerdictReason,
        step_ids: tuple[str, ...],
        detail: Mapping[str, Any] | None,
        now: datetime,
    ) -> str:
        """Create the one pending request a parked trajectory holds, with its event.

        The caller moves the trajectory row to ``awaiting_approval`` on the same session; this
        writes the row and ``approval.requested`` beside it (ADR-0044).

        Args:
            session: The caller's session.
            events: The caller's event writer.
            view: The trajectory.
            kind: What the request asks.
            reason: Why a person is being asked.
            step_ids: The steps the request is scoped to — every step for a held plan, exactly
                one for a gated step or a re-approval, ``loop`` for the bypass gate.
            detail: The ask, for a re-approval or a ceiling raise.
            now: The instant; ``expires_at`` is ``request_timeout_hours`` after it.

        Returns:
            The request id.

        Raises:
            ApprovalInvalidStateError: A pending request already exists for this trajectory.
                A trajectory parks on exactly one (ADR-0049 rule 6).
        """
        existing = session.execute(
            select(models.ApprovalRequest.id).where(
                models.ApprovalRequest.trajectory_id == view.trajectory_id,
                models.ApprovalRequest.status == RequestStatus.PENDING.value,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ApprovalInvalidStateError(
                f"Trajectory {view.trajectory_id} already holds pending request {existing}; a "
                "trajectory parks on exactly one (ADR-0049 rule 6).",
                details={"trajectory_id": view.trajectory_id, "request_id": existing},
            )
        request_id = self._ids()
        expires = now + timedelta(hours=self._settings.approval.request_timeout_hours)
        session.add(
            models.ApprovalRequest(
                id=request_id,
                trajectory_id=view.trajectory_id,
                status=RequestStatus.PENDING.value,
                reason=reason.value,
                step_ids_json=list(step_ids),
                expires_at=expires,
                created_at=now,
                kind=kind.value,
                detail_json=dict(detail) if detail is not None else None,
            )
        )
        events.append(
            view.trajectory_id,
            ApprovalRequested(
                trajectory_id=view.trajectory_id,
                approval_request_id=request_id,
                step_ids=step_ids,
                reason=reason,
                expires_at=to_rfc3339(expires),
            ),
            now=now,
        )
        return request_id

    def pending(self, *, trajectory_id: str | None = None) -> list[ApprovalRequestView]:
        """Every pending request, oldest first, optionally for one trajectory."""
        with self._database.read() as session:
            statement = (
                select(models.ApprovalRequest)
                .where(models.ApprovalRequest.status == RequestStatus.PENDING.value)
                .order_by(models.ApprovalRequest.created_at, models.ApprovalRequest.id)
            )
            if trajectory_id is not None:
                statement = statement.where(models.ApprovalRequest.trajectory_id == trajectory_id)
            return [_view_of(row) for row in session.execute(statement).scalars()]

    def requests(self, trajectory_id: str) -> list[ApprovalRequestView]:
        """Every request a trajectory ever raised, oldest first, whatever became of it."""
        with self._database.read() as session:
            rows = session.execute(
                select(models.ApprovalRequest)
                .where(models.ApprovalRequest.trajectory_id == trajectory_id)
                .order_by(models.ApprovalRequest.created_at, models.ApprovalRequest.id)
            ).scalars()
            return [_view_of(row) for row in rows]

    # ---- T8 --------------------------------------------------------------------------------

    def grant(
        self,
        trajectory_id: str,
        *,
        approver: Approver,
        budget_raise: BudgetRaise | None = None,
    ) -> GrantOutcome:
        """Grant the trajectory's pending request: T8, minting what the request asked for.

        Idempotent per request: granting a request already granted returns the same outcome and
        writes nothing. The trajectory leaves ``awaiting_approval`` for ``executing`` with **no
        lease**; the worker claims a released trajectory on its next pass.

        Args:
            trajectory_id: The trajectory.
            approver: The identity the grant is recorded under. The web layer has already
                established that it holds the ``approve`` scope.
            budget_raise: The new ceiling, required for a ``ceiling_raise`` request and refused
                for any other kind.

        Returns:
            What was minted and the state reached.

        Raises:
            TrajectoryNotFoundError: No such trajectory.
            ApprovalInvalidStateError: Nothing is pending and the last request was not granted,
                or the trajectory is no longer ``awaiting_approval``.
            ValidationError: A ceiling raise with no new ceiling, a raise offered to a request
                that is not one, or a widening the declaration does not permit.
        """
        now = self._clock()
        with self._sink.write() as (session, events):
            row = session.get(models.Trajectory, trajectory_id)
            if row is None:
                raise TrajectoryNotFoundError(
                    f"No trajectory {trajectory_id!r}.", details={"trajectory_id": trajectory_id}
                )
            view = view_of(row)
            request = self._latest_request(session, trajectory_id)
            if request is None:
                raise ApprovalInvalidStateError(
                    f"Trajectory {trajectory_id} has no approval request to grant.",
                    details={"trajectory_id": trajectory_id, "state": view.state.value},
                )
            if request.status == RequestStatus.GRANTED.value:
                return GrantOutcome(
                    request=_view_of(request),
                    minted=(),
                    state=view.state,
                    already_resolved=True,
                )
            self._require_pending(request, view)
            kind = ApprovalKind(request.kind)
            if (budget_raise is not None) != (kind is ApprovalKind.CEILING_RAISE):
                message = (
                    "a new budget is offered exactly with a ceiling_raise request; "
                    f"request {request.id} is a {kind.value}"
                )
                raise ValidationError(message, details={"field": "budget", "kind": kind.value})
            ctx = self._context(session, view)
            minted_by = MintedBy(MintKind.APPROVER, approver_token_id=approver.token_id)
            if budget_raise is not None:
                view = self._apply_raise(session, view, budget_raise, now=now)
                ctx = self._context(session, view)
            minted = self._mint_for_grant(
                session, ctx, request, kind=kind, minted_by=minted_by, now=now
            )
            outcome = grant_approval(
                view.state,
                approver_has_scope=True,
                request_pending=True,
                intents_minted=len(minted),
            )
            request.status = RequestStatus.GRANTED.value
            request.resolved_at = now
            request.approver_token_id = approver.token_id
            for intent in minted:
                session.add(intent_row(intent))
            self._cas(
                session,
                trajectory_id,
                expected=TrajectoryState.AWAITING_APPROVAL,
                values={
                    "status": outcome.state.value,
                    "halted_reason": None,
                    "updated_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
            )
            events.append(
                trajectory_id,
                ApprovalGranted(
                    trajectory_id=trajectory_id,
                    approval_request_id=request.id,
                    approver_token_id=approver.token_id,
                    minted_intent_ids=tuple(intent.intent_id for intent in minted),
                ),
                now=now,
            )
            for intent in minted:
                events.append(trajectory_id, IntentMinted.of(intent), now=now)
            session.flush()
            return GrantOutcome(request=_view_of(request), minted=minted, state=outcome.state)

    def _mint_for_grant(
        self,
        session: Session,
        ctx: GovernanceContext,
        request: models.ApprovalRequest,
        *,
        kind: ApprovalKind,
        minted_by: MintedBy,
        now: datetime,
    ) -> tuple[ExecutionIntent, ...]:
        """Mint what one request's grant authorises."""
        step_ids = tuple(str(item) for item in request.step_ids_json)
        if kind in {ApprovalKind.PLAN, ApprovalKind.GATED_STEP}:
            plan_steps, verdict = _plan_and_verdict(session, ctx.view.trajectory_id)
            chosen = [plan_steps[step_id] for step_id in step_ids if step_id in plan_steps]
            if len(chosen) != len(step_ids):
                message = f"request {request.id} names steps the recorded plan does not hold"
                raise ValidationError(message, details={"field": "step_ids"})
            return tuple(
                self._mint_step(
                    ctx, step, verdict, minted_by=minted_by, now=now, approval_request_id=request.id
                )
                for step in chosen
            )
        chains = rebuild_intents(
            session,
            view=ctx.view,
            declaration=ctx.declaration,
            tier_policy=ctx.tier_policy,
            policy=ctx.approval_policy,
        )
        live = live_intents(chains)
        target = step_ids[0] if step_ids else BYPASS_STEP_ID
        previous = live.get(target)
        if previous is None:
            message = f"request {request.id} names step {target!r}, which holds no live intent"
            raise ValidationError(message, details={"field": "step_ids", "step_id": target})
        widening = _widening_for(kind, request, previous, ctx)
        return (
            supersede(
                previous,
                tier_policy=ctx.tier_policy,
                policy=ctx.approval_policy,
                minted_by=minted_by,
                minted_at=now,
                approval_request_id=request.id,
                **widening.fields,
            ),
        )

    def _apply_raise(
        self, session: Session, view: TrajectoryView, budget_raise: BudgetRaise, *, now: datetime
    ) -> TrajectoryView:
        """Raise the trajectory's own ceiling on its row; the ledger reads ceilings per call."""
        values: dict[str, Any] = {"updated_at": now}
        if budget_raise.token_ceiling is not None:
            values["budget_token_ceiling"] = budget_raise.token_ceiling
        if budget_raise.money_ceiling is not None:
            values["budget_money_currency"] = budget_raise.money_ceiling.currency
            values["budget_money_nanos"] = budget_raise.money_ceiling.nanos
        self._cas(
            session, view.trajectory_id, expected=TrajectoryState.AWAITING_APPROVAL, values=values
        )
        session.flush()
        row = session.get(models.Trajectory, view.trajectory_id)
        assert row is not None  # noqa: S101 — the CAS just changed it
        session.refresh(row)
        return view_of(row)

    # ---- T9 --------------------------------------------------------------------------------

    def deny(
        self, trajectory_id: str, *, approver: Approver, reason: str | None = None
    ) -> ApprovalRequestView:
        """Deny the pending request: T9, halted with the denial recorded.

        Idempotent per request: denying a request already denied returns it unchanged.

        Raises:
            TrajectoryNotFoundError: No such trajectory.
            ApprovalInvalidStateError: Nothing is pending and the last request was not denied.
        """
        now = self._clock()
        with self._sink.write() as (session, events):
            row = session.get(models.Trajectory, trajectory_id)
            if row is None:
                raise TrajectoryNotFoundError(
                    f"No trajectory {trajectory_id!r}.", details={"trajectory_id": trajectory_id}
                )
            view = view_of(row)
            request = self._latest_request(session, trajectory_id)
            if request is None:
                raise ApprovalInvalidStateError(
                    f"Trajectory {trajectory_id} has no approval request to deny.",
                    details={"trajectory_id": trajectory_id, "state": view.state.value},
                )
            if request.status == RequestStatus.DENIED.value:
                return _view_of(request)
            self._require_pending(request, view)
            stated = (reason or "").strip() or "denied by the approver"
            self._resolve_as_halted(
                session,
                events,
                request,
                view,
                status=RequestStatus.DENIED,
                approver=approver,
                resolution_reason=stated,
                timed_out=False,
                now=now,
            )
            session.flush()
            return _view_of(request)

    def expire(self, *, now: datetime) -> tuple[str, ...]:
        """Time out every pending request whose ``expires_at`` has passed: T9, halted.

        Called by the worker on every pass; the clock is the persisted ``expires_at``, so a
        request survives a restart with its deadline intact and a timeout is never a grant.

        Returns:
            The ids of the requests expired in this pass.
        """
        expired: list[str] = []
        with self._database.read() as session:
            due = list(
                session.execute(
                    select(models.ApprovalRequest.id, models.ApprovalRequest.trajectory_id).where(
                        models.ApprovalRequest.status == RequestStatus.PENDING.value,
                        models.ApprovalRequest.expires_at <= now,
                    )
                ).all()
            )
        for request_id, trajectory_id in due:
            with self._sink.write() as (session, events):
                request = session.get(models.ApprovalRequest, request_id)
                row = session.get(models.Trajectory, trajectory_id)
                if request is None or row is None or request.status != RequestStatus.PENDING.value:
                    continue
                view = view_of(row)
                if view.state is not TrajectoryState.AWAITING_APPROVAL:
                    continue
                hours = self._settings.approval.request_timeout_hours
                self._resolve_as_halted(
                    session,
                    events,
                    request,
                    view,
                    status=RequestStatus.EXPIRED,
                    approver=None,
                    resolution_reason=(
                        f"no answer within request_timeout_hours ({hours:g}); a timeout is never "
                        "a grant (ADR-0049 rule 4)"
                    ),
                    timed_out=True,
                    now=now,
                )
                expired.append(request_id)
        return tuple(expired)

    def _resolve_as_halted(
        self,
        session: Session,
        events: EventWriter,
        request: models.ApprovalRequest,
        view: TrajectoryView,
        *,
        status: RequestStatus,
        approver: Approver | None,
        resolution_reason: str,
        timed_out: bool,
        now: datetime,
    ) -> None:
        outcome = deny_or_time_out_approval(view.state)
        request.status = status.value
        request.resolved_at = now
        request.resolution_reason = resolution_reason
        request.approver_token_id = approver.token_id if approver is not None else None
        cause = (
            f"approval request {request.id} ({request.kind}, {request.reason}) "
            f"{'expired' if timed_out else 'was denied'}: {resolution_reason}"
        )
        self._cas(
            session,
            view.trajectory_id,
            expected=TrajectoryState.AWAITING_APPROVAL,
            values={
                "status": outcome.state.value,
                "halted_reason": cause,
                "error_code": ErrorCode.APPROVAL_REQUIRED.value,
                "completed_at": now,
                "updated_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
            },
        )
        events.append(
            view.trajectory_id,
            ApprovalDenied(
                trajectory_id=view.trajectory_id,
                approval_request_id=request.id,
                timed_out=timed_out,
                approver_token_id=approver.token_id if approver is not None else None,
            ),
            now=now,
        )
        events.append(
            view.trajectory_id,
            TrajectoryHalted(
                trajectory_id=view.trajectory_id,
                cause=cause,
                error_code=ErrorCode.APPROVAL_REQUIRED.value,
            ),
            now=now,
        )

    # ---- helpers ---------------------------------------------------------------------------

    def _context(self, session: Session, view: TrajectoryView) -> GovernanceContext:
        return load_context(
            session,
            view,
            self._settings,
            loadcoach_has_remote_provider=self._remote_provider,
        )

    def _latest_request(
        self, session: Session, trajectory_id: str
    ) -> models.ApprovalRequest | None:
        return session.execute(
            select(models.ApprovalRequest)
            .where(models.ApprovalRequest.trajectory_id == trajectory_id)
            .order_by(models.ApprovalRequest.created_at.desc(), models.ApprovalRequest.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    @staticmethod
    def _require_pending(request: models.ApprovalRequest, view: TrajectoryView) -> None:
        if request.status != RequestStatus.PENDING.value:
            raise ApprovalInvalidStateError(
                f"Request {request.id} is already {request.status}; resolution is idempotent per "
                "request and a resolved request cannot be resolved differently.",
                details={"request_id": request.id, "status": request.status},
            )
        if view.state is not TrajectoryState.AWAITING_APPROVAL:
            raise ApprovalInvalidStateError(
                f"Trajectory {view.trajectory_id} is {view.state.value}, not awaiting_approval.",
                details={"trajectory_id": view.trajectory_id, "state": view.state.value},
            )

    def _cas(
        self,
        session: Session,
        trajectory_id: str,
        *,
        expected: TrajectoryState,
        values: dict[str, Any],
    ) -> None:
        result = cast(
            "CursorResult[Any]",
            session.execute(
                update(models.Trajectory)
                .where(
                    models.Trajectory.id == trajectory_id,
                    models.Trajectory.status == expected.value,
                )
                .values(**values)
            ),
        )
        if result.rowcount != 1:
            raise ApprovalInvalidStateError(
                f"Trajectory {trajectory_id} changed state while being resolved.",
                details={"trajectory_id": trajectory_id},
            )


def _first_ready_gated(plan: Plan) -> tuple[PlanStep, ...]:
    """The first gated step that could start now — T5's "no ungated work ready" case."""
    ready = plan.ready_steps(set())
    return (ready[0],) if ready else (plan.steps[0],)


def _rejection_cause(verdict: PlanVerdict) -> str:
    """``PLAN_REJECTED`` lists every step's verdict and the ceiling that rejected it (spec §13)."""
    steps = "; ".join(
        f"{step.step_id}: {step.outcome.value} ({step.reason.value}"
        + (f", {step.error_code.value}" if step.error_code is not None else "")
        + ")"
        for step in verdict.steps
    )
    ceilings = ", ".join(
        f"{item.scope} {'exceeded' if item.exceeded else 'binds'}"
        for item in verdict.headroom
        if item.binds
    )
    return f"the plan was rejected: {steps}" + (f"; ceilings: {ceilings}" if ceilings else "")


def _plan_and_verdict(
    session: Session, trajectory_id: str
) -> tuple[dict[str, PlanStep], PlanVerdict]:
    """The validated plan's steps and the recorded verdict, rebuilt for a grant to mint from."""
    recorded = recorded_plan(session, trajectory_id)
    if recorded is None:
        message = f"trajectory {trajectory_id} holds no validated plan to grant"
        raise ValidationError(message, details={"field": "plan"})
    if recorded.verdict is None:
        message = f"trajectory {trajectory_id} holds a plan with no recorded verdict"
        raise ValidationError(message, details={"field": "plan_approvals"})
    return {step.step_id: step for step in recorded.plan.steps}, recorded.verdict


def _widening_for(
    kind: ApprovalKind,
    request: models.ApprovalRequest,
    previous: ExecutionIntent,
    ctx: GovernanceContext,
) -> _Widening:
    """What a grant changes on the superseding revision, by the request's kind.

    A ``bypass_gate`` grant changes nothing: the envelope stands, and the new revision exists so
    the record holds the human grant beside the gated envelope nobody executed under. A
    ``ceiling_raise`` grant changes the intent's budget on the bypass path, where the intent's
    slice *is* the trajectory's ceiling; a planned step's slice was not what refused, so its
    revision records the grant and nothing else.
    """
    if kind is ApprovalKind.BYPASS_GATE:
        return _Widening()
    if kind is ApprovalKind.CEILING_RAISE:
        fields: dict[str, Any] = {}
        if previous.step_id == BYPASS_STEP_ID:
            fields["token_budget"] = ctx.declaration.token_budget
            if ctx.declaration.money_budget is not None:
                fields["money_budget"] = ctx.declaration.money_budget
        return _Widening(fields)
    if kind is not ApprovalKind.REAPPROVAL or request.detail_json is None:
        message = f"request {request.id} ({kind.value}) carries no ask a grant could widen"
        raise ValidationError(message, details={"field": "detail", "kind": kind.value})
    ask = ReapprovalAsk.from_json(request.detail_json)
    return _Widening(_widen(ask, previous, ctx))


def _widen(ask: ReapprovalAsk, previous: ExecutionIntent, ctx: GovernanceContext) -> dict[str, Any]:
    """The supersession for one drift category — exactly what it asked, and nothing else."""
    match ask.category:
        case DeviationCategory.UNDECLARED_TOOL:
            outside = sorted(set(ask.tools) - ctx.declaration.tool_allowlist)
            if outside:
                message = (
                    f"tool(s) {', '.join(outside)} are outside the trajectory allowlist and are "
                    "never re-approvable (lifecycle §5)"
                )
                raise ValidationError(message, details={"field": "tools", "tools": outside})
            return {"approved_tools": set(previous.approved_tools) | set(ask.tools)}
        case DeviationCategory.TIER_ESCALATION:
            if ask.next_tier is None:
                message = "the escalation order is exhausted; there is no tier to grant"
                raise ValidationError(message, details={"field": "next_tier"})
            return {
                "approved_tier": ask.next_tier,
                "fallback_tiers": tuple(t for t in previous.fallback_tiers if t != ask.next_tier),
            }
        case DeviationCategory.TURN_OVERRUN:
            return {"max_turns": previous.max_turns + (ask.extend_turns or previous.max_turns)}
        case DeviationCategory.BUDGET_OVERRUN:
            spent = ask.tokens_spent if ask.tokens_spent is not None else previous.token_budget
            fields: dict[str, Any] = {
                "token_budget": max(spent, previous.token_budget)
                + (ask.token_slice or previous.token_budget)
            }
            if ask.money_spent is not None and previous.money_budget is not None:
                fields["money_budget"] = Money(
                    currency=previous.money_budget.currency,
                    nanos=max(ask.money_spent.nanos, previous.money_budget.nanos)
                    + previous.money_budget.nanos,
                )
            return fields
        case DeviationCategory.CLASSIFICATION_EXCEEDED:
            observed = ask.observed_classification or previous.max_classification
            if observed > ctx.declaration.classification:
                message = (
                    f"a re-approval cannot raise a step above the trajectory's declared "
                    f"{ctx.declaration.classification.value!r}"
                )
                raise ValidationError(message, details={"field": "max_classification"})
            return {"max_classification": observed}
        case DeviationCategory.TIER_VIOLATION:
            message = "a violation is never re-approvable (lifecycle §5)"
            raise ValidationError(message, details={"field": "category"})
    message = f"no re-approval rule for {ask.category.value}"  # pragma: no cover — closed enum
    raise ValidationError(message, details={"field": "category"})


def _view_of(row: models.ApprovalRequest) -> ApprovalRequestView:
    return ApprovalRequestView(
        request_id=row.id,
        trajectory_id=row.trajectory_id,
        kind=ApprovalKind(row.kind),
        status=RequestStatus(row.status),
        reason=row.reason,
        step_ids=tuple(str(item) for item in row.step_ids_json),
        detail=dict(row.detail_json) if row.detail_json is not None else None,
        created_at=row.created_at,
        expires_at=row.expires_at,
        resolved_at=row.resolved_at,
        approver_token_id=row.approver_token_id,
        resolution_reason=row.resolution_reason,
    )
