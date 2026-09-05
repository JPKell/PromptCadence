"""promptcadence.services.loop — both paths: claim, plan, approve, dispatch, turn, record, finish.

Phase 3 built the bypass path; Phase 7 adds the planned one **around the same turn**. What
executes a turn — the four pre-flights in ADR-0073's order, ``turn.started``, the call, the debit,
the subject verification, :func:`~promptcadence.domain.turns.decide_finish`, the facts and
:func:`~promptcadence.domain.deviation.compare` — is one method, :meth:`LoopController._turn`,
and it takes a :class:`_StepRun`: an intent, the thread that intent's turns go in, and the step
the intent governs. The bypass path hands it one run whose step is the synthetic ``loop``; the
planned path hands it one run per approved step, in ready-set order. Nothing below the run knows
which path built it (ADR-0048, ADR-0056), and contract 1's diff test is what proves that.

What each path does before the first turn, and where each commits:

1. **Claim.** A bypassed trajectory is T3 — one write: ``queued → executing`` under this worker's
   lease, the default :class:`~promptcadence.domain.intent.ExecutionIntent` minted and written,
   ``trajectory.claimed`` + ``intent.minted``. If the default intent's gate fired
   (``requires_human_approval``, ADR-0049 rule 3) the same write continues into **T10**: an
   ``approval_request`` of kind ``bypass_gate`` and ``awaiting_approval``, so a bypassed
   trajectory under ``manual`` (or a gated ``hybrid``) never runs a turn nobody approved. A planned
   trajectory is T2 — ``queued → planning`` under the lease, ``trajectory.claimed``.
2. **Plan** (planned only). The :class:`~promptcadence.services.planner.Planner` drafts under
   ``tools.plan``; every attempt is a ``plans`` row with ``plan.drafted`` in its own write, so a
   crash between attempts leaves the drafts on the record; exhaustion is T7. The
   :class:`~promptcadence.services.approvals.ApprovalService` then renders the verdict and does
   what the mode says: T4 (mint, ``executing``), T5 (hold) or T6 (``rejected``) — one write.
3. **Dispatch.** The ready set is the plan's steps whose dependencies committed; a ready step with
   a live intent starts (``step.started`` in the write that opens its thread); a ready step with
   no intent is a hybrid-gated one, and it parks the trajectory **at that point** (T10), after the
   ungated ready work has run. Above ``max_concurrent_steps = 1`` the pure rule in
   :mod:`promptcadence.domain.dispatch` decides what may run together.
4. **Turn.** As Phase 3 wrote it, plus two things a plan makes real: the intent's fallback tiers
   are tried in order when a tier cannot serve (``NO_ELIGIBLE_MODEL`` or unavailable), and when
   the set is exhausted the turn is a ``tier_escalation`` deviation rather than a bare halt. A
   drift whose disposition is ``scoped_reapproval`` no longer halts: it parks the trajectory on a
   request scoped to **that step** (T10) carrying exactly what the drift asked for, and the grant
   mints a superseding revision.
5. **Finish.** Only on what was declared. A step's declared finish commits it (``step.completed``);
   when every step has committed the trajectory completes (T11) in the same write.

**What a lease means here.** Every write to the trajectory row is a compare-and-set on
``(id, status ∈ {planning, executing}, lease_owner=<this worker>)``. A worker whose lease was taken
over cannot commit a turn, a plan, a halt or a completion: its next write affects zero rows and
raises :class:`LeaseLost`, and it stops.

**Every intent is re-minted, never rehydrated.**
:func:`~promptcadence.services.intents.rebuild_intents` re-mints every recorded revision from
the recorded inputs and refuses to run unless each is
byte-identical to its row; a configuration change that would alter an envelope fails the
trajectory naming the mismatch rather than running turns under an envelope nobody minted.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, cast

from baseaicore import (
    UNSUPPORTED,
    TokenUsage,
    ValidationError,
    canonical_json,
    is_supported,
    new_id,
    sha256_of,
)
from commissioner import Verdict
from sqlalchemy import select, true, update
from toolyard import MAX_RECORDED_NAME_CHARS, StoreFailure, ToolCallRequest
from toolyard import EgressClass as ToolEgressClass

from promptcadence.config import ConfigurationError
from promptcadence.domain.deviation import (
    Deviation,
    DeviationCategory,
    DeviationDetected,
    Disposition,
    TierServiceFailure,
    TurnFacts,
    compare,
    disposition,
)
from promptcadence.domain.dispatch import StepCompleted, StepStarted, dispatchable
from promptcadence.domain.errors import (
    CompactionFailedError,
    ErrorCode,
    LoadCoachError,
    LoadCoachUnavailableError,
    PlanDraftFailedError,
    TierNotConfiguredError,
    TierUnavailableError,
)
from promptcadence.domain.intent import (
    BYPASS_STEP_ID,
    ExecutionIntent,
    IntentMinted,
    mint_bypass_default,
)
from promptcadence.domain.plan import Plan, PlanDrafted, PlanStep
from promptcadence.domain.policy import (
    BudgetHeadroom,
    PartialPricing,
    VerdictReason,
    gate_reason,
    requires_human_approval,
)
from promptcadence.domain.threads import FinishReason, Thread, Turn, TurnRole
from promptcadence.domain.tiers import Tier, TierPolicy
from promptcadence.domain.tools import ToolCallCompleted, ToolCallStarted
from promptcadence.domain.trajectory import (
    BudgetWindowWait,
    TrajectoryCancelled,
    TrajectoryClaimed,
    TrajectoryCompleted,
    TrajectoryFailed,
    TrajectoryHalted,
    TrajectoryRecovered,
    TrajectoryResumed,
    TrajectoryState,
    WindowWait,
    cancel,
    claim_for_bypass,
    claim_for_planning,
    complete,
    fail,
    halt,
    halt_window_wait,
    park_for_window,
    request_approval,
    resume_from_window,
)
from promptcadence.domain.turns import (
    FinishDecision,
    FinishOutcome,
    TurnCompleted,
    TurnStarted,
    decide_finish,
)
from promptcadence.infrastructure.db import models
from promptcadence.infrastructure.loadcoach import (
    NON_TERMINAL_JOB_STATES,
    SUBJECT_ABSENT,
    GenerateRequest,
    GenerationResponse,
    LoadCoachClient,
    Message,
    RequestedToolCall,
    assemble_tool_calls,
    parse_generation,
)
from promptcadence.infrastructure.threads import SqlThreadStore, thread_row, turn_row
from promptcadence.infrastructure.tool_calls import CollectingToolCallStore, ToolCallLinks
from promptcadence.observability.logging import correlation
from promptcadence.services.approvals import (
    ApprovalKind,
    ApprovalService,
    PlanDecision,
    ReapprovalAsk,
)
from promptcadence.services.budget import (
    CurrencyMismatchError,
    render_remaining_money,
    render_remaining_tokens,
)
from promptcadence.services.egress import (
    EgressService,
    fetch_target,
    host_of,
    tier_target,
)
from promptcadence.services.governance import GovernanceContext, load_context
from promptcadence.services.intents import (
    RecordedPlan,
    intent_row,
    live_intents,
    rebuild_intents,
    recorded_plan,
)
from promptcadence.services.loadcoach_surface import (
    ProviderSurface,
    load_provider_surface,
    resolve_subject,
)
from promptcadence.services.planner import (
    DraftAttempt,
    Planner,
    PlanningCancelled,
    PlanningInputs,
    plan_job_key_prefix,
)
from promptcadence.services.prompts import STEP_EXECUTE_PROMPT_ID, render
from promptcadence.services.tools import ToolPlant, TrajectoryTools, outcome_of
from promptcadence.services.views import TrajectoryView, view_of

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from setspec.prompts import RenderedPrompt
    from sqlalchemy import CursorResult
    from sqlalchemy.orm import Session

    from promptcadence.config import Settings
    from promptcadence.domain.intent import TurnProvenance
    from promptcadence.services.budget import BudgetService
    from promptcadence.services.database import Database
    from promptcadence.services.estimates import StepEstimator
    from promptcadence.services.events import EventWriter, StoredEvent, TrajectoryEventSink

__all__ = [
    "BypassGate",
    "LeaseLost",
    "LoopController",
    "ReconcileOutcome",
    "RunSignals",
    "TierRouter",
]

logger = logging.getLogger(__name__)

_UNVERIFIED_SUBJECT_REASONS: Final[frozenset[str]] = frozenset(
    {SUBJECT_ABSENT, "subject_unverifiable"}
)
"""The two ways a turn's execution subject fails to be verifiable, both violations.

``subject_absent`` is the response naming no ``model.canonical_id`` — it claims work and declines
to say by what. ``subject_unverifiable`` is
:func:`~promptcadence.services.loadcoach_surface.resolve_subject` finding no single configured
provider to check the answer against. They reach the loop as ordinary ``LoadCoachError``s and must
not be handled as ordinary ones: fail closed, recorded (spec §11 contract 4).
"""

_SERVICE_FAILURE_REASONS: Final[Mapping[str, TierServiceFailure]] = {
    "no_eligible_model": TierServiceFailure.NO_ELIGIBLE_MODEL,
    "loadcoach_has_no_remote_provider": TierServiceFailure.TIER_UNAVAILABLE,
    "task_profile_not_found": TierServiceFailure.TIER_UNAVAILABLE,
}
"""The ``TierUnavailableError`` reasons that mean *this tier cannot serve now* — the two
lifecycle §5 names for ``tier_escalation`` — and so fall to the intent's next tier rather than
halting. Any other reason halts with its cause as before."""

_LEASE_HOLDING: Final[frozenset[TrajectoryState]] = frozenset(
    {TrajectoryState.PLANNING, TrajectoryState.EXECUTING}
)

_DEVIATION_LIMIT_PER_STEP: Final = 3
"""Lifecycle §5: more than this many deviations on one step halts with ``DEVIATION_HALTED``."""


class LeaseLost(Exception):  # noqa: N818 — an internal signal, not a caller-facing error
    """This worker no longer holds the trajectory's lease; stop without committing anything."""


class _StepDone(Exception):  # noqa: N818 — an internal signal
    """The step reached its declared finish; the dispatcher decides what runs next."""


@dataclass(frozen=True, slots=True)
class BypassGate:
    """Whether a trajectory bypasses planning, from configuration and the request (lifecycle §1).

    Attributes:
        planning_enabled: ``[planning] enabled`` — the bypass switch.
        allow_request_override: ``[planning] allow_request_override``.
    """

    planning_enabled: bool
    allow_request_override: bool

    def decide(self, requested: bool | None) -> bool:
        """Decide the bypass for one request.

        Args:
            requested: The request's ``bypass_planning``, or ``None`` when it said nothing.

        Returns:
            ``True`` to bypass planning. With no request override, the configured switch decides.

        Raises:
            ValidationError: The request asked for a bypass the configuration does not permit, or
                asked for planning while planning is disabled. Both are refused rather than
                silently overridden — a caller told "yes" to a flag that was ignored is the
                silent failure this gate exists to prevent.
        """
        if requested is None:
            return not self.planning_enabled
        if requested and not self.allow_request_override:
            message = (
                "bypass_planning is not permitted per request: [planning] "
                "allow_request_override is false"
            )
            raise ValidationError(message, details={"field": "bypass_planning"})
        if not requested and not self.planning_enabled:
            message = "bypass_planning=false cannot be honoured: [planning] enabled is false"
            raise ValidationError(message, details={"field": "bypass_planning"})
        return requested


@dataclass(frozen=True, slots=True)
class TierRouter:
    """Resolve the tier a turn is dispatched on from the intent, and nothing else (ADR-0047).

    Which *model* within the tier is LoadCoach's; this only answers "may this tier serve now?"
    and hands back its task profile.
    """

    tier_policy: TierPolicy

    def tier_of(self, intent: ExecutionIntent) -> Tier:
        """Return the intent's approved tier as configured, asking nothing about availability.

        Split from :meth:`ensure_available` at Phase 6, and the split is load-bearing. A tier's
        **egress class is a property of its configuration**; whether it can serve right now is a
        property of the deployment. Governance must be decided on the first and never gated on the
        second (ADR-0073).

        Raises:
            TierNotConfiguredError: The intent names a tier the snapshot does not define.
        """
        return self.tier_policy.snapshot.require(intent.approved_tier)

    def permitted(self, intent: ExecutionIntent) -> tuple[Tier, ...]:
        """Return every tier the intent permits, approved tier first, as configured.

        Raises:
            TierNotConfiguredError: A permitted tier is not in the snapshot.
        """
        return tuple(self.tier_policy.snapshot.require(name) for name in intent.permitted_tiers)

    def ensure_available(self, tier: Tier) -> None:
        """Refuse a tier that cannot serve right now.

        Called **after** the egress decision has been rendered and recorded, so that a tier this
        trajectory may not use is refused on those grounds whatever the deployment looks like.

        Raises:
            TierUnavailableError: The tier cannot serve right now — today only
                ``loadcoach_has_no_remote_provider``.
        """
        availability = self.tier_policy.availability(tier.name)
        if not availability.available:
            reason = availability.reason.value if availability.reason is not None else "unknown"
            message = f"tier {tier.name!r} cannot serve: {reason}"
            raise TierUnavailableError(message, details={"reason": reason, "tier": tier.name})

    def resolve(self, intent: ExecutionIntent) -> Tier:
        """Return the intent's approved tier, if it can serve.

        The two checks in their pre-Phase-6 order, kept for callers that make no egress decision
        of their own — recovery's reconciliation, which is re-reading a turn that already ran.

        Raises:
            TierUnavailableError: The tier cannot serve right now.
            TierNotConfiguredError: The intent names a tier the snapshot does not define.
        """
        tier = self.tier_of(intent)
        self.ensure_available(tier)
        return tier


class ReconcileOutcome(StrEnum):
    """What recovery did with one lease-holding trajectory (lifecycle §8.3)."""

    RESUMED = "resumed"
    HALTED = "halted"
    FINISHED = "finished"
    DEFERRED = "deferred"


