"""Per-credential rate limits with ``Retry-After`` at the boundary (spec §14, Security §14).

The two ``[server]`` limits were declared and enforced nowhere until Phase 9 — a configured limit
nothing reads is worse than an absent one, because ``doctor`` and the configuration reference
both report it as set. These tests are what make the setting true.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from tests.fakes.loadcoach_app import FakeLoadCoach, build_fake_app, shipped_profiles

from promptcadence.config import load_settings
from promptcadence.services.runtime import build_runtime
from promptcadence.services.tokens import create_token
from promptcadence.web.app import create_app
from promptcadence.web.rate_limit import TokenBucket, credential_key


def _serve(monkeypatch: pytest.MonkeyPatch, **server: str) -> TestClient:
    for key, value in server.items():
        monkeypatch.setenv(f"PROMPTCADENCE_SERVER__{key.upper()}", value)
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles("tools.agent.local_fast", "tools.agent.local_large"))
    settings = load_settings().settings
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        settings, runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http)
    )
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str, str]]:
    """Three requests at once, then one a second; two read tokens; the brake at two failures."""
    with _serve(
        monkeypatch, rate_limit_per_minute="60", rate_limit_burst="3", failed_auth_per_minute="2"
    ) as running:
        runtime = cast("FastAPI", running.app).state.runtime
        now = datetime.now(UTC)
        a = create_token(runtime.database, name="a", scopes=["read"], now=now).token
        b = create_token(runtime.database, name="b", scopes=["read"], now=now).token
        yield running, a, b


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def test_a_caller_at_the_limit_gets_429_with_retry_after_not_a_dropped_request(
    client: tuple[TestClient, str, str],
) -> None:
    running, a, _ = client
    for _ in range(3):
        assert running.get("/api/v1/health", headers=_auth(a)).status_code == 200
    limited = running.get("/api/v1/health", headers=_auth(a))
    assert limited.status_code == 429
    body = limited.json()["error"]
    assert body["code"] == "RATE_LIMITED"
    assert int(limited.headers["Retry-After"]) >= 1
    assert body["details"]["retry_after_seconds"] == int(limited.headers["Retry-After"])
    assert limited.headers["X-Request-ID"]


def test_the_limit_is_per_token_not_per_connection(client: tuple[TestClient, str, str]) -> None:
    running, a, b = client
    for _ in range(3):
        running.get("/api/v1/health", headers=_auth(a))
    assert running.get("/api/v1/health", headers=_auth(a)).status_code == 429
    # A different token has its own budget; the same token on a "new connection" does not.
    assert running.get("/api/v1/health", headers=_auth(b)).status_code == 200
    with TestClient(running.app, base_url="http://127.0.0.1") as second_connection:
        assert second_connection.get("/api/v1/health", headers=_auth(a)).status_code == 429


def test_version_is_exempt_and_the_console_is_not_limited(
    client: tuple[TestClient, str, str],
) -> None:
    running, _, _ = client
    for _ in range(6):
        assert running.get("/api/v1/version").status_code == 200
    # The console is outside /api/v1; a person paging is not the failure mode. Tokens exist, so
    # the pages answer 401 — what matters here is that it is never 429.
    for _ in range(6):
        assert running.get("/").status_code == 401


def test_failed_authentication_is_braked_per_address(client: tuple[TestClient, str, str]) -> None:
    running, _, b = client
    for _ in range(2):
        assert running.get("/api/v1/health", headers=_auth("wrong")).status_code == 401
    braked = running.get("/api/v1/health", headers=_auth("wrong"))
    assert braked.status_code == 429
    assert "failed authentications" in braked.json()["error"]["message"]
    # The brake is on the address, whatever it presents now — a right token included.
    assert running.get("/api/v1/health", headers=_auth(b)).status_code == 429


def test_the_bucket_arithmetic_and_the_credential_key() -> None:
    bucket = TokenBucket(capacity=2.0, per_second=1.0, tokens=2.0, updated_at=0.0)
    assert bucket.take(0.0) == 0.0 and bucket.take(0.0) == 0.0
    assert bucket.take(0.0) == pytest.approx(1.0)  # one second until one token
    assert bucket.take(1.0) == 0.0  # refilled
    assert bucket.take(100.0) == 0.0 and bucket.tokens == pytest.approx(1.0)  # capped
    key, authenticated = credential_key(Headers({"authorization": "Bearer abc"}), "10.0.0.1")
    assert authenticated and key.startswith("token:") and "abc" not in key
    assert credential_key(Headers({}), "10.0.0.1") == ("address:10.0.0.1", False)
    assert credential_key(Headers({"authorization": "Basic x"}), None) == ("address:unknown", False)


def test_zero_disables_the_request_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    with _serve(monkeypatch, rate_limit_per_minute="0", rate_limit_burst="1") as running:
        for _ in range(20):
            assert running.get("/api/v1/health").status_code == 200
