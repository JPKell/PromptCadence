"""promptcadence.services.worker — the trajectory worker: claim, keep the lease, recover.

The durable-queue discipline of ADR-0010/ADR-0029, applied to trajectories: workers are threads
in the serving process, the queue is the ``trajectories`` table, a claim is a compare-and-set
that takes the lease, and a lease is renewed by a keeper thread while the loop runs. There is no
broker and nothing in memory that the database does not also hold — which is what makes a
``kill -9`` recoverable rather than fatal.

**Recovery runs before any work is accepted** (lifecycle §8.3, ADR-0036), and again periodically
for leases that expire while the process lives. At startup every lease found belongs to a process
that is gone — this is a single-process design — so it is taken over without waiting for it to
expire. The reaper, by contrast, takes over only *expired* leases: a live worker in this process
renews its own every ``lease_seconds / 3``, so an expired one is a worker that stalled.

Recovery is idempotent: a second pass finds nothing foreign and nothing expired.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models
from promptcadence.services.budget import BudgetService
from promptcadence.services.egress import EgressService
from promptcadence.services.estimates import StepEstimator
from promptcadence.services.loop import LoopController, ReconcileOutcome, RunSignals
from promptcadence.services.pricing import PricingCatalog
from promptcadence.services.tools import ToolPlant

if TYPE_CHECKING:
    from collections.abc import Callable

    from promptcadence.config import Settings
    from promptcadence.infrastructure.loadcoach import LoadCoachClient
    from promptcadence.services.database import Database
    from promptcadence.services.events import TrajectoryEventSink

__all__ = ["LeaseKeeper", "RecoverySummary", "TrajectoryWorker", "process_owner_prefix", "recover"]

logger = logging.getLogger(__name__)


def process_owner_prefix() -> str:
    """This process's lease-owner prefix: ``<host>:<pid>``. Any other prefix is another process."""
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    """What one recovery pass did — the reconciliation queue §10 asks every application to log."""

    resumed: tuple[str, ...] = ()
    finished: tuple[str, ...] = ()
    halted: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()

    @property
    def touched(self) -> int:
        """How many trajectories changed state or owner."""
        return len(self.resumed) + len(self.finished) + len(self.halted) + len(self.failed)

    def as_json(self) -> dict[str, Any]:
        """The summary as ``/system/status`` reports it."""
        return {
            "resumed": list(self.resumed),
            "finished": list(self.finished),
            "halted": list(self.halted),
            "failed": list(self.failed),
            "deferred": list(self.deferred),
            "touched": self.touched,
        }


def recover(
    controller: LoopController,
    database: Database,
    *,
    owner_prefix: str,
    now: datetime,
    only_expired: bool,
) -> RecoverySummary:
    """Run lifecycle §8.3's recovery pass over every lease-holding trajectory.

    Args:
        controller: The controller that will hold whatever is taken over.
        database: The application's database handle.
        owner_prefix: This process's prefix; a lease under any other prefix is dead at startup.
        now: The recovery instant.
        only_expired: Take over only expired leases (the running reaper), or every foreign one
            (startup).

    Returns:
        The summary. ``resumed`` trajectories are held by ``controller`` and must be run.
    """
    with database.read() as session:
        statement = select(models.Trajectory.id, models.Trajectory.status).where(
            models.Trajectory.status.in_(
                [TrajectoryState.PLANNING.value, TrajectoryState.EXECUTING.value]
            )
        )
        if only_expired:
            statement = statement.where(models.Trajectory.lease_expires_at <= now)
        else:
            statement = statement.where(
                or_(
                    models.Trajectory.lease_owner.is_(None),
                    models.Trajectory.lease_owner.not_like(f"{owner_prefix}/%"),
                    models.Trajectory.lease_expires_at <= now,
                )
            )
        rows = session.execute(statement.order_by(models.Trajectory.created_at)).all()
    resumed: list[str] = []
    finished: list[str] = []
    halted: list[str] = []
    failed: list[str] = []
    deferred: list[str] = []
    for trajectory_id, status in rows:
        if status == TrajectoryState.PLANNING.value:
            (failed if controller.fail_planning(trajectory_id, now=now) else deferred).append(
                trajectory_id
            )
            continue
        outcome = controller.reconcile(trajectory_id, require_expired=only_expired)
        bucket = {
            ReconcileOutcome.RESUMED: resumed,
            ReconcileOutcome.FINISHED: finished,
            ReconcileOutcome.HALTED: halted,
            ReconcileOutcome.DEFERRED: deferred,
        }[outcome]
        bucket.append(trajectory_id)
    summary = RecoverySummary(
        resumed=tuple(resumed),
        finished=tuple(finished),
        halted=tuple(halted),
        failed=tuple(failed),
        deferred=tuple(deferred),
    )
    if rows:
        logger.info("trajectories.recovered", extra=summary.as_json())
    return summary


