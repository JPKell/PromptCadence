"""Degradation: a migration fails mid-upgrade (graceful-degradation.md, row "Database migration
fails").

Documented behaviour: "Automatic restore from the pre-migration backup; original DB never left
half-migrated" — the same property FreeWeight's and LoadCoach's own
``test_failed_migration_restores_the_original_database_byte_identical`` /
``test_failed_migration_restores_backup_on_sqlite`` prove; this is that test for PromptCadence,
following LoadCoach's shape (a deliberately failing revision stacked on the real head, backed by a
real ``MigrationRunner`` rather than a mock).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text
from weightsdb import MigrationFailed, MigrationRunner, create_engine_for

from promptcadence.services.database import MIGRATIONS_LOCATION

if TYPE_CHECKING:
    from collections.abc import Iterator

_BROKEN_REVISION_ID = "9999"


def _broken_revision(down_revision: str) -> str:
    """A revision that always fails, stacked on whatever the real head currently is."""
    return (
        f'"""broken\n\nRevision ID: {_BROKEN_REVISION_ID}\nRevises: {down_revision}\n"""\n'
        "from __future__ import annotations\n\n"
        f'revision: str = "{_BROKEN_REVISION_ID}"\n'
        f'down_revision: str | None = "{down_revision}"\n'
        "branch_labels = None\n"
        "depends_on = None\n\n"
        "def upgrade() -> None:\n"
        "    raise RuntimeError('deliberate failure')\n\n"
        "def downgrade() -> None:\n"
        "    pass\n"
    )


def _head(tmp_path: Path) -> str:
    """The single head of PromptCadence's own linear history."""
    engine = create_engine_for(f"sqlite:///{tmp_path / 'head_probe.sqlite3'}")
    try:
        heads = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).heads()
    finally:
        engine.dispose()
    assert len(heads) == 1, f"PromptCadence's history must stay linear; found heads {heads}"
    return heads[0]


@pytest.fixture
def broken_migrations(tmp_path: Path) -> Iterator[str]:
    """A copy of the real migration history with one always-failing revision stacked on head."""
    head = _head(tmp_path)
    broken_dir = tmp_path / "broken_migrations"
    shutil.copytree(MIGRATIONS_LOCATION, broken_dir)
    (broken_dir / "versions" / "9999_broken.py").write_text(_broken_revision(head))
    yield str(broken_dir)


def test_a_failing_migration_restores_the_backup_and_keeps_existing_rows(
    tmp_path: Path, broken_migrations: str
) -> None:
    head = _head(tmp_path)
    engine = create_engine_for(f"sqlite:///{tmp_path / 'promptcadence.sqlite3'}")
    try:
        runner = MigrationRunner(engine, script_location=broken_migrations)
        runner.upgrade(head, backup=False)

        with engine.connect() as connection:
            connection.execute(
                text("INSERT INTO settings (key, value_json, updated_at) VALUES (:k, :v, :u)"),
                {"k": "wire", "v": '"interim"', "u": "2026-08-29T00:00:00"},
            )
            connection.commit()

        with pytest.raises(MigrationFailed) as excinfo:
            runner.upgrade(_BROKEN_REVISION_ID)
        assert excinfo.value.details["restored"] is True
        assert runner.current() == head

        with engine.connect() as connection:
            rows = connection.execute(text("SELECT key FROM settings")).fetchall()
        assert [row[0] for row in rows] == ["wire"]
    finally:
        engine.dispose()
