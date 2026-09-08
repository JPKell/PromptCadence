"""promptcadence.cli.commands.db — upgrade, status, backup, restore.

Every command here is **local** mode (CLI standards §6): it runs the service layer in-process
against the configured database and needs no server running. Only ``typer`` and ``json`` load at
module level, so registering this subgroup never pulls in SQLAlchemy or Alembic (CLI standards
§12).
"""

from __future__ import annotations

import json as json_module
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from baseaicore import utc_now

from promptcadence.cli._backend import load_settings_or_exit

if TYPE_CHECKING:
    from promptcadence.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="Database migration and maintenance.")


@contextmanager
def _open_database(config: str | None) -> Iterator[Database]:
    """Resolve configuration and open one database handle for this command, or exit 3."""
    from promptcadence.services.database import Database

    database_url = load_settings_or_exit(config).storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        typer.echo("Error: no database_url configured (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    with Database.from_url(database_url) as database:
        yield database


@app.command("upgrade")
def upgrade(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
) -> None:
    """Run every pending migration. Mode: local. A no-op at head is a documented no-op (exit 0).

    Example:
        promptcadence db upgrade
    """
    from promptcadence.services.database import upgrade as upgrade_database

    with _open_database(config) as database:
        outcome = upgrade_database(database)

    if json_output:
        typer.echo(
            json_module.dumps(
                {
                    "from_revision": outcome.from_revision,
                    "to_revision": outcome.to_revision,
                    "backed_up": outcome.backed_up,
                    "backup_path": str(outcome.backup_path) if outcome.backup_path else None,
                }
            )
        )
    else:
        typer.echo(f"{outcome.from_revision or '(empty)'} -> {outcome.to_revision}")
        if outcome.backed_up:
            typer.echo(f"Backup: {outcome.backup_path}")


@app.command("status")
def status(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of a table.")
    ] = False,
) -> None:
    """Report the schema revision and the database size. Mode: local.

    Example:
        promptcadence db status --json
    """
    from promptcadence.services.database import get_status

    with _open_database(config) as database:
        report = get_status(database)

    if json_output:
        typer.echo(
            json_module.dumps(
                {
                    "dialect": report.dialect,
                    "current_revision": report.current_revision,
                    "head_revision": report.head_revision,
                    "at_head": report.at_head,
                    "size_bytes": report.size_bytes,
                }
            )
        )
        return
    typer.echo(f"dialect         {report.dialect}")
    typer.echo(f"current         {report.current_revision or '(empty)'}")
    typer.echo(f"head            {report.head_revision}")
    typer.echo(f"at head         {report.at_head}")
    typer.echo(f"size            {report.size_bytes} bytes")


@app.command("backup")
def backup(
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Backup destination file path.")
    ] = None,
    keep: Annotated[int, typer.Option("--keep", help="How many automatic backups to retain.")] = 5,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Write a consistent backup of the database. Mode: local.

    With no ``--output``, writes a timestamped file under the database's own directory and rotates
    old ones against ``--keep``; an operator-chosen ``--output`` path is never rotated.

    Example:
        promptcadence db backup
    """
    from promptcadence.services.database import backup_database

    with _open_database(config) as database:
        result = backup_database(database, output=output, keep=keep)

    typer.echo(f"Wrote {result.path} ({result.size_bytes} bytes).")


@app.command("restore")
def restore(
    source: Annotated[Path, typer.Argument(help="Backup file to restore from.")],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Confirm the restore; required, non-interactive.")
    ] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Restore the database from SOURCE, overwriting the current one. Mode: local.

    Requires ``--yes``: there is no interactive prompt (CLI standards §5), and refusing without it
    is exit 2 naming the flag that would have answered it.

    Example:
        promptcadence db restore ./backups/promptcadence-20260902T090000Z.sqlite3 --yes
    """
    from weightsdb import restore as weightsdb_restore

    if not source.is_file():
        typer.echo(f"Error: {source} does not exist.", err=True)
        raise typer.Exit(1)
    if not yes:
        typer.echo("Error: --yes is required to confirm this destructive operation.", err=True)
        raise typer.Exit(2)

    with _open_database(config) as database:
        result = weightsdb_restore(database.engine, source, confirm=True)
    typer.echo(f"Restored {result.path} from {result.source}.")


@app.command("rebuild-explanations")
def rebuild_explanations(
    trajectory: Annotated[
        str | None,
        typer.Option("--trajectory", help="One trajectory id, or omit for every terminal one."),
    ] = None,
    drop: Annotated[
        bool,
        typer.Option(
            "--drop",
            help="Delete every materialized revision first, then rebuild from the rows.",
        ),
    ] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of a summary.")
    ] = False,
) -> None:
    """Recompose materialized explanations from the rows. Mode: local.

    The maintenance arm of the derived cache (lifecycle §9.1). The rows are authoritative and
    ``explanation_revisions`` is a cache over them, so this command is always safe: it composes
    each terminal trajectory's document again and writes a revision only where the bytes actually
    changed. A run over an intact cache reports ``rebuilt 0``, which is the assertion that the
    cache was correct rather than a run that did nothing.

    ``--drop`` deletes every revision first, which is the stronger form: the cache is discarded
    and rebuilt from nothing. Reads keep working throughout — a trajectory with no revision is
    composed live (ADR-0093).

    Example:
        promptcadence db rebuild-explanations --drop --json
    """
    from promptcadence.config import load_settings
    from promptcadence.services.budget import BudgetService
    from promptcadence.services.egress import EgressService
    from promptcadence.services.explanation import ExplanationBuilder, explanation_store
    from promptcadence.services.pricing import PricingCatalog

    settings = load_settings(config_path=config).settings
    with _open_database(config) as database:
        builder = ExplanationBuilder(
            database,
            budget=BudgetService(
                database, settings, PricingCatalog.from_settings(settings), clock=utc_now
            ),
            egress=EgressService(database, clock=utc_now),
            artifacts=explanation_store(settings),
        )
        dropped = builder.drop_revisions(trajectory) if drop else 0
        considered, written = builder.rebuild(now=datetime.now(UTC), trajectory_id=trajectory)

    if json_output:
        typer.echo(
            json_module.dumps({"dropped": dropped, "considered": considered, "rebuilt": written})
        )
        return
    typer.echo(f"dropped         {dropped}")
    typer.echo(f"considered      {considered}")
    typer.echo(f"rebuilt         {written}")