class LeaseKeeper:
    """Renews one trajectory's lease while its loop runs, and relays a cancel to it.

    Every ``lease_seconds / 3`` the keeper renews by compare-and-set. A renewal that changes no
    row means another worker took the trajectory over (or it left ``executing``): the keeper
    raises ``signals.lease_lost`` and the loop stops at its next boundary — and could not commit
    anyway, since its own writes are fenced on the owner. A renewal that reads
    ``cancel_requested`` raises ``signals.cancel_requested`` and cancels the in-flight LoadCoach
    job, so the turn's blocked ``/generate`` call returns promptly with a cancelled job.
    """

    def __init__(
        self,
        controller: LoopController,
        trajectory_id: str,
        *,
        interval_seconds: float,
        signals: RunSignals,
    ) -> None:
        """Create a keeper; :meth:`start` runs it."""
        self._controller = controller
        self._trajectory_id = trajectory_id
        self._interval = interval_seconds
        self._signals = signals
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"promptcadence-lease-{trajectory_id[-6:]}", daemon=True
        )

    def start(self) -> None:
        """Start renewing."""
        self._thread.start()

    def stop(self) -> None:
        """Stop renewing and wait for the thread."""
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        cancelled_job = False
        while not self._stop.wait(self._interval):
            renewed, cancel_requested = self._controller.renew_lease(self._trajectory_id)
            if not renewed:
                self._signals.lease_lost.set()
                return
            if cancel_requested and not self._signals.cancel_requested.is_set():
                self._signals.cancel_requested.set()
            if cancel_requested and not cancelled_job:
                in_flight = self._signals.in_flight_turn_id
                if in_flight is not None:
                    self._controller.cancel_in_flight(in_flight)
                    cancelled_job = True


