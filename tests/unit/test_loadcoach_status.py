"""Tests for promptcadence.services.loadcoach_status: the one LoadCoach call Phase 1 makes.

Every outcome except a genuine ``status: ok`` response must resolve to ``DEGRADED`` — never
``UNAVAILABLE`` — per ADR-0045 rule 3 (LoadCoach is required for execution, never for startup).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from mirrorwall import ComponentStatus

from promptcadence.services.loadcoach_status import loadcoach_health_component


@respx.mock
def test_ok_status_reported_as_ok() -> None:
    respx.get("http://127.0.0.1:8766/api/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    component = loadcoach_health_component(base_url="http://127.0.0.1:8766")
    assert component.status is ComponentStatus.OK
    assert component.data["reported_status"] == "ok"


@respx.mock
def test_degraded_upstream_status_reported_as_degraded() -> None:
    respx.get("http://127.0.0.1:8766/api/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "degraded"})
    )
    component = loadcoach_health_component(base_url="http://127.0.0.1:8766")
    assert component.status is ComponentStatus.DEGRADED


@respx.mock
def test_upstream_unavailable_status_capped_at_degraded() -> None:
    """A downstream outage is never *our* outage (ADR-0045 rule 3)."""
    respx.get("http://127.0.0.1:8766/api/v1/health").mock(
        return_value=httpx.Response(503, json={"status": "unavailable"})
    )
    component = loadcoach_health_component(base_url="http://127.0.0.1:8766")
    assert component.status is ComponentStatus.DEGRADED


@respx.mock
def test_non_json_body_reported_as_degraded() -> None:
    respx.get("http://127.0.0.1:8766/api/v1/health").mock(
        return_value=httpx.Response(200, text="not json")
    )
    component = loadcoach_health_component(base_url="http://127.0.0.1:8766")
    assert component.status is ComponentStatus.DEGRADED
    assert "non-JSON" in component.detail


@respx.mock
def test_connection_error_reported_as_degraded_never_unavailable() -> None:
    respx.get("http://127.0.0.1:8766/api/v1/health").mock(side_effect=httpx.ConnectError("refused"))
    component = loadcoach_health_component(base_url="http://127.0.0.1:8766")
    assert component.status is ComponentStatus.DEGRADED
    assert "unreachable" in component.detail


@respx.mock
def test_bearer_token_attached_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PC_TOKEN", "s3cr3t")
    route = respx.get("http://127.0.0.1:8766/api/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    loadcoach_health_component(base_url="http://127.0.0.1:8766", api_key_env="PC_TOKEN")
    assert route.calls.last.request.headers["authorization"] == "Bearer s3cr3t"


@respx.mock
def test_bearer_token_attached_from_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-secret\n")
    route = respx.get("http://127.0.0.1:8766/api/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    loadcoach_health_component(base_url="http://127.0.0.1:8766", api_key_file=str(token_file))
    assert route.calls.last.request.headers["authorization"] == "Bearer file-secret"


def test_no_credential_configured_sends_no_authorization_header() -> None:
    with respx.mock:
        route = respx.get("http://127.0.0.1:8766/api/v1/health").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        loadcoach_health_component(base_url="http://127.0.0.1:8766")
        assert "authorization" not in route.calls.last.request.headers


@respx.mock
def test_a_refused_token_is_named_as_such_and_an_accepted_one_is_recorded() -> None:
    """Row W10 (W6 §8 item 4): the token check `promptcadence doctor` shows."""
    respx.get("http://127.0.0.1:8766/api/v1/health").mock(
        return_value=httpx.Response(
            401, json={"error": {"code": "UNAUTHORIZED", "message": "no usable bearer token"}}
        )
    )
    refused = loadcoach_health_component(base_url="http://127.0.0.1:8766")
    assert refused.status is ComponentStatus.DEGRADED
    assert refused.data["token_accepted"] is False
    assert "refuses the configured token (401)" in refused.detail
    assert "loadcoach token create" in refused.detail

    respx.get("http://127.0.0.1:8766/api/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    accepted = loadcoach_health_component(base_url="http://127.0.0.1:8766")
    assert accepted.data["token_accepted"] is True


@respx.mock
def test_an_unreachable_loadcoach_decides_nothing_about_the_token() -> None:
    respx.get("http://127.0.0.1:8766/api/v1/health").mock(side_effect=httpx.ConnectError("down"))
    component = loadcoach_health_component(base_url="http://127.0.0.1:8766")
    assert "token_accepted" not in component.data
