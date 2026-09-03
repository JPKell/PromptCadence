"""promptcadence.services.database — engine construction, startup migration and status.

Route handlers and CLI command bodies never call :func:`weightsdb.create_engine_for` directly
(CLI standards §1, coding standards §5); they call a function here. That is what makes
``promptcadence health --json`` and ``GET /api/v1/health`` report identical database status by
construction rather than by review.

PromptCadence writes no database plumbing of its own: the engine, the session scopes, the
migration runner, the backup and the health probe all come from WeightsDB.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Final

from mirrorwall import ComponentHealth, ComponentStatus
from weightsdb import (
    DatabaseError,
    MigrationRequired,
    MigrationRunner,
    create_engine_for,
    database_health,
    database_size_bytes,
    session_factory,
    session_scope,
    transaction,
)
from weightsdb import backup as weightsdb_backup
from weightsdb.backup import sqlite_path

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker
    from weightsdb import MigrationOutcome
    from weightsdb.backup import BackupResult

__all__ = [
    "MIGRATIONS_LOCATION",
    "Database",
    "DatabaseStatus",
    "backup_database",
    "build_engine",
    "database_health_component",
    "ensure_ready",
    "get_status",
    "migration_runner",
    "upgrade",
]

MIGRATIONS_LOCATION: Final = str(
    Path(__file__).resolve().parent.parent / "infrastructure" / "db" / "migrations"
)

_APPLICATION_NAME: Final = "promptcadence"


def build_engine(database_url: str, *, statement_timeout_ms: int | None = None) -> Engine:
    """Build the engine for the configured database URL.

    Args:
        database_url: ``settings.storage.database_url`` — never ``None`` once Settings validated.
        statement_timeout_ms: ``settings.storage.statement_timeout_ms``; PostgreSQL only.

    Returns:
        A dialect-configured engine. Opens no connection until first use.
    """
    return create_engine_for(
        database_url,
        statement_timeout_ms=statement_timeout_ms,
        application_name=_APPLICATION_NAME,
    )


class Database:
    """The application's live connection to its database: one engine, for as long as it serves.

    Owned by the caller — the web application creates one in its lifespan and disposes it at
    shutdown; a CLI command creates one, runs, and closes it on the way out. Every service function
    takes a handle rather than building an engine from a URL.
    """

    __slots__ = ("_engine", "_sessions")

    def __init__(self, engine: Engine) -> None:
        """Wrap an existing engine. Prefer :meth:`from_url` unless you built the engine yourself."""
        self._engine = engine
        self._sessions = session_factory(engine)

    @classmethod
    def from_url(cls, database_url: str, *, statement_timeout_ms: int | None = None) -> Database:
        """Build a handle for ``database_url``. Opens no connection until first use."""
        return cls(build_engine(database_url, statement_timeout_ms=statement_timeout_ms))

    @property
    def engine(self) -> Engine:
        """The underlying engine, for the file-level operations that need one directly."""
        return self._engine

    @property
    def sessions(self) -> sessionmaker[Session]:
        """The session factory bound to this handle's engine."""
        return self._sessions

    @contextmanager
    def write(self) -> Iterator[Session]:
        """One read-write unit of work, committed on success and rolled back on any exception."""
        with session_scope(self._sessions) as session:
            yield session

    @contextmanager
    def read(self) -> Iterator[Session]:
        """One read-only unit of work.

        Enforced, not merely declared: a write attempted inside this scope is refused by SQLite
        rather than silently taken (:func:`weightsdb.transaction`).
        """
        with session_scope(self._sessions) as session, transaction(session, immediate=False):
            yield session

    def close(self) -> None:
        """Dispose the pool. The handle must not be used afterwards."""
        self._engine.dispose()

    def __enter__(self) -> Database:
        """Support ``with Database.from_url(...) as db:`` for one-shot callers like the CLI."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Always dispose the pool, whether the body succeeded or raised."""
        self.close()


def migration_runner(engine: Engine, *, backup_retention: int = 5) -> MigrationRunner:
    """Build the runner over PromptCadence's own migration history."""
    return MigrationRunner(
        engine, script_location=MIGRATIONS_LOCATION, backup_retention=backup_retention
    )


