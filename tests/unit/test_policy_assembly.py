"""Tests for promptcadence.services.policy_assembly: configuration becoming domain values."""

from __future__ import annotations

from pathlib import Path

import pytest
from baseaicore import ConfigurationError, DataClassification, Money

from promptcadence.config import load_settings
from promptcadence.domain.policy import ApprovalMode, ReapprovalScope
from promptcadence.domain.tiers import EgressClass
from promptcadence.services.policy_assembly import (
    approval_policy_from_settings,
    money_from_amount,
    tier_from_config,
    tier_policy_from_settings,
    tier_snapshot_from_settings,
)

_CONFIG = """
[policy]
default_tier = "local_fast"
escalation_order = ["local_fast", "local_large", "remote_cheap"]

[tiers.local_fast]
task_profile = "tools.agent.local_fast"
context_budget_tokens = 16384

[tiers.local_large]
task_profile = "tools.agent.local_large"
context_budget_tokens = 32768

[tiers.remote_cheap]
task_profile = "tools.agent.remote_cheap"
remote = true
max_data_classification = "internal"
context_budget_tokens = 64000
pricing_file = "pricing/remote_cheap.json"

[approval]
mode = "hybrid"
gate_egress_at = "public"
request_timeout_hours = 6.0

[approval.gate_step_cost]
currency = "USD"
nanos = 2000000000

[planning]
reapproval_scope = "any_deviation"
"""


@pytest.fixture
def settings_path(tmp_path: Path) -> Path:
    """A configuration file exercising both tier kinds and every approval knob."""
    path = tmp_path / "config.toml"
    path.write_text(_CONFIG, encoding="utf-8")
    return path


def test_the_zero_configuration_default_assembles_into_two_local_tiers(tmp_path: Path) -> None:
    settings = load_settings(config_path=tmp_path / "absent.toml").settings
    snapshot = tier_snapshot_from_settings(settings)
    assert [tier.name for tier in snapshot.tiers] == ["local_fast", "local_large"]
    assert all(tier.egress_class is EgressClass.LOCAL for tier in snapshot.tiers)
    assert snapshot.default_tier == "local_fast"


def test_a_configured_remote_tier_carries_its_ceiling_and_pricing(settings_path: Path) -> None:
    settings = load_settings(config_path=settings_path).settings
    snapshot = tier_snapshot_from_settings(settings)
    remote = snapshot.require("remote_cheap")
    assert remote.egress_class is EgressClass.REMOTE
    assert remote.max_data_classification is DataClassification.INTERNAL
    assert remote.pricing_source == "pricing/remote_cheap.json"
    assert remote.context_budget_tokens == 64_000


def test_the_snapshot_is_ordered_by_name_so_its_content_address_is_stable(
    settings_path: Path,
) -> None:
    settings = load_settings(config_path=settings_path).settings
    first = tier_snapshot_from_settings(settings)
    second = tier_snapshot_from_settings(settings)
    assert [tier.name for tier in first.tiers] == sorted(tier.name for tier in first.tiers)
    assert first.snapshot_id == second.snapshot_id


def test_the_tier_policy_takes_loadcoachs_remote_provider_as_a_parameter(
    settings_path: Path,
) -> None:
    """It is a runtime fact from ``/system/status``, not a configuration value (lifecycle §3)."""
    settings = load_settings(config_path=settings_path).settings
    assert tier_policy_from_settings(settings).loadcoach_has_remote_provider is False
    assert (
        tier_policy_from_settings(
            settings, loadcoach_has_remote_provider=True
        ).loadcoach_has_remote_provider
        is True
    )


def test_the_approval_policy_takes_every_knob_including_the_planning_scope(
    settings_path: Path,
) -> None:
    settings = load_settings(config_path=settings_path).settings
    policy = approval_policy_from_settings(settings)
    assert policy.mode is ApprovalMode.HYBRID
    assert policy.gate_egress_at is DataClassification.PUBLIC
    assert policy.gate_step_cost == Money(currency="USD", nanos=2_000_000_000)
    assert policy.request_timeout_hours == 6.0
    assert policy.reapproval_scope is ReapprovalScope.ANY_DEVIATION


def test_changing_a_gate_in_configuration_changes_the_approval_policy_version(
    tmp_path: Path, settings_path: Path
) -> None:
    """The whole point of a derived version: an edited gate reinterprets no stored record."""
    baseline = approval_policy_from_settings(
        load_settings(config_path=settings_path).settings
    ).version
    edited = tmp_path / "edited.toml"
    edited.write_text(_CONFIG.replace('gate_egress_at = "public"', 'gate_egress_at = "internal"'))
    changed = approval_policy_from_settings(load_settings(config_path=edited).settings).version
    assert changed != baseline


def test_money_conversion_round_trips_the_configured_amount(settings_path: Path) -> None:
    settings = load_settings(config_path=settings_path).settings
    assert money_from_amount(settings.budget.default_money_ceiling).currency == "USD"


def test_a_remote_tier_missing_its_ceiling_is_refused_before_the_domain_ever_sees_it(
    tmp_path: Path,
) -> None:
    """``config.load_settings`` refuses first; ``tier_from_config`` is the same wall later."""
    broken = tmp_path / "broken.toml"
    broken.write_text(
        '[tiers.remote_bad]\ntask_profile = "x"\nremote = true\npricing_file = "p.json"\n'
    )
    with pytest.raises(ConfigurationError, match="max_data_classification"):
        load_settings(config_path=broken)


def test_tier_from_config_maps_the_remote_flag_onto_the_egress_class(
    settings_path: Path,
) -> None:
    settings = load_settings(config_path=settings_path).settings
    assert tier_from_config("local_fast", settings.tiers["local_fast"]).is_remote is False
    assert tier_from_config("remote_cheap", settings.tiers["remote_cheap"]).is_remote is True