@dataclass
class TrajectoryWorker:
    """The worker pool: ``execution.max_concurrent_trajectories`` threads over one queue.

    Attributes:
        database: The application's database handle.
        sink: The event sink.
        loadcoach: The LoadCoach client.
        settings: The validated configuration.
        owner_prefix: This process's lease-owner prefix.
        clock: The instant source.
        poll_interval_seconds: How long an idle thread waits before looking again; a
            :meth:`wake` cuts the wait short.
        controller_factory: Builds a controller per thread; injected so a test can script it.
        budget: The ceilings, the ledger and the debits (P5), shared by every thread — a ledger
            is stateless, and one instance keeps the pricing catalogue loaded once. ``None``
            builds one over an empty pricing catalogue, which is the right default for a test
            running only local tiers and never for a served process.
        tools: The process's tool registry, sandbox and artifact store, shared by every thread so
            the isolation probe runs once rather than once per worker. ``None`` lets each
            controller build its own from ``[tools]``, which is right for a single-threaded test
            and wasteful in a pool.
    """

    database: Database
    sink: TrajectoryEventSink
    loadcoach: LoadCoachClient
    settings: Settings
    budget: BudgetService | None = None
    tools: ToolPlant | None = None
    egress: EgressService | None = None
    owner_prefix: str = field(default_factory=process_owner_prefix)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    poll_interval_seconds: float = 0.5
    controller_factory: Callable[[str], LoopController] | None = None
    _threads: list[threading.Thread] = field(default_factory=list, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _wake: threading.Event = field(default_factory=threading.Event, init=False)
    last_recovery: RecoverySummary | None = field(default=None, init=False)

    def controller(self, owner: str) -> LoopController:
        """The controller a thread runs with."""
        if self.controller_factory is not None:
            return self.controller_factory(owner)
        budget = self.budget if self.budget is not None else self._default_budget()
        return LoopController(
            database=self.database,
            sink=self.sink,
            loadcoach=self.loadcoach,
            settings=self.settings,
            owner=owner,
            budget=budget,
            estimator=StepEstimator(budget, self.settings, clock=self.clock),
            egress=self.egress if self.egress is not None else self._default_egress(),
            clock=self.clock,
            tools=self.tools,
        )

    def _default_egress(self) -> EgressService:
        """An egress service over Commissioner's shipped policy, for a worker built without one."""
        return EgressService(self.database, clock=self.clock)

    def _default_budget(self) -> BudgetService:
        """A budget service over an empty pricing catalogue, for a worker built without one."""
        return BudgetService(
            self.database, self.settings, PricingCatalog(by_tier={}), clock=self.clock
        )

    def recover_at_startup(self) -> RecoverySummary:
        """Run the startup recovery pass, taking over every foreign lease, and run what resumed."""
        controller = self.controller(f"{self.owner_prefix}/recovery")
        summary = recover(
            controller,
            self.database,
            owner_prefix=self.owner_prefix,
            now=self.clock(),
            only_expired=False,
        )
        self.last_recovery = summary
        for trajectory_id in summary.resumed:
            self._run_held(controller, trajectory_id)
        return summary

    def start(self) -> None:
        """Recover, then start the worker threads."""
        self.recover_at_startup()
        for index in range(self.settings.execution.max_concurrent_trajectories):
            thread = threading.Thread(
                target=self._thread_main,
                args=(f"{self.owner_prefix}/{index}",),
                name=f"promptcadence-worker-{index}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        """Ask every thread to stop after its current turn boundary, and wait."""
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=10)

    def wake(self) -> None:
        """Wake an idle thread now — called after ``POST /trajectories``."""
        self._wake.set()

    def _thread_main(self, owner: str) -> None:
        controller = self.controller(owner)
        next_reap = self.clock()
        while not self._stop.is_set():
            now = self.clock()
            if now >= next_reap:
                summary = recover(
                    controller,
                    self.database,
                    owner_prefix=self.owner_prefix,
                    now=now,
                    only_expired=True,
                )
                for trajectory_id in summary.resumed:
                    self._run_held(controller, trajectory_id)
                next_reap = now + _seconds(self.settings.execution.lease_seconds)
            # `awaiting_window` holds no lease, so recovery never sees a parked trajectory and
            # nothing else would ever look at its clock. This pass is what makes the UTC day edge
            # arrive: it resumes what the new day admits, counts an edge for what it still
            # refuses, and halts what has waited `window_wait_max_days` (T16/T17).
            for parked in controller.parked_trajectory_ids():
                if controller.release_window(parked) is TrajectoryState.EXECUTING:
                    self._run_held(controller, parked)
            candidate = controller.next_queued()
            if candidate is None:
                self._wake.wait(self.poll_interval_seconds)
                self._wake.clear()
                continue
            claimed = controller.claim(candidate)
            if claimed is TrajectoryState.EXECUTING:
                self._run_held(controller, candidate)

    def _run_held(self, controller: LoopController, trajectory_id: str) -> None:
        signals = RunSignals.fresh()
        keeper = LeaseKeeper(
            controller,
            trajectory_id,
            interval_seconds=max(self.settings.execution.lease_seconds / 3.0, 0.05),
            signals=signals,
        )
        keeper.start()
        try:
            controller.run(trajectory_id, signals=signals)
        finally:
            keeper.stop()


def _seconds(value: int) -> Any:
    from datetime import timedelta

    return timedelta(seconds=value)
