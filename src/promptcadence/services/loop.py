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

from baseaicore import ValidationError, is_supported, new_id, sha256_of
from sqlalchemy import select, update

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
from promptcadence.domain.threads import Thread, Turn, TurnRole
from promptcadence.domain.tiers import Tier, TierPolicy
from promptcadence.domain.trajectory import (
    TrajectoryCancelled,
    TrajectoryClaimed,
    TrajectoryCompleted,
    TrajectoryFailed,
    TrajectoryHalted,
    TrajectoryRecovered,
    TrajectoryState,
    cancel,
    claim_for_bypass,
    claim_for_planning,
    complete,
    fail,
    halt,
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
    parse_generation,
)
from promptcadence.infrastructure.threads import SqlThreadStore, thread_row, turn_row
from promptcadence.observability.logging import correlation
from promptcadence.services.loadcoach_surface import (
    ProviderSurface,
    load_provider_surface,
    resolve_subject,
)
from promptcadence.services.policy_assembly import (
    approval_policy_from_settings,
    tier_snapshot_from_document,
)
from promptcadence.services.views import TrajectoryView, declaration_of, view_of

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy import CursorResult
    from sqlalchemy.orm import Session

    from promptcadence.config import Settings
    from promptcadence.domain.intent import TurnProvenance
    from promptcadence.domain.policy import ApprovalPolicy
    from promptcadence.domain.trajectory import TrajectoryDeclaration
    from promptcadence.services.database import Database
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
        "_clock",
        "_database",
        "_ids",
        "_loadcoach",
        "_settings",
        "_sink",
        "_surface_loader",
        "_threads",
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
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] = new_id,
        surface_loader: Callable[[LoadCoachClient], ProviderSurface] = load_provider_surface,
    ) -> None:
        """Bind the controller to one worker's identity and the process's handles.

        Args:
            database: The application's database handle.
            sink: The event sink every write goes through.
            loadcoach: The LoadCoach client.
            settings: The validated configuration.
            owner: This worker's lease owner id, ``<process prefix>/<thread index>``.
            clock: The instant source, injected for determinism.
            id_factory: The id source for turns and intents.
            surface_loader: How the provider surface is read; injected so a test can script it.
        """
        self._database = database
        self._sink = sink
        self._loadcoach = loadcoach
        self._settings = settings
        self.owner = owner
        self._clock = clock if clock is not None else _utc_now
        self._ids = id_factory
        self._surface_loader = surface_loader
        self._threads = SqlThreadStore(database.sessions)

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
        """One turn: ``turn.started``, the call, then the turn row with its verdict."""
        trajectory_id = governance.view.trajectory_id
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
                Message(role=turn.role.value, content=turn.content or "") for turn in turns
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
            # A reconciled turn is read from LoadCoach's job document, which renders no
            # validation checks, so a schema validation cannot be confirmed from it. The turn is
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
        prior_assistant = [turn for turn in turns if turn.role is TurnRole.ASSISTANT]
        spent = _tokens_spent([*prior_assistant, assistant])
        facts = TurnFacts(
            turn_id,
            tier.name,
            subject,
            governance.declaration.classification,
            len(prior_assistant) + 1,
            spent,
            requested_tools=tuple(
                str(call.get("name", "")) for call in response.tool_calls if call.get("name")
            ),
            trajectory_allowlist=governance.declaration.tool_allowlist,
            finish_declared=decision.outcome is FinishOutcome.COMPLETE,
        )
        deviations = compare(facts, intent)
        scope = governance.approval_policy.reapproval_scope
        stopping = [
            deviation
            for deviation in deviations
            if disposition(deviation, scope=scope) is not Disposition.CONTINUE_RECORDED
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
                self._transition(
                    session,
                    events,
                    trajectory_id,
                    halt,
                    cause=(
                        f"{decision.cause}; tool calls are not executed before Phase 4, and a "
                        "requested tool that cannot run is not a completed turn"
                    ),
                    error_code=ErrorCode.TOOL_NOT_FOUND,
                    now=now,
                )
                return TrajectoryState.HALTED
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
