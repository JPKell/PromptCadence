"""The runtime-changeable registry: what moves, what is refused, and which layer wins.

Four properties, and the third is the one a reviewer should read first:

* Every key in the registry coerces its own type and refuses outside its own bounds.
* A security-relevant key is ``FORBIDDEN`` **naming the key**; an unknown one is
  ``VALIDATION_ERROR`` naming it and listing what can be changed. Neither is a silent ignore.
* **The environment beats a stored row** (configuration standards §7, ADR-0100), and a row that
  does nothing is reported as shadowed rather than dropped.
* A stored row this build cannot read falls back to configuration instead of raising: a row
  written by another version must not stop this one from serving.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from baseaicore import ValidationError
from weightsdb import MigrationRunner, upsert
from weightsdb.testing import temporary_sqlite

from promptcadence.config import Settings, env_var_for
from promptcadence.infrastructure.db.models import Setting
from promptcadence.services.database import MIGRATIONS_LOCATION, Database
from promptcadence.services.settings import (
    RUNTIME_SETTINGS,
    RuntimeSetting,
    SettingConfigOnly,
    apply_runtime_settings,
    config_only_keys,
    is_config_only,
    read_runtime_settings,
    runtime_settings_document,
    shadowing_source,
    write_runtime_settings,
)

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def database() -> Iterator[Database]:
    with temporary_sqlite() as engine:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        yield Database(engine)


@pytest.fixture
def settings() -> Settings:
    return Settings()


def _store(database: Database, key: str, value: object) -> None:
    """Write a row directly, bypassing validation — what another version might have left."""
    with database.write() as session:
        upsert(
            session,
            Setting,
            values={"key": key, "value_json": value, "updated_at": _NOW},
            index_elements=["key"],
        )


def test_the_registry_names_five_tuning_keys_and_every_one_exists_in_the_model(
    settings: Settings,
) -> None:
    assert set(RUNTIME_SETTINGS) == {
        "storage.content_retention_hours",
        "compaction.threshold",
        "execution.step_retries",
        "execution.max_turns_per_step",
        "planning.corrective_retries",
    }
    for setting in RUNTIME_SETTINGS.values():
        section = getattr(settings, setting.section)
        assert setting.field in type(section).model_fields, setting.key
        assert isinstance(getattr(section, setting.field), setting.kind), setting.key


@pytest.mark.parametrize("key", sorted(RUNTIME_SETTINGS))
def test_every_key_coerces_its_own_type_and_refuses_at_its_bounds(key: str) -> None:
    setting = RUNTIME_SETTINGS[key]
    assert setting.minimum is not None and setting.maximum is not None, key
    assert setting.coerce(setting.minimum) == setting.minimum
    assert setting.coerce(setting.maximum) == setting.maximum
    for outside in (setting.minimum - 1, setting.maximum + 1):
        with pytest.raises(ValidationError) as refused:
            setting.coerce(outside)
        assert key in refused.value.message
    with pytest.raises(ValidationError):
        setting.coerce("8")
    with pytest.raises(ValidationError):
        setting.coerce(True)


def test_an_integer_key_refuses_a_fraction_and_a_boolean_key_refuses_a_number() -> None:
    with pytest.raises(ValidationError, match="whole number"):
        RUNTIME_SETTINGS["execution.step_retries"].coerce(1.5)
    flag = RuntimeSetting("storage.retain_content", bool, "not in the set; the type's behaviour")
    assert flag.coerce(False) is False
    with pytest.raises(ValidationError, match="true or false"):
        flag.coerce(1)


def test_a_security_relevant_key_is_forbidden_by_name_and_writes_nothing(
    database: Database, settings: Settings
) -> None:
    with pytest.raises(SettingConfigOnly) as refused:
        write_runtime_settings(
            database,
            {"execution.step_retries": 3, "server.host": "0.0.0.0"},  # noqa: S104 — refused, never bound
            settings=settings,
            now=_NOW,
        )
    assert refused.value.code == "FORBIDDEN"
    assert "server.host" in refused.value.message
    assert refused.value.details["key"] == "server.host"
    with database.read() as session:
        assert session.get(Setting, "execution.step_retries") is None, "refused whole"


@pytest.mark.parametrize(
    "key",
    [
        "server.host",
        "server.allow_lan_exposure",
        "loadcoach.api_key_env",
        "approval.mode",
        "approval.gate_egress_at",
        "budget.daily_money_ceiling",
        "budget.projects.research.money_ceiling",
        "tools.workspace_root",
        "tools.fetch_allowed_hosts",
        "tiers.remote_frontier.remote",
        "policy.default_tier",
        "storage.database_url",
        "storage.retain_content",
        "planning.enabled",
        "planning.allow_request_override",
        "logging.include_content",
    ],
)
def test_the_config_only_set_covers_exposure_egress_credentials_containment_and_spend(
    key: str,
) -> None:
    assert is_config_only(key), key
    assert key not in RUNTIME_SETTINGS


def test_the_configured_config_only_keys_name_the_tiers_the_operator_configured(
    settings: Settings,
) -> None:
    listed = config_only_keys(settings)
    assert "tiers.local_fast.remote" in listed, "the configured tier, not a <name> placeholder"
    assert "budget.daily_money_ceiling" in listed
    assert not any(key in RUNTIME_SETTINGS for key in listed)
    assert "compaction.policy_chain" not in listed, "unknown, not refused by name"


def test_an_unknown_key_is_a_validation_error_naming_it_and_the_changeable_set(
    database: Database, settings: Settings
) -> None:
    with pytest.raises(ValidationError) as refused:
        write_runtime_settings(
            database, {"compaction.policy_chain": ["x"]}, settings=settings, now=_NOW
        )
    assert "compaction.policy_chain" in refused.value.message
    assert refused.value.details["runtime_changeable"] == sorted(RUNTIME_SETTINGS)


def test_a_stored_value_is_effective_and_reported_as_database_sourced(
    database: Database, settings: Settings
) -> None:
    effective = write_runtime_settings(
        database, {"execution.step_retries": 4}, settings=settings, now=_NOW
    )
    assert effective["execution.step_retries"] == 4
    assert effective["planning.corrective_retries"] == settings.planning.corrective_retries
    document = runtime_settings_document(database, settings=settings)
    definition = document["definitions"]["execution.step_retries"]
    assert definition["source"] == "database"
    assert definition["stored"] == 4
    assert definition["configured"] == settings.execution.step_retries
    assert definition["shadowed_by"] is None


def test_the_environment_beats_a_stored_row_and_the_row_is_reported_as_shadowed(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var_for("execution.step_retries"), "7")
    from promptcadence.config import load_settings

    configured = load_settings().settings
    assert configured.execution.step_retries == 7
    assert shadowing_source("execution.step_retries") == (
        "env PROMPTCADENCE_EXECUTION__STEP_RETRIES"
    )
    effective = write_runtime_settings(
        database, {"execution.step_retries": 2}, settings=configured, now=_NOW
    )
    assert effective["execution.step_retries"] == 7, "configuration standards §7 precedence"
    definition = runtime_settings_document(database, settings=configured)["definitions"][
        "execution.step_retries"
    ]
    assert definition["stored"] == 2, "the row is kept and shown, not discarded"
    assert definition["source"] == "configuration"
    assert definition["shadowed_by"] == "env PROMPTCADENCE_EXECUTION__STEP_RETRIES"


def test_the_stored_row_wins_again_once_the_environment_stops_pinning_it(
    database: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var_for("execution.step_retries"), "7")
    write_runtime_settings(database, {"execution.step_retries": 2}, settings=settings, now=_NOW)
    monkeypatch.delenv(env_var_for("execution.step_retries"))
    assert read_runtime_settings(database, settings=settings)["execution.step_retries"] == 2


@pytest.mark.parametrize("stored", ["nonsense", -1, 99, None])
def test_a_row_this_build_cannot_read_falls_back_to_configuration(
    database: Database, settings: Settings, stored: object
) -> None:
    _store(database, "execution.step_retries", stored)
    effective = read_runtime_settings(database, settings=settings)
    assert effective["execution.step_retries"] == settings.execution.step_retries


def test_the_document_carries_every_definition_and_the_config_only_list(
    database: Database, settings: Settings
) -> None:
    document = runtime_settings_document(database, settings=settings)
    assert set(document["settings"]) == set(RUNTIME_SETTINGS)
    assert set(document["definitions"]) == set(RUNTIME_SETTINGS)
    threshold = document["definitions"]["compaction.threshold"]
    assert threshold["type"] == "float"
    assert (threshold["minimum"], threshold["maximum"]) == (0.01, 1.0)
    assert threshold["stored"] is None and threshold["source"] == "configuration"
    assert "server.host" in document["config_only"]


def test_applying_writes_the_effective_values_onto_the_process_settings_in_place(
    settings: Settings,
) -> None:
    section = settings.execution
    apply_runtime_settings(
        settings,
        {
            "execution.step_retries": 5,
            "compaction.threshold": 0.5,
            "not.a.key": 1,
        },
    )
    assert settings.execution.step_retries == 5
    assert settings.compaction.threshold == 0.5
    assert section is settings.execution, "in place: every holder of the object sees the change"
    assert not hasattr(settings, "not")
