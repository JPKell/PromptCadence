"""``promptcadence tiers check`` and the ``tiers`` health component (spec §12, §17)."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from tests.fakes.loadcoach_app import FakeLoadCoach, build_fake_app, shipped_profiles

from promptcadence.config import load_settings
from promptcadence.infrastructure.loadcoach import LoadCoachClient
from promptcadence.services.tiers import check_tiers, tiers_health_component


def _client(fake: FakeLoadCoach) -> LoadCoachClient:
    return LoadCoachClient(TestClient(build_fake_app(fake), base_url="http://loadcoach.test"))


def test_every_configured_tier_and_tools_plan_resolve() -> None:
    settings = load_settings().settings
    fake = FakeLoadCoach()
    fake.register_profile(
        *shipped_profiles("tools.agent.local_fast", "tools.agent.local_large", "tools.plan")
    )
    result = check_tiers(settings, _client(fake))
    assert result.ok and result.reachable
    assert [(c.tier, c.task_profile, c.found) for c in result.checks] == [
        ("local_fast", "tools.agent.local_fast", True),
        ("local_large", "tools.agent.local_large", True),
        (None, "tools.plan", True),
    ]
    assert "tools.plan included" in result.detail
    assert tiers_health_component(settings, _client(fake)).status.value == "ok"


def test_a_missing_planner_profile_is_named_and_degrades_health() -> None:
    settings = load_settings().settings
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles("tools.agent.local_fast", "tools.agent.local_large"))
    result = check_tiers(settings, _client(fake))
    assert not result.ok and result.reachable
    assert "tools.plan" in result.detail
    planner = result.checks[-1]
    assert planner.tier is None and planner.found is False
    component = tiers_health_component(settings, _client(fake))
    assert component.status.value == "degraded"
    assert component.data is not None and component.data["ok"] is False


def test_an_unreachable_loadcoach_is_reported_never_raised() -> None:
    settings = load_settings().settings
    client = LoadCoachClient(httpx.Client(base_url="http://127.0.0.1:9", timeout=0.2))
    result = check_tiers(settings, client)
    assert result.reachable is False and result.ok is False
    assert all(check.found is False for check in result.checks)
    assert tiers_health_component(settings, client).status.value == "degraded"
