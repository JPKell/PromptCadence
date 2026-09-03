"""promptcadence.cli.commands.config — show, validate, init, path.

Only ``typer`` and ``json`` load at module level; ``promptcadence.config`` (which imports pydantic)
is imported lazily inside each command body, per the same startup-performance discipline as
:mod:`promptcadence.cli.commands.system`.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

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
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "values": dumped,
                    "sources": loaded.sources,
                    "config_path": str(loaded.config_path),
                }
            )
        )
        return

    typer.echo(
        f"# {loaded.config_path}{'' if loaded.config_file_used else ' (not found; defaults apply)'}"
    )
    for path, value in _flatten(dumped):
        source = loaded.sources.get(path, "default")
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