@dataclass
class RunSignals:
    """The flags a lease keeper raises and the loop reads at every boundary.

    ``in_flight_turn_id`` is the most recent turn announced and not yet answered — what a keeper
    cancels when a cancel arrives mid-turn. Above ``max_concurrent_steps = 1`` several turns can be
    in flight at once, so ``in_flight_turn_ids`` holds every one and the keeper cancels them all.
    """

    cancel_requested: threading.Event
    lease_lost: threading.Event
    in_flight_turn_id: str | None = None
    in_flight_turn_ids: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def fresh(cls) -> RunSignals:
        """Signals with nothing raised."""
        return cls(cancel_requested=threading.Event(), lease_lost=threading.Event())

    def announce(self, turn_id: str) -> None:
        """Record a turn as in flight."""
        with self.lock:
            self.in_flight_turn_ids.add(turn_id)
            self.in_flight_turn_id = turn_id

    def settle(self, turn_id: str) -> None:
        """Record a turn as no longer in flight."""
        with self.lock:
            self.in_flight_turn_ids.discard(turn_id)
            if self.in_flight_turn_id == turn_id:
                self.in_flight_turn_id = next(iter(self.in_flight_turn_ids), None)

    def in_flight(self) -> tuple[str, ...]:
        """Every turn currently in flight."""
        with self.lock:
            return tuple(self.in_flight_turn_ids)


@dataclass(frozen=True, slots=True)
class _StepRun:
    """One step's execution: the envelope its turns run under, and the thread they go in.

    The unit :meth:`LoopController._turn` works on, whichever path built it. ``step`` is the plan
    step on the planned path and ``None`` on the bypass path, whose one run is the synthetic
    ``loop``; nothing governance-related reads it — it is what the framing turn is rendered from.
    """

    step_id: str
    intent: ExecutionIntent
    thread_id: str
    step: PlanStep | None = None


@dataclass(frozen=True, slots=True)
class _Loaded:
    """Everything a run needs beyond the row, rebuilt from the record at every entry."""

    ctx: GovernanceContext
    plan: RecordedPlan | None
    live: Mapping[str, ExecutionIntent]


