"""Integration test for the downgrade drill packaging standards §6.1 promises.

"Downgrading the application without downgrading the database is refused, not attempted: a
database ahead of the code raises ``SchemaAhead`` at startup and names both revisions and the
backup directory. A supported downgrade path is: stop the application, restore the automatic
pre-migration backup, install the older version." Until this test, nothing in any of the four
applications' suites drove that drill end to end (M9_AUDIT.md Group 3, item O2) — this is the
one that does, on PromptCadence's own bootstrap path.

Writing this test surfaced that PromptCadence's ``ensure_ready`` had never actually implemented
the ``SchemaAhead`` check FreeWeight and LoadCoach already had: a database ahead of head fell
straight through to an attempted ``runner.upgrade()`` (when ``auto_migrate`` is true, the SQLite
default), which alembic would fail on its own terms rather than refusing cleanly. It now checks
``known_revisions()`` first, the same as its siblings.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from weightsdb import SchemaAhead
from weightsdb import restore as weightsdb_restore

from promptcadence.config import load_settings
from promptcadence.services.database import (
    Database,
    _backup_directory,
    backup_database,
    ensure_ready,
    migration_runner,
)
from promptcadence.services.settings import read_runtime_settings, write_runtime_settings


def test_schema_ahead_is_refused_and_the_pre_migration_backup_restores_it() -> None:
    """Upgrade, write a row, back up, jump the version ahead, refuse, restore, start again."""
    loaded = load_settings()
    settings = loaded.settings
    assert settings.storage.database_url is not None
    database_url = settings.storage.database_url

    database = Database.from_url(database_url)
    # 1. Bootstrap's own path to a fresh database: migrate to head.
    ensure_ready(database, auto_migrate=True)
    head = migration_runner(database.engine).heads()[0]

    # 2. Write one row through the application's own settings layer, not raw SQL.
    write_runtime_settings(
        database,
        {"storage.content_retention_hours": 12},
        settings=settings,
        now=datetime.now(UTC),
    )

    # 3. `promptcadence db backup` — the real path an operator runs before anything risky.
    backup_result = backup_database(database, output=None, keep=5)
    assert backup_result.path.is_file()

    # 4. Simulate a newer application version having migrated this database further: hand-set
    # `alembic_version` to a revision this build's history does not contain. `database` is closed
    # here (not reopened at the end) — the file-level jump below must not run underneath its pool.
    fake_future_revision = "9999_from_the_future"
    with database.engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": fake_future_revision},
        )
    database.close()

    # 5. Startup now refuses — the exact drill packaging standards §6.1 promises.
    reopened = Database.from_url(database_url)
    try:
        try:
            ensure_ready(reopened, auto_migrate=True)
        except SchemaAhead as exc:
            assert fake_future_revision in str(exc)
            assert head in str(exc)
            assert exc.details["current"] == fake_future_revision
            assert exc.details["head"] == head
            expected_backup_directory = str(_backup_directory(reopened.engine))
            assert expected_backup_directory in str(exc)
            assert exc.details["backup_directory"] == expected_backup_directory
        else:  # pragma: no cover — defensive: the test proves nothing if this branch runs
            raise AssertionError("SchemaAhead was not raised for a database ahead of head")
    finally:
        reopened.close()

    # 6. The supported downgrade path: restore the pre-migration backup.
    restored = Database.from_url(database_url)
    try:
        weightsdb_restore(restored.engine, backup_result.path, confirm=True)

        # 7. The application starts again, at the revision it knew about all along, and the row
        # written before the jump is intact.
        ensure_ready(restored, auto_migrate=True)
        assert migration_runner(restored.engine).current() == head
        effective = read_runtime_settings(restored, settings=settings)
        assert effective["storage.content_retention_hours"] == 12
    finally:
        restored.close()
