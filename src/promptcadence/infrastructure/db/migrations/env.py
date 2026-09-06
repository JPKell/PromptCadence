"""Alembic environment for PromptCadence's own migration history.

Always run through :class:`weightsdb.MigrationRunner`, never through the bare ``alembic`` CLI:
``config.attributes["connection"]`` is always populated by the runner with an already-open
connection from the application's own dialect-configured engine, so this module never builds its
own engine from a URL and never runs in Alembic's offline (SQL-script-generation) mode — neither is
a code path anything in this application uses.
"""

from __future__ import annotations

from alembic import context

from promptcadence.infrastructure.db.models import Base

config = context.config
target_metadata = Base.metadata


def _set_foreign_keys(connection: object, *, on: bool) -> None:
    """Set SQLite's ``foreign_keys`` pragma on this connection, outside any transaction.

    Run through the raw DBAPI cursor deliberately. WeightsDB puts the driver in autocommit and
    emits ``BEGIN IMMEDIATE`` from SQLAlchemy's ``begin`` event, so executing the pragma through
    the SQLAlchemy connection would open a transaction first — and ``PRAGMA foreign_keys`` is a
    documented no-op inside one, which is a silent no-op, the worst kind.
    """
    raw = connection.connection  # type: ignore[attr-defined]  # alembic hands a Connection
    cursor = raw.cursor()
    try:
        cursor.execute(f"PRAGMA foreign_keys={'ON' if on else 'OFF'}")
    finally:
        cursor.close()


def run_migrations_online() -> None:
    """Run migrations against the connection the caller placed in ``config.attributes``.

    Foreign keys are enforced off for the duration on SQLite (they are ``ON`` in normal operation,
    database standards §2), copying LoadCoach's ``env.py`` verbatim rather than solving the same
    problem a second way. Altering a column on SQLite is a table rebuild — alembic's batch mode
    copies, drops and renames — and dropping a table that other rows reference ``ON DELETE
    CASCADE`` deletes those rows. Migration ``0010`` makes ``plans.raw_document`` nullable so the
    retention sweep can scrub it, and ``plan_steps`` cascades from ``plans``, so without this the
    rebuild would silently delete every step of every recorded plan. The pragma takes effect only
    outside a transaction, which is why it runs before ``begin_transaction`` and is restored after.
    """
    connection = config.attributes["connection"]
    sqlite = connection.dialect.name == "sqlite"
    if sqlite:
        _set_foreign_keys(connection, on=False)
    version_table = config.attributes.get("version_table", "alembic_version")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=version_table,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    try:
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if sqlite:
            _set_foreign_keys(connection, on=True)


run_migrations_online()