class LoopController:
    """Claim, plan, run and reconcile trajectories. One per worker thread."""

    __slots__ = (
        "_approvals",
        "_budget",
        "_clock",
        "_database",
        "_egress",
        "_estimator",
        "_ids",
        "_loadcoach",
        "_planner",
        "_remote_provider",
        "_render",
        "_settings",
        "_sink",
        "_surface_loader",
        "_threads",
        "_tools",
        "owner",
    )

    def __init__(
        self,
        *,
        database: Database,
        sink: TrajectoryEventSink,
        loadcoach: LoadCoachClient,
        settings: Settings,
        owner: str,
        budget: BudgetService,
        estimator: StepEstimator,
        egress: EgressService,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] = new_id,
        surface_loader: Callable[[LoadCoachClient], ProviderSurface] = load_provider_surface,
        tools: ToolPlant | None = None,
        approvals: ApprovalService | None = None,
        planner: Planner | None = None,
        prompt_renderer: Callable[..., RenderedPrompt] = render,
        loadcoach_has_remote_provider: bool = False,
    ) -> None:
        """Bind the controller to one worker's identity and the process's handles.

        Args:
            database: The application's database handle.
            sink: The event sink every write goes through.
            loadcoach: The LoadCoach client.
            settings: The validated configuration.
            owner: This worker's lease owner id, ``<process prefix>/<thread index>``.
            budget: The ceilings, the ledger and the debits (P5). Required: a loop that could run
                without one would be a loop that could spend without recording it.
            estimator: The layered step estimator the pre-flight asks for a prospective spend.
            egress: The egress policy and its ledger (P6). Required for the same reason
                ``budget`` is; governance is not conditional on a mode (ADR-0048).
            clock: The instant source, injected for determinism.
            id_factory: The id source for turns, threads, requests and intents.
            surface_loader: How the provider surface is read; injected so a test can script it.
            tools: The process's registry, sandbox and artifact store, or ``None`` to build one
                from ``[tools]``.
            approvals: The approval service, or ``None`` to build one over the same handles.
            planner: The planner, or ``None`` to build one over ``loadcoach`` and
                ``[planning] corrective_retries``.
            prompt_renderer: How prompt records are rendered; injected so a test can watch the
                step framing without a pack on disk.
            loadcoach_has_remote_provider: Whether LoadCoach has a remote provider registered —
                ``False`` until LC-E1 (lifecycle §3). See
                :func:`promptcadence.services.governance.tier_policy_of`.
        """
        self._database = database
        self._sink = sink
        self._loadcoach = loadcoach
        self._settings = settings
        self.owner = owner
        self._budget = budget
        self._estimator = estimator
        self._egress = egress
        self._clock = clock if clock is not None else _utc_now
        self._ids = id_factory
        self._remote_provider = loadcoach_has_remote_provider
        self._surface_loader = surface_loader
        self._threads = SqlThreadStore(database.sessions)
        self._tools = tools if tools is not None else ToolPlant(settings)
        self._approvals = (
            approvals
            if approvals is not None
            else ApprovalService(
                database,
                sink,
                settings,
                estimator=estimator,
                budget=budget,
                clock=self._clock,
                id_factory=id_factory,
                loadcoach_has_remote_provider=loadcoach_has_remote_provider,
            )
        )
        self._planner = (
            planner
            if planner is not None
            else Planner(
                loadcoach,
                corrective_retries=settings.planning.corrective_retries,
                id_factory=id_factory,
                prompt_renderer=prompt_renderer,
            )
        )
        self._render = prompt_renderer

    @property
    def approvals(self) -> ApprovalService:
        """The approval service this controller parks through and the worker expires through."""
        return self._approvals

    # ----------------------------------------------------------------------------------------
    # Claiming
    # ----------------------------------------------------------------------------------------

    def next_queued(self) -> str | None:
        """Return the oldest ``queued`` trajectory's id, or ``None``."""
        with self._database.read() as session:
            return session.execute(
                select(models.Trajectory.id)
                .where(models.Trajectory.status == TrajectoryState.QUEUED.value)
                .order_by(models.Trajectory.created_at, models.Trajectory.id)
                .limit(1)
            ).scalar_one_or_none()

    def next_released(self) -> str | None:
        """Return the oldest ``executing`` trajectory holding no lease, or ``None``.

        A grant (T8) moves a trajectory to ``executing`` with **no** lease — the approving request
        runs in the web layer, which is not a worker — and this is how a worker finds it. The
        recovery pass would too, but only at startup or once ``lease_seconds`` have passed; this
        makes a grant resume within one poll interval.
        """
        with self._database.read() as session:
            return session.execute(
                select(models.Trajectory.id)
                .where(
                    models.Trajectory.status == TrajectoryState.EXECUTING.value,
                    models.Trajectory.lease_owner.is_(None),
                )
                .order_by(models.Trajectory.updated_at, models.Trajectory.id)
                .limit(1)
            ).scalar_one_or_none()

    def claim_released(self, trajectory_id: str) -> bool:
        """Take the lease of a released ``executing`` trajectory; ``True`` iff it is now ours."""
        now = self._clock()
        expires = now + timedelta(seconds=self._settings.execution.lease_seconds)
        with self._database.write() as session:
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    update(models.Trajectory)
                    .where(
                        models.Trajectory.id == trajectory_id,
                        models.Trajectory.status == TrajectoryState.EXECUTING.value,
                        models.Trajectory.lease_owner.is_(None),
                    )
                    .values(lease_owner=self.owner, lease_expires_at=expires, updated_at=now)
                ),
            )
            return result.rowcount == 1

    def claim(self, trajectory_id: str) -> TrajectoryState | None:
        """Claim a queued trajectory: T3 for a bypassed one, T2 for a planned one.

        Both are one write. On the bypass path the default intent is minted in the claim and, when
        its gate fires under the configured mode, the same write continues into T10 — the
        trajectory parks on a ``bypass_gate`` request before any turn runs (ADR-0048, ADR-0049
        rule 3). On the planned path the claim takes the lease and nothing more; drafting is
        :meth:`run`'s.

        Args:
            trajectory_id: The trajectory.

        Returns:
            The state after the claim — ``executing``, ``awaiting_approval`` or ``planning`` — or
            ``None`` when another worker claimed it first.
        """
        now = self._clock()
        expires = now + timedelta(seconds=self._settings.execution.lease_seconds)
        with self._sink.write() as (session, events):
            row = session.get(models.Trajectory, trajectory_id)
            if row is None or row.status != TrajectoryState.QUEUED.value:
                return None
            view = view_of(row)
            if not view.bypass_planning:
                claimed = claim_for_planning(
                    TrajectoryState.QUEUED, planning_enabled=True, lease_acquired=True
                )
                if not self._cas(
                    session,
                    trajectory_id,
                    expected=TrajectoryState.QUEUED,
                    values={
                        "status": claimed.state.value,
                        "lease_owner": self.owner,
                        "lease_expires_at": expires,
                        "updated_at": now,
                    },
                ):
                    return None
                events.append(
                    trajectory_id,
                    TrajectoryClaimed(
                        trajectory_id=trajectory_id,
                        state=claimed.state,
                        worker_id=self.owner,
                        lease_expires_at=expires,
                    ),
                    now=now,
                )
                return claimed.state
            ctx = self._context(session, view)
            intent = mint_bypass_default(
                intent_id=self._ids(),
                declaration=ctx.declaration,
                tier_policy=ctx.tier_policy,
                policy=ctx.approval_policy,
                minted_at=now,
                tier_override=view.tier_override,
            )
            outcome = claim_for_bypass(
                TrajectoryState.QUEUED,
                bypass_permitted=True,
                lease_acquired=True,
                default_intent_minted=True,
            )
            if not self._cas(
                session,
                trajectory_id,
                expected=TrajectoryState.QUEUED,
                values={
                    "status": outcome.state.value,
                    "lease_owner": self.owner,
                    "lease_expires_at": expires,
                    "updated_at": now,
                },
            ):
                return None
            session.add(intent_row(intent))
            events.append(
                trajectory_id,
                TrajectoryClaimed(
                    trajectory_id=trajectory_id,
                    state=outcome.state,
                    worker_id=self.owner,
                    lease_expires_at=expires,
                ),
                now=now,
            )
            events.append(trajectory_id, IntentMinted.of(intent), now=now)
            if requires_human_approval(intent.gate, mode=ctx.approval_policy.mode):
                # T3 then T10 in one write: the gate fired at the minting of the default intent,
                # and bypass removes planning, never approval of gated egress (lifecycle §4.2).
                reason = gate_reason(intent.gate, mode=ctx.approval_policy.mode)
                request_id = self._approvals.request(
                    session,
                    events,
                    view=view,
                    kind=ApprovalKind.BYPASS_GATE,
                    reason=reason,
                    step_ids=(BYPASS_STEP_ID,),
                    detail=None,
                    now=now,
                )
                parked = request_approval(TrajectoryState.EXECUTING, request_created=True)
                self._owned_cas(
                    session,
                    trajectory_id,
                    values={
                        "status": parked.state.value,
                        "halted_reason": (
                            f"the default intent's gate fired ({reason.value}) under "
                            f"approval.mode = {ctx.approval_policy.mode.value}; held for a person "
                            f"(request {request_id})"
                        ),
                        "updated_at": now,
                        "lease_owner": None,
                        "lease_expires_at": None,
                    },
                )
                return parked.state
            return outcome.state

    # ----------------------------------------------------------------------------------------
    # Leases
    # ----------------------------------------------------------------------------------------

    def renew_lease(self, trajectory_id: str) -> tuple[bool, bool]:
        """Extend this worker's lease by ``lease_seconds``.

        Returns:
            ``(renewed, cancel_requested)``. ``renewed`` is false when the row is no longer this
            worker's — it was recovered by another, or it left a lease-holding state — and the
            caller must stop. ``cancel_requested`` is the row's flag, read in the same statement's
            transaction so a cancel is seen within one renewal interval.
        """
        now = self._clock()
        expires = now + timedelta(seconds=self._settings.execution.lease_seconds)
        with self._database.write() as session:
            renewed = self._cas(
                session,
                trajectory_id,
                expected=_LEASE_HOLDING,
                values={"lease_expires_at": expires, "updated_at": now},
                require_owner=True,
            )
            flag = session.execute(
                select(models.Trajectory.cancel_requested).where(
                    models.Trajectory.id == trajectory_id
                )
            ).scalar_one_or_none()
        return renewed, bool(flag)

    def _cas(
        self,
        session: Session,
        trajectory_id: str,
        *,
        expected: TrajectoryState | frozenset[TrajectoryState],
        values: dict[str, Any],
        require_owner: bool = False,
    ) -> bool:
        """Compare-and-set the trajectory row; ``True`` iff exactly one row changed."""
        states = {expected} if isinstance(expected, TrajectoryState) else expected
        conditions = [
            models.Trajectory.id == trajectory_id,
            models.Trajectory.status.in_([state.value for state in states]),
        ]
        if require_owner:
            conditions.append(models.Trajectory.lease_owner == self.owner)
        result = cast(
            "CursorResult[Any]",
            session.execute(update(models.Trajectory).where(*conditions).values(**values)),
        )
        return result.rowcount == 1

    def _owned_cas(
        self,
        session: Session,
        trajectory_id: str,
        *,
        values: dict[str, Any],
        expected: TrajectoryState = TrajectoryState.EXECUTING,
    ) -> None:
        """A compare-and-set on a lease-holding row this worker leases, or :class:`LeaseLost`."""
        if not self._cas(
            session,
            trajectory_id,
            expected=expected,
            values=values,
            require_owner=True,
        ):
            raise LeaseLost(trajectory_id)

    # ----------------------------------------------------------------------------------------
    # Governance reconstruction
    # ----------------------------------------------------------------------------------------

    def _context(self, session: Session, view: TrajectoryView) -> GovernanceContext:
        return load_context(
            session, view, self._settings, loadcoach_has_remote_provider=self._remote_provider
        )

    def _load(self, trajectory_id: str, *, expected: TrajectoryState) -> _Loaded:
        """Rebuild the run's governance from rows, re-minting and verifying every intent.

        Raises:
            LeaseLost: The trajectory is not in ``expected`` under this worker.
            ValidationError: A re-minted intent differs from its row — a configuration change
                since the minting — or the rows are incomplete.
        """
        with self._database.read() as session:
            row = session.get(models.Trajectory, trajectory_id)
            if row is None or row.status != expected.value or row.lease_owner != self.owner:
                raise LeaseLost(trajectory_id)
            view = view_of(row)
            ctx = self._context(session, view)
            chains = rebuild_intents(
                session,
                view=view,
                declaration=ctx.declaration,
                tier_policy=ctx.tier_policy,
                policy=ctx.approval_policy,
            )
            plan = None if view.bypass_planning else recorded_plan(session, trajectory_id)
        return _Loaded(ctx=ctx, plan=plan, live=live_intents(chains))

    # ----------------------------------------------------------------------------------------
    # Running
    # ----------------------------------------------------------------------------------------

    def run(self, trajectory_id: str, *, signals: RunSignals | None = None) -> TrajectoryState:
        """Run a trajectory this worker holds: draft and approve if ``planning``, then execute.

        Args:
            trajectory_id: The trajectory.
            signals: The lease keeper's flags; fresh ones when running without a keeper.

        Returns:
            The state reached — terminal, ``awaiting_approval``, ``awaiting_window`` — or the
            lease-holding state itself when this worker lost the lease and stopped without
            committing (the recovering worker owns it now).
        """
        flags = signals if signals is not None else RunSignals.fresh()
        with correlation(trajectory_id=trajectory_id):
            with self._database.read() as session:
                row = session.get(models.Trajectory, trajectory_id)
                status = TrajectoryState(row.status) if row is not None else None
            if status is TrajectoryState.PLANNING:
                try:
                    state = self._plan(trajectory_id, flags)
                except LeaseLost:
                    logger.warning("trajectory.lease_lost", extra={"trajectory_id": trajectory_id})
                    return TrajectoryState.PLANNING
                if state is not TrajectoryState.EXECUTING:
                    return state
            elif status is not TrajectoryState.EXECUTING:
                return status if status is not None else TrajectoryState.FAILED
            try:
                loaded = self._load(trajectory_id, expected=TrajectoryState.EXECUTING)
            except LeaseLost:
                return TrajectoryState.EXECUTING
            except ValidationError as exc:
                return self._end_with(
                    trajectory_id, fail, cause=exc.message, error_code=ErrorCode.LOADCOACH_ERROR
                )
            try:
                return self._execute(loaded, flags)
            except LeaseLost:
                logger.warning("trajectory.lease_lost", extra={"trajectory_id": trajectory_id})
                return TrajectoryState.EXECUTING

    # ---- planning (T2 → T4/T5/T6/T7) --------------------------------------------------------

    def _plan(self, trajectory_id: str, flags: RunSignals) -> TrajectoryState:
        """Draft, record every attempt, and hand the validated plan to approval.

        Raises:
            LeaseLost: This worker no longer holds the ``planning`` lease.
        """
        with self._database.read() as session:
            row = session.get(models.Trajectory, trajectory_id)
            if (
                row is None
                or row.status != TrajectoryState.PLANNING.value
                or row.lease_owner != self.owner
            ):
                raise LeaseLost(trajectory_id)
            view = view_of(row)
            ctx = self._context(session, view)
        if flags.cancel_requested.is_set() or self._cancel_requested(trajectory_id):
            return self._cancel_at_boundary(trajectory_id, from_state=TrajectoryState.PLANNING)
        inputs = PlanningInputs(
            task=view.task,
            classification=view.classification,
            tool_allowlist=view.tools,
            tool_descriptions={
                name: entry.description
                for name in view.tools
                if (entry := self._tools.entry(name)) is not None
            },
            tier_snapshot=ctx.tier_policy.snapshot,
            max_plan_steps=(
                view.max_steps
                if view.max_steps is not None
                else self._settings.planning.max_plan_steps
            ),
        )
        plan_ids: dict[int, str] = {}

        def record(attempt: DraftAttempt) -> None:
            plan_ids[attempt.attempt] = self._record_attempt(view, attempt)

        def should_stop() -> bool:
            return (
                flags.lease_lost.is_set()
                or flags.cancel_requested.is_set()
                or self._cancel_requested(trajectory_id)
            )

        try:
            plan = self._planner.draft(
                inputs, trajectory_id=trajectory_id, on_attempt=record, should_stop=should_stop
            )
        except PlanningCancelled:
            return self._cancel_at_boundary(trajectory_id, from_state=TrajectoryState.PLANNING)
        except PlanDraftFailedError as exc:
            return self._fail_planning(trajectory_id, cause=exc.message, code=exc.code)
        except LoadCoachUnavailableError as exc:
            return self._fail_planning(
                trajectory_id,
                cause=f"{exc.message}{self._abandon_plan_jobs(trajectory_id)}",
                code=ErrorCode.LOADCOACH_UNAVAILABLE.value,
            )
        except (TierUnavailableError, CompactionFailedError, LoadCoachError) as exc:
            # The call failed, but the job it started may still be running (a LoadCoach whose
            # corrective retry refused its own request leaves the job executing); cancel it so
            # no orphaned planning job holds the GPU, and say what was done.
            return self._fail_planning(
                trajectory_id,
                cause=f"{exc.message}{self._abandon_plan_jobs(trajectory_id)}",
                code=ErrorCode(exc.code).value,
            )
        plan_id = plan_ids[max(plan_ids)]
        return self._approve(ctx, plan, plan_id)

    def _record_attempt(self, view: TrajectoryView, attempt: DraftAttempt) -> str:
        """Persist one drafting attempt — valid or not — with ``plan.drafted``, in its own write."""
        now = self._clock()
        plan_id = self._ids()
        usage = attempt.usage
        with self._sink.write() as (session, events):
            self._owned_cas(
                session,
                view.trajectory_id,
                values={"updated_at": now},
                expected=TrajectoryState.PLANNING,
            )
            session.add(
                models.Plan(
                    id=plan_id,
                    trajectory_id=view.trajectory_id,
                    document_sha256=attempt.document_sha256,
                    raw_document=attempt.raw_document,
                    validated_json=attempt.plan.as_canonical() if attempt.plan else {},
                    attempt=attempt.attempt,
                    valid=attempt.valid,
                    issues_json=(
                        [issue.as_canonical() for issue in attempt.issues]
                        if not attempt.valid
                        else None
                    ),
                    idempotency_key=attempt.idempotency_key,
                    loadcoach_job_id=attempt.job_id,
                    model_canonical_id=attempt.model_canonical_id,
                    input_tokens=_supported(usage.input_tokens) if usage else None,
                    output_tokens=_supported(usage.output_tokens) if usage else None,
                    cache_write_tokens=_supported(usage.cache_write_tokens) if usage else None,
                    cache_read_tokens=_supported(usage.cache_read_tokens) if usage else None,
                    loadcoach_ms=attempt.loadcoach_ms,
                    prompt_id=attempt.prompt_id,
                    prompt_version=attempt.prompt_version,
                    prompt_sha256=attempt.prompt_sha256,
                    created_at=now,
                )
            )
            if attempt.plan is not None:
                for index, step in enumerate(attempt.plan.steps, start=1):
                    session.add(
                        models.PlanStep(
                            id=self._ids(),
                            plan_id=plan_id,
                            step_id=step.step_id,
                            sequence=index,
                            description=step.description,
                            depends_on_json=list(step.depends_on),
                            tools_json=list(step.tools),
                            tier=step.tier,
                            data_classification=step.data_classification.value,
                            expected_turns=step.expected_turns,
                            status="pending",
                        )
                    )
            events.append(
                view.trajectory_id,
                PlanDrafted(
                    trajectory_id=view.trajectory_id,
                    plan_id=plan_id,
                    attempt=attempt.attempt,
                    valid=attempt.valid,
                    step_count=len(attempt.plan.steps) if attempt.plan else 0,
                    issue_count=len(attempt.issues),
                    document_sha256=attempt.document_sha256,
                ),
                now=now,
            )
        return plan_id

    def _approve(self, ctx: GovernanceContext, plan: Plan, plan_id: str) -> TrajectoryState:
        """T4, T5 or T6 in one write: the verdict, what it minted or held, and the transition."""
        now = self._clock()
        trajectory_id = ctx.view.trajectory_id
        with self._sink.write() as (session, events):
            decision: PlanDecision = self._approvals.decide_plan(
                session, events, ctx=ctx, plan=plan, plan_id=plan_id, now=now
            )
            values: dict[str, Any] = {"status": decision.state.value, "updated_at": now}
            if decision.state is TrajectoryState.REJECTED:
                values.update(
                    halted_reason=decision.cause,
                    error_code=decision.error_code.value if decision.error_code else None,
                    completed_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            elif decision.state is TrajectoryState.AWAITING_APPROVAL:
                values.update(halted_reason=decision.cause, lease_owner=None, lease_expires_at=None)
            self._owned_cas(
                session, trajectory_id, values=values, expected=TrajectoryState.PLANNING
            )
        logger.info(
            "trajectory.plan_decided",
            extra={"trajectory_id": trajectory_id, "state": decision.state.value},
        )
        return decision.state

    def _fail_planning(self, trajectory_id: str, *, cause: str, code: str) -> TrajectoryState:
        """T7: the draft failed after the corrective budget, or LoadCoach failed the draft."""
        now = self._clock()
        outcome = fail(TrajectoryState.PLANNING, cause=cause)
        with self._sink.write() as (session, events):
            self._owned_cas(
                session,
                trajectory_id,
                values={
                    "status": outcome.state.value,
                    "halted_reason": cause,
                    "error_code": code,
                    "completed_at": now,
                    "updated_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
                expected=TrajectoryState.PLANNING,
            )
            events.append(
                trajectory_id,
                TrajectoryFailed(trajectory_id=trajectory_id, cause=cause, error_code=code),
                now=now,
            )
        return outcome.state

    # ---- executing (dispatch over the ready set) ------------------------------------------

    def _execute(self, loaded: _Loaded, flags: RunSignals) -> TrajectoryState:
        trajectory_id = loaded.ctx.view.trajectory_id
        try:
            surface = self._surface_loader(self._loadcoach)
        except LoadCoachUnavailableError as exc:
            return self._end_with(
                trajectory_id, fail, cause=exc.message, error_code=ErrorCode.LOADCOACH_UNAVAILABLE
            )
        except LoadCoachError as exc:
            return self._end_with(
                trajectory_id, halt, cause=exc.message, error_code=ErrorCode(exc.code)
            )
        if loaded.plan is None:
            return self._execute_bypass(loaded, surface, flags)
        return self._execute_plan(loaded, surface, flags)

    def _execute_bypass(
        self, loaded: _Loaded, surface: ProviderSurface, flags: RunSignals
    ) -> TrajectoryState:
        """The bypass path: one run, the synthetic ``loop`` step, to its declared finish."""
        ctx = loaded.ctx
        intent = loaded.live.get(BYPASS_STEP_ID)
        if intent is None:
            return self._end_with(
                ctx.view.trajectory_id,
                fail,
                cause="a bypassed trajectory is executing with no default intent",
                error_code=ErrorCode.LOADCOACH_ERROR,
            )
        run = self._step_run(ctx, BYPASS_STEP_ID, intent, step=None, dependencies={})
        state = self._run_step(ctx, run, surface, flags, all_steps={BYPASS_STEP_ID})
        return state if state is not None else TrajectoryState.COMPLETED

    def _execute_plan(
        self, loaded: _Loaded, surface: ProviderSurface, flags: RunSignals
    ) -> TrajectoryState:
        """The planned path: ready-set dispatch until every step commits or something stops it."""
        ctx = loaded.ctx
        trajectory_id = ctx.view.trajectory_id
        assert loaded.plan is not None  # noqa: S101 — the caller branched on it
        plan = loaded.plan.plan
        all_steps = set(plan.step_ids)
        live = dict(loaded.live)
        while True:
            if flags.lease_lost.is_set():
                raise LeaseLost(trajectory_id)
            if flags.cancel_requested.is_set() or self._cancel_requested(trajectory_id):
                return self._cancel_at_boundary(trajectory_id)
            status = self._step_status(trajectory_id, loaded.plan.plan_id)
            committed = {step_id for step_id, state in status.items() if state == "committed"}
            if committed >= all_steps:
                return self._complete_now(trajectory_id, step_count=len(all_steps))
            ready = plan.ready_steps(committed)
            ungated = [step for step in ready if step.step_id in live]
            if not ungated:
                gated = [step for step in ready if step.step_id not in live]
                if gated:
                    return self._park_for_gated_step(ctx, loaded.plan, gated[0])
                return self._end_with(
                    trajectory_id,
                    fail,
                    cause=(
                        "no step is ready and not every step has committed; the DAG cannot advance"
                    ),
                    error_code=ErrorCode.LOADCOACH_ERROR,
                )
            tier_of = {
                step.step_id: ctx.tier_policy.snapshot.require(live[step.step_id].approved_tier)
                for step in ungated
            }
            chosen = dispatchable(
                ungated,
                in_flight=(),
                tier_of=tier_of,
                max_concurrent_steps=self._settings.execution.max_concurrent_steps,
                max_concurrent_remote_steps=self._settings.execution.max_concurrent_remote_steps,
            )
            if not chosen:  # pragma: no cover — nothing in flight, so the rule always picks one
                chosen = (ungated[0],)
            runs = [
                self._step_run(
                    ctx,
                    step.step_id,
                    live[step.step_id],
                    step=step,
                    dependencies=self._dependency_results(trajectory_id, step),
                )
                for step in chosen
            ]
            state = self._run_steps(ctx, runs, surface, flags, all_steps=all_steps)
            if state is not None:
                return state

    def _run_steps(
        self,
        ctx: GovernanceContext,
        runs: Sequence[_StepRun],
        surface: ProviderSurface,
        flags: RunSignals,
        *,
        all_steps: set[str],
    ) -> TrajectoryState | None:
        """Run the chosen steps — serially, or together when the dispatch rule allowed it."""
        if len(runs) == 1:
            return self._run_step(ctx, runs[0], surface, flags, all_steps=all_steps)
        with ThreadPoolExecutor(max_workers=len(runs)) as pool:
            futures = [
                pool.submit(self._run_step, ctx, run, surface, flags, all_steps=all_steps)
                for run in runs
            ]
            states: list[TrajectoryState | None] = []
            for future in futures:
                try:
                    states.append(future.result())
                except LeaseLost:
                    # Another step ended the trajectory and this one's fence caught it.
                    states.append(None)
        return next((state for state in states if state is not None), None)

    def _run_step(
        self,
        ctx: GovernanceContext,
        run: _StepRun,
        surface: ProviderSurface,
        flags: RunSignals,
        *,
        all_steps: set[str],
    ) -> TrajectoryState | None:
        """Run one step to its declared finish, a park, or a terminal state.

        Returns:
            ``None`` when the step committed and the trajectory continues; a state otherwise.
        """
        trajectory_id = ctx.view.trajectory_id
        router = TierRouter(ctx.tier_policy)
        while True:
            if flags.lease_lost.is_set():
                raise LeaseLost(trajectory_id)
            if flags.cancel_requested.is_set() or self._cancel_requested(trajectory_id):
                return self._cancel_at_boundary(trajectory_id)
            turns = self._threads.turns(run.thread_id)
            pending = self._pending_tool_calls(turns)
            if pending is not None:
                # The last assistant turn asked for tools nobody has run yet — the step was parked
                # for a scoped re-approval, or crashed, between the turn and its calls. Run them
                # under the envelope the step holds *now*, which is what the grant widened.
                last, calls = pending
                state = self._run_tool_calls(
                    ctx, run, turn_id=last.turn_id, sequence=last.sequence, calls=calls
                )
                if state is not None:
                    return state
                continue
            turns_used = sum(1 for turn in turns if turn.role is TurnRole.ASSISTANT)
            if turns_used >= run.intent.max_turns:
                return self._end_with(
                    trajectory_id,
                    halt,
                    cause=(
                        f"step {run.step_id}: the intent's max_turns ({run.intent.max_turns}) is "
                        "spent with no declared finish"
                    ),
                    error_code=ErrorCode.STEP_LIMIT_EXCEEDED,
                )
            try:
                state = self._turn(ctx, run, surface, router, turns, flags, all_steps=all_steps)
            except _StepDone:
                return None
            if state is not None:
                return state

    # ---- steps ------------------------------------------------------------------------------

    def _step_run(
        self,
        ctx: GovernanceContext,
        step_id: str,
        intent: ExecutionIntent,
        *,
        step: PlanStep | None,
        dependencies: Mapping[str, str],
    ) -> _StepRun:
        """Find the step's thread, or open it — ``step.started`` in the write that does."""
        trajectory_id = ctx.view.trajectory_id
        with self._database.read() as session:
            thread_id = session.execute(
                select(models.Thread.id).where(
                    models.Thread.trajectory_id == trajectory_id,
                    models.Thread.step_id == step_id,
                )
            ).scalar_one_or_none()
        if thread_id is not None:
            return _StepRun(step_id=step_id, intent=intent, thread_id=thread_id, step=step)
        now = self._clock()
        thread = Thread(thread_id=self._ids(), owner_id=trajectory_id, created_at=now)
        provenance = intent.provenance(trajectory_id=trajectory_id, tier=intent.approved_tier)
        task_turn = Turn(
            self._ids(),
            thread.thread_id,
            1,
            TurnRole.USER,
            provenance,
            content=ctx.view.task,
            content_sha256=sha256_of(ctx.view.task),
        )
        framing: tuple[Turn[TurnProvenance], tuple[str, str, str]] | None = None
        if step is not None:
            rendered = self._render(
                STEP_EXECUTE_PROMPT_ID,
                {
                    "step_id": step.step_id,
                    "description": step.description,
                    "dependency_results": _render_dependencies(step, dependencies),
                    "tools": ", ".join(sorted(intent.approved_tools)) or "none",
                },
            )
            framing = (
                Turn(
                    self._ids(),
                    thread.thread_id,
                    2,
                    TurnRole.USER,
                    provenance,
                    content=rendered.user,
                    content_sha256=sha256_of(rendered.user),
                ),
                (rendered.prompt_id, rendered.version, rendered.sha256),
            )
        with self._sink.write() as (session, events):
            self._owned_cas(session, trajectory_id, values={"updated_at": now})
            session.add(thread_row(thread, step_id=step_id))
            session.add(turn_row(task_turn))
            if framing is not None:
                session.add(turn_row(framing[0], prompt=framing[1]))
            if step is not None:
                session.execute(
                    update(models.PlanStep)
                    .where(
                        models.PlanStep.step_id == step_id,
                        models.PlanStep.plan_id.in_(_valid_plan_ids(trajectory_id)),
                    )
                    .values(status="running", started_at=now)
                )
            events.append(
                trajectory_id,
                StepStarted(
                    trajectory_id=trajectory_id,
                    step_id=step_id,
                    thread_id=thread.thread_id,
                    intent_id=intent.intent_id,
                    intent_revision=intent.revision,
                    approved_tier=intent.approved_tier,
                    depends_on=step.depends_on if step is not None else (),
                ),
                now=now,
            )
        return _StepRun(step_id=step_id, intent=intent, thread_id=thread.thread_id, step=step)

    def _step_status(self, trajectory_id: str, plan_id: str) -> dict[str, str]:
        with self._database.read() as session:
            return {
                step_id: status
                for step_id, status in session.execute(
                    select(models.PlanStep.step_id, models.PlanStep.status).where(
                        models.PlanStep.plan_id == plan_id
                    )
                ).all()
            }

    def _dependency_results(self, trajectory_id: str, step: PlanStep) -> dict[str, str]:
        """Each dependency's final assistant answer, by step id, for the framing turn."""
        results: dict[str, str] = {}
        with self._database.read() as session:
            for dependency in step.depends_on:
                thread_id = session.execute(
                    select(models.Thread.id).where(
                        models.Thread.trajectory_id == trajectory_id,
                        models.Thread.step_id == dependency,
                    )
                ).scalar_one_or_none()
                if thread_id is None:
                    continue
                last = session.execute(
                    select(models.Turn.content_text)
                    .where(
                        models.Turn.thread_id == thread_id,
                        models.Turn.role == TurnRole.ASSISTANT.value,
                    )
                    .order_by(models.Turn.sequence.desc())
                    .limit(1)
                ).scalar_one_or_none()
                results[dependency] = last or ""
        return results

    def _park_for_gated_step(
        self, ctx: GovernanceContext, recorded: RecordedPlan, step: PlanStep
    ) -> TrajectoryState:
        """T10: a hybrid-gated step became ready and no ungated work is left to run first."""
        view = ctx.view
        now = self._clock()
        verdict = recorded.verdict.verdict_for(step.step_id) if recorded.verdict else None
        reason = (
            gate_reason(verdict.gate, mode=ctx.approval_policy.mode)
            if verdict is not None and verdict.gate is not None
            else VerdictReason.MANUAL_MODE
        )
        outcome = request_approval(TrajectoryState.EXECUTING, request_created=True)
        with self._sink.write() as (session, events):
            request_id = self._approvals.request(
                session,
                events,
                view=view,
                kind=ApprovalKind.GATED_STEP,
                reason=reason,
                step_ids=(step.step_id,),
                detail=None,
                now=now,
            )
            self._owned_cas(
                session,
                view.trajectory_id,
                values={
                    "status": outcome.state.value,
                    "halted_reason": (
                        f"step {step.step_id} is ready and its gate fired ({reason.value}); held "
                        f"for a person (request {request_id})"
                    ),
                    "updated_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
            )
        return outcome.state

    def _complete_now(self, trajectory_id: str, *, step_count: int) -> TrajectoryState:
        """T11 from the dispatcher, for a trajectory resumed after its last step had committed."""
        now = self._clock()
        outcome = complete(TrajectoryState.EXECUTING, all_steps_succeeded=True)
        with self._sink.write() as (session, events):
            self._owned_cas(
                session,
                trajectory_id,
                values={
                    "status": outcome.state.value,
                    "halted_reason": "every step reached a declared finish",
                    "completed_at": now,
                    "updated_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
            )
            events.append(
                trajectory_id,
                TrajectoryCompleted(
                    trajectory_id=trajectory_id, step_count=step_count, turn_count=0
                ),
                now=now,
            )
        return outcome.state

    # ---- the turn ---------------------------------------------------------------------------

    def _turn(
        self,
        ctx: GovernanceContext,
        run: _StepRun,
        surface: ProviderSurface,
        router: TierRouter,
        turns: Sequence[Turn[TurnProvenance]],
        flags: RunSignals,
        *,
        all_steps: set[str],
    ) -> TrajectoryState | None:
        """One turn: the four pre-flights, ``turn.started``, the call, the debit, the turn row.

        **The pre-flights run in this order and the order is a decision** (ADR-0073). Every one of
        them happens before ``turn.started`` and therefore before any request is built, which is
        what makes "refused before any HTTP request leaves" (spec §20 #4, #5) a property of the
        code's shape rather than of a test that happens to check it.

        1. **Egress**, from the trajectory's classification against the tier as configured.
        2. **Pricing**, because unpriced egress is refused rather than treated as free.
        3. **Availability**, the deployment's answer rather than policy's.
        4. **Budget**, the numbers, last.

        The intent's tiers are tried in order. A tier that cannot serve — unavailable, or LoadCoach
        answering ``NO_ELIGIBLE_MODEL`` — falls to the next permitted one; when none can, the
        turn is a ``tier_escalation`` deviation and the deviation policy decides (spec §13).

        Raises:
            _StepDone: The step reached its declared finish and the trajectory continues.
        """
        trajectory_id = ctx.view.trajectory_id
        intent = run.intent
        failure: TierServiceFailure | None = None
        failed_tier: str | None = None
        turn_id = self._ids()
        egress_denied: TrajectoryState | None = None
        try:
            tiers = router.permitted(intent)
        except TierNotConfiguredError as exc:
            return self._end_with(
                trajectory_id, halt, cause=exc.message, error_code=ErrorCode(exc.code)
            )
        for tier in tiers:
            refused = self._egress_preflight(ctx, tier, turn_id=turn_id)
            if refused is not None:
                egress_denied = refused
                continue
            if tier.is_remote and not self._budget.pricing.claiming(
                tier=tier.name, at=self._clock()
            ):
                # Spec §11 contract 5: the check is about the **record**, not the `pricing_file`
                # field — startup already refuses a remote tier naming no file.
                return self._end_with(
                    trajectory_id,
                    halt,
                    cause=(
                        f"remote tier {tier.name} has no ModelPricing record claiming this "
                        "instant, so its egress cannot be priced and is refused rather than "
                        "treated as free (spec §11 contract 5)"
                    ),
                    error_code=ErrorCode.UNPRICED_EGRESS_REFUSED,
                )
            try:
                router.ensure_available(tier)
            except TierUnavailableError as exc:
                reason = str(exc.details.get("reason", ""))
                if reason in _SERVICE_FAILURE_REASONS:
                    failure, failed_tier = _SERVICE_FAILURE_REASONS[reason], tier.name
                    continue
                return self._end_with(
                    trajectory_id, halt, cause=exc.message, error_code=ErrorCode(exc.code)
                )
            parked = self._preflight(ctx, run, tier)
            if parked is not None:
                return parked
            served = self._call(ctx, run, surface, tier, turns, flags, turn_id=turn_id)
            if isinstance(served, TierServiceFailure):
                failure, failed_tier = served, tier.name
                turn_id = self._ids()  # the next tier's announcement is a fresh turn
                continue
            if isinstance(served, TrajectoryState):
                return served
            return self._record_turn(
                ctx,
                run,
                surface,
                tier,
                turns,
                turn_id=turn_id,
                sequence=len(turns) + 1,
                response=served[0],
                overhead_ms=served[1],
                recovered_from_job=None,
                all_steps=all_steps,
            )
        if failure is None:
            return egress_denied
        return self._escalate(
            ctx, run, turns, turn_id=turn_id, failure=failure, failed_tier=failed_tier
        )

    def _call(
        self,
        ctx: GovernanceContext,
        run: _StepRun,
        surface: ProviderSurface,
        tier: Tier,
        turns: Sequence[Turn[TurnProvenance]],
        flags: RunSignals,
        *,
        turn_id: str,
    ) -> tuple[GenerationResponse, float] | TierServiceFailure | TrajectoryState:
        """``turn.started`` in its own write, then ``POST /generate`` under the turn's key.

        Returns:
            The response and this application's overhead, a
            :class:`~promptcadence.domain.deviation.TierServiceFailure` when the tier could not
            serve and the intent's next tier should be tried, or a terminal state.
        """
        del surface  # the subject is verified when the turn is recorded, not here
        trajectory_id = ctx.view.trajectory_id
        sequence = len(turns) + 1
        started = self._clock()
        started_clock = time.perf_counter()
        with self._sink.write() as (session, events):
            self._owned_cas(session, trajectory_id, values={"updated_at": started})
            events.append(
                trajectory_id,
                TurnStarted(
                    trajectory_id=trajectory_id,
                    turn_id=turn_id,
                    sequence=sequence,
                    tier=tier.name,
                    task_profile=tier.task_profile,
                    intent_id=run.intent.intent_id,
                    intent_revision=run.intent.revision,
                ),
                now=started,
            )
        flags.announce(turn_id)
        request = GenerateRequest(
            task=tier.task_profile,
            messages=tuple(
                Message(
                    role=turn.role.value,
                    content=turn.content or "",
                    tool_call_id=turn.tool_call_id,
                )
                for turn in turns
            ),
            idempotency_key=turn_id,
        )
        with correlation(turn_id=turn_id, tier=tier.name):
            try:
                response = self._loadcoach.generate(request)
            except LoadCoachUnavailableError as exc:
                note = self._abandon_in_flight(turn_id)
                return self._end_with(
                    trajectory_id,
                    fail,
                    cause=f"{exc.message}{note}",
                    error_code=ErrorCode.LOADCOACH_UNAVAILABLE,
                )
            except (TierUnavailableError, CompactionFailedError, LoadCoachError) as exc:
                reason = str(exc.details.get("reason", ""))
                if reason in _UNVERIFIED_SUBJECT_REASONS:
                    return self._subject_violation(ctx, tier, turn_id=turn_id, cause=exc.message)
                if reason in _SERVICE_FAILURE_REASONS:
                    return _SERVICE_FAILURE_REASONS[reason]
                note = self._abandon_in_flight(turn_id) if reason == "client_timeout" else ""
                return self._end_with(
                    trajectory_id,
                    halt,
                    cause=f"{exc.message}{note}",
                    error_code=ErrorCode(exc.code),
                )
            finally:
                flags.settle(turn_id)
        overhead_ms = (time.perf_counter() - started_clock) * 1000.0 - float(
            response.timing.total_ms or 0
        )
        return response, max(overhead_ms, 0.0)

    def _escalate(
        self,
        ctx: GovernanceContext,
        run: _StepRun,
        turns: Sequence[Turn[TurnProvenance]],
        *,
        turn_id: str,
        failure: TierServiceFailure,
        failed_tier: str | None,
    ) -> TrajectoryState:
        """No permitted tier could serve: a ``tier_escalation`` deviation, then the policy.

        The facts name no executed tier and no subject — nothing ran — and ``compare`` produces
        exactly one category for that. The deviation is a row and an event with no turn row to
        point at (the turn was announced, never answered), which is why ``deviations.turn_id``
        carries no foreign key since migration 0007.
        """
        trajectory_id = ctx.view.trajectory_id
        intent = run.intent
        prior = [turn for turn in turns if turn.role is TurnRole.ASSISTANT]
        facts = TurnFacts(
            turn_id,
            None,
            None,
            intent.max_classification,
            len(prior) + 1,
            _tokens_spent(prior),
            trajectory_allowlist=ctx.declaration.tool_allowlist,
            tier_service_failure=failure,
        )
        deviations = compare(facts, intent)
        scope = ctx.approval_policy.reapproval_scope
        last_permitted = intent.permitted_tiers[-1]
        next_tier = ctx.tier_policy.next_escalation(last_permitted, intent.max_classification)
        now = self._clock()
        with self._sink.write() as (session, events):
            for deviation in deviations:
                body = DeviationDetected.of(deviation, trajectory_id=trajectory_id, scope=scope)
                session.add(_deviation_row(deviation, trajectory_id=trajectory_id, body=body))
                events.append(trajectory_id, body, now=now)
            escalation = next(
                (d for d in deviations if d.category is DeviationCategory.TIER_ESCALATION), None
            )
            cause_prefix = (
                f"step {run.step_id}: no permitted tier could serve turn {turn_id} "
                f"({failure.value} on {failed_tier or last_permitted})"
            )
            if (
                escalation is not None
                and disposition(escalation, scope=scope) is Disposition.SCOPED_REAPPROVAL
                and next_tier is not None
            ):
                ask = ReapprovalAsk.of(escalation, next_tier=next_tier.name, intent=intent)
                return self._park_for_reapproval(
                    session, events, ctx, run, ask, cause=cause_prefix, now=now
                )
            self._transition(
                session,
                events,
                trajectory_id,
                halt,
                cause=(
                    f"{cause_prefix}; the escalation order is exhausted"
                    if next_tier is None
                    else f"{cause_prefix}; disposition halt"
                ),
                error_code=ErrorCode.TIER_UNAVAILABLE,
                now=now,
            )
            return TrajectoryState.HALTED

    def _park_for_reapproval(
        self,
        session: Session,
        events: EventWriter,
        ctx: GovernanceContext,
        run: _StepRun,
        ask: ReapprovalAsk,
        *,
        cause: str,
        now: datetime,
    ) -> TrajectoryState:
        """T10 for a drift: one request scoped to this step, carrying exactly what it asked."""
        view = ctx.view
        outcome = request_approval(TrajectoryState.EXECUTING, request_created=True)
        request_id = self._approvals.request(
            session,
            events,
            view=view,
            kind=ApprovalKind.REAPPROVAL,
            reason=VerdictReason.SCOPED_REAPPROVAL,
            step_ids=(run.step_id,),
            detail=ask.as_json(),
            now=now,
        )
        self._owned_cas(
            session,
            view.trajectory_id,
            values={
                "status": outcome.state.value,
                "halted_reason": (
                    f"{cause}; scoped re-approval requested for step {run.step_id} "
                    f"({ask.category.value}, request {request_id})"
                ),
                "updated_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
            },
        )
        return outcome.state

    def _record_turn(
        self,
        ctx: GovernanceContext,
        run: _StepRun,
        surface: ProviderSurface,
        tier: Tier,
        turns: Sequence[Turn[TurnProvenance]],
        *,
        turn_id: str,
        sequence: int,
        response: GenerationResponse,
        overhead_ms: float,
        recovered_from_job: str | None,
        all_steps: set[str],
    ) -> TrajectoryState | None:
        """Write the assistant turn and everything the response decides, in one transaction.

        Returns:
            The state the turn decided, or ``None`` to continue the step's loop.

        Raises:
            _StepDone: The step reached its declared finish and the trajectory has other steps
                left; the step is committed with ``step.completed``.
        """
        trajectory_id = ctx.view.trajectory_id
        intent = run.intent
        now = self._clock()
        if not response.completed:
            return self._end_with(
                trajectory_id,
                halt,
                cause=(
                    f"LoadCoach job {response.job_id} ended {response.status!r} rather than "
                    "completed; the turn produced no answer"
                ),
                error_code=ErrorCode.LOADCOACH_ERROR,
                recovered_note=recovered_from_job,
            )
        # The call happened and came back, so the spend is real whatever is decided about the
        # answer below. Debit it **before** the turn row and before any halt: idempotent by
        # `source_ref`, its own write, so a rolled-back turn row does not roll back recorded spend.
        try:
            self._debit_turn(ctx, tier, turn_id=turn_id, response=response, now=now)
        except CurrencyMismatchError as exc:
            return self._end_with(
                trajectory_id, halt, cause=str(exc), error_code=ErrorCode.BUDGET_EXCEEDED
            )
        try:
            subject = resolve_subject(response.model, surface=surface)
        except LoadCoachError as exc:
            return self._subject_violation(ctx, tier, turn_id=turn_id, cause=exc.message)
        decision = decide_finish(
            finish_reason=response.finish_reason,
            schema_validated=response.validation.schema_validated,
            tool_calls_requested=len(response.tool_calls),
            undeclared_reason=response.undeclared_finish_reason,
        )
        if recovered_from_job is not None and not response.validation.checks_reported:
            decision = FinishDecision(
                decision.outcome,
                f"{decision.cause} (reconciled after a crash from LoadCoach job "
                f"{recovered_from_job}'s document, which carries no validation checks)",
                decision.error_code,
            )
        assistant = Turn(
            turn_id,
            run.thread_id,
            sequence,
            TurnRole.ASSISTANT,
            intent.provenance(trajectory_id=trajectory_id, tier=tier.name),
            content=response.text,
            content_sha256=sha256_of(response.text),
            model_canonical_id=response.model.canonical_id,
            finish_reason=response.finish_reason,
            usage=response.usage,
        )
        requested = assemble_tool_calls(response.tool_calls)
        prior_assistant = [turn for turn in turns if turn.role is TurnRole.ASSISTANT]
        spent = _tokens_spent([*prior_assistant, assistant])
        facts = TurnFacts(
            turn_id,
            tier.name,
            subject,
            # What entered the turn, after any operator-flagged path raised it. No flagged path is
            # configured in this build, so what came back is what the envelope admits; the step's
            # declared ceiling is the baseline and a tool result is where a raise would be
            # observed (lifecycle §5).
            intent.max_classification,
            len(prior_assistant) + 1,
            spent,
            requested_tools=_ordered_names(requested),
            trajectory_allowlist=ctx.declaration.tool_allowlist,
            finish_declared=decision.outcome is FinishOutcome.COMPLETE,
        )
        deviations = compare(facts, intent)
        scope = ctx.approval_policy.reapproval_scope
        halting = [d for d in deviations if disposition(d, scope=scope) is Disposition.HALT]
        reapproving = [
            d for d in deviations if disposition(d, scope=scope) is Disposition.SCOPED_REAPPROVAL
        ]
        step_done = False
        with self._sink.write() as (session, events):
            session.add(
                turn_row(
                    assistant,
                    loadcoach_job_id=response.job_id,
                    loadcoach_ms=response.timing.total_ms,
                    overhead_ms=overhead_ms,
                    tool_calls=response.tool_calls,
                )
            )
            events.append(
                trajectory_id,
                TurnCompleted(
                    trajectory_id=trajectory_id,
                    turn_id=turn_id,
                    sequence=sequence,
                    tier=tier.name,
                    model_canonical_id=response.model.canonical_id,
                    loadcoach_job_id=response.job_id,
                    finish_reason=(
                        response.finish_reason.value if response.finish_reason else None
                    ),
                    schema_validated=response.validation.schema_validated,
                    input_tokens=_supported(response.usage.input_tokens),
                    output_tokens=_supported(response.usage.output_tokens),
                    loadcoach_ms=response.timing.total_ms,
                    overhead_ms=int(overhead_ms),
                    decision=decision.outcome,
                ),
                now=now,
            )
            for deviation in deviations:
                body = DeviationDetected.of(deviation, trajectory_id=trajectory_id, scope=scope)
                session.add(_deviation_row(deviation, trajectory_id=trajectory_id, body=body))
                events.append(trajectory_id, body, now=now)
                if deviation.category is DeviationCategory.TIER_VIOLATION:
                    self._egress.record_violation(
                        run_id=trajectory_id,
                        source_ref=turn_id,
                        classification=ctx.declaration.classification,
                        target=tier_target(tier),
                        reason=_violation_reason(deviation),
                        decided_at=now,
                        decision_id=self._ids(),
                        session=session,
                    )
            if recovered_from_job is not None:
                events.append(
                    trajectory_id,
                    TrajectoryRecovered(
                        trajectory_id=trajectory_id,
                        recovered_from=TrajectoryState.EXECUTING,
                        outcome=f"reconciled_completed_job:{recovered_from_job}",
                    ),
                    now=now,
                )
            if halting:
                first = halting[0]
                self._transition(
                    session,
                    events,
                    trajectory_id,
                    halt,
                    cause=(
                        f"deviation {first.category.value} ({first.severity.value}) on turn "
                        f"{turn_id}: disposition halt"
                    ),
                    error_code=ErrorCode.DEVIATION_HALTED,
                    now=now,
                )
                return TrajectoryState.HALTED
            over_limit = self._deviations_on_step(session, intent.intent_id)
            if over_limit > _DEVIATION_LIMIT_PER_STEP:
                self._transition(
                    session,
                    events,
                    trajectory_id,
                    halt,
                    cause=(
                        f"step {run.step_id} has raised {over_limit} deviations, more than the "
                        f"{_DEVIATION_LIMIT_PER_STEP} lifecycle §5 permits on one step"
                    ),
                    error_code=ErrorCode.DEVIATION_HALTED,
                    now=now,
                )
                return TrajectoryState.HALTED
            if reapproving:
                first = reapproving[0]
                next_tier = ctx.tier_policy.next_escalation(
                    intent.permitted_tiers[-1], intent.max_classification
                )
                ask = ReapprovalAsk.of(
                    first, next_tier=next_tier.name if next_tier else None, intent=intent
                )
                return self._park_for_reapproval(
                    session,
                    events,
                    ctx,
                    run,
                    ask,
                    cause=(
                        f"deviation {first.category.value} ({first.severity.value}) on turn "
                        f"{turn_id}: disposition scoped_reapproval"
                    ),
                    now=now,
                )
            if decision.outcome is FinishOutcome.COMPLETE:
                if self._commit_step(
                    session,
                    events,
                    ctx,
                    run,
                    final_turn_id=turn_id,
                    turn_count=sequence,
                    cause=decision.cause,
                    now=now,
                    all_steps=all_steps,
                ):
                    return TrajectoryState.COMPLETED
                step_done = True
            elif decision.outcome is FinishOutcome.CONTINUE:
                cap = self._settings.execution.max_turns_per_step
                round_trips = _round_trips(turns) + 1
                if round_trips > cap:
                    self._transition(
                        session,
                        events,
                        trajectory_id,
                        halt,
                        cause=(
                            f"{decision.cause}; the step's max_turns_per_step ({cap}) is spent "
                            "with no declared finish"
                        ),
                        error_code=ErrorCode.STEP_LIMIT_EXCEEDED,
                        now=now,
                    )
                    return TrajectoryState.HALTED
            else:
                self._transition(
                    session,
                    events,
                    trajectory_id,
                    halt,
                    cause=decision.cause,
                    error_code=decision.error_code or ErrorCode.LOADCOACH_ERROR,
                    now=now,
                )
                return TrajectoryState.HALTED
        if step_done:
            # Raised *after* the write committed: an exception inside the sink's block is a
            # rollback, and a step whose commit rolled back would be dispatched again forever.
            raise _StepDone(run.step_id)
        # The requested calls run on the step loop's next iteration, read back from the row just
        # written, so a crash or a park between this write and their execution loses nothing.
        return None

    def _commit_step(
        self,
        session: Session,
        events: EventWriter,
        ctx: GovernanceContext,
        run: _StepRun,
        *,
        final_turn_id: str,
        turn_count: int,
        cause: str,
        now: datetime,
        all_steps: set[str],
    ) -> bool:
        """``step.completed`` on the caller's session, and T11 too when it was the last step.

        Returns:
            ``True`` when this was the last step and the trajectory completed (T11) in this
            write; ``False`` when other steps remain — the caller signals the dispatcher only
            after the write has committed.
        """
        trajectory_id = ctx.view.trajectory_id
        committed: set[str] = {run.step_id}
        if run.step is not None:
            session.execute(
                update(models.PlanStep)
                .where(
                    models.PlanStep.step_id == run.step_id,
                    models.PlanStep.plan_id.in_(_valid_plan_ids(trajectory_id)),
                )
                .values(status="committed", completed_at=now)
            )
            committed |= {
                step_id
                for step_id, status in session.execute(
                    select(models.PlanStep.step_id, models.PlanStep.status).where(
                        models.PlanStep.plan_id.in_(_valid_plan_ids(trajectory_id))
                    )
                ).all()
                if status == "committed"
            }
        events.append(
            trajectory_id,
            StepCompleted(
                trajectory_id=trajectory_id,
                step_id=run.step_id,
                thread_id=run.thread_id,
                intent_id=run.intent.intent_id,
                intent_revision=run.intent.revision,
                turn_count=turn_count,
                final_turn_id=final_turn_id,
            ),
            now=now,
        )
        if committed < all_steps:
            self._owned_cas(session, trajectory_id, values={"updated_at": now})
            return False
        outcome = complete(TrajectoryState.EXECUTING, all_steps_succeeded=True)
        self._owned_cas(
            session,
            trajectory_id,
            values={
                "status": outcome.state.value,
                "halted_reason": cause,
                "completed_at": now,
                "updated_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
            },
        )
        events.append(
            trajectory_id,
            TrajectoryCompleted(
                trajectory_id=trajectory_id, step_count=len(all_steps), turn_count=turn_count
            ),
            now=now,
        )
        return True

    def _pending_tool_calls(
        self, turns: Sequence[Turn[TurnProvenance]]
    ) -> tuple[Turn[TurnProvenance], tuple[RequestedToolCall, ...]] | None:
        """The last assistant turn's requested calls, when no ``TOOL`` turn has answered them."""
        if not turns:
            return None
        last = turns[-1]
        if last.role is not TurnRole.ASSISTANT or last.finish_reason is not FinishReason.TOOL_CALLS:
            return None
        with self._database.read() as session:
            raw = session.execute(
                select(models.Turn.tool_calls_json).where(models.Turn.id == last.turn_id)
            ).scalar_one_or_none()
        if not raw:
            return None
        return last, assemble_tool_calls([dict(entry) for entry in raw])

    @staticmethod
    def _deviations_on_step(session: Session, intent_id: str) -> int:
        """How many deviations this step's envelope has raised, across its revisions."""
        return len(
            list(
                session.execute(
                    select(models.Deviation.id).where(models.Deviation.intent_id == intent_id)
                ).scalars()
            )
        )

    # ----------------------------------------------------------------------------------------
    # Budget (Phase 5)
    # ----------------------------------------------------------------------------------------

    def _subject_violation(
        self, ctx: GovernanceContext, tier: Tier, *, turn_id: str, cause: str
    ) -> TrajectoryState:
        """Record a ``VIOLATION`` and halt when a turn's execution subject cannot be verified.

        **Absence is a violation, not a pass** (spec §11 contract 4, ADR-0043). The recorded
        target is the tier that *promised* to serve the turn, because where the data went is
        precisely what could not be established.
        """
        trajectory_id = ctx.view.trajectory_id
        self._egress.record_violation(
            run_id=trajectory_id,
            source_ref=turn_id,
            classification=ctx.declaration.classification,
            target=tier_target(tier),
            reason="execution_subject_unverified",
            decided_at=self._clock(),
            decision_id=self._ids(),
        )
        return self._end_with(
            trajectory_id,
            halt,
            cause=(
                f"the execution subject of turn {turn_id} on tier {tier.name} could not be "
                f"verified, which is a violation and not a pass (spec §11 contract 4): {cause}"
            ),
            error_code=ErrorCode.DEVIATION_HALTED,
        )

    def _egress_preflight(
        self, ctx: GovernanceContext, tier: Tier, *, turn_id: str
    ) -> TrajectoryState | None:
        """Decide whether this trajectory's data may reach this tier, and record the verdict.

        Runs **before** ``turn.started`` and before any request is built (spec §20 #4), on
        **every** turn, local tiers included: a local tier is approved with ``target_not_remote``
        rather than skipped, so "every turn carries an egress decision" is checkable by counting.

        Returns:
            ``None`` when the turn may proceed. The halted state when the verdict was a denial —
            ``EGRESS_DENIED`` with the policy's own reason. The caller tries the intent's next
            permitted tier first, and returns this only when none was permitted.
        """
        trajectory_id = ctx.view.trajectory_id
        decision = self._egress.evaluate(
            run_id=trajectory_id,
            source_ref=turn_id,
            classification=ctx.declaration.classification,
            target=tier_target(tier),
        )
        if decision.verdict is Verdict.APPROVED:
            return None
        cause = (
            f"egress to tier {tier.name} was denied for a "
            f"{ctx.declaration.classification.value} trajectory: {decision.reason} "
            f"(decision {decision.decision_id}, policy {decision.policy_name} "
            f"{decision.policy_version})"
        )
        return self._end_with(trajectory_id, halt, cause=cause, error_code=ErrorCode.EGRESS_DENIED)

    def _preflight(
        self, ctx: GovernanceContext, run: _StepRun, tier: Tier
    ) -> TrajectoryState | None:
        """Ask every active ceiling about the next step, and act on the most restrictive answer.

        The **only** place a ceiling stops work. Returns the state the trajectory was moved to —
        ``awaiting_window``, ``awaiting_approval`` or ``halted`` — or ``None`` when every ceiling
        admits the step.
        """
        view = ctx.view
        estimate, priced = self._estimator.estimate(tier=tier.name)
        try:
            position = self._budget.preflight(view, tier=tier.name, estimate=priced)
        except CurrencyMismatchError as exc:
            return self._end_with(
                view.trajectory_id, halt, cause=str(exc), error_code=ErrorCode.BUDGET_EXCEEDED
            )
        binding = position.binding
        if binding is None:
            return None
        budget = self._settings.budget
        cause = (
            f"the {binding.scope} budget refuses the next step on tier {tier.name}: "
            f"{_ceiling_cause(binding)}; the estimate was {estimate.token_estimate} tokens "
            f"(source {estimate.source.value}, {estimate.sample_count} samples)"
        )
        if position.daily_binds:
            policy = budget.on_daily_exhausted
            if policy == "window":
                return self._park_for_window(ctx, cause=cause)
        else:
            policy = budget.on_exhausted
        if policy == "approval":
            return self._request_ceiling_raise(ctx, run, cause=cause, scope=binding.scope)
        return self._end_with(
            view.trajectory_id, halt, cause=cause, error_code=_exceeded_code(binding)
        )

    def _debit_turn(
        self,
        ctx: GovernanceContext,
        tier: Tier,
        *,
        turn_id: str,
        response: GenerationResponse,
        now: datetime,
    ) -> None:
        """Record one turn's spend, once, in its own write with its ``budget.debited`` event.

        Idempotent by ``source_ref``: a turn the ledger already holds a debit for is skipped.

        Raises:
            CurrencyMismatchError: If the turn priced in a currency an active money ceiling caps in
                another. Refused before anything is written (ADR-0030 rule 3).
        """
        view = ctx.view
        if turn_id in self._budget.debited_turn_ids(view.trajectory_id):
            return
        priced = self._budget.price(
            tier=tier.name,
            canonical_id=response.model.canonical_id,
            usage=response.usage,
            at=now,
        )
        with self._sink.write() as (session, events):
            body = self._budget.debit(
                session, view=view, turn_id=turn_id, tier=tier.name, priced=priced, at=now
            )
            events.append(view.trajectory_id, body, now=now)

    def _park_for_window(self, ctx: GovernanceContext, *, cause: str) -> TrajectoryState:
        """T15: park on the per-day ceiling until the next UTC-day edge, releasing the lease."""
        view = ctx.view
        now = self._clock()
        wait = WindowWait(
            parked_from=TrajectoryState.EXECUTING,
            next_edge_at=self._budget.next_day_edge(now),
            days_waited=0,
        )
        outcome = park_for_window(
            TrajectoryState.EXECUTING, wait=wait, lease_released=True, turns_settled=True
        )
        with self._sink.write() as (session, events):
            self._owned_cas(
                session,
                view.trajectory_id,
                values={
                    "status": outcome.state.value,
                    "halted_reason": cause,
                    "window_parked_from": wait.parked_from.value,
                    "window_next_edge_at": wait.next_edge_at,
                    "window_days_waited": wait.days_waited,
                    "updated_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
            )
            events.append(
                view.trajectory_id,
                BudgetWindowWait(
                    trajectory_id=view.trajectory_id,
                    parked_from=wait.parked_from,
                    next_edge_at=wait.next_edge_at,
                    days_waited=wait.days_waited,
                    window_wait_max_days=self._settings.budget.window_wait_max_days,
                ),
                now=now,
            )
        return outcome.state

    def _request_ceiling_raise(
        self, ctx: GovernanceContext, run: _StepRun, *, cause: str, scope: str
    ) -> TrajectoryState:
        """T10: park on one pending request asking for the ceiling to be raised.

        Exactly one ``approval_request`` row exists before the state moves (ADR-0049 rule 6). The
        grant carries the new ceiling; it raises the trajectory's own, because the per-day and
        per-project ceilings are configuration and a grant cannot move them — the request records
        which scope refused so the approver knows whether a raise can help.
        """
        view = ctx.view
        now = self._clock()
        outcome = request_approval(TrajectoryState.EXECUTING, request_created=True)
        with self._sink.write() as (session, events):
            request_id = self._approvals.request(
                session,
                events,
                view=view,
                kind=ApprovalKind.CEILING_RAISE,
                reason=VerdictReason.BUDGET_EXCEEDED,
                step_ids=(run.step_id,),
                detail={"scope": scope, "step_id": run.step_id},
                now=now,
            )
            self._owned_cas(
                session,
                view.trajectory_id,
                values={
                    "status": outcome.state.value,
                    "halted_reason": f"{cause} (request {request_id})",
                    "updated_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
            )
        return outcome.state

    def parked_trajectory_ids(self) -> Sequence[str]:
        """Return every ``awaiting_window`` trajectory, oldest first."""
        with self._database.read() as session:
            return list(
                session.execute(
                    select(models.Trajectory.id)
                    .where(models.Trajectory.status == TrajectoryState.AWAITING_WINDOW.value)
                    .order_by(models.Trajectory.window_next_edge_at, models.Trajectory.id)
                ).scalars()
            )

    def release_window(self, trajectory_id: str) -> TrajectoryState | None:
        """T16/T17: decide what a parked trajectory does now that the clock has been read.

        Returns:
            The state after this pass, or ``None`` when the trajectory is not parked or the row
            moved under this worker.
        """
        now = self._clock()
        with self._database.read() as session:
            row = session.get(models.Trajectory, trajectory_id)
            if row is None or row.status != TrajectoryState.AWAITING_WINDOW.value:
                return None
            view = view_of(row)
        wait = view.window
        if wait is None or now < wait.next_edge_at:
            return TrajectoryState.AWAITING_WINDOW
        crossed = WindowWait(
            parked_from=wait.parked_from,
            next_edge_at=self._budget.next_day_edge(now),
            days_waited=wait.days_waited + 1,
        )
        maximum = self._settings.budget.window_wait_max_days
        admits = not self._day_refuses(view)
        if admits and crossed.days_waited <= maximum:
            return self._resume_from_window(view, wait=crossed)
        if crossed.days_waited >= maximum:
            cause = (
                f"the per-day budget still refused after {crossed.days_waited} UTC day edges, "
                f"the configured window_wait_max_days ({maximum})"
            )
            return self._halt_window_wait(view, wait=crossed, cause=cause)
        return self._keep_parked(view, wait=crossed)

    def _day_refuses(self, view: TrajectoryView) -> bool:
        """Whether the per-day ceiling would still refuse this trajectory's next step."""
        tier = view.tier_override or self._settings.policy.default_tier
        _, priced = self._estimator.estimate(tier=tier)
        try:
            return self._budget.preflight(view, tier=tier, estimate=priced).daily_binds
        except CurrencyMismatchError:
            return True

    def _resume_from_window(self, view: TrajectoryView, *, wait: WindowWait) -> TrajectoryState:
        """T16: the day rolled, the ceiling admits, and this worker takes the lease back."""
        now = self._clock()
        outcome = resume_from_window(
            TrajectoryState.AWAITING_WINDOW,
            wait=wait,
            ceiling_admits=True,
            window_wait_max_days=self._settings.budget.window_wait_max_days,
            lease_acquired=True,
        )
        expires = now + timedelta(seconds=self._settings.execution.lease_seconds)
        with self._sink.write() as (session, events):
            if not self._cas(
                session,
                view.trajectory_id,
                expected=TrajectoryState.AWAITING_WINDOW,
                values={
                    "status": outcome.state.value,
                    "halted_reason": None,
                    "window_parked_from": None,
                    "window_next_edge_at": None,
                    "window_days_waited": wait.days_waited,
                    "lease_owner": self.owner,
                    "lease_expires_at": expires,
                    "updated_at": now,
                },
            ):
                return TrajectoryState.AWAITING_WINDOW
            events.append(
                view.trajectory_id,
                TrajectoryResumed(
                    trajectory_id=view.trajectory_id,
                    resumed_to=outcome.state,
                    days_waited=wait.days_waited,
                ),
                now=now,
            )
        return outcome.state

    def _keep_parked(self, view: TrajectoryView, *, wait: WindowWait) -> TrajectoryState:
        """The day rolled and the new day is already spent: count the edge, wait for the next."""
        now = self._clock()
        with self._sink.write() as (session, events):
            del events
            self._cas(
                session,
                view.trajectory_id,
                expected=TrajectoryState.AWAITING_WINDOW,
                values={
                    "window_next_edge_at": wait.next_edge_at,
                    "window_days_waited": wait.days_waited,
                    "updated_at": now,
                },
            )
        return TrajectoryState.AWAITING_WINDOW

    def _halt_window_wait(
        self, view: TrajectoryView, *, wait: WindowWait, cause: str
    ) -> TrajectoryState:
        """T17: ``window_wait_max_days`` edges passed and the ceiling never admitted."""
        now = self._clock()
        outcome = halt_window_wait(
            TrajectoryState.AWAITING_WINDOW,
            wait=wait,
            window_wait_max_days=self._settings.budget.window_wait_max_days,
            cause=cause,
        )
        with self._sink.write() as (session, events):
            if not self._cas(
                session,
                view.trajectory_id,
                expected=TrajectoryState.AWAITING_WINDOW,
                values={
                    "status": outcome.state.value,
                    "halted_reason": cause,
                    "error_code": ErrorCode.BUDGET_EXCEEDED.value,
                    "window_next_edge_at": wait.next_edge_at,
                    "window_days_waited": wait.days_waited,
                    "completed_at": now,
                    "updated_at": now,
                },
            ):
                return TrajectoryState.AWAITING_WINDOW
            events.append(
                view.trajectory_id,
                TrajectoryHalted(
                    trajectory_id=view.trajectory_id,
                    cause=cause,
                    error_code=ErrorCode.BUDGET_EXCEEDED.value,
                ),
                now=now,
            )
        return outcome.state

    def reconcile_debits(self, trajectory_id: str) -> int:
        """Debit every persisted turn the ledger has not seen. Idempotent by ``source_ref``.

        Returns:
            How many debits this pass wrote. ``0`` on a second pass.
        """
        already = self._budget.debited_turn_ids(trajectory_id)
        with self._database.read() as session:
            row = session.get(models.Trajectory, trajectory_id)
            if row is None:
                return 0
            view = view_of(row)
            pending = [
                _TurnSpend(
                    turn_id=turn.id,
                    tier=turn.tier or "",
                    canonical_id=turn.model_canonical_id or "",
                    usage=_usage_of(turn),
                    at=turn.created_at,
                )
                for turn in session.execute(
                    select(models.Turn)
                    .where(
                        models.Turn.trajectory_id == trajectory_id,
                        models.Turn.role == TurnRole.ASSISTANT.value,
                        models.Turn.id.not_in(already) if already else true(),
                    )
                    .order_by(models.Turn.sequence)
                ).scalars()
            ]
        written = 0
        for spend in pending:
            priced = self._budget.price(
                tier=spend.tier, canonical_id=spend.canonical_id, usage=spend.usage, at=spend.at
            )
            with self._sink.write() as (session, events):
                body = self._budget.debit(
                    session,
                    view=view,
                    turn_id=spend.turn_id,
                    tier=spend.tier,
                    priced=priced,
                    at=spend.at,
                )
                events.append(trajectory_id, body, now=self._clock())
            written += 1
        return written

    # ----------------------------------------------------------------------------------------
    # Tool round trips
    # ----------------------------------------------------------------------------------------

    def _run_tool_calls(
        self,
        ctx: GovernanceContext,
        run: _StepRun,
        *,
        turn_id: str,
        sequence: int,
        calls: Sequence[RequestedToolCall],
    ) -> TrajectoryState | None:
        """Execute one assistant turn's tool calls and append each result as a ``TOOL`` turn.

        **No exception a model can cause escapes this method.** ToolYard resolves everything the
        model chose to a :class:`toolyard.ToolResult`; what is left is the application's own
        failures, each of which halts the trajectory with its cause named.
        """
        trajectory_id = ctx.view.trajectory_id
        try:
            tools = self._tools.for_trajectory(
                trajectory_id, allowlist=ctx.declaration.tool_allowlist
            )
        except ConfigurationError as exc:
            return self._end_with(
                trajectory_id, halt, cause=exc.message, error_code=ErrorCode.TOOL_EXECUTION_FAILED
            )
        position = sequence
        for call in calls:
            position += 1
            state = self._run_one_tool_call(
                ctx, run, tools, turn_id=turn_id, sequence=position, call=call
            )
            if state is not None:
                return state
        return None

    def _tool_egress_ceiling(
        self, ctx: GovernanceContext, *, call: RequestedToolCall, invocation_id: str
    ) -> ToolEgressClass:
        """Decide and record whether one tool call may reach the network.

        Evaluated for the ``NETWORK`` tools only, from the trajectory's classification and never
        the model's text (spec §14); a denial leaves the ceiling closed and ToolYard refuses the
        call as a structured result (ADR-0054).
        """
        entry = self._tools.entry(call.name)
        if entry is None or entry.egress != ToolEgressClass.NETWORK.value:
            return ToolEgressClass.NONE
        url = call.arguments.get("url")
        tools_settings = self._settings.tools
        decision = self._egress.evaluate(
            run_id=ctx.view.trajectory_id,
            source_ref=invocation_id,
            classification=ctx.declaration.classification,
            target=fetch_target(
                host_of(url) if isinstance(url, str) else None,
                allowed_hosts=frozenset(
                    host.lower() for host in tools_settings.fetch_allowed_hosts
                ),
                ceiling=tools_settings.fetch_max_data_classification,
            ),
        )
        if decision.verdict is Verdict.APPROVED:
            return ToolEgressClass.NETWORK
        return ToolEgressClass.NONE

    def _run_one_tool_call(
        self,
        ctx: GovernanceContext,
        run: _StepRun,
        tools: TrajectoryTools,
        *,
        turn_id: str,
        sequence: int,
        call: RequestedToolCall,
    ) -> TrajectoryState | None:
        """Start, execute and record one call. See :meth:`_run_tool_calls` for the shape."""
        trajectory_id = ctx.view.trajectory_id
        invocation_id = self._ids()
        context = tools.context(
            invocation_id,
            approved_tools=run.intent.approved_tools,
            max_egress=self._tool_egress_ceiling(ctx, call=call, invocation_id=invocation_id),
        )
        request = ToolCallRequest(name=call.name, args=call.arguments)
        store = CollectingToolCallStore()
        args_digest = sha256_of(_args_text(call))
        with self._sink.write() as (session, events):
            self._owned_cas(session, trajectory_id, values={"updated_at": self._clock()})
            events.append(
                trajectory_id,
                ToolCallStarted(
                    trajectory_id=trajectory_id,
                    turn_id=turn_id,
                    invocation_id=invocation_id,
                    tool_name=_recorded_name(call.name),
                    args_sha256=args_digest,
                ),
                now=self._clock(),
            )
        with correlation(turn_id=turn_id, tool_call_id=invocation_id):
            try:
                result = tools.executor(store).execute(request, context)
            except StoreFailure as exc:
                return self._end_with(
                    trajectory_id,
                    halt,
                    cause=f"a tool call ran and could not be recorded: {exc}",
                    error_code=ErrorCode.TOOL_EXECUTION_FAILED,
                )
        record = store.records[0] if store.records else None
        limit = self._settings.tools.max_result_chars
        artifact_ref = (
            self._tools.spill(result, result_sha256=record.result_sha256, limit=limit)
            if record is not None
            else None
        )
        shown, truncated = _shown_result(result.content, limit=limit, artifact_ref=artifact_ref)
        now = self._clock()
        tool_turn = Turn(
            self._ids(),
            run.thread_id,
            sequence,
            TurnRole.TOOL,
            run.intent.provenance(trajectory_id=trajectory_id, tier=run.intent.approved_tier),
            content=shown,
            content_sha256=sha256_of(shown),
            tool_call_id=invocation_id,
        )
        with self._sink.write() as (session, events):
            self._owned_cas(session, trajectory_id, values={"updated_at": now})
            session.add(turn_row(tool_turn))
            if record is not None:
                store.flush(
                    session,
                    trajectory_id=trajectory_id,
                    turn_id=turn_id,
                    links=ToolCallLinks(
                        tool_turn_id=tool_turn.turn_id,
                        artifact_ref=artifact_ref,
                        output_truncated=truncated,
                        isolation_tier=_isolation_of(self._tools, record.tool_name),
                    ),
                    row_ids=[self._ids() for _ in store.records],
                )
            events.append(
                trajectory_id,
                ToolCallCompleted(
                    trajectory_id=trajectory_id,
                    turn_id=turn_id,
                    invocation_id=invocation_id,
                    tool_name=_recorded_name(call.name),
                    outcome=outcome_of(result.status),
                    reason=result.reason,
                    duration_ms=result.duration_ms,
                    result_sha256=record.result_sha256 if record is not None else "",
                    artifact_ref=artifact_ref,
                    output_truncated=truncated,
                ),
                now=now,
            )
        return None

    # ----------------------------------------------------------------------------------------
    # Endings
    # ----------------------------------------------------------------------------------------

    def _transition(
        self,
        session: Session,
        events: EventWriter,
        trajectory_id: str,
        row_function: Callable[..., Any],
        *,
        cause: str,
        error_code: ErrorCode,
        now: datetime,
    ) -> None:
        """T12 or T13 on the caller's session: the row and its event, one write."""
        outcome = row_function(TrajectoryState.EXECUTING, cause=cause)
        self._owned_cas(
            session,
            trajectory_id,
            values={
                "status": outcome.state.value,
                "halted_reason": cause,
                "error_code": error_code.value,
                "completed_at": now,
                "updated_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
            },
        )
        body: Any
        if outcome.state is TrajectoryState.HALTED:
            body = TrajectoryHalted(
                trajectory_id=trajectory_id, cause=cause, error_code=error_code.value
            )
        else:
            body = TrajectoryFailed(
                trajectory_id=trajectory_id, cause=cause, error_code=error_code.value
            )
        events.append(trajectory_id, body, now=now)

    def _end_with(
        self,
        trajectory_id: str,
        row_function: Callable[..., Any],
        *,
        cause: str,
        error_code: ErrorCode,
        recovered_note: str | None = None,
    ) -> TrajectoryState:
        """End the trajectory in its own write, from a place with no open transaction."""
        now = self._clock()
        with self._sink.write() as (session, events):
            if recovered_note is not None:
                events.append(
                    trajectory_id,
                    TrajectoryRecovered(
                        trajectory_id=trajectory_id,
                        recovered_from=TrajectoryState.EXECUTING,
                        outcome=f"reconciled_job:{recovered_note}",
                    ),
                    now=now,
                )
            self._transition(
                session,
                events,
                trajectory_id,
                row_function,
                cause=cause,
                error_code=error_code,
                now=now,
            )
        target = row_function(TrajectoryState.EXECUTING, cause=cause).state
        logger.info(
            "trajectory.ended",
            extra={"trajectory_id": trajectory_id, "state": target.value, "cause": cause},
        )
        return TrajectoryState(target)

    def _cancel_at_boundary(
        self, trajectory_id: str, *, from_state: TrajectoryState = TrajectoryState.EXECUTING
    ) -> TrajectoryState:
        """T14 from a lease-holding state, honoured here, at a boundary, in one write."""
        now = self._clock()
        outcome = cancel(from_state, at_turn_boundary=True)
        with self._sink.write() as (session, events):
            self._owned_cas(
                session,
                trajectory_id,
                values={
                    "status": outcome.state.value,
                    "completed_at": now,
                    "updated_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
                expected=from_state,
            )
            events.append(
                trajectory_id,
                TrajectoryCancelled(trajectory_id=trajectory_id, cancelled_from=from_state),
                now=now,
            )
        if from_state is TrajectoryState.PLANNING:
            self._abandon_plan_jobs(trajectory_id)
        return outcome.state

    def _cancel_requested(self, trajectory_id: str) -> bool:
        with self._database.read() as session:
            flag = session.execute(
                select(models.Trajectory.cancel_requested).where(
                    models.Trajectory.id == trajectory_id
                )
            ).scalar_one_or_none()
        return bool(flag)

    def _abandon_in_flight(self, turn_id: str) -> str:
        """Cancel the LoadCoach job a failed call may have started; say what was done."""
        try:
            job = self._loadcoach.find_job(turn_id, states=NON_TERMINAL_JOB_STATES)
            if job is None:
                return ""
            self._loadcoach.cancel_job(job.job_id)
        except (LoadCoachError, LoadCoachUnavailableError) as exc:
            return (
                f" (a LoadCoach job under idempotency key {turn_id} may be in flight and could "
                f"not be cancelled: {exc.message})"
            )
        return f" (LoadCoach job {job.job_id} was cancelled)"

    def _abandon_plan_jobs(self, trajectory_id: str) -> str:
        """Cancel every in-flight planning job of a trajectory; say what was done.

        Lifecycle §8.3's ``planning`` recovery edge and T14 from ``planning`` both need it: the
        keys are ``plan:<trajectory>:<session>:<attempt>``, so this pages this application's
        non-terminal jobs and matches the prefix.
        """
        prefix = plan_job_key_prefix(trajectory_id)
        cancelled: list[str] = []
        try:
            cursor: str | None = None
            while True:
                page, cursor = self._loadcoach.list_jobs(
                    states=NON_TERMINAL_JOB_STATES, cursor=cursor
                )
                for job in page:
                    if job.idempotency_key and job.idempotency_key.startswith(prefix):
                        self._loadcoach.cancel_job(job.job_id)
                        cancelled.append(job.job_id)
                if cursor is None:
                    break
        except (LoadCoachError, LoadCoachUnavailableError) as exc:
            return f" (in-flight planning jobs could not be cancelled: {exc.message})"
        if not cancelled:
            return ""
        return f" (LoadCoach planning job(s) {', '.join(cancelled)} cancelled)"

    def cancel_in_flight(self, turn_id: str) -> str | None:
        """Cancel the in-flight LoadCoach job for ``turn_id``, if one exists. Best effort."""
        try:
            job = self._loadcoach.find_job(turn_id, states=NON_TERMINAL_JOB_STATES)
            if job is None:
                return None
            self._loadcoach.cancel_job(job.job_id)
        except (LoadCoachError, LoadCoachUnavailableError):
            return None
        return job.job_id

    # ----------------------------------------------------------------------------------------
    # Recovery (lifecycle §8.3, ADR-0036)
    # ----------------------------------------------------------------------------------------

    def reconcile(self, trajectory_id: str, *, require_expired: bool) -> ReconcileOutcome:
        """Recover one ``executing`` trajectory whose worker is gone.

        Returns:
            :attr:`ReconcileOutcome.RESUMED` — this worker holds the lease and the loop may run;
            :attr:`ReconcileOutcome.FINISHED` — the reconciled turn completed the trajectory;
            :attr:`ReconcileOutcome.HALTED` — unreconcilable, halted ``recovered_after_crash``;
            :attr:`ReconcileOutcome.DEFERRED` — LoadCoach could not be reached; the next pass
            tries again.
        """
        now = self._clock()
        events = self._sink.events(trajectory_id)
        with self._database.read() as session:
            row = session.get(models.Trajectory, trajectory_id)
            if row is None or row.status != TrajectoryState.EXECUTING.value:
                return ReconcileOutcome.DEFERRED
        self.reconcile_debits(trajectory_id)
        with self._database.read() as session:
            row = session.get(models.Trajectory, trajectory_id)
            if row is None:  # pragma: no cover — the row was checked one statement ago
                return ReconcileOutcome.DEFERRED
            if require_expired and not (
                row.lease_expires_at is None or row.lease_expires_at <= now
            ):
                return ReconcileOutcome.DEFERRED
            committed_turn_ids = set(
                session.execute(
                    select(models.Turn.id).where(models.Turn.trajectory_id == trajectory_id)
                ).scalars()
            )
        dangling = _dangling_turn(events, committed_turn_ids)
        if dangling is None:
            return self._take_over(trajectory_id, outcome="resumed", now=now)
        turn_id = str(dangling.data["turn_id"])
        try:
            in_flight = self._loadcoach.find_job(turn_id, states=NON_TERMINAL_JOB_STATES)
            if in_flight is not None:
                # The lease first, the cancel second. A stalled worker whose call returns the
                # cancelled document would otherwise halt the trajectory itself in the instant
                # between the cancel and the takeover; once the lease has moved, its write is
                # fenced and it stops.
                taken = self._take_over(
                    trajectory_id,
                    outcome=f"cancelled_in_flight_job:{in_flight.job_id}",
                    now=now,
                )
                if taken is ReconcileOutcome.RESUMED:
                    self._loadcoach.cancel_job(in_flight.job_id)
                return taken
            finished = self._loadcoach.find_job(turn_id)
        except LoadCoachUnavailableError:
            logger.warning("trajectory.recovery_deferred", extra={"trajectory_id": trajectory_id})
            return ReconcileOutcome.DEFERRED
        except LoadCoachError as exc:
            return self._halt_recovered(trajectory_id, detail=exc.message, now=now)
        if finished is None:
            return self._halt_recovered(
                trajectory_id,
                detail=(
                    f"turn {turn_id} started and no LoadCoach job holds its idempotency key; "
                    "the in-flight work cannot be reconciled"
                ),
                now=now,
            )
        if finished.state != "completed":
            return self._take_over(
                trajectory_id, outcome=f"job_{finished.state}:{finished.job_id}", now=now
            )
        taken = self._take_over(trajectory_id, outcome=None, now=now)
        if taken is not ReconcileOutcome.RESUMED:  # pragma: no cover — the CAS raced
            return taken
        try:
            loaded = self._load(trajectory_id, expected=TrajectoryState.EXECUTING)
            surface = self._surface_loader(self._loadcoach)
            run = self._run_of_announced_turn(loaded, dangling)
            tier = TierRouter(loaded.ctx.tier_policy).resolve(run.intent)
            response = parse_generation(finished.document)
        except LoadCoachUnavailableError:
            return ReconcileOutcome.DEFERRED
        except (ValidationError, LoadCoachError, TierUnavailableError, LeaseLost) as exc:
            return self._halt_recovered(trajectory_id, detail=str(exc), now=now)
        turns = self._threads.turns(run.thread_id)
        all_steps = set(loaded.plan.plan.step_ids) if loaded.plan is not None else {BYPASS_STEP_ID}
        try:
            state = self._record_turn(
                loaded.ctx,
                run,
                surface,
                tier,
                turns,
                turn_id=turn_id,
                sequence=len(turns) + 1,
                response=response,
                overhead_ms=0.0,
                recovered_from_job=finished.job_id,
                all_steps=all_steps,
            )
        except _StepDone:
            return ReconcileOutcome.RESUMED
        except LeaseLost:
            return ReconcileOutcome.DEFERRED
        if state is None:
            return ReconcileOutcome.RESUMED
        return ReconcileOutcome.FINISHED

    def _run_of_announced_turn(self, loaded: _Loaded, announced: StoredEvent) -> _StepRun:
        """The step run a dangling ``turn.started`` belongs to, from the intent it named."""
        intent_id = str(announced.data.get("intent_id", ""))
        for step_id, intent in loaded.live.items():
            if intent.intent_id == intent_id:
                step = (
                    loaded.plan.plan.step(step_id)
                    if loaded.plan is not None and step_id != BYPASS_STEP_ID
                    else None
                )
                return self._step_run(loaded.ctx, step_id, intent, step=step, dependencies={})
        message = f"turn.started names intent {intent_id!r}, which no live envelope matches"
        raise ValidationError(message, details={"field": "intent_id"})

    def _take_over(
        self, trajectory_id: str, *, outcome: str | None, now: datetime
    ) -> ReconcileOutcome:
        """Re-claim the lease and, when ``outcome`` is given, record ``trajectory.recovered``."""
        expires = now + timedelta(seconds=self._settings.execution.lease_seconds)
        with self._sink.write() as (session, events):
            if not self._cas(
                session,
                trajectory_id,
                expected=TrajectoryState.EXECUTING,
                values={"lease_owner": self.owner, "lease_expires_at": expires, "updated_at": now},
            ):
                return ReconcileOutcome.DEFERRED
            if outcome is not None:
                events.append(
                    trajectory_id,
                    TrajectoryRecovered(
                        trajectory_id=trajectory_id,
                        recovered_from=TrajectoryState.EXECUTING,
                        outcome=outcome,
                    ),
                    now=now,
                )
        return ReconcileOutcome.RESUMED

    def _halt_recovered(
        self, trajectory_id: str, *, detail: str, now: datetime
    ) -> ReconcileOutcome:
        """The unreconcilable edge: ``halted`` with ``recovered_after_crash`` (lifecycle §8.3)."""
        cause = f"recovered_after_crash: {detail}"
        with self._sink.write() as (session, events):
            if not self._cas(
                session,
                trajectory_id,
                expected=TrajectoryState.EXECUTING,
                values={
                    "status": TrajectoryState.HALTED.value,
                    "halted_reason": cause,
                    "error_code": ErrorCode.LOADCOACH_ERROR.value,
                    "completed_at": now,
                    "updated_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
            ):
                return ReconcileOutcome.DEFERRED
            events.append(
                trajectory_id,
                TrajectoryHalted(
                    trajectory_id=trajectory_id,
                    cause=cause,
                    error_code=ErrorCode.LOADCOACH_ERROR.value,
                ),
                now=now,
            )
        return ReconcileOutcome.HALTED

    def redraft(self, trajectory_id: str, *, now: datetime) -> bool:
        """Recovery for ``planning``: re-claim, cancel any in-flight plan job, and redraft.

        Lifecycle §8.3: drafting has no side effects to reconcile, so the partial draft is simply
        left on the record (every attempt already is a row) and :meth:`run` drafts again under
        this worker's lease. Emits ``trajectory.recovered``.

        Returns:
            ``True`` when this worker now holds the lease and the caller should run the
            trajectory; ``False`` when the row moved under it.
        """
        expires = now + timedelta(seconds=self._settings.execution.lease_seconds)
        with self._database.write() as session:
            if not self._cas(
                session,
                trajectory_id,
                expected=TrajectoryState.PLANNING,
                values={"lease_owner": self.owner, "lease_expires_at": expires, "updated_at": now},
            ):
                return False
        # The lease first, the cancel second (see ``reconcile``): a stalled planner whose call
        # returns the cancelled document would otherwise fail the trajectory itself before the
        # takeover fenced it.
        note = self._abandon_plan_jobs(trajectory_id)
        with self._sink.write() as (session, events):
            self._owned_cas(
                session,
                trajectory_id,
                values={"updated_at": now},
                expected=TrajectoryState.PLANNING,
            )
            events.append(
                trajectory_id,
                TrajectoryRecovered(
                    trajectory_id=trajectory_id,
                    recovered_from=TrajectoryState.PLANNING,
                    outcome=f"redraft{note}",
                ),
                now=now,
            )
        return True


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _valid_plan_ids(trajectory_id: str) -> Any:
    """A subquery naming the trajectory's validated plan row(s)."""
    return select(models.Plan.id).where(
        models.Plan.trajectory_id == trajectory_id, models.Plan.valid.is_(True)
    )


def _render_dependencies(step: PlanStep, results: Mapping[str, str]) -> str:
    """The RESULTS OF EARLIER STEPS block of a framing turn, or an empty string."""
    if not step.depends_on:
        return ""
    lines = ["", "RESULTS OF EARLIER STEPS"]
    for dependency in step.depends_on:
        lines.append(f"- {dependency}: {results.get(dependency, '') or '(no answer recorded)'}")
    lines.append("")
    return "\n".join(lines)


def _violation_reason(deviation: Deviation) -> str:
    """Describe a ``tier_violation`` in the vocabulary of the record, not of the log."""
    subject = deviation.subject
    served = subject.egress_class.value if subject is not None else "unknown"
    permitted = "|".join(deviation.permitted_tiers) or "none"
    return (
        f"tier_violation:served_{served}:executed_{deviation.executed_tier}:permitted_{permitted}"
    )


def _deviation_row(
    deviation: Deviation, *, trajectory_id: str, body: DeviationDetected
) -> models.Deviation:
    """Map one deviation onto its row: an event *and* a row (lifecycle §5)."""
    return models.Deviation(
        id=new_id(),
        trajectory_id=trajectory_id,
        turn_id=deviation.turn_id,
        intent_id=deviation.intent_id,
        intent_revision=deviation.intent_revision,
        category=deviation.category.value,
        severity=deviation.severity.value,
        disposition=body.disposition.value,
        reapprovable=body.reapprovable,
        detail_json=deviation.as_canonical(),
    )


def _dangling_turn(events: Sequence[StoredEvent], committed: set[str]) -> StoredEvent | None:
    """The last ``turn.started`` whose turn never got a row — the in-flight work at the crash.

    A ``turn.started`` followed by a ``deviation.detected`` naming the same turn is not dangling:
    that turn was announced and no tier could serve it, and the escalation already recorded it.
    """
    escalated = {
        str(event.data.get("turn_id"))
        for event in events
        if event.event_type == "deviation.detected"
        and event.data.get("category") == DeviationCategory.TIER_ESCALATION.value
    }
    for event in reversed(events):
        if event.event_type == "turn.started":
            turn_id = event.data.get("turn_id")
            return None if turn_id in committed or turn_id in escalated else event
    return None


def _tokens_spent(turns: Sequence[Turn[TurnProvenance]]) -> int:
    """Input plus output tokens over the assistant turns, skipping unreported classes (a floor)."""
    total = 0
    for turn in turns:
        if turn.usage is None:
            continue
        for value in (turn.usage.input_tokens, turn.usage.output_tokens):
            if is_supported(value) and isinstance(value, int):
                total += value
    return total


def _supported(value: object) -> int | None:
    return value if isinstance(value, int) and is_supported(value) else None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _round_trips(turns: Sequence[Turn[TurnProvenance]]) -> int:
    """Count the tool round trips already spent in this step: assistant turns declaring tools."""
    return sum(
        1
        for turn in turns
        if turn.role is TurnRole.ASSISTANT and turn.finish_reason is FinishReason.TOOL_CALLS
    )


def _ordered_names(calls: Sequence[RequestedToolCall]) -> tuple[str, ...]:
    """The distinct tool names a turn requested, in first-requested order, empty names dropped."""
    return tuple(dict.fromkeys(call.name for call in calls if call.name))


def _args_text(call: RequestedToolCall) -> str:
    """The text whose digest identifies one call's arguments in the ``tool.call.started`` event."""
    if call.arguments_parsed:
        return canonical_json(call.arguments)
    return call.arguments if isinstance(call.arguments, str) else repr(call.arguments)


def _recorded_name(name: str) -> str:
    """Cap a model-chosen tool name for an event body, the way ToolYard caps it for a record."""
    cleaned = name.replace("\x00", "")
    return cleaned[:MAX_RECORDED_NAME_CHARS]


def _shown_result(content: str, *, limit: int, artifact_ref: str | None) -> tuple[str, bool]:
    """Cap what the model sees of a tool result, and label the cap when there is one."""
    if len(content) <= limit:
        return content, False
    location = f"; full output recorded as {artifact_ref}" if artifact_ref else ""
    label = f"\n[truncated by promptcadence: {limit} of {len(content)} characters shown{location}]"
    return content[:limit] + label, True


def _isolation_of(plant: ToolPlant, tool_name: str) -> str | None:
    """The isolation rung to record for one call, or ``None`` when the tool runs no process."""
    entry = plant.entry(tool_name)
    if entry is None or not entry.requires_isolation:
        return None
    return plant.isolation().tier.value


@dataclass(frozen=True, slots=True)
class _TurnSpend:
    """One persisted turn's facts, read out of the row so the debit can be re-derived from it."""

    turn_id: str
    tier: str
    canonical_id: str
    usage: TokenUsage
    at: datetime


def _usage_of(row: models.Turn) -> TokenUsage:
    """Rebuild a turn's usage from its row, keeping "not reported" distinct from zero."""
    return TokenUsage(
        input_tokens=row.input_tokens if row.input_tokens is not None else UNSUPPORTED,
        output_tokens=row.output_tokens if row.output_tokens is not None else UNSUPPORTED,
        cache_write_tokens=(
            row.cache_write_tokens if row.cache_write_tokens is not None else UNSUPPORTED
        ),
        cache_read_tokens=(
            row.cache_read_tokens if row.cache_read_tokens is not None else UNSUPPORTED
        ),
    )


def _exceeded_code(headroom: BudgetHeadroom) -> ErrorCode:
    """Which error code a refused pre-flight carries."""
    if headroom.tokens_remaining is not None and headroom.tokens_remaining < 0:
        return ErrorCode.TOKEN_BUDGET_EXCEEDED
    return ErrorCode.BUDGET_EXCEEDED


def _ceiling_cause(headroom: BudgetHeadroom) -> str:
    """Say which bound refused, and say it about a **pre-flight** rather than about history."""
    kind = "tokens" if _exceeded_code(headroom) is ErrorCode.TOKEN_BUDGET_EXCEEDED else "money"
    left = (
        render_remaining_tokens(headroom.tokens_remaining, is_floor=False)
        if kind == "tokens"
        else render_remaining_money(headroom.money_remaining, is_floor=False)
    )
    parts = [
        f"the {kind} cap cannot admit it — counting this step the cap is over by {left.lstrip('-')}"
    ]
    if headroom.partial_pricing is PartialPricing.STRICT and headroom.untotalled_debit_count:
        parts.append(
            f"partial_pricing is strict and {headroom.untotalled_debit_count} debit(s) in this "
            "window carried an estimate that did not total, which counts as exceeding"
        )
    elif headroom.money_is_floor:
        parts.append(
            f"the money in this window is a floor over {headroom.unpriced_debit_count} debit(s) "
            "that could not be fully priced — this step included — so what is left is at most "
            f"{render_remaining_money(headroom.money_remaining, is_floor=False)}"
        )
    if headroom.tokens_are_floor:
        parts.append(
            f"{headroom.unmetered_debit_count} debit(s) left a token class unreported, so the "
            "token balance is a floor too"
        )
    return "; ".join(parts)
