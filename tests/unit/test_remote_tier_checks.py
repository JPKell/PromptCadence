"""A remote tier is unavailable for exactly one recorded reason (ADR-0098 rule 3, spec §17)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mirrorwall import ComponentStatus
from tests.fakes.harness import PRICING_DOCUMENT, remote_tier_env
from tests.fakes.loadcoach_app import FakeLoadCoach, FakeModel, build_fake_app, shipped_profiles

from promptcadence.config import Settings, load_settings
from promptcadence.infrastructure.loadcoach import LoadCoachClient
from promptcadence.services.console import tiers_report
from promptcadence.services.pricing import PricingCatalog
from promptcadence.services.tiers import check_tiers, tiers_health_component

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
_REMOTE = FakeModel(
    canonical_id="openai_compatible/qwen3:8b@sha256:" + "e" * 64,
    provider_kind="openai_compatible",
    provider_name="openrouter",
    is_remote=True,
)


def _client(fake: FakeLoadCoach) -> LoadCoachClient:
    fake.register_profile(
        *shipped_profiles(
            "tools.agent.local_fast",
            "tools.agent.local_large",
            "tools.agent.remote_cheap",
            "tools.plan",
        )
    )
    return LoadCoachClient(TestClient(build_fake_app(fake), base_url="http://loadcoach.test"))


@pytest.fixture
def remote_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    pricing = tmp_path / "pricing.json"
    pricing.write_text(PRICING_DOCUMENT)
    for key, value in remote_tier_env(str(pricing)).items():
        monkeypatch.setenv(key, value)
    return load_settings().settings


def test_no_remote_registration_is_the_first_reason_and_degrades_the_component(
    remote_settings: Settings,
) -> None:
    client = _client(FakeLoadCoach())
    check = check_tiers(remote_settings, client, pricing=PricingCatalog(by_tier={}), now=_NOW)
    (remote,) = check.remote
    assert (remote.tier, remote.available, remote.reason) == (
        "remote_cheap",
        False,
        "loadcoach_has_no_remote_provider",
    )
    assert check.ok is False and "remote_cheap (loadcoach_has_no_remote_provider)" in check.detail
    component = tiers_health_component(remote_settings, client, pricing=None, now=_NOW)
    assert component.status is ComponentStatus.DEGRADED, "degraded with a reason, never down"
    assert component.data["remote"][0]["reason"] == "loadcoach_has_no_remote_provider"


def test_an_unpriced_tier_is_the_second_reason(remote_settings: Settings) -> None:
    client = _client(FakeLoadCoach(remote_model=_REMOTE))
    unpriced = PricingCatalog(by_tier={"remote_cheap": ()})
    check = check_tiers(remote_settings, client, pricing=unpriced, now=_NOW)
    assert check.remote[0].reason == "unpriced" and check.ok is False
    report = tiers_report(
        remote_settings, loadcoach_has_remote_provider=True, unpriced=frozenset({"remote_cheap"})
    )
    row = next(one for one in report["rows"] if one["name"] == "remote_cheap")
    assert (row["available"], row["unavailable_reason"]) == (False, "unpriced")


def test_a_registered_and_priced_remote_tier_is_available(remote_settings: Settings) -> None:
    client = _client(FakeLoadCoach(remote_model=_REMOTE))
    priced = PricingCatalog.from_settings(remote_settings)
    check = check_tiers(remote_settings, client, pricing=priced, now=_NOW)
    assert check.remote[0].available is True and check.remote[0].reason is None
    assert check.ok is True
    assert tiers_health_component(remote_settings, client, pricing=priced, now=_NOW).status is (
        ComponentStatus.OK
    )
    report = tiers_report(remote_settings, loadcoach_has_remote_provider=True)
    row = next(one for one in report["rows"] if one["name"] == "remote_cheap")
    assert row["available"] is True and row["unavailable_reason"] is None


def test_a_local_only_install_reports_no_remote_tiers() -> None:
    settings = load_settings().settings
    check = check_tiers(settings, _client(FakeLoadCoach()), pricing=None, now=_NOW)
    assert check.remote == () and check.ok is True