def upgrade(
    database: Database, *, revision: str = "head", backup_retention: int = 5
) -> MigrationOutcome:
    """Run ``promptcadence db upgrade``: migrate to ``revision``, taking a backup first.

    Idempotent — calling this when already at ``revision`` is a documented no-op (CLI standards
    §11).
    """
    runner = migration_runner(database.engine, backup_retention=backup_retention)
    return runner.upgrade(revision, backup=runner.current() is not None)


def ensure_ready(database: Database, *, auto_migrate: bool) -> None:
    """Bring the schema to head, or refuse to run against a stale one.

    Args:
        database: The handle to check.
        auto_migrate: ``settings.storage.auto_migrate``.

    Raises:
        MigrationRequired: Migrations are pending and ``auto_migrate`` is false — which is the
            PostgreSQL default, because a failed migration there cannot be rolled back
            automatically (database standards §5.1).
        SchemaAhead: The database was written by a newer build.
    """
    runner = migration_runner(database.engine)
    if runner.is_at_head():
        return
    if auto_migrate:
        runner.upgrade()
        return
    message = (
        "The database schema is not at head and storage.auto_migrate is false. Run "
        "`promptcadence db upgrade` after taking a backup."
    )
    raise MigrationRequired(message, details={"current": runner.current(), "head": runner.heads()})


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    """What ``promptcadence db status`` reports."""

    dialect: str
    current_revision: str | None
    head_revision: str | None
    at_head: bool
    size_bytes: int | None


def get_status(database: Database) -> DatabaseStatus:
    """Report the schema revision and the file size, without modifying anything."""
    runner = migration_runner(database.engine)
    heads = runner.heads()
    try:
        size = database_size_bytes(database.engine)
    except DatabaseError:  # pragma: no cover — a dialect that cannot report size
        size = None
    return DatabaseStatus(
        dialect=database.engine.dialect.name,
        current_revision=runner.current(),
        head_revision=heads[0] if heads else None,
        at_head=runner.is_at_head(),
        size_bytes=size,
    )


def backup_database(database: Database, *, output: Path | None, keep: int) -> BackupResult:
    """Run ``promptcadence db backup``: take a consistent backup, rotating automatic ones.

    Args:
        database: The application's database handle.
        output: An operator-chosen destination *file* path, never rotated. ``None`` chooses an
            automatic, timestamped path under the database's own directory and rotates it against
            ``keep`` — ``weightsdb.backup`` writes directly to the path it is given, so this is the
            one place that turns "a backup" into a concrete filename.
        keep: ``settings.storage.backup_retention``; ignored when ``output`` is given.
    """
    from datetime import UTC, datetime

    engine = database.engine
    if output is not None:
        return weightsdb_backup(engine, output)
    source = sqlite_path(engine)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = source.parent / "backups" / f"promptcadence-{stamp}{source.suffix}"
    return weightsdb_backup(engine, destination, keep=keep, prefix="promptcadence-")


def database_health_component(database: Database | None) -> ComponentHealth:
    """Report the ``database`` health component.

    Args:
        database: The handle, or ``None`` when the process has not opened one.

    Returns:
        WeightsDB's own verdict, not a second opinion: it already classifies reachability, pending
        migrations, integrity and free space into a :class:`~mirrorwall.ComponentStatus`, and a
        re-derivation here could disagree with what ``promptcadence db status`` reports.
    """
    if database is None:
        return ComponentHealth(
            name="database",
            status=ComponentStatus.NOT_CONFIGURED,
            detail="No database handle is open.",
        )
    try:
        health = database_health(database.engine, migration_runner(database.engine))
    except DatabaseError as exc:
        return ComponentHealth(name="database", status=ComponentStatus.UNAVAILABLE, detail=str(exc))
    detail = (
        "; ".join(health.degraded_reasons)
        if health.degraded_reasons
        else f"{health.dialect}, schema at head."
    )
    return ComponentHealth(
        name="database",
        # WeightsDB annotates `status` as the literal set; ComponentStatus is that set.
        status=ComponentStatus(health.status),
        detail=detail,
        data={"revision": health.current_revision, "dialect": health.dialect},
    )
