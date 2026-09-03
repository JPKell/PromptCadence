"""promptcadence.services.runtime — the handles one process owns, and the health they report.

Separate from :mod:`promptcadence.bootstrap` because the CLI needs these and may not reach the web
layer: the composition root imports ``web``, so anything the CLI took from it would drag ``cli``
into ``web`` and break both the layering and the web/CLI independence contract.

Opening a runtime never fails because LoadCoach is unreachable (ADR-0045 rule 3) — reachability is
a health component, never a startup failure. It *does* fail loudly when storage cannot be opened:
unlike an unreachable peer, a database PromptCadence cannot read or write is not a state Phase 1
can degrade around, since every one of its endpoints already needs one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mirrorwall import ComponentHealth

from promptcadence.services.database import Database, database_health_component, ensure_ready
from promptcadence.services.loadcoach_status import loadcoach_health_component

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from promptcadence.config import Settings

__all__ = ["Runtime", "build_runtime"]


class Runtime:
    """The database handle this process owns, and the health checks built on it and on LoadCoach."""

    __slots__ = ("_database", "settings")

    def __init__(self, settings: Settings) -> None:
        """Open storage. Raises if it cannot be opened; never raises for an unreachable LoadCoach.

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

    @property
    def database(self) -> Database:
        """The application's live database handle."""
        return self._database

    @property
    def health_checkers(self) -> Sequence[Callable[[], ComponentHealth]]:
        """The two components Phase 1 can report: ``database`` and ``loadcoach``."""
        return (self._database_health, self._loadcoach_health)

    def _database_health(self) -> ComponentHealth:
        return database_health_component(self._database)

    def _loadcoach_health(self) -> ComponentHealth:
        loadcoach = self.settings.loadcoach
        return loadcoach_health_component(
            base_url=loadcoach.base_url,
            api_key_env=loadcoach.api_key_env,
            api_key_file=loadcoach.api_key_file,
        )

    def close(self) -> None:
        """Dispose the database handle. Safe to call more than once."""
        self._database.close()


def build_runtime(settings: Settings) -> Runtime:
    """Open the handles a served process needs.

    Args:
        settings: The validated configuration.

    Returns:
        The runtime. Raises if storage cannot be opened or migrated; never raises because
        LoadCoach is unreachable — that is a health component (ADR-0045 rule 3).
    """
    return Runtime(settings)
