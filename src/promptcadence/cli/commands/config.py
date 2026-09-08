"""promptcadence.cli.commands.config — show, validate, init, path, reference.

Only ``typer`` and ``json`` load at module level; ``promptcadence.config`` (which imports pydantic)
is imported lazily inside each command body, per the same startup-performance discipline as
:mod:`promptcadence.cli.commands.system`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:  # imported for typing only: `promptcadence.config` loads pydantic, and the
    from promptcadence.config import Settings  # CLI keeps that out of module import time

__all__ = ["app"]

app = typer.Typer(help="Configuration inspection and management.")


def _looks_secret(field_name: str) -> bool:
    lowered = field_name.lower()
    return any(marker in lowered for marker in ("token", "key", "secret", "password"))


def _flatten(payload: dict[str, object], prefix: str = "") -> list[tuple[str, object]]:
    """Flatten a nested settings dump into dotted ``path, value`` pairs."""
    rows: list[tuple[str, object]] = []
    for key, value in payload.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            rows.extend(_flatten(value, f"{path}."))
        else:
            rows.append((path, value))
    return rows


def _database_overlay(settings: Settings) -> dict[str, tuple[object, str]]:
    """The runtime-changeable values the ``settings`` table decides, and how to label them.

    Configuration standards §7 asks ``config show`` to mark database-sourced values
    ``(database)``. This opens the configured database read-only to find them, and **never
    raises**: an absent, unmigrated or unreadable database is not a failure of ``config show`` —
    printing the configured values is exactly the right answer when there is no database to
    consult, and a command that needed one would be unusable on a fresh install.

    Args:
        settings: The loaded :class:`~promptcadence.config.Settings`.

    Returns:
        ``path -> (value, source)`` for the keys the database changes, plus the keys whose stored
        row is shadowed by the environment — those keep their configured value and say that a row
        exists and does nothing. Empty when no database can be read.
    """
    from baseaicore import SuiteError
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError

    from promptcadence.services.database import Database
    from promptcadence.services.settings import runtime_settings_document

    database_url = settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        return {}
    url = make_url(database_url)
    # Connecting would create the file. An inspection command must not leave a database behind
    # that `db status` would then report as unmigrated.
    if (
        url.drivername.startswith("sqlite")
        and url.database not in (None, ":memory:")
        and not Path(str(url.database)).is_file()
    ):
        return {}
    try:
        with Database.from_url(database_url) as database:
            document = runtime_settings_document(database, settings=settings)
    except (SQLAlchemyError, SuiteError, OSError):
        return {}
    overlay: dict[str, tuple[object, str]] = {}
    for key, definition in document["definitions"].items():
        if definition["source"] == "database":
            overlay[key] = (document["settings"][key], "database")
        elif definition["shadowed_by"] is not None:
            overlay[key] = (
                document["settings"][key],
                f"{definition['shadowed_by']}; database row {definition['stored']} shadowed",
            )
    return overlay


@app.command("show")
def show(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of a table.")
    ] = False,
) -> None:
    """Print the effective configuration, with the source of every value.

    A runtime-changeable key whose stored row is in force is marked ``(database)`` and shows the
    stored value (configuration standards §7); a stored row the environment shadows is marked as
    shadowed beside the variable that beats it. With no readable database — absent, unmigrated or
    on another host — the output is exactly what it was before there was a settings table.

    Example:
        promptcadence config show --json
    """
    from promptcadence.config import ConfigurationError, load_settings

    try:
        loaded = load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc

    dumped = loaded.settings.model_dump(mode="json")
    overlay = _database_overlay(loaded.settings)
    sources = dict(loaded.sources)
    for path, (value, source) in overlay.items():
        sources[path] = source
        section, _, field_name = path.partition(".")
        if section in dumped:
            dumped[section][field_name] = value
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "values": dumped,
                    "sources": sources,
                    "config_path": str(loaded.config_path),
                }
            )
        )
        return

    typer.echo(
        f"# {loaded.config_path}{'' if loaded.config_file_used else ' (not found; defaults apply)'}"
    )
    for path, value in _flatten(dumped):
        source = sources.get(path, "default")
        rendered = "********" if _looks_secret(path) else value
        typer.echo(f"{path:<48} {rendered!s:<24} ({source})")


@app.command("validate")
def validate(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Validate configuration without starting the service. Exit 0 or 3.

    Example:
        promptcadence config validate --config ./config.toml
    """
    from promptcadence.config import ConfigurationError, load_settings

    try:
        load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc
    typer.echo("Configuration is valid.")


@app.command("path")
def path(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Print the configuration, data and state directories in use.

    Example:
        promptcadence config path
    """
    from promptcadence.config import config_dir, data_dir, resolve_config_path, state_dir

    typer.echo(f"config file  {resolve_config_path(config)}")
    typer.echo(f"config dir   {config_dir()}")
    typer.echo(f"data dir     {data_dir()}")
    typer.echo(f"state dir    {state_dir()}")


@app.command("init")
def init(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to write the config file to.")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a fully commented example configuration file.

    Example:
        promptcadence config init --force
    """
    from promptcadence.config import EXAMPLE_CONFIG_TOML, resolve_config_path

    target = resolve_config_path(config)
    if target.exists() and not force:
        typer.echo(f"Error: {target} already exists (use --force to overwrite).", err=True)
        raise typer.Exit(3)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG_TOML, encoding="utf-8")
    typer.echo(str(target))


@app.command("reference")
def reference(
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Exit 1 if docs/configuration.md differs from the generated text."
        ),
    ] = False,
    output: Annotated[
        str | None, typer.Option("--output", help="Write to this file instead of stdout.")
    ] = None,
) -> None:
    """Generate the configuration reference from the settings model (configuration standards §8).

    Mode: local. With ``--check``, compares against ``--output`` (or ``docs/configuration.md``)
    and exits 1 on drift, which is what CI runs.

    Example:
        promptcadence config reference --output docs/configuration.md
    """
    from pathlib import Path

    from promptcadence.services.config_reference import render_configuration_reference

    rendered = render_configuration_reference()
    target = Path(output) if output else Path("docs/configuration.md")
    if check:
        committed = target.read_text(encoding="utf-8") if target.is_file() else ""
        if committed != rendered:
            typer.echo(
                f"{target} differs from the generated reference; run "
                "`promptcadence config reference --output docs/configuration.md`",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(f"{target} matches the settings model")
        return
    if output:
        target.write_text(rendered, encoding="utf-8")
        typer.echo(f"wrote {target}")
    else:
        typer.echo(rendered)
