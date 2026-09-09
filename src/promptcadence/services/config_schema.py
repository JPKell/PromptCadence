"""promptcadence.services.config_schema — the ADR-0127 settings-schema document.

``<app> config schema --json`` (spec §7.2) prints this document so WeightRoomGym can render a
settings form for PromptCadence without hardcoding its configuration surface (ADR-0127 rule 1).
Built from the same objects the generated ``docs/configuration.md`` reference and ``PUT
/settings`` already use — :class:`~promptcadence.config.Settings` itself,
:data:`~promptcadence.services.settings.RUNTIME_SETTINGS` and
:func:`~promptcadence.services.settings.config_only_keys` — so there is exactly one place each
key set is named, per Gate A's "no duplicated key lists".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from promptcadence.__about__ import __version__
from promptcadence.config import ENV_PREFIX, Settings, load_settings_tolerant
from promptcadence.services.settings import (
    RUNTIME_SETTINGS,
    all_configured_keys,
    config_only_keys,
    database_source_overlay,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["SCHEMA_VERSION", "build_schema_document"]

SCHEMA_VERSION: Final = "1.0"


def build_schema_document(*, config_path: str | Path | None = None) -> dict[str, Any]:
    """Build the ADR-0127 rule 1 settings-schema document.

    Args:
        config_path: Same as :func:`~promptcadence.config.load_settings`.

    Returns:
        A JSON-shaped document with ``schema_version``, ``application``, ``version``,
        ``env_prefix``, ``config_path``, ``json_schema`` (``Settings.model_json_schema()``),
        ``runtime_changeable`` (the registry, verbatim), ``security_keys`` (every leaf
        ``PUT /settings`` refuses by name), ``config_only`` (every other non-runtime leaf),
        ``sources`` (``config show``'s per-leaf layer, database overlay included) and
        ``problems`` (an unknown key path the configuration file named, never dropped).

    Raises:
        ConfigurationError: Any refusal other than an unknown file key — see
            :func:`~promptcadence.config.load_settings_tolerant`.
    """
    loaded, problems = load_settings_tolerant(config_path=config_path)
    settings = loaded.settings

    sources = dict(loaded.sources)
    for path, (_value, source) in database_source_overlay(settings).items():
        sources[path] = source

    runtime_changeable = [
        {
            "key": setting.key,
            "kind": setting.kind.__name__,
            "minimum": setting.minimum,
            "maximum": setting.maximum,
            "description": setting.description,
        }
        for setting in RUNTIME_SETTINGS.values()
    ]
    security_keys = set(config_only_keys(settings))
    config_only = sorted(set(all_configured_keys(settings)) - set(RUNTIME_SETTINGS) - security_keys)

    return {
        "schema_version": SCHEMA_VERSION,
        "application": "promptcadence",
        "version": __version__,
        "env_prefix": ENV_PREFIX,
        "config_path": str(loaded.config_path),
        "json_schema": Settings.model_json_schema(),
        "runtime_changeable": runtime_changeable,
        "security_keys": sorted(security_keys),
        "config_only": config_only,
        "sources": sources,
        "problems": [{"key": key, "reason": "unknown configuration key"} for key in problems],
    }
