"""PromptCadence's own migration history: up, down, parity and backup/restore (dev plan Phase 1).

Head is read from the script directory rather than written down, so adding a revision in a later
phase does not require editing an assertion that was never about the revision number.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from commissioner.sql import DEFAULT_TABLE_PREFIX as EGRESS_TABLE_PREFIX
from loadledger.sql import DEFAULT_TABLE_PREFIX
from sqlalchemy import inspect
from weightsdb import MigrationRunner, restore
from weightsdb.testing import temporary_postgres, temporary_sqlite

from promptcadence.infrastructure.db.models import EGRESS_TABLES, LEDGER_TABLES, Base
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
        assert outcome.to_revision == _head() == "0007"
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


def test_upgrade_from_empty_creates_exactly_the_mounted_ledger_schema() -> None:
    """0005 is the first package mount, so its DDL is proved against the package's own shapes.

    ADR-0050's whole promise is that a mounted table upgrades with the host's history and is
    identical to what the package declared. Comparing the migrated database column-for-column
    against ``mount_ledger_tables``' output is what makes that a fact rather than an intention —
    and it is what catches the failure mode the pattern names, where a hand-edited revision drifts
    from the mount and the parity check below is the only thing that would have noticed.
    """
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        inspector = inspect(engine)
        created = set(inspector.get_table_names())
        for table in LEDGER_TABLES.all_tables:
            assert table.name in created, f"{table.name} was mounted but never migrated"
            columns = {column["name"]: column for column in inspector.get_columns(table.name)}
            assert set(columns) == {column.name for column in table.columns}
            for column in table.columns:
                assert columns[column.name]["nullable"] == column.nullable, column.name
        assert LEDGER_TABLES.prefix == DEFAULT_TABLE_PREFIX


def test_downgrade_removes_the_mounted_ledger_tables() -> None:
    """A mount that cannot be undone is a mount that pins a host to one package version."""
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        runner.downgrade("0004")
        remaining = set(inspect(engine).get_table_names())
        assert not remaining & {table.name for table in LEDGER_TABLES.all_tables}
        assert "trajectories" in remaining


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


def test_upgrade_from_empty_creates_exactly_the_mounted_egress_schema() -> None:
    """0006 is the second package mount, and it earns the same proof as the first.

    Transcribing a migration by hand from a package's table shapes is exactly where the two drift,
    and a drift here is silent: the application would still start, still record decisions, and
    only fail on the column the revision spelled differently. So the migrated database is compared
    column-for-column against ``mount_egress_tables``' own output, the same way ``0005``'s ledger
    tables are.
    """
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        inspector = inspect(engine)
        created = set(inspector.get_table_names())
        for table in EGRESS_TABLES.all_tables:
            assert table.name in created, f"{table.name} was mounted but never migrated"
            columns = {column["name"]: column for column in inspector.get_columns(table.name)}
            assert set(columns) == {column.name for column in table.columns}
            for column in table.columns:
                assert columns[column.name]["nullable"] == column.nullable, column.name
        assert EGRESS_TABLES.prefix == EGRESS_TABLE_PREFIX


def test_egress_decision_indexes_are_migrated_under_their_mounted_names() -> None:
    """The indexes are what make a decision queryable by run without opening every document.

    They carry the prefix because index names are global per schema on PostgreSQL, so a host that
    migrated them under different names would collide with the next mounted package rather than
    fail here.
    """
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        migrated = {index["name"] for index in inspect(engine).get_indexes("egress_decisions")}
        assert migrated == {index.name for index in EGRESS_TABLES.decisions.indexes}


def test_downgrade_removes_the_mounted_egress_table() -> None:
    """A mount that cannot be undone is a mount that pins a host to one package version."""
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        runner.downgrade("0005")
        remaining = set(inspect(engine).get_table_names())
        assert not remaining & {table.name for table in EGRESS_TABLES.all_tables}
        assert "ledger_entries" in remaining, "0006's downgrade must not take 0005's tables with it"
        assert "trajectories" in remaining


def test_migrated_schema_matches_the_metadata_autogenerate_would_diff_against() -> None:
    """Both mounts, proved the way ADR-0050 says a mount is proved: no pending autogenerate diff.

    The parity check is the one assertion that would catch a mounted table that exists in the
    database but not in ``Base.metadata`` — the failure mode where a lazy mount produces a
    revision that *drops* the package's tables rather than omitting them.
    """
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        parity = runner.check_parity(Base.metadata)
        assert parity.matches, parity.diff
