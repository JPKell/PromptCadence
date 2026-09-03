"""PromptCadence's own migration history: up, down, parity and backup/restore (dev plan Phase 1).

Head is read from the script directory rather than written down, so adding a revision in a later
phase does not require editing an assertion that was never about the revision number.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from weightsdb import MigrationRunner, restore
from weightsdb.testing import temporary_postgres, temporary_sqlite

from promptcadence.infrastructure.db.models import Base
from promptcadence.services.database import (
    MIGRATIONS_LOCATION,
    Database,
    backup_database,
    ensure_ready,
)


def _head() -> str:
    """The single head of PromptCadence's own linear history."""
    with temporary_sqlite() as engine:
        heads = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).heads()
    assert len(heads) == 1, f"PromptCadence's history must stay linear; found heads {heads}"
    return heads[0]


def test_fresh_database_migrates_to_head_sqlite() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        assert runner.current() is None
        outcome = runner.upgrade(backup=False)
        assert outcome.to_revision == _head() == "0001"
        assert runner.is_at_head()


@pytest.mark.integration
def test_fresh_database_migrates_to_head_postgres() -> None:
    with temporary_postgres() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        outcome = runner.upgrade(backup=False)
        assert outcome.to_revision == _head()
        assert runner.is_at_head()


def test_upgrade_head_twice_is_idempotent() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        second = runner.upgrade(backup=False)
        assert second.backed_up is False
        assert second.from_revision == second.to_revision == _head()


def test_downgrade_to_base_removes_every_table() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        runner.downgrade("base")
        assert runner.current() is None


def test_check_parity_matches_models_after_upgrade() -> None:
    """models.py and the migration history describe the same schema (database standards §5.2)."""
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        result = runner.check_parity(Base.metadata)
        assert result.matches, result.diff


@pytest.mark.integration
def test_check_parity_matches_on_postgresql() -> None:
    with temporary_postgres() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        result = runner.check_parity(Base.metadata)
        assert result.matches, result.diff


def test_backup_then_restore_round_trips_the_schema(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'source.sqlite3'}"
    database = Database.from_url(url)
    try:
        ensure_ready(database, auto_migrate=True)
        result = backup_database(database, output=tmp_path / "backup.sqlite3", keep=5)
        assert result.path.is_file()
    finally:
        database.close()

    # `restore` overwrites the database file at the *target* engine's path, so the target must
    # already exist and be migrated — mirroring `promptcadence db restore`'s own precondition.
    restored_url = f"sqlite:///{tmp_path / 'restored.sqlite3'}"
    target = Database.from_url(restored_url)
    try:
        ensure_ready(target, auto_migrate=True)
        restore_result = restore(target.engine, result.path, confirm=True)
        assert restore_result.path == tmp_path / "restored.sqlite3"
    finally:
        target.close()

    verify = Database.from_url(restored_url)
    try:
        runner = MigrationRunner(verify.engine, script_location=MIGRATIONS_LOCATION)
        assert runner.is_at_head()
    finally:
        verify.close()
