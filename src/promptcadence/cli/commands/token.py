"""promptcadence.cli.commands.token — ``token create|list|revoke`` (spec §7.2, §14).

Mode: local. The token is printed exactly once, at creation; the database holds only its SHA-256.
Scopes are ``read``, ``write``, ``approve`` and ``admin`` — ``approve`` deliberately separate from
``write`` (ADR-0049 rule 2), ``admin`` containing the rest.

Only ``typer`` and ``json`` load at module level (CLI standards §12).
"""

from __future__ import annotations

import json as json_module
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from promptcadence.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="API tokens and their scopes.")


@contextmanager
def _open(config: str | None) -> Iterator[Database]:
    from promptcadence.config import ConfigurationError, load_settings
    from promptcadence.services.database import Database, ensure_ready

    try:
        settings = load_settings(config_path=config).settings
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc
    database_url = settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        typer.echo("Error: no database_url configured (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    with Database.from_url(database_url) as database:
        ensure_ready(database, auto_migrate=settings.storage.auto_migrate)
        yield database


@app.command("create")
def create(
    name: Annotated[str, typer.Argument(help="The token's name, unique among active tokens.")],
    scope: Annotated[
        str,
        typer.Option("--scope", help="Comma-separated: read, write, approve, admin."),
    ] = "read",
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the record, including the token, as JSON.")
    ] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Create a token and print it once. Mode: local."""
    from datetime import UTC, datetime

    from baseaicore import ValidationError

    from promptcadence.services.tokens import create_token

    with _open(config) as database:
        try:
            issued = create_token(
                database, name=name, scopes=scope.split(","), now=datetime.now(UTC)
            )
        except ValidationError as exc:
            typer.echo(f"Error: {exc.message} (VALIDATION_ERROR)", err=True)
            raise typer.Exit(2) from exc
    if json_output:
        typer.echo(json_module.dumps({"token": issued.token, **issued.record.as_json()}))
        return
    typer.echo(f"token        {issued.token}")
    typer.echo(f"name         {issued.record.name}")
    typer.echo(f"scopes       {','.join(sorted(issued.record.scopes))}")
    typer.echo("Store it now: it is not shown again and the database holds only its hash.")


@app.command("list")
def list_tokens(
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """List tokens, revoked ones included, never the secret. Mode: local."""
    from promptcadence.services.tokens import list_tokens as _list

    with _open(config) as database:
        records = _list(database)
    if json_output:
        typer.echo(json_module.dumps({"items": [record.as_json() for record in records]}))
        return
    if not records:
        typer.echo("No tokens.")
        return
    for record in records:
        status = "active " if record.active else "revoked"
        typer.echo(
            f"{record.token_id}  {status}  {record.name:<20} "
            f"{','.join(sorted(record.scopes)):<28} uses={record.use_count}"
        )


@app.command("revoke")
def revoke(
    name: Annotated[str, typer.Argument(help="The token's name.")],
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Revoke the active token with that name. Mode: local."""
    from datetime import UTC, datetime

    from promptcadence.services.tokens import TokenNotFoundError, revoke_token

    with _open(config) as database:
        try:
            record = revoke_token(database, name=name, now=datetime.now(UTC))
        except TokenNotFoundError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(1) from exc
    typer.echo(f"revoked      {record.name} ({record.token_id})")
