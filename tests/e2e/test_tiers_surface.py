"""``GET /tiers`` (spec §7.1, absent until I2) and the ``tiers`` health component say honestly
what the install can and cannot reach (ADR-0098 rule 3)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.fakes.harness import PRICING_DOCUMENT, remote_tier_env
from tests.fakes.loadcoach_app import FakeLoadCoach, FakeModel, build_fake_app, shipped_profiles

from promptcadence.config import load_settings
from promptcadence.services.runtime import build_runtime
from promptcadence.web.app import create_app

_REMOTE = FakeModel(
    canonical_id="openai_compatible/qwen3:8b@sha256:" + "e" * 64,
    provider_kind="openai_compatible",
    provider_name="openrouter",
    is_remote=True,
)


def _serve(fake: FakeLoadCoach) -> TestClient:
    fake.register_profile(
        *shipped_profiles(
            "tools.agent.local_fast",
            "tools.agent.local_large",
            "tools.agent.remote_cheap",
            "tools.plan",
        )
    )
    settings = load_settings().settings
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        settings, runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http)
    )
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def priced_remote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    pricing = tmp_path / "pricing.json"
    pricing.write_text(PRICING_DOCUMENT)
    for key, value in remote_tier_env(str(pricing)).items():
        monkeypatch.setenv(key, value)
    yield


def test_get_tiers_names_why_a_remote_tier_cannot_serve(priced_remote: None) -> None:
    with _serve(FakeLoadCoach()) as client:
        body = client.get("/api/v1/tiers").json()
        rows = {row["name"]: row for row in body["rows"]}
        assert body["remote_provider"] is False
        assert rows["local_fast"]["available"] is True
        assert rows["remote_cheap"]["available"] is False
        assert rows["remote_cheap"]["unavailable_reason"] == "loadcoach_has_no_remote_provider"
        health = client.get("/api/v1/health").json()
        tiers = next(c for c in health["components"] if c["name"] == "tiers")
        assert tiers["status"] == "degraded"
        assert "remote_cheap (loadcoach_has_no_remote_provider)" in tiers["detail"]


def test_get_tiers_reports_a_registered_priced_remote_tier_available(priced_remote: None) -> None:
    with _serve(FakeLoadCoach(remote_model=_REMOTE)) as client:
        body = client.get("/api/v1/tiers").json()
        rows = {row["name"]: row for row in body["rows"]}
        assert body["remote_provider"] is True
        assert rows["remote_cheap"]["available"] is True
        assert rows["remote_cheap"]["unavailable_reason"] is None
        tiers = next(
            c for c in client.get("/api/v1/health").json()["components"] if c["name"] == "tiers"
        )
        assert tiers["status"] == "ok"
        page = client.get("/tiers")
        assert page.status_code == 200 and "Remote provider registered" in page.text


def test_get_tiers_is_in_the_generated_openapi_and_needs_read() -> None:
    with _serve(FakeLoadCoach()) as client:
        assert "/api/v1/tiers" in client.get("/api/v1/openapi.json").json()["paths"]
        assert client.get("/api/v1/tiers").status_code == 200
