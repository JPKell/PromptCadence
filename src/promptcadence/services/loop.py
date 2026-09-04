"""promptcadence.services.loop — the bypass path: claim, mint, turn, record, finish.

The simplest thing that executes a turn, built first so every later phase adds a governance
layer to *both* paths at once (development plan). What it does, in order, and where each step
commits:

1. **Claim (T3)** — one write: the trajectory row moves ``queued → executing`` under this
   worker's lease, the thread is opened with the caller's task as its first turn, the default
   :class:`~promptcadence.domain.intent.ExecutionIntent` is minted and its row written, and
   ``trajectory.claimed`` + ``intent.minted`` are appended (ADR-0044). The intent is minted by
   :func:`~promptcadence.domain.intent.mint_bypass_default` and by nothing else.
2. **Turn** — ``turn.started`` in its own write *before* the LoadCoach call, carrying the
   ``turn_id`` that is also the request's ``idempotency_key``; then ``POST /generate``; then one
   write holding the turn row, ``turn.completed``, every deviation as a row and an event, and —
   when the turn decides the trajectory — the terminal transition and its event.
3. **Finish** — only on what was *declared*: :func:`~promptcadence.domain.turns.decide_finish`
   reads the provider's ``finish_reason`` and LoadCoach's schema validation and nothing else.
   ``LENGTH``, ``ERROR`` and absence halt with the cause named; a model never decides control
   flow.

**Governance is not bypassed.** Every turn runs under the intent, every response's subject is
verified against the provider surface read before it (spec §11 contract 4), and
:func:`~promptcadence.domain.deviation.compare` runs on every turn — identical to what the planned
path will run, with no mode branch. In this phase a deviation that is not ``continue_recorded``
halts: scoped re-approval is Phase 7's, and continuing past a drift nobody can approve would be
the silent continuation lifecycle §5 forbids.

**What a lease means here.** Every write to the trajectory row is a compare-and-set on
``(id, status='executing', lease_owner=<this worker>)``. A worker whose lease was taken over —
by recovery after it stalled — cannot commit a turn, a halt or a completion: its next write
affects zero rows and raises :class:`LeaseLost`, and it stops. That is the fence that turns a
lease race into a lost turn rather than a duplicated one.

**Every intent is re-minted, never rehydrated.** A resumed trajectory needs its intent object,
and there is no path that constructs one from a row (``test_no_module_mints_an_intent_outside_
domain_intent``). So :meth:`LoopController.run` re-mints the default intent from the same inputs
the claim used — the recorded declaration, the recorded tier snapshot, the recorded ``minted_at``
and id — and refuses to run unless the result is byte-identical to the row it wrote. A
configuration change that would alter the envelope halts the trajectory naming the mismatch
rather than running turns under an envelope nobody minted.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
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
from baseaicore.timeutil import to_rfc3339
from sqlalchemy import select, true, update
from toolyard import MAX_RECORDED_NAME_CHARS, StoreFailure, ToolCallRequest

from promptcadence.config import ConfigurationError
from promptcadence.domain.deviation import (
    Deviation,
    DeviationDetected,
    Disposition,
    TurnFacts,
    compare,
    disposition,
)
from promptcadence.domain.errors import (
    CompactionFailedError,
    ErrorCode,
    IllegalTransitionError,
    LoadCoachError,
    LoadCoachUnavailableError,
    TierNotConfiguredError,
    TierUnavailableError,
)
from promptcadence.domain.intent import ExecutionIntent, IntentMinted, mint_bypass_default
from promptcadence.domain.policy import (
    ApprovalRequested,
    BudgetHeadroom,
    PartialPricing,
    VerdictReason,
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
from promptcadence.infrastructure.db.models import ExecutionIntent as ExecutionIntentRow
from promptcadence.infrastructure.loadcoach import (
    NON_TERMINAL_JOB_STATES,
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
from promptcadence.services.budget import (
    CurrencyMismatchError,
    render_remaining_money,
    render_remaining_tokens,
)
from promptcadence.services.loadcoach_surface import (
    ProviderSurface,
    load_provider_surface,
    resolve_subject,
)
from promptcadence.services.policy_assembly import (
    approval_policy_from_settings,
    tier_snapshot_from_document,
)
from promptcadence.services.tools import ToolPlant, TrajectoryTools, outcome_of
from promptcadence.services.views import TrajectoryView, declaration_of, view_of

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy import CursorResult
    from sqlalchemy.orm import Session

    from promptcadence.config import Settings
    from promptcadence.domain.intent import TurnProvenance
    from promptcadence.domain.policy import ApprovalPolicy
    from promptcadence.domain.trajectory import TrajectoryDeclaration
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

_STOPPING_DISPOSITIONS: Final[frozenset[Disposition]] = frozenset(
    {Disposition.HALT, Disposition.SCOPED_REAPPROVAL}
)
"""The dispositions that end a trajectory in this phase, and the two that do not.

:attr:`~promptcadence.domain.deviation.Disposition.CONTINUE_RECORDED` was already a continuation.
:attr:`~promptcadence.domain.deviation.Disposition.REFUSED_NOT_REAPPROVABLE` joins it at Phase 4,
and that is the phase's whole change to deviation handling: lifecycle §5 says a tool outside the
**trajectory** allowlist has *the call* refused outright and recorded — never re-approvable,
because the allowlist is the caller's — and says nothing about the trajectory ending. Before tools
executed there was no way to refuse a call, so the only available reading of that cell was a halt.
Now there is: ToolYard returns ``not_allowlisted``, the result is fed back as a ``TOOL`` turn, and
the model gets to answer without it.

