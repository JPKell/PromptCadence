"""services.config_schema builds the ADR-0127 rule 1 settings-schema document.

The golden is over the document with ``config_path`` masked — it names a per-test tmp XDG root,
which is not the shape under test. What the golden holds is everything else: key order, the
``json_schema`` pydantic produces, and the three registries' contents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from baseaicore import canonical_json

from promptcadence.services.config_schema import SCHEMA_VERSION, build_schema_document
from promptcadence.services.settings import (
    RUNTIME_SETTINGS,
    all_configured_keys,
    config_only_keys,
)

_GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "config_schema.json"


def _masked(document: dict[str, Any]) -> dict[str, Any]:
    masked = dict(document)
    masked["config_path"] = "<config_path>"
    return masked


def _resolve_in_json_schema(schema: dict[str, Any], path: str) -> bool:
    """Whether the dotted ``path`` (a runtime_changeable/security key) names a real field.

    Walks ``properties``/``$ref`` for a plain ``section.field`` path and
    ``additionalProperties``/``$ref`` for a keyed-table path (``tiers.<name>.field``,
    ``budget.projects.<name>.field``), where the dynamic entry name is consumed without a lookup.
    """
    defs = schema.get("$defs", {})

    def deref(node: dict[str, Any]) -> dict[str, Any]:
        ref = node.get("$ref")
        if ref is not None:
            return dict(defs[ref.rsplit("/", 1)[-1]])
        return node

    node = schema
    for part in path.split("."):
        node = deref(node)
        properties = node.get("properties")
        if properties is not None and part in properties:
            node = properties[part]
            continue
        additional = node.get("additionalProperties")
        if isinstance(additional, dict):
            node = additional
            continue
        return False
    return True


def test_the_schema_document_golden() -> None:
    produced = canonical_json(_masked(build_schema_document())) + "\n"
    if not _GOLDEN.exists():  # pragma: no cover — first run writes the golden
        _GOLDEN.write_text(produced, encoding="utf-8")
    assert produced == _GOLDEN.read_text(encoding="utf-8")


def test_schema_version_is_1_0() -> None:
    assert build_schema_document()["schema_version"] == SCHEMA_VERSION == "1.0"


def test_every_runtime_changeable_and_security_key_exists_in_json_schema() -> None:
    """Gate A: neither registry can drift ahead of the model it is drawn from."""
    document = build_schema_document()
    schema = document["json_schema"]
    for setting in RUNTIME_SETTINGS:
        assert _resolve_in_json_schema(schema, setting), setting
    for key in document["security_keys"]:
        assert _resolve_in_json_schema(schema, key), key


def test_the_three_key_sets_partition_every_configured_leaf() -> None:
    """runtime_changeable, security_keys and config_only never overlap and never miss a leaf."""
    from promptcadence.config import Settings

    document = build_schema_document()
    runtime_keys = set(RUNTIME_SETTINGS)
    security_keys = set(document["security_keys"])
    config_only = set(document["config_only"])

    assert not (runtime_keys & security_keys)
    assert not (runtime_keys & config_only)
    assert not (security_keys & config_only)
    assert runtime_keys | security_keys | config_only == set(all_configured_keys(Settings()))


def test_config_only_keys_matches_the_registry_helper() -> None:
    from promptcadence.config import Settings

    document = build_schema_document()
    assert set(document["security_keys"]) == set(config_only_keys(Settings()))


def test_an_unknown_file_key_is_reported_as_a_problem_not_dropped(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhosts = "127.0.0.1"\nport = 9500\n')  # typo: hosts

    document = build_schema_document(config_path=config_file)

    assert {"key": "server.hosts", "reason": "unknown configuration key"} in document["problems"]
    assert document["sources"]["server.port"] == "file"


def test_sources_marks_a_database_sourced_runtime_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The document's ``sources`` gets the same database overlay ``config show`` does (§7)."""
    from datetime import UTC, datetime

    from weightsdb import MigrationRunner

    from promptcadence.config import load_settings
    from promptcadence.services.database import MIGRATIONS_LOCATION, Database
    from promptcadence.services.settings import write_runtime_settings

    database_path = tmp_path / "promptcadence.sqlite3"
    monkeypatch.setenv("PROMPTCADENCE_STORAGE__DATABASE_URL", f"sqlite:///{database_path}")
    with Database.from_url(f"sqlite:///{database_path}") as database:
        MigrationRunner(database.engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        write_runtime_settings(
            database,
            {"execution.step_retries": 7},
            settings=load_settings().settings,
            now=datetime.now(UTC),
        )
    document = build_schema_document()
    assert document["sources"]["execution.step_retries"] == "database"


def test_an_unknown_key_alongside_a_real_type_error_still_raises(tmp_path: Path) -> None:
    """Pruning only ever removes an unknown key; a genuine type error is never swallowed with it."""
    from baseaicore import ConfigurationError

    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhosts = "127.0.0.1"\nport = "not-a-number"\n')

    with pytest.raises(ConfigurationError, match="port"):
        build_schema_document(config_path=config_file)


def test_no_secret_shaped_value_reaches_the_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """The document carries key paths and layers, never a credential (kickoff §Demonstrate)."""
    monkeypatch.setenv("PROMPTCADENCE_LOADCOACH__API_KEY_ENV", "SOME_SECRET_VALUE_NAME")
    document = build_schema_document()
    assert "SOME_SECRET_VALUE_NAME" not in canonical_json(document)
