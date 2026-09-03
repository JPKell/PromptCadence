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


def run_migrations_online() -> None:
    """Run migrations against the connection the caller placed in ``config.attributes``."""
    connection = config.attributes["connection"]
    version_table = config.attributes.get("version_table", "alembic_version")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=version_table,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