:attr:`~promptcadence.domain.deviation.Disposition.SCOPED_REAPPROVAL` still halts, because the
approver arrives at Phase 7 and continuing past a drift nobody can approve is the silent
continuation lifecycle §5 forbids.
"""

_PLANNER_ABSENT: Final = (
    "planning is not available before Phase 7; submit with bypass_planning=true (or set "
    "[planning] enabled = false) until the planner lands"
)


class LeaseLost(Exception):  # noqa: N818 — an internal signal, not a caller-facing error
    """This worker no longer holds the trajectory's lease; stop without committing anything."""


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

    def resolve(self, intent: ExecutionIntent) -> Tier:
        """Return the intent's approved tier, if it can serve.

        Raises:
            TierUnavailableError: The tier cannot serve right now — today only
                ``loadcoach_has_no_remote_provider``. The automatic policy grants no fallbacks
                (lifecycle §3), so there is nothing to fall to and the loop halts with the reason.
            TierNotConfiguredError: The intent names a tier the snapshot does not define.
        """
        availability = self.tier_policy.availability(intent.approved_tier)
        if not availability.available:
            reason = availability.reason.value if availability.reason is not None else "unknown"
            message = f"tier {intent.approved_tier!r} cannot serve: {reason}"
            raise TierUnavailableError(
                message, details={"reason": reason, "tier": intent.approved_tier}
            )
        return self.tier_policy.snapshot.require(intent.approved_tier)


class ReconcileOutcome(StrEnum):
    """What recovery did with one ``executing`` trajectory (lifecycle §8.3)."""

    RESUMED = "resumed"
    HALTED = "halted"
    FINISHED = "finished"
    DEFERRED = "deferred"


@dataclass
class RunSignals:
    """The flags a lease keeper raises and the loop reads at every boundary."""

    cancel_requested: threading.Event
    lease_lost: threading.Event
    in_flight_turn_id: str | None = None

    @classmethod
    def fresh(cls) -> RunSignals:
        """Signals with nothing raised."""
        return cls(cancel_requested=threading.Event(), lease_lost=threading.Event())


@dataclass(frozen=True, slots=True)
class _Governance:
    """Everything a run needs beyond the row: the declaration, the policies and the intent."""

    view: TrajectoryView
    declaration: TrajectoryDeclaration
    tier_policy: TierPolicy
    approval_policy: ApprovalPolicy
    intent: ExecutionIntent
    thread_id: str


