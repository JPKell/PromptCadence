"""promptcadence.services.trajectories — submit, read, list and cancel; T1 and T14.

Route handlers and CLI command bodies call one method here and render (coding standards §5).
Every state change commits with its event through
:class:`~promptcadence.services.events.TrajectoryEventSink` (ADR-0044), and every change to a
trajectory row is a compare-and-set on its current state, so a worker claiming a trajectory and a
caller cancelling it cannot both succeed.

**T1 records the whole envelope.** A trajectory carries the tier snapshot it was submitted under
(content-addressed, deduplicated by its id) and the approval policy version, so its record stays
truthful after an operator edits a tier or a gate (lifecycle §3, §4.2). The bypass decision is
made **once, here**, from configuration and the request's override, and stored — the worker
reads the stored flag rather than re-deciding at claim, so a configuration change between submit
and claim cannot silently turn a planned trajectory into a bypassed one.

**T14 has two halves.** A trajectory that holds no lease (``queued``, ``awaiting_approval``,
``awaiting_window``) is cancelled here, at once, in one write with its event. An ``executing``
trajectory is *asked* to cancel: the row's ``cancel_requested`` flag is set and the worker
honours it at the next turn boundary, in one write with the event, after cancelling any in-flight
LoadCoach job (lifecycle §8.2 T14). ``planning`` is treated like ``executing``: a worker holds it.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from baseaicore import DataClassification, Money, ValidationError, new_id
from baseaicore.timeutil import to_rfc3339
from sqlalchemy import select, update

from promptcadence.domain.errors import (
    ProjectUnknownError,
    ToolNotFoundError,
    TrajectoryNotCancellableError,
    TrajectoryNotFoundError,
)
from promptcadence.domain.tiers import TierSnapshot
from promptcadence.domain.trajectory import (
    TrajectoryCancelled,
    TrajectoryCreated,
    TrajectoryState,
    cancel,
    create,
)
from promptcadence.infrastructure.db import models
from promptcadence.infrastructure.threads import SqlThreadStore
from promptcadence.services.loop import BypassGate
from promptcadence.services.policy_assembly import (
    approval_policy_from_settings,
    money_from_amount,
    tier_snapshot_from_settings,
)
from promptcadence.services.views import TrajectoryView, TurnView, declaration_of, view_of

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy import CursorResult
    from sqlalchemy.orm import Session

    from promptcadence.config import Settings
    from promptcadence.services.budget import BudgetService
    from promptcadence.services.database import Database
    from promptcadence.services.events import StoredEvent, TrajectoryEventSink

__all__ = [
    "TrajectoryService",
    "TrajectorySubmission",
    "TrajectoryView",
    "TurnView",
    "declaration_of",
    "view_of",
]

_LEASE_HOLDING: Final = frozenset({TrajectoryState.PLANNING, TrajectoryState.EXECUTING})


@dataclass(frozen=True, slots=True)
class TrajectorySubmission:
    """One ``POST /trajectories`` request, validated at the edge, decided here (spec §7.1).

    Attributes:
        task: The caller's task text, passed to the model unmodified (spec §9).
        classification: The declared classification; the edge defaults it to ``confidential``.
        tools: The caller's allowlist, or ``None`` for the whole registry.
        bypass_planning: The per-request override, or ``None`` for the configured default.
        tier: A tier pin, recorded as the intent's ``tier_override``; policy still applies.
        max_turns: The bypass loop's turn cap, within ``execution.max_steps``.
        max_steps: The planned path's step cap, within ``planning.max_plan_steps``.
        project: A ``[budget.projects.<name>]`` label, or ``None``.
        token_budget: The trajectory's token ceiling, or ``None`` for the configured default.
        money_budget: Its money ceiling, or ``None`` for the configured default.
        partial_pricing: ``"floor"`` or ``"strict"`` for this trajectory's money ceilings, or
            ``None`` for ``[budget] partial_pricing`` (ADR-0069). A per-request override like the
            ceilings themselves, because "never cross this cap" is a property of the piece of work
            rather than of the installation.
    """

    task: str
    classification: DataClassification = DataClassification.CONFIDENTIAL
    tools: tuple[str, ...] | None = None
    bypass_planning: bool | None = None
    tier: str | None = None
    max_turns: int | None = None
    max_steps: int | None = None
    project: str | None = None
    token_budget: int | None = None
    money_budget: Money | None = None
    partial_pricing: Literal["floor", "strict"] | None = None


class TrajectoryService:
    """T1, T14 and the reads. One instance per process, over the shared handles."""

    __slots__ = ("_budget", "_clock", "_database", "_ids", "_settings", "_sink", "_threads")

    def __init__(
        self,
        database: Database,
        sink: TrajectoryEventSink,
        settings: Settings,
        *,
        budget: BudgetService | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] = new_id,
    ) -> None:
        """Bind the service to the process's handles.

        Args:
            database: The application's database handle.
            sink: The event sink every write goes through.
            settings: The validated configuration; the source of every default here.
            budget: The budget service, whose ledger every trajectory is declared with at
                creation. Optional only so a test that exercises the reads need not build one;
                a runtime always passes it, and ``submit`` without one declares nothing, which
                the pre-flight would meet as ``UnknownRun``.
            clock: The instant source, injected for determinism.
            id_factory: The id source, injected so a test can name its trajectories.
        """
        self._database = database
        self._sink = sink
        self._settings = settings
        self._budget = budget
        self._clock = clock if clock is not None else _utc_now
        self._ids = id_factory
        self._threads = SqlThreadStore(database.sessions)

    # ---- T1 ---------------------------------------------------------------------------------

    def submit(self, submission: TrajectorySubmission) -> TrajectoryView:
        """T1: validate, decide the bypass, snapshot the tiers, and queue — in one write.

        Args:
            submission: The request.

        Returns:
            The queued trajectory.

        Raises:
            ValidationError: An empty task, a cap above the configured ceiling, or a per-request
                bypass when ``planning.allow_request_override`` is false. A refused override is a
                refusal, never a silent ignore.
            ProjectUnknownError: ``project`` names no configured ``[budget.projects.<name>]``.
            ToolNotFoundError: The allowlist names a tool ``[tools] enabled`` does not list.
            TierNotConfiguredError: ``tier`` names no configured tier.
        """
        settings = self._settings
        if not submission.task.strip():
            message = "task must not be empty"
            raise ValidationError(message, details={"field": "task"})
        if submission.project is not None and submission.project not in settings.budget.projects:
            message = f"project {submission.project!r} names no configured [budget.projects.<name>]"
            raise ProjectUnknownError(
                message,
                details={
                    "field": "project",
                    "project": submission.project,
                    "configured": sorted(settings.budget.projects),
                },
            )
        registry = tuple(settings.tools.enabled)
        tools = registry if submission.tools is None else tuple(dict.fromkeys(submission.tools))
        outside = sorted(set(tools) - set(registry))
        if outside:
            message = (
                f"tool(s) {', '.join(outside)} are not in the registry ({', '.join(registry)})"
            )
            raise ToolNotFoundError(message, details={"field": "tools", "tools": outside})
        max_turns = _within(
            submission.max_turns, settings.execution.max_steps, field_name="max_turns"
        )
        max_steps = _within(
            submission.max_steps, settings.planning.max_plan_steps, field_name="max_steps"
        )
        snapshot = tier_snapshot_from_settings(settings)
        if submission.tier is not None:
            snapshot.require(submission.tier)
        bypass = BypassGate(
            planning_enabled=settings.planning.enabled,
            allow_request_override=settings.planning.allow_request_override,
        ).decide(submission.bypass_planning)
        policy_version = approval_policy_from_settings(settings).version
        token_budget = (
            submission.token_budget
            if submission.token_budget is not None
            else settings.budget.default_token_ceiling
        )
        money = (
            submission.money_budget
            if submission.money_budget is not None
            else money_from_amount(settings.budget.default_money_ceiling)
        )
        if token_budget < 1:
            message = "budget.tokens must be positive"
            raise ValidationError(message, details={"field": "budget.tokens"})
        trajectory_id = self._ids()
        now = self._clock()
        outcome = create()
        with self._sink.write() as (session, events):
            _ensure_snapshot(session, snapshot, now=now)
            session.add(
                models.Trajectory(
                    id=trajectory_id,
                    task=submission.task,
                    data_classification=submission.classification.value,
                    status=outcome.state.value,
                    project=submission.project,
                    tools_json=list(tools),
                    bypass_planning=bypass,
                    tier_override=submission.tier,
                    max_steps=max_steps,
                    max_turns=max_turns,
                    budget_money_currency=money.currency if money.nanos > 0 else None,
                    budget_money_nanos=money.nanos if money.nanos > 0 else None,
                    budget_token_ceiling=token_budget,
                    budget_partial_pricing=submission.partial_pricing,
                    tier_snapshot_id=snapshot.snapshot_id,
                    approval_policy_version=policy_version,
                    created_at=now,
                    updated_at=now,
                )
            )
            events.append(
                trajectory_id,
                TrajectoryCreated(
                    trajectory_id=trajectory_id,
                    classification=submission.classification,
                    tool_allowlist=tools,
                    token_budget=token_budget,
                    bypass_planning=bypass,
                    project=submission.project,
                ),
                now=now,
            )
            if self._budget is not None:
                # `declare_run` fires **here** — at trajectory creation, in the write that
                # persists the row, before plan approval and long before the first turn. A run
                # exists to LoadLedger "once debited or declared" (its spec §13), and a
                # trajectory that had never been declared would meet `UnknownRun` on its very
                # first pre-flight, which is every trajectory's first budget question. Moving
                # this to the first turn would work for exactly as long as nothing checked a
                # budget before spending against it.
                self._budget.declare(session, trajectory_id)
        return self.get(trajectory_id)

    # ---- reads ------------------------------------------------------------------------------

    def get(self, trajectory_id: str) -> TrajectoryView:
        """Return one trajectory.

        Raises:
            TrajectoryNotFoundError: No trajectory has that id.
        """
        with self._database.read() as session:
            row = session.get(models.Trajectory, trajectory_id)
            if row is None:
                raise TrajectoryNotFoundError(
                    f"No trajectory {trajectory_id!r}.", details={"trajectory_id": trajectory_id}
                )
            return view_of(row)

    def resolve(self, reference: str) -> TrajectoryView:
        """Return the trajectory a full id or an unambiguous prefix names (CLI standards §7).

        Raises:
            TrajectoryNotFoundError: No trajectory matches.
            ValidationError: More than one matches; ``details["candidates"]`` lists them.
        """
        with self._database.read() as session:
            rows = (
                session.execute(
                    select(models.Trajectory)
                    .where(models.Trajectory.id.like(f"{reference}%"))
                    .order_by(models.Trajectory.id)
                    .limit(5)
                )
                .scalars()
                .all()
            )
            exact = [row for row in rows if row.id == reference]
            if exact:
                return view_of(exact[0])
            if not rows:
                raise TrajectoryNotFoundError(
                    f"No trajectory matches {reference!r}.", details={"trajectory_id": reference}
                )
            if len(rows) > 1:
                message = f"{reference!r} is ambiguous; it matches {len(rows)} trajectories"
                raise ValidationError(
                    message,
                    details={"field": "trajectory_id", "candidates": [row.id for row in rows]},
                )
            return view_of(rows[0])

    def list(
        self, *, state: TrajectoryState | None = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[Sequence[TrajectoryView], str | None]:
        """List trajectories newest first, filtered by state, cursor-paginated (API §6).

        Returns:
            The page and the next cursor, or ``None`` at the end.
        """
        before = _decode_cursor(cursor)
        with self._database.read() as session:
            statement = (
                select(models.Trajectory)
                .order_by(models.Trajectory.created_at.desc(), models.Trajectory.id.desc())
                .limit(limit + 1)
            )
            if state is not None:
                statement = statement.where(models.Trajectory.status == state.value)
            if before is not None:
                statement = statement.where(models.Trajectory.created_at < before)
            rows = session.execute(statement).scalars().all()
            page = [view_of(row) for row in rows[:limit]]
        has_more = len(rows) > limit
        next_cursor = _encode_cursor(page[-1]) if has_more and page else None
        return page, next_cursor

    def turns(self, trajectory_id: str) -> Sequence[TurnView]:
        """Return the trajectory's transcript in order, with each turn's LoadCoach reference.

        Raises:
            TrajectoryNotFoundError: No trajectory has that id.
        """
        self.get(trajectory_id)
        with self._database.read() as session:
            thread_id = session.execute(
                select(models.Thread.id).where(models.Thread.trajectory_id == trajectory_id)
            ).scalar_one_or_none()
            if thread_id is None:
                return []
            extras = {
                row.id: row
                for row in session.execute(
                    select(models.Turn).where(models.Turn.thread_id == thread_id)
                ).scalars()
            }
        return [
            TurnView(
                turn=turn,
                loadcoach_job_id=extras[turn.turn_id].loadcoach_job_id,
                loadcoach_ms=extras[turn.turn_id].loadcoach_ms,
                overhead_ms=extras[turn.turn_id].overhead_ms,
                created_at=extras[turn.turn_id].created_at,
            )
            for turn in self._threads.turns(thread_id)
        ]

    def events(self, trajectory_id: str, *, after_sequence: int = 0) -> Sequence[StoredEvent]:
        """Return the persisted events after ``after_sequence`` (ADR-0044 rule 3's second read).

        Raises:
            TrajectoryNotFoundError: No trajectory has that id.
        """
        self.get(trajectory_id)
        return self._sink.events(trajectory_id, after_sequence=after_sequence)

    # ---- T14 --------------------------------------------------------------------------------

    def cancel(self, trajectory_id: str) -> TrajectoryView:
        """T14: cancel a non-terminal trajectory.

        A trajectory holding no lease is cancelled at once, in one write with
        ``trajectory.cancelled``. A leased one (``planning``/``executing``) has
        ``cancel_requested`` set; the worker honours it at the next turn boundary. Idempotent:
        a second request on an already-requested cancel changes nothing.

        Raises:
            TrajectoryNotFoundError: No trajectory has that id.
            TrajectoryNotCancellableError: The trajectory is terminal — the service-layer name
                for the state machine's refusal.
        """
        now = self._clock()
        with self._sink.write() as (session, events):
            row = session.get(models.Trajectory, trajectory_id)
            if row is None:
                raise TrajectoryNotFoundError(
                    f"No trajectory {trajectory_id!r}.", details={"trajectory_id": trajectory_id}
                )
            current = TrajectoryState(row.status)
            if current.is_terminal:
                raise TrajectoryNotCancellableError(
                    f"Trajectory {trajectory_id} is already {current.value}; nothing to cancel.",
                    details={"trajectory_id": trajectory_id, "state": current.value},
                )
            if current in _LEASE_HOLDING:
                result = cast(
                    "CursorResult[Any]",
                    session.execute(
                        update(models.Trajectory)
                        .where(
                            models.Trajectory.id == trajectory_id,
                            models.Trajectory.status == current.value,
                        )
                        .values(cancel_requested=True, updated_at=now)
                    ),
                )
                if result.rowcount != 1:  # pragma: no cover — a race the CAS makes harmless
                    raise TrajectoryNotCancellableError(
                        f"Trajectory {trajectory_id} changed state while being cancelled.",
                        details={"trajectory_id": trajectory_id},
                    )
            else:
                outcome = cancel(current)
                result = cast(
                    "CursorResult[Any]",
                    session.execute(
                        update(models.Trajectory)
                        .where(
                            models.Trajectory.id == trajectory_id,
                            models.Trajectory.status == current.value,
                        )
                        .values(
                            status=outcome.state.value,
                            cancel_requested=True,
                            completed_at=now,
                            updated_at=now,
                            lease_owner=None,
                            lease_expires_at=None,
                        )
                    ),
                )
                if result.rowcount != 1:  # pragma: no cover — a race the CAS makes harmless
                    raise TrajectoryNotCancellableError(
                        f"Trajectory {trajectory_id} changed state while being cancelled.",
                        details={"trajectory_id": trajectory_id},
                    )
                events.append(
                    trajectory_id,
                    TrajectoryCancelled(trajectory_id=trajectory_id, cancelled_from=current),
                    now=now,
                )
        return self.get(trajectory_id)


# --------------------------------------------------------------------------------------------
# Row ↔ value helpers, shared with the loop and the worker
# --------------------------------------------------------------------------------------------


def _ensure_snapshot(session: Session, snapshot: TierSnapshot, *, now: datetime) -> None:
    """Write the content-addressed tier snapshot row unless an identical one exists."""
    if session.get(models.TierSnapshot, snapshot.snapshot_id) is None:
        session.add(
            models.TierSnapshot(
                id=snapshot.snapshot_id, document_json=snapshot.as_canonical(), created_at=now
            )
        )


def _within(value: int | None, ceiling: int, *, field_name: str) -> int | None:
    """Refuse a cap above its configured ceiling; ``None`` means the ceiling itself applies."""
    if value is None:
        return None
    if value < 1 or value > ceiling:
        message = f"{field_name} must be between 1 and the configured cap {ceiling}, got {value}"
        raise ValidationError(message, details={"field": field_name, "cap": ceiling})
    return value


def _encode_cursor(view: TrajectoryView) -> str:
    raw = f"{to_rfc3339(view.created_at)}|{view.trajectory_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str | None) -> datetime | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        stamp, _, _ = raw.partition("|")
        return datetime.fromisoformat(stamp)
    except (ValueError, UnicodeDecodeError):
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)
