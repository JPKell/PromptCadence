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
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from promptcadence.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="Database migration and maintenance.")


@contextmanager
def _open_database(config: str | None) -> Iterator[Database]:
    """Resolve configuration and open one database handle for this command, or exit 3."""
    from promptcadence.config import ConfigurationError, load_settings
    from promptcadence.services.database import Database

    try:
        loaded = load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc
    database_url = loaded.settings.storage.database_url
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
