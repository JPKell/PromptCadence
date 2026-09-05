"""Tests for promptcadence.config: precedence chain and every startup refusal."""

from __future__ import annotations

from pathlib import Path

import pytest
from baseaicore import ConfigurationError

from promptcadence.config import (
    EXAMPLE_CONFIG_TOML,
    InsecureBindingError,
    load_settings,
)


def test_zero_configuration_defaults_validate_cleanly(tmp_path: Path) -> None:
    """A fresh install with no config file at all is fully functional (spec §20 AC1)."""
    loaded = load_settings(config_path=tmp_path / "does-not-exist.toml")
    assert loaded.config_file_used is False
    assert loaded.settings.server.host == "127.0.0.1"
    assert loaded.settings.server.port == 8768
    assert loaded.settings.storage.database_url is not None
    assert loaded.settings.storage.database_url.startswith("sqlite:///")
    assert loaded.settings.storage.database_url.endswith("promptcadence.sqlite3")
    # The two zero-config tiers are both local and therefore need no pricing or classification.
    assert set(loaded.settings.tiers) == {"local_fast", "local_large"}
    assert all(not tier.remote for tier in loaded.settings.tiers.values())
    assert loaded.settings.policy.default_tier == "local_fast"


def test_precedence_file_then_env_then_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nport = 9001\n[logging]\nlevel = "DEBUG"\n')

    loaded = load_settings(config_path=config_file)
    assert loaded.settings.server.port == 9001
    assert loaded.settings.logging.level == "DEBUG"
    assert loaded.sources["server.port"] == "file"

    monkeypatch.setenv("PROMPTCADENCE_SERVER__PORT", "9002")
    loaded = load_settings(config_path=config_file)
    assert loaded.settings.server.port == 9002
    assert loaded.settings.logging.level == "DEBUG"  # untouched by the env override
    assert loaded.sources["server.port"].startswith("env")

    loaded = load_settings(config_path=config_file, cli_overrides={"server": {"port": 9003}})
    assert loaded.settings.server.port == 9003
    assert loaded.sources["server.port"] == "cli"


@pytest.mark.parametrize(
    ("section", "field", "file_value", "env_var", "env_value", "cli_value"),
    [
        (
            "server",
            "rate_limit_per_minute",
            "300",
            "PROMPTCADENCE_SERVER__RATE_LIMIT_PER_MINUTE",
            "400",
            500,
        ),
        (
            "storage",
            "content_retention_hours",
            "12",
            "PROMPTCADENCE_STORAGE__CONTENT_RETENTION_HOURS",
            "6",
            3,
        ),
        (
            "loadcoach",
            "timeout_seconds",
            "10.0",
            "PROMPTCADENCE_LOADCOACH__TIMEOUT_SECONDS",
            "20.0",
            30.0,
        ),
        ("planning", "max_plan_steps", "5", "PROMPTCADENCE_PLANNING__MAX_PLAN_STEPS", "6", 7),
        (
            "approval",
            "request_timeout_hours",
            "1.0",
            "PROMPTCADENCE_APPROVAL__REQUEST_TIMEOUT_HOURS",
            "2.0",
            3.0,
        ),
        ("execution", "lease_seconds", "10", "PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "20", 30),
        (
            "budget",
            "estimate_min_samples",
            "5",
            "PROMPTCADENCE_BUDGET__ESTIMATE_MIN_SAMPLES",
            "6",
            7,
        ),
        (
            "compaction",
            "protected_recent_turns",
            "1",
            "PROMPTCADENCE_COMPACTION__PROTECTED_RECENT_TURNS",
            "2",
            3,
        ),
        (
            "policy",
            "default_tier",
            "local_large",
            "PROMPTCADENCE_POLICY__DEFAULT_TIER",
            "local_fast",
            "local_large",
        ),
        ("logging", "level", "DEBUG", "PROMPTCADENCE_LOGGING__LEVEL", "WARNING", "ERROR"),
    ],
)
def test_precedence_field_by_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    file_value: str,
    env_var: str,
    env_value: str,
    cli_value: object,
) -> None:
    """Every layer wins over the one below it, for one field per configuration section."""
    config_file = tmp_path / "config.toml"
    file_literal = f'"{file_value}"' if not file_value.replace(".", "", 1).isdigit() else file_value
    config_file.write_text(f"[{section}]\n{field} = {file_literal}\n")

    loaded = load_settings(config_path=config_file)
    assert str(getattr(getattr(loaded.settings, section), field)) == file_value
    assert loaded.sources[f"{section}.{field}"] == "file"

    monkeypatch.setenv(env_var, env_value)
    loaded = load_settings(config_path=config_file)
    assert str(getattr(getattr(loaded.settings, section), field)) == env_value
    assert loaded.sources[f"{section}.{field}"].startswith("env")

    loaded = load_settings(config_path=config_file, cli_overrides={section: {field: cli_value}})
    assert getattr(getattr(loaded.settings, section), field) == cli_value
    assert loaded.sources[f"{section}.{field}"] == "cli"


