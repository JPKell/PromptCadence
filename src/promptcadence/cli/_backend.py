"""promptcadence.cli._backend — how a CLI command resolves its configuration.

Every command resolves configuration first and exits ``3`` on a configuration error (CLI standards
§4) with the same one-line message; that is here once. The configuration module is imported
inside the function, never at module level, so ``--help`` stays cheap (CLI standards §12).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from promptcadence.config import Settings

__all__ = ["load_settings_or_exit"]


def load_settings_or_exit(config: str | None) -> Settings:
    """Resolve configuration, or exit ``3`` with the refusal on stderr.

    Args:
        config: The ``--config`` path, or ``None`` for the default search.
    """
    from promptcadence.config import ConfigurationError, load_settings

    try:
        return load_settings(config_path=config).settings
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc
