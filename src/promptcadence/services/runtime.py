"""promptcadence.services.runtime — the handles one process owns, and the health they report.

Separate from :mod:`promptcadence.bootstrap` because the CLI needs these and may not reach the web
layer: the composition root imports ``web``, so anything the CLI took from it would drag ``cli``
into ``web`` and break both the layering and the web/CLI independence contract.

Opening a runtime never fails because LoadCoach is unreachable (ADR-0045 rule 3) — reachability is
a health component, never a startup failure. It *does* fail loudly when storage cannot be opened:
unlike an unreachable peer, a database PromptCadence cannot read or write is not a state this
application can degrade around, since every one of its endpoints already needs one.

From Phase 3 the runtime also owns the event sink, the trajectory service, the LoadCoach client
and the worker pool. The worker runs its recovery pass and starts its threads in :meth:`start`,
which the lifespan calls once the application serves; a test that wants the handles without the
threads builds the runtime and never starts it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mirrorwall import ComponentHealth

from promptcadence.infrastructure.loadcoach import LoadCoachClient
from promptcadence.services.database import Database, database_health_component, ensure_ready
from promptcadence.services.events import TrajectoryEventSink
from promptcadence.services.loadcoach_status import loadcoach_health_component
from promptcadence.services.tools import ToolPlant, tools_health_component
from promptcadence.services.trajectories import TrajectoryService
from promptcadence.services.worker import TrajectoryWorker

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import httpx

    from promptcadence.config import Settings

__all__ = ["Runtime", "build_runtime"]


class Runtime:
    """The handles this process owns: storage, events, the LoadCoach client and the worker."""

    __slots__ = (
        "_database",
        "_started",
        "loadcoach",
        "settings",
        "sink",
        "tools",
        "trajectories",
        "worker",
    )

    def __init__(self, settings: Settings, *, loadcoach_http: httpx.Client | None = None) -> None:
        """Open storage and build the handles. Never raises for an unreachable LoadCoach.

        Args:
            settings: The validated configuration.
            loadcoach_http: An httpx client to reach LoadCoach through, or ``None`` to build one
                from ``[loadcoach]``. Injected so a test hands over Starlette's ``TestClient``
                on the fake LoadCoach and the whole loop runs in-process, without a socket.

        Raises:
            weightsdb.DatabaseError: Storage could not be opened or migrated — see
                :func:`promptcadence.services.database.ensure_ready`.
        """
        self.settings = settings
        database_url = settings.storage.database_url
        if database_url is None:  # pragma: no cover — StorageSettings always fills this in
            message = "no database_url configured"
            raise RuntimeError(message)
        database = Database.from_url(database_url)
        ensure_ready(database, auto_migrate=settings.storage.auto_migrate)
        self._database = database
        self.sink = TrajectoryEventSink(database)
        self.trajectories = TrajectoryService(database, self.sink, settings)
        loadcoach = settings.loadcoach
        self.loadcoach = (
            LoadCoachClient(loadcoach_http)
            if loadcoach_http is not None
            else LoadCoachClient.from_settings(
                base_url=loadcoach.base_url,
                timeout_seconds=loadcoach.timeout_seconds,
                api_key_env=loadcoach.api_key_env,
                api_key_file=loadcoach.api_key_file,
            )
        )
        # One plant per process: the isolation probe launches a real canary, and every worker
        # thread asking the same question of the same host should ask it once.
        self.tools = ToolPlant(settings)
        self.worker = TrajectoryWorker(
            database=database,
            sink=self.sink,
            loadcoach=self.loadcoach,
            settings=settings,
            tools=self.tools,
        )
        self._started = False

    @property
    def database(self) -> Database:
        """The application's live database handle."""
        return self._database

    @property
    def health_checkers(self) -> Sequence[Callable[[], ComponentHealth]]:
        """The three components this build reports: ``database``, ``loadcoach`` and ``tools``."""
        return (self._database_health, self._loadcoach_health, self._tools_health)

    def start(self) -> None:
        """Run startup recovery and start the worker threads. Idempotent."""
        if not self._started:
            self.worker.start()
            self._started = True

    def _database_health(self) -> ComponentHealth:
        return database_health_component(self._database)

    def _loadcoach_health(self) -> ComponentHealth:
        loadcoach = self.settings.loadcoach
        return loadcoach_health_component(
            base_url=loadcoach.base_url,
            api_key_env=loadcoach.api_key_env,
            api_key_file=loadcoach.api_key_file,
        )

    def _tools_health(self) -> ComponentHealth:
        return tools_health_component(self.tools)

    def close(self) -> None:
        """Stop the worker, close the LoadCoach client and dispose the database. Idempotent."""
        if self._started:
            self.worker.stop()
            self._started = False
        self.loadcoach.close()
        self._database.close()


def build_runtime(settings: Settings, *, loadcoach_http: httpx.Client | None = None) -> Runtime:
    """Open the handles a served process needs and start its worker.

    Args:
        settings: The validated configuration.
        loadcoach_http: See :class:`Runtime`.

    Returns:
        The started runtime. Raises if storage cannot be opened or migrated; never raises
        because LoadCoach is unreachable — that is a health component (ADR-0045 rule 3).
    """
    runtime = Runtime(settings, loadcoach_http=loadcoach_http)
    runtime.start()
    return runtime