class LoopController:
    """Claim, run and reconcile bypassed trajectories. One per worker thread."""

    __slots__ = (
        "_budget",
        "_clock",
        "_database",
        "_estimator",
        "_ids",
        "_loadcoach",
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
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] = new_id,
        surface_loader: Callable[[LoadCoachClient], ProviderSurface] = load_provider_surface,
        tools: ToolPlant | None = None,
    ) -> None:
        """Bind the controller to one worker's identity and the process's handles.

        Args:
            database: The application's database handle.
            sink: The event sink every write goes through.
            loadcoach: The LoadCoach client.
            settings: The validated configuration.
            owner: This worker's lease owner id, ``<process prefix>/<thread index>``.
            budget: The ceilings, the ledger and the debits (P5). Required rather than optional:
                a loop that could run without one would be a loop that could spend without
                recording it, and every path below asks it something before it calls LoadCoach.
            estimator: The layered step estimator the pre-flight asks for a prospective spend.
            clock: The instant source, injected for determinism.
            id_factory: The id source for turns and intents.
            surface_loader: How the provider surface is read; injected so a test can script it.
            tools: The process's registry, sandbox and artifact store, or ``None`` to build one
                from ``[tools]``. Injected so a test can shape the isolation probe's view of the
                host — and so a worker's threads share one probe rather than each paying for it.
        """
        self._database = database
        self._sink = sink
        self._loadcoach = loadcoach
        self._settings = settings
        self.owner = owner
        self._budget = budget
        self._estimator = estimator
        self._clock = clock if clock is not None else _utc_now
        self._ids = id_factory
        self._surface_loader = surface_loader
        self._threads = SqlThreadStore(database.sessions)
        self._tools = tools if tools is not None else ToolPlant(settings)

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

    def claim(self, trajectory_id: str) -> TrajectoryState | None:
        """Claim a queued trajectory: T3 for a bypassed one, T2 then T7 for a planned one.

        Both are one write. A planned trajectory cannot be served before Phase 7, and leaving it
        ``queued`` for a planner that does not exist would be a trajectory that never reports
        why it is not moving; it is claimed and failed with the cause on the row instead.

        Args:
            trajectory_id: The trajectory.

        Returns:
            The state after the claim (``executing`` or ``failed``), or ``None`` when another
            worker claimed it first — the compare-and-set affected no row.
        """
        now = self._clock()
        with self._sink.write() as (session, events):
            row = session.get(models.Trajectory, trajectory_id)
            if row is None or row.status != TrajectoryState.QUEUED.value:
                return None
            view = view_of(row)
            if not view.bypass_planning:
                return self._fail_planned(session, events, view, now=now)
            governance = self._mint(session, view, intent_id=self._ids(), minted_at=now)
            outcome = claim_for_bypass(
                TrajectoryState.QUEUED,
                bypass_permitted=True,
                lease_acquired=True,
                default_intent_minted=True,
            )
            expires = now + timedelta(seconds=self._settings.execution.lease_seconds)
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
            thread = Thread(thread_id=self._ids(), owner_id=trajectory_id, created_at=now)
            session.add(thread_row(thread))
            session.add(_intent_row(governance.intent))
            task_turn = Turn(
                self._ids(),
                thread.thread_id,
                1,
                TurnRole.USER,
                governance.intent.provenance(
                    trajectory_id=trajectory_id, tier=governance.intent.approved_tier
                ),
                content=view.task,
                content_sha256=sha256_of(view.task),
            )
            session.add(turn_row(task_turn))
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
            events.append(trajectory_id, IntentMinted.of(governance.intent), now=now)
            return outcome.state

    def _fail_planned(
        self, session: Session, events: EventWriter, view: TrajectoryView, *, now: datetime
    ) -> TrajectoryState | None:
        """T2 then T7 in one transaction: claimed for planning, failed for want of a planner."""
        claimed = claim_for_planning(
            TrajectoryState.QUEUED, planning_enabled=True, lease_acquired=True
        )
        failed = fail(claimed.state, cause=_PLANNER_ABSENT)
        if not self._cas(
            session,
            view.trajectory_id,
            expected=TrajectoryState.QUEUED,
            values={
                "status": failed.state.value,
                "halted_reason": _PLANNER_ABSENT,
                "error_code": ErrorCode.PLAN_DRAFT_FAILED.value,
                "completed_at": now,
                "updated_at": now,
            },
        ):
            return None
        events.append(
            view.trajectory_id,
            TrajectoryClaimed(
                trajectory_id=view.trajectory_id,
                state=claimed.state,
                worker_id=self.owner,
                lease_expires_at=now,
            ),
            now=now,
        )
        events.append(
            view.trajectory_id,
            TrajectoryFailed(
                trajectory_id=view.trajectory_id,
                cause=_PLANNER_ABSENT,
                error_code=ErrorCode.PLAN_DRAFT_FAILED.value,
            ),
            now=now,
        )
        return failed.state

    # ----------------------------------------------------------------------------------------
    # Leases
    # ----------------------------------------------------------------------------------------

    def renew_lease(self, trajectory_id: str) -> tuple[bool, bool]:
        """Extend this worker's lease by ``lease_seconds``.

        Returns:
            ``(renewed, cancel_requested)``. ``renewed`` is false when the row is no longer this
            worker's — it was recovered by another, or it left ``executing`` — and the caller
            must stop. ``cancel_requested`` is the row's flag, read in the same statement's
            transaction so a cancel is seen within one renewal interval.
        """
        now = self._clock()
        expires = now + timedelta(seconds=self._settings.execution.lease_seconds)
        with self._database.write() as session:
            renewed = self._cas(
                session,
                trajectory_id,
                expected=TrajectoryState.EXECUTING,
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
        expected: TrajectoryState,
        values: dict[str, Any],
        require_owner: bool = False,
    ) -> bool:
        """Compare-and-set the trajectory row; ``True`` iff exactly one row changed."""
        conditions = [
            models.Trajectory.id == trajectory_id,
            models.Trajectory.status == expected.value,
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
    ) -> None:
        """A compare-and-set on an ``executing`` row this worker leases, or :class:`LeaseLost`."""
        if not self._cas(
            session,
            trajectory_id,
            expected=TrajectoryState.EXECUTING,
            values=values,
            require_owner=True,
        ):
            raise LeaseLost(trajectory_id)

    # ----------------------------------------------------------------------------------------
    # Governance reconstruction
    # ----------------------------------------------------------------------------------------

    def _tier_policy(self, session: Session, view: TrajectoryView) -> TierPolicy:
        """The trajectory's own recorded snapshot, wrapped with today's availability."""
        row = (
            session.get(models.TierSnapshot, view.tier_snapshot_id)
            if view.tier_snapshot_id is not None
            else None
        )
        if row is None:
            message = f"trajectory {view.trajectory_id} records no tier snapshot"
            raise ValidationError(message, details={"field": "tier_snapshot_id"})
        snapshot = tier_snapshot_from_document(row.document_json)
        if snapshot.snapshot_id != view.tier_snapshot_id:  # pragma: no cover — a corrupt row
            message = "the recorded tier snapshot does not match its content address"
            raise ValidationError(message, details={"field": "tier_snapshot_id"})
        return TierPolicy(snapshot=snapshot, loadcoach_has_remote_provider=False)

    def _mint(
        self, session: Session, view: TrajectoryView, *, intent_id: str, minted_at: datetime
    ) -> _Governance:
        """Mint the bypass default intent from the recorded inputs (ADR-0056 §2)."""
        declaration = declaration_of(view, default_max_turns=self._settings.execution.max_steps)
        tier_policy = self._tier_policy(session, view)
        approval_policy = approval_policy_from_settings(self._settings)
        intent = mint_bypass_default(
            intent_id=intent_id,
            declaration=declaration,
            tier_policy=tier_policy,
            policy=approval_policy,
            minted_at=minted_at,
            tier_override=view.tier_override,
        )
        return _Governance(
            view=view,
            declaration=declaration,
            tier_policy=tier_policy,
            approval_policy=approval_policy,
            intent=intent,
            thread_id="",
        )

    def _load(self, trajectory_id: str) -> _Governance:
        """Rebuild the run's governance from rows, re-minting and verifying the intent.

        Raises:
            LeaseLost: The trajectory is not ``executing`` under this worker.
            ValidationError: The re-minted intent differs from the recorded one — a
                configuration change since the claim — or the rows are incomplete.
        """
        with self._database.read() as session:
            row = session.get(models.Trajectory, trajectory_id)
            if (
                row is None
                or row.status != TrajectoryState.EXECUTING.value
                or row.lease_owner != self.owner
            ):
                raise LeaseLost(trajectory_id)
            view = view_of(row)
            recorded = session.execute(
                select(ExecutionIntentRow)
                .where(ExecutionIntentRow.trajectory_id == trajectory_id)
                .order_by(ExecutionIntentRow.revision.desc())
                .limit(1)
            ).scalar_one_or_none()
            if recorded is None:
                message = f"trajectory {trajectory_id} is executing with no intent row"
                raise ValidationError(message, details={"field": "execution_intents"})
            governance = self._mint(
                session, view, intent_id=recorded.intent_id, minted_at=recorded.minted_at
            )
            stored = _intent_document(recorded)
            if governance.intent.as_canonical() != stored:
                message = (
                    "the default intent re-minted from the recorded declaration and tier "
                    "snapshot does not match the intent this trajectory was claimed under; the "
                    "approval policy has changed since the claim, and turns cannot run under an "
                    "envelope nobody minted"
                )
                raise ValidationError(
                    message,
                    details={
                        "field": "execution_intents",
                        "recorded": stored,
                        "reminted": governance.intent.as_canonical(),
                    },
                )
            thread_id = session.execute(
                select(models.Thread.id).where(models.Thread.trajectory_id == trajectory_id)
            ).scalar_one_or_none()
            if thread_id is None:
                message = f"trajectory {trajectory_id} is executing with no thread"
                raise ValidationError(message, details={"field": "threads"})
        return _Governance(
            view=governance.view,
            declaration=governance.declaration,
            tier_policy=governance.tier_policy,
            approval_policy=governance.approval_policy,
            intent=governance.intent,
            thread_id=thread_id,
        )

    # ----------------------------------------------------------------------------------------
    # Running
    # ----------------------------------------------------------------------------------------

    def run(self, trajectory_id: str, *, signals: RunSignals | None = None) -> TrajectoryState:
        """Run the bypass loop on a trajectory this worker holds ``executing``.

        Args:
            trajectory_id: The trajectory.
            signals: The lease keeper's flags; fresh ones when running without a keeper.

        Returns:
            The terminal state reached, or ``executing`` when this worker lost the lease and
            stopped without committing (the recovering worker owns it now).
        """
        flags = signals if signals is not None else RunSignals.fresh()
        with correlation(trajectory_id=trajectory_id):
            try:
                governance = self._load(trajectory_id)
            except LeaseLost:
                return TrajectoryState.EXECUTING
            except ValidationError as exc:
                return self._end_with(
                    trajectory_id,
                    fail,
                    cause=exc.message,
                    error_code=ErrorCode.LOADCOACH_ERROR,
                )
            try:
                return self._loop(governance, flags)
            except LeaseLost:
                logger.warning("trajectory.lease_lost", extra={"trajectory_id": trajectory_id})
                return TrajectoryState.EXECUTING

    def _loop(self, governance: _Governance, flags: RunSignals) -> TrajectoryState:
        trajectory_id = governance.view.trajectory_id
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
        router = TierRouter(governance.tier_policy)
        while True:
            if flags.lease_lost.is_set():
                raise LeaseLost(trajectory_id)
            if flags.cancel_requested.is_set() or self._cancel_requested(trajectory_id):
                return self._cancel_at_boundary(trajectory_id)
            turns = self._threads.turns(governance.thread_id)
            assistant_turns = [turn for turn in turns if turn.role is TurnRole.ASSISTANT]
            turns_used = len(assistant_turns)
            if turns_used >= governance.intent.max_turns:
                return self._end_with(
                    trajectory_id,
                    halt,
                    cause=(
                        f"the intent's max_turns ({governance.intent.max_turns}) is spent with "
                        "no declared finish"
                    ),
                    error_code=ErrorCode.STEP_LIMIT_EXCEEDED,
                )
            try:
                tier = router.resolve(governance.intent)
            except (TierUnavailableError, TierNotConfiguredError) as exc:
                return self._end_with(
                    trajectory_id, halt, cause=exc.message, error_code=ErrorCode(exc.code)
                )
            state = self._turn(governance, surface, tier, turns, flags)
            if state is not None:
                return state

    def _turn(
        self,
        governance: _Governance,
        surface: ProviderSurface,
        tier: Tier,
        turns: Sequence[Turn[TurnProvenance]],
        flags: RunSignals,
    ) -> TrajectoryState | None:
        """One turn: pre-flight, ``turn.started``, the call, the debit, then the turn row."""
        trajectory_id = governance.view.trajectory_id
        parked = self._preflight(governance, tier)
        if parked is not None:
            return parked
        turn_id = self._ids()
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
                    intent_id=governance.intent.intent_id,
                    intent_revision=governance.intent.revision,
                ),
                now=started,
            )
        flags.in_flight_turn_id = turn_id
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
                flags.in_flight_turn_id = None
                note = self._abandon_in_flight(turn_id)
                return self._end_with(
                    trajectory_id,
                    fail,
                    cause=f"{exc.message}{note}",
                    error_code=ErrorCode.LOADCOACH_UNAVAILABLE,
                )
            except (TierUnavailableError, CompactionFailedError, LoadCoachError) as exc:
                flags.in_flight_turn_id = None
                note = (
                    self._abandon_in_flight(turn_id)
                    if exc.details.get("reason") == "client_timeout"
                    else ""
                )
                return self._end_with(
                    trajectory_id,
                    halt,
                    cause=f"{exc.message}{note}",
                    error_code=ErrorCode(exc.code),
                )
            finally:
                flags.in_flight_turn_id = None
        overhead_ms = (time.perf_counter() - started_clock) * 1000.0 - float(
            response.timing.total_ms or 0
        )
        return self._record_turn(
            governance,
            surface,
            tier,
            turns,
            turn_id=turn_id,
            sequence=sequence,
            response=response,
            overhead_ms=max(overhead_ms, 0.0),
            recovered_from_job=None,
        )

    def _record_turn(
        self,
        governance: _Governance,
        surface: ProviderSurface,
        tier: Tier,
        turns: Sequence[Turn[TurnProvenance]],
        *,
        turn_id: str,
        sequence: int,
        response: GenerationResponse,
        overhead_ms: float,
        recovered_from_job: str | None,
    ) -> TrajectoryState | None:
        """Write the assistant turn and everything the response decides, in one transaction.

        Returns:
            The terminal state the turn decided, or ``None`` to continue the loop.
        """
        trajectory_id = governance.view.trajectory_id
        intent = governance.intent
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
        # answer below. Debit it **before** the turn row and before any halt: a debit written
        # first can never be lost by a crash that also loses the turn, and can never be written
        # twice, because `_debit_turn` refuses a `source_ref` the ledger already holds. Its own
        # write, not this turn's, so a rolled-back turn row does not roll back recorded spend.
        try:
            self._debit_turn(governance, tier, turn_id=turn_id, response=response, now=now)
        except CurrencyMismatchError as exc:
            return self._end_with(
                trajectory_id, halt, cause=str(exc), error_code=ErrorCode.BUDGET_EXCEEDED
            )
        try:
            subject = resolve_subject(response.model, surface=surface)
        except LoadCoachError as exc:
            return self._end_with(
                trajectory_id, halt, cause=exc.message, error_code=ErrorCode.LOADCOACH_ERROR
            )
        decision = decide_finish(
            finish_reason=response.finish_reason,
            schema_validated=response.validation.schema_validated,
            tool_calls_requested=len(response.tool_calls),
            undeclared_reason=response.undeclared_finish_reason,
        )
        if recovered_from_job is not None and not response.validation.checks_reported:
            # A reconciled turn is read from LoadCoach's job document. Since LoadCoach
            # 846348b that document carries the validation checks; one from an older
            # LoadCoach does not, so a schema validation cannot be confirmed from it. The turn is
            # recorded exactly as it happened; the verdict says why it could not complete.
            decision = FinishDecision(
                decision.outcome,
                f"{decision.cause} (reconciled after a crash from LoadCoach job "
                f"{recovered_from_job}'s document, which carries no validation checks)",
                decision.error_code,
            )
        assistant = Turn(
            turn_id,
            governance.thread_id,
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
            governance.declaration.classification,
            len(prior_assistant) + 1,
            spent,
            requested_tools=_ordered_names(requested),
            trajectory_allowlist=governance.declaration.tool_allowlist,
            finish_declared=decision.outcome is FinishOutcome.COMPLETE,
        )
        deviations = compare(facts, intent)
        scope = governance.approval_policy.reapproval_scope
        stopping = [
            deviation
            for deviation in deviations
            if disposition(deviation, scope=scope) in _STOPPING_DISPOSITIONS
        ]
        with self._sink.write() as (session, events):
            session.add(
                turn_row(
                    assistant,
                    loadcoach_job_id=response.job_id,
                    loadcoach_ms=response.timing.total_ms,
                    overhead_ms=overhead_ms,
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
            if stopping:
                first = stopping[0]
                cause = (
                    f"deviation {first.category.value} ({first.severity.value}) on turn "
                    f"{turn_id}: disposition {disposition(first, scope=scope).value}"
                    + (
                        "; scoped re-approval is not available before Phase 7"
                        if disposition(first, scope=scope) is Disposition.SCOPED_REAPPROVAL
                        else ""
                    )
                )
                self._transition(
                    session,
                    events,
                    trajectory_id,
                    halt,
                    cause=cause,
                    error_code=ErrorCode.DEVIATION_HALTED,
                    now=now,
                )
                return TrajectoryState.HALTED
            if decision.outcome is FinishOutcome.COMPLETE:
                outcome = complete(TrajectoryState.EXECUTING, all_steps_succeeded=True)
                self._owned_cas(
                    session,
                    trajectory_id,
                    values={
                        "status": outcome.state.value,
                        "halted_reason": decision.cause,
                        "completed_at": now,
                        "updated_at": now,
                        "lease_owner": None,
                        "lease_expires_at": None,
                    },
                )
                events.append(
                    trajectory_id,
                    TrajectoryCompleted(
                        trajectory_id=trajectory_id, step_count=1, turn_count=sequence
                    ),
                    now=now,
                )
                return TrajectoryState.COMPLETED
            if decision.outcome is FinishOutcome.CONTINUE:
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
        return self._run_tool_calls(governance, turn_id=turn_id, sequence=sequence, calls=requested)

    # ----------------------------------------------------------------------------------------
    # Budget (Phase 5)
    # ----------------------------------------------------------------------------------------

    def _preflight(self, governance: _Governance, tier: Tier) -> TrajectoryState | None:
        """Ask every active ceiling about the next step, and act on the most restrictive answer.

        The **only** place a ceiling stops work, and deliberately so. A ceiling crossed by a debit
        is not acted on where the debit happens: the debit's own verdicts are recorded in
        ``budget.debited``, and the next turn's pre-flight is what refuses to spend more. One rule
        in one place, and no path where a step is refused after it has already run.

        Returns:
            The state the trajectory was moved to — ``awaiting_window``, ``awaiting_approval`` or
            ``halted`` — or ``None`` when every ceiling admits the step and the turn may run.
        """
        view = governance.view
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
                return self._park_for_window(governance, cause=cause)
        else:
            policy = budget.on_exhausted
        if policy == "approval":
            return self._request_ceiling_raise(governance, cause=cause)
        return self._end_with(
            view.trajectory_id, halt, cause=cause, error_code=_exceeded_code(binding)
        )

    def _debit_turn(
        self,
        governance: _Governance,
        tier: Tier,
        *,
        turn_id: str,
        response: GenerationResponse,
        now: datetime,
    ) -> None:
        """Record one turn's spend, once, in its own write with its ``budget.debited`` event.

        Idempotent by ``source_ref``: a turn the ledger already holds a debit for is skipped
        silently, which is what makes both crash windows recoverable without a double debit — the
        one between LoadCoach's response and this write, and the one between this write and the
        turn row that follows it.

        The usage is rebuilt from **all four** classes LoadCoach put on the wire (ADR-0070, row
        C6); a class the protocol cannot bill is ``0`` and a class simply missing stays
        ``UNSUPPORTED``, which the ledger excludes from the balance rather than counting as zero.

        Raises:
            CurrencyMismatchError: If the turn priced in a currency an active money ceiling caps in
                another. Refused before anything is written (ADR-0030 rule 3).
        """
        view = governance.view
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

    def _park_for_window(self, governance: _Governance, *, cause: str) -> TrajectoryState:
        """T15: park on the per-day ceiling until the next UTC-day edge, releasing the lease.

        ``awaiting_window`` holds no lease (lifecycle §8.1) — a trajectory parked for up to
        ``window_wait_max_days`` holding one would keep a worker slot for days — so the clock is a
        persisted value on the row and the release is part of the same write.
        """
        view = governance.view
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

    def _request_ceiling_raise(self, governance: _Governance, *, cause: str) -> TrajectoryState:
        """T10: park on one pending approval request asking for the ceiling to be raised.

        **Granting the raise is Phase 7's**, and nothing here grants one. What this does is the
        half the lifecycle table requires of the *parking* side: exactly one ``approval_request``
        row exists before the state moves, because a trajectory parked with no request is one
        nobody can release (ADR-0049 rule 6). Until P7 the request simply expires, which halts the
        trajectory with the cause — a timeout is never a grant.
        """
        view = governance.view
        now = self._clock()
        request_id = self._ids()
        expires = now + timedelta(hours=self._settings.approval.request_timeout_hours)
        outcome = request_approval(TrajectoryState.EXECUTING, request_created=True)
        with self._sink.write() as (session, events):
            session.add(
                models.ApprovalRequest(
                    id=request_id,
                    trajectory_id=view.trajectory_id,
                    status="pending",
                    reason=VerdictReason.BUDGET_EXCEEDED.value,
                    step_ids_json=[],
                    expires_at=expires,
                    created_at=now,
                )
            )
            self._owned_cas(
                session,
                view.trajectory_id,
                values={
                    "status": outcome.state.value,
                    "halted_reason": cause,
                    "updated_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
            )
            events.append(
                view.trajectory_id,
                ApprovalRequested(
                    trajectory_id=view.trajectory_id,
                    approval_request_id=request_id,
                    step_ids=(),
                    reason=VerdictReason.BUDGET_EXCEEDED,
                    expires_at=to_rfc3339(expires),
                ),
                now=now,
            )
        return outcome.state

    def parked_trajectory_ids(self) -> Sequence[str]:
        """Return every ``awaiting_window`` trajectory, oldest first.

        A parked trajectory holds no lease, so the recovery pass — which reads lease-holding rows —
        never sees it. Nothing else in the process would look at its clock either, which is why
        the worker asks this on every pass.
        """
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

        Called by the worker on every pass. Three answers, and the *middle* one is the one a naive
        implementation gets wrong:

        * The edge has not arrived — nothing happens, and the trajectory stays parked.
        * The edge arrived and the per-day ceiling **still refuses** (another trajectory has
          already spent the new day) — one more edge is counted, the next edge is persisted, and
          it **stays parked**. Waiting a day does not entitle it to run.
        * The edge arrived and the ceiling admits — T16 back to the state it parked from, with the
          lease re-acquired in the same write.

        After ``window_wait_max_days`` edges have passed with the ceiling still refusing, T17
        halts with the cause; it never waits forever.

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
        """Whether the per-day ceiling would still refuse this trajectory's next step.

        T16's guard is "the per-day ceiling now **admits the plan or step**", which is a different
        question from "is the ceiling exceeded". A day with headroom smaller than the next step's
        estimate is a day that still refuses, and asking the cheaper question would wake a
        trajectory only for its next pre-flight to park it again — one park per day edge, forever,
        with an event stream to match. So this asks exactly what the pre-flight asks.
        """
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

        **The turn row is the source of truth and this re-derives the debit from it.** A crash
        between LoadCoach's response and the debit loses neither: the recorded turn carries the
        four token classes, the tier, the answering model and the instant, which is everything the
        debit needs and everything a re-costing needs. Run it twice and the second run debits
        nothing, because every turn is then in ``debited_turn_ids``.

        It also covers a database migrated into Phase 5 with turns already in it: those rows have
        usage and no debit, and this is what accounts for them rather than leaving spend the ledger
        never saw.

        Args:
            trajectory_id: The trajectory to reconcile.

        Returns:
            How many debits this pass wrote. ``0`` on a second pass, which is the property the
            recovery test asserts directly.
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
        governance: _Governance,
        *,
        turn_id: str,
        sequence: int,
        calls: Sequence[RequestedToolCall],
    ) -> TrajectoryState | None:
        """Execute one assistant turn's tool calls and append each result as a ``TOOL`` turn.

        Runs after the assistant turn is committed, so a crash mid-call leaves the turn that
        requested the calls on the record. Each call is three steps: ``tool.call.started`` in its
        own write, the call itself with **no** transaction open — a ``run_command`` may spend its
        whole timeout inside a container, and holding a SQLite write lock for that would stall
        every other worker — then one write holding the record, the ``TOOL`` turn and
        ``tool.call.completed``.

        **No exception a model can cause escapes this method.** ToolYard resolves everything the
        model chose to a :class:`toolyard.ToolResult`; what is left is the application's own
        failures — a workspace that cannot be created, a store that will not take a record — and
        each of those halts the trajectory with its cause named rather than propagating.

        Args:
            governance: The run's declaration, policies and intent.
            turn_id: The assistant turn whose ``tool_calls`` these are.
            sequence: That turn's position; the ``TOOL`` turns follow it.
            calls: The assembled calls, in the order the model made them.

        Returns:
            ``None`` to continue the loop — the ordinary outcome, including when every call was
            refused, because a refusal is a result the model reads and answers (ADR-0053). A
            terminal state when the application itself could not proceed.
        """
        trajectory_id = governance.view.trajectory_id
        try:
            tools = self._tools.for_trajectory(
                trajectory_id, allowlist=governance.declaration.tool_allowlist
            )
        except ConfigurationError as exc:
            return self._end_with(
                trajectory_id, halt, cause=exc.message, error_code=ErrorCode.TOOL_EXECUTION_FAILED
            )
        position = sequence
        for call in calls:
            position += 1
            state = self._run_one_tool_call(
                governance, tools, turn_id=turn_id, sequence=position, call=call
            )
            if state is not None:
                return state
        return None

    def _run_one_tool_call(
        self,
        governance: _Governance,
        tools: TrajectoryTools,
        *,
        turn_id: str,
        sequence: int,
        call: RequestedToolCall,
    ) -> TrajectoryState | None:
        """Start, execute and record one call. See :meth:`_run_tool_calls` for the shape."""
        trajectory_id = governance.view.trajectory_id
        invocation_id = self._ids()
        context = tools.context(invocation_id, approved_tools=governance.intent.approved_tools)
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
                # The call ran and its record could not be collected. ToolYard carries both on the
                # exception precisely so this is diagnosable; there is nothing safe to continue on.
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
            governance.thread_id,
            sequence,
            TurnRole.TOOL,
            governance.intent.provenance(
                trajectory_id=trajectory_id, tier=governance.intent.approved_tier
            ),
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

    def _cancel_at_boundary(self, trajectory_id: str) -> TrajectoryState:
        """T14 from ``executing``, honoured here, at a turn boundary, in one write."""
        now = self._clock()
        outcome = cancel(TrajectoryState.EXECUTING, at_turn_boundary=True)
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
            )
            events.append(
                trajectory_id,
                TrajectoryCancelled(
                    trajectory_id=trajectory_id, cancelled_from=TrajectoryState.EXECUTING
                ),
                now=now,
            )
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

    def cancel_in_flight(self, turn_id: str) -> str | None:
        """Cancel the in-flight LoadCoach job for ``turn_id``, if one exists. Best effort.

        Called by the lease keeper when a cancel arrives mid-turn (T14: "any in-flight LoadCoach
        job cancelled"). The turn's own ``/generate`` call then returns the cancelled job's
        document, which :meth:`_record_turn` refuses to read as an answer.

        Returns:
            The cancelled job's id, or ``None`` when no in-flight job holds the key.
        """
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

        Args:
            trajectory_id: The trajectory.
            require_expired: Take over only an *expired* lease (the running reaper), or any
                lease not held by this process (startup: every lease found belongs to a process
                that is gone).

        Returns:
            :attr:`ReconcileOutcome.RESUMED` — this worker holds the lease and the loop may run;
            :attr:`ReconcileOutcome.FINISHED` — the reconciled turn completed the trajectory;
            :attr:`ReconcileOutcome.HALTED` — unreconcilable, halted ``recovered_after_crash``;
            :attr:`ReconcileOutcome.DEFERRED` — LoadCoach could not be reached, so nothing was
            decided; the next pass tries again.
        """
        now = self._clock()
        events = self._sink.events(trajectory_id)
        with self._database.read() as session:
            row = session.get(models.Trajectory, trajectory_id)
            if row is None or row.status != TrajectoryState.EXECUTING.value:
                return ReconcileOutcome.DEFERRED
        # Before anything else: account for every turn already on disk that the ledger has not
        # seen. A crash between a turn's debit and its row is impossible by construction (the
        # debit is written first), but a crash *before* the debit is not, and a database migrated
        # into Phase 5 carries turns that predate the ledger entirely. Idempotent by `source_ref`,
        # so this is a no-op on the ordinary pass and the whole recovery on the crashed one.
        self.reconcile_debits(trajectory_id)
        with self._database.read() as session:
            row = session.get(models.Trajectory, trajectory_id)
            if row is None:  # pragma: no cover — the row was checked one statement ago
                return ReconcileOutcome.DEFERRED
            if require_expired and not (
                row.lease_expires_at is None or row.lease_expires_at <= now
            ):
                return ReconcileOutcome.DEFERRED
            thread_id = session.execute(
                select(models.Thread.id).where(models.Thread.trajectory_id == trajectory_id)
            ).scalar_one_or_none()
        turns = self._threads.turns(thread_id) if thread_id is not None else []
        dangling = _dangling_turn(events, {turn.turn_id for turn in turns})
        if dangling is None:
            return self._take_over(trajectory_id, outcome="resumed", now=now)
        turn_id = str(dangling.data["turn_id"])
        try:
            in_flight = self._loadcoach.find_job(turn_id, states=NON_TERMINAL_JOB_STATES)
            if in_flight is not None:
                self._loadcoach.cancel_job(in_flight.job_id)
                return self._take_over(
                    trajectory_id,
                    outcome=f"cancelled_in_flight_job:{in_flight.job_id}",
                    now=now,
                )
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
            governance = self._load(trajectory_id)
            surface = self._surface_loader(self._loadcoach)
            tier = TierRouter(governance.tier_policy).resolve(governance.intent)
            response = parse_generation(finished.document)
        except LoadCoachUnavailableError:
            return ReconcileOutcome.DEFERRED
        except (ValidationError, LoadCoachError, TierUnavailableError, LeaseLost) as exc:
            return self._halt_recovered(trajectory_id, detail=str(exc), now=now)
        try:
            state = self._record_turn(
                governance,
                surface,
                tier,
                turns,
                turn_id=turn_id,
                sequence=len(turns) + 1,
                response=response,
                overhead_ms=0.0,
                recovered_from_job=finished.job_id,
            )
        except LeaseLost:
            return ReconcileOutcome.DEFERRED
        if state is None:
            return ReconcileOutcome.RESUMED
        return ReconcileOutcome.FINISHED

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

    def fail_planning(self, trajectory_id: str, *, now: datetime) -> bool:
        """Recovery for ``planning``: T7 with the cause, since no planner exists to redraft."""
        cause = f"recovered_after_crash: {_PLANNER_ABSENT}"
        try:
            outcome = fail(TrajectoryState.PLANNING, cause=cause)
        except IllegalTransitionError:  # pragma: no cover — planning → failed is T7
            return False
        with self._sink.write() as (session, events):
            if not self._cas(
                session,
                trajectory_id,
                expected=TrajectoryState.PLANNING,
                values={
                    "status": outcome.state.value,
                    "halted_reason": cause,
                    "error_code": ErrorCode.PLAN_DRAFT_FAILED.value,
                    "completed_at": now,
                    "updated_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
            ):
                return False
            events.append(
                trajectory_id,
                TrajectoryFailed(
                    trajectory_id=trajectory_id,
                    cause=cause,
                    error_code=ErrorCode.PLAN_DRAFT_FAILED.value,
                ),
                now=now,
            )
        return True


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _intent_row(intent: ExecutionIntent) -> ExecutionIntentRow:
    """Map a minted intent onto its ``execution_intents`` row.

    The row class is imported under an alias: ``test_no_module_mints_an_intent_outside_domain_
    intent`` walks every call named ``ExecutionIntent`` in the package, and constructing the ORM
    row must not read as minting the domain envelope — which it is not.
    """
    return ExecutionIntentRow(
        intent_id=intent.intent_id,
        revision=intent.revision,
        trajectory_id=intent.trajectory_id,
        step_id=intent.step_id,
        supersedes=intent.supersedes,
        approved_tier=intent.approved_tier,
        fallback_tiers_json=list(intent.fallback_tiers),
        permitted_egress_class=intent.permitted_egress_class.value,
        approved_tools_json=sorted(intent.approved_tools),
        max_classification=intent.max_classification.value,
        token_budget=intent.token_budget,
        money_budget_currency=intent.money_budget.currency if intent.money_budget else None,
        money_budget_nanos=intent.money_budget.nanos if intent.money_budget else None,
        budget_source=intent.budget_source.value,
        budget_sample_count=intent.budget_sample_count,
        max_turns=intent.max_turns,
        minted_by=intent.minted_by.as_recorded(),
        minted_at=intent.minted_at,
        approval_request_id=intent.approval_request_id,
        gate_json=intent.gate.as_canonical(),
    )


def _intent_document(row: ExecutionIntentRow) -> dict[str, Any]:
    """The recorded intent in its canonical form, for comparison with a re-minted one."""
    return {
        "intent_id": row.intent_id,
        "trajectory_id": row.trajectory_id,
        "step_id": row.step_id,
        "revision": row.revision,
        "supersedes": row.supersedes,
        "approved_tier": row.approved_tier,
        "fallback_tiers": list(row.fallback_tiers_json),
        "permitted_egress_class": row.permitted_egress_class,
        "approved_tools": sorted(str(tool) for tool in row.approved_tools_json),
        "max_classification": row.max_classification,
        "token_budget": row.token_budget,
        "money_budget": (
            {"currency": row.money_budget_currency, "nanos": row.money_budget_nanos}
            if row.money_budget_currency is not None and row.money_budget_nanos is not None
            else None
        ),
        "budget_source": row.budget_source,
        "budget_sample_count": row.budget_sample_count,
        "max_turns": row.max_turns,
        "minted_by": row.minted_by,
        "minted_at": row.minted_at.isoformat(),
        "approval_request_id": row.approval_request_id,
        "gate": dict(row.gate_json),
    }


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
    """The last ``turn.started`` whose turn never got a row — the in-flight work at the crash."""
    for event in reversed(events):
        if event.event_type == "turn.started":
            turn_id = event.data.get("turn_id")
            return None if turn_id in committed else event
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
    """Count the tool round trips already spent in this step.

    A round trip is one assistant turn that declared ``tool_calls``, read off the persisted turn
    rather than recomputed from the ordering of ``TOOL`` turns: one assistant turn can request
    several calls, so counting results would count a single round trip several times, and a
    ``max_turns_per_step`` that fell with the number of tools a model asked for at once would
    punish the model for batching.

    Args:
        turns: The thread's turns so far.

    Returns:
        How many round trips are already on the record.
    """
    return sum(
        1
        for turn in turns
        if turn.role is TurnRole.ASSISTANT and turn.finish_reason is FinishReason.TOOL_CALLS
    )


def _ordered_names(calls: Sequence[RequestedToolCall]) -> tuple[str, ...]:
    """The distinct tool names a turn requested, in first-requested order.

    Deduplicated because :func:`~promptcadence.domain.deviation.compare` reports *which tools* were
    undeclared, and naming one tool twice in a deviation says nothing the first mention did not.
    Empty names are dropped: a call that named no tool is refused with ``unknown_tool`` at the
    executor and recorded there, and it contradicts no intent field.
    """
    return tuple(dict.fromkeys(call.name for call in calls if call.name))


def _args_text(call: RequestedToolCall) -> str:
    """The text whose digest identifies one call's arguments in the ``tool.call.started`` event.

    Deliberately *not* ToolYard's ``args_sha256``: that digest is computed inside ``execute``, and
    this event is written before the call so a crash mid-call still leaves evidence of what was
    attempted. Canonical JSON when the model produced an object, the raw text otherwise, so a call
    whose arguments would not parse is still identified by what it actually said.
    """
    if call.arguments_parsed:
        return canonical_json(call.arguments)
    return call.arguments if isinstance(call.arguments, str) else repr(call.arguments)


def _recorded_name(name: str) -> str:
    """Cap a model-chosen tool name for an event body, the way ToolYard caps it for a record.

    An unbounded name is a write amplification attack on the audit log, and an event body reaches
    SSE and the logs before any record does.
    """
    cleaned = name.replace("\x00", "")
    return cleaned[:MAX_RECORDED_NAME_CHARS]


def _shown_result(content: str, *, limit: int, artifact_ref: str | None) -> tuple[str, bool]:
    """Cap what the model sees of a tool result, and label the cap when there is one.

    The label is part of the contract, not a courtesy: a model that assumes a result *ended* rather
    than *stopped* answers from half a file. Where the whole output was filed as an artifact the
    label names its digest, so an operator reading the transcript can find the rest.

    Args:
        content: What the executor returned — the whole cleaned output while it fits under
            :data:`~promptcadence.services.tools.ARTIFACT_CEILING_BYTES`.
        limit: ``[tools] max_result_chars``.
        artifact_ref: The digest the whole output was filed under, or ``None``.

    Returns:
        The text for the ``TOOL`` turn, and whether it was truncated.
    """
    if len(content) <= limit:
        return content, False
    location = f"; full output recorded as {artifact_ref}" if artifact_ref else ""
    label = f"\n[truncated by promptcadence: {limit} of {len(content)} characters shown{location}]"
    return content[:limit] + label, True


def _isolation_of(plant: ToolPlant, tool_name: str) -> str | None:
    """The isolation rung to record for one call, or ``None`` when the tool runs no process.

    Read from the plant's cached probe rather than from the result, because a refused
    ``run_command`` never reached the sandbox and still ran under whatever rung the host has — the
    record should say which rung *would* have run it, since ``isolation_unavailable`` is precisely
    the refusal that names it.
    """
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
    """Rebuild a turn's usage from its row, keeping "not reported" distinct from zero.

    ADR-0070 in the one place it is easiest to get wrong. A ``NULL`` column is a class LoadCoach
    did not report and stays ``UNSUPPORTED``, which the ledger excludes from the token balance; a
    stored ``0`` is a class that was reported as unused and counts as the zero it is. Reading
    ``NULL`` as ``0`` here would make every unreported class look measured, and every money figure
    downstream would be wrong in the same direction.
    """
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
    """Which error code a refused pre-flight carries.

    ``TOKEN_BUDGET_EXCEEDED`` when the token bound is the one with nothing left — the universal
    brake, and the only one that can bind local work — and ``BUDGET_EXCEEDED`` otherwise. A
    ceiling binding both reports whichever actually ran out, because "which cap stopped me" is the
    first question an operator asks and a single code cannot answer it.
    """
    if headroom.tokens_remaining is not None and headroom.tokens_remaining < 0:
        return ErrorCode.TOKEN_BUDGET_EXCEEDED
    return ErrorCode.BUDGET_EXCEEDED


def _ceiling_cause(headroom: BudgetHeadroom) -> str:
    """Say which bound refused, and say it about a **pre-flight** rather than about history.

    Every figure here is the verdict ``would_exceed`` gave for the *prospective* spend, so the
    counts include the step that has not run. A cause reading "the cap is spent" would be wrong
    on the common case — a ceiling too small to admit the first step, with nothing spent against
    it at all — and a reader who then found an empty ledger would conclude the ledger was broken.
    """
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