def test_per_leaf_override_leaves_siblings_alone(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhost = "127.0.0.1"\nport = 9100\n')
    loaded = load_settings(
        config_path=config_file, cli_overrides={"server": {"allow_lan_exposure": True}}
    )
    assert loaded.settings.server.port == 9100  # from file, not clobbered
    assert loaded.settings.server.allow_lan_exposure is True  # from cli


def test_unknown_key_rejected_with_suggestion(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhosts = "127.0.0.1"\n')  # typo: hosts, not host
    with pytest.raises(ConfigurationError, match="hosts"):
        load_settings(config_path=config_file)


def test_invalid_toml_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("this is not [ valid toml")
    with pytest.raises(ConfigurationError, match="not valid TOML"):
        load_settings(config_path=config_file)


# --- ADR-0026: the config-only half of the binding refusal set -------------------------------


def test_lan_exposure_without_acknowledgement_refused(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhost = "0.0.0.0"\n')
    with pytest.raises(InsecureBindingError, match="allow_lan_exposure"):
        load_settings(config_path=config_file)


def test_lan_exposure_with_acknowledgement_alone_still_refused_without_allowed_hosts(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhost = "0.0.0.0"\nallow_lan_exposure = true\n')
    with pytest.raises(InsecureBindingError, match="allowed_hosts"):
        load_settings(config_path=config_file)


def test_non_loopback_named_host_requires_allowed_hosts(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhost = "promptcadence.example"\n')
    with pytest.raises(InsecureBindingError, match="allowed_hosts"):
        load_settings(config_path=config_file)


def test_non_loopback_host_with_allowed_hosts_passes_the_config_level_check(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[server]\nhost = "promptcadence.example"\nallowed_hosts = ["promptcadence.example"]\n'
    )
    loaded = load_settings(config_path=config_file)  # does not raise
    assert loaded.settings.server.allowed_hosts == ("promptcadence.example",)


# --- ADR-0047 §2/§3: remote tier rules --------------------------------------------------------


def test_remote_tier_without_classification_refused(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[tiers.remote_cheap]\ntask_profile = "tools.agent.remote_cheap"\nremote = true\n'
        'pricing_file = "pricing.toml"\n'
    )
    with pytest.raises(ConfigurationError, match="max_data_classification"):
        load_settings(config_path=config_file)


def test_remote_tier_without_pricing_source_refused(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[tiers.remote_cheap]\ntask_profile = "tools.agent.remote_cheap"\nremote = true\n'
        'max_data_classification = "internal"\n'
    )
    with pytest.raises(ConfigurationError, match="pricing_file"):
        load_settings(config_path=config_file)


def test_remote_tier_with_classification_and_pricing_validates(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[tiers.remote_cheap]\ntask_profile = "tools.agent.remote_cheap"\nremote = true\n'
        'max_data_classification = "internal"\npricing_file = "pricing.toml"\n'
    )
    loaded = load_settings(config_path=config_file)  # does not raise
    assert loaded.settings.tiers["remote_cheap"].remote is True


def test_tier_without_task_profile_refused(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[tiers.mystery]\ncontext_budget_tokens = 4096\n")
    with pytest.raises(ConfigurationError, match="task_profile"):
        load_settings(config_path=config_file)


def test_unknown_classification_value_rejected(tmp_path: Path) -> None:
    """An unknown ``DataClassification`` value is a plain pydantic type failure (spec §12)."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[tiers.remote_cheap]\ntask_profile = "tools.agent.remote_cheap"\nremote = true\n'
        'max_data_classification = "top_secret"\npricing_file = "pricing.toml"\n'
    )
    with pytest.raises(ConfigurationError, match="max_data_classification"):
        load_settings(config_path=config_file)


def test_local_tier_needs_neither_classification_nor_pricing(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[tiers.extra_local]\ntask_profile = "tools.agent.local_fast"\n')
    loaded = load_settings(config_path=config_file)  # does not raise
    assert loaded.settings.tiers["extra_local"].remote is False


# --- spec §12: a project binding neither ceiling is refused ----------------------------------


def test_project_budget_with_neither_ceiling_refused(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[budget.projects.research]\n")
    with pytest.raises(ConfigurationError, match="research"):
        load_settings(config_path=config_file)


def test_project_budget_with_token_ceiling_only_validates(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[budget.projects.research]\ntoken_ceiling = 100000\n")
    loaded = load_settings(config_path=config_file)  # does not raise
    assert loaded.settings.budget.projects["research"].token_ceiling == 100000


def test_project_budget_with_money_ceiling_only_validates(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[budget.projects.research.money_ceiling]\ncurrency = "USD"\nnanos = 50000000000\n'
    )
    loaded = load_settings(config_path=config_file)  # does not raise
    assert loaded.settings.budget.projects["research"].money_ceiling is not None


# --- ADR-0026 §4: exactly one credential source -----------------------------------------------


def test_loadcoach_credential_cannot_name_both_sources(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[loadcoach]\napi_key_env = "PC_TOKEN"\napi_key_file = "/run/secrets/pc_token"\n'
    )
    with pytest.raises(ConfigurationError, match="api_key"):
        load_settings(config_path=config_file)


# --- MoneyAmount --------------------------------------------------------------------------------


def test_money_amount_parses_and_normalizes_currency(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[budget.default_money_ceiling]\ncurrency = "usd"\nnanos = 1000000000\n')
    loaded = load_settings(config_path=config_file)
    assert loaded.settings.budget.default_money_ceiling.currency == "USD"
    assert loaded.settings.budget.default_money_ceiling.nanos == 1_000_000_000


def test_step_retries_defaults_to_one_and_zero_means_no_repeat(tmp_path: Path) -> None:
    """ADR-0076's budget is configuration, not a constant, and its default is pinned here.

    ``0`` is one attempt and no repeat — the ``[planning] corrective_retries`` reading exactly —
    and it must be a legal value, because an operator who wants the old halting behaviour has to
    be able to ask for it without editing the loop.
    """
    assert load_settings(config_path=tmp_path / "absent.toml").settings.execution.step_retries == 1

    target = tmp_path / "no-repeat.toml"
    target.write_text("[execution]\nstep_retries = 0\n", encoding="utf-8")
    assert load_settings(config_path=target).settings.execution.step_retries == 0


def test_the_shipped_example_config_pins_the_same_step_retries(tmp_path: Path) -> None:
    """The file `config init` writes must say what the default is, not drift from it."""
    target = tmp_path / "example.toml"
    target.write_text(EXAMPLE_CONFIG_TOML, encoding="utf-8")
    assert load_settings(config_path=target).settings.execution.step_retries == 1
