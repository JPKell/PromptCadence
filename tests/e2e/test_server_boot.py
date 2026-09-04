"""End-to-end: the server boots with zero configuration and serves a real request.

No LoadCoach is ever started in this suite, so it passes with no GPU, no Ollama and no network —
development plan Phase 1's own acceptance criterion 1: health degraded, never dead.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from promptcadence.bootstrap import bootstrap
from promptcadence.config import InsecureBindingError


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The bootstrapped application, with LoadCoach's address the one thing configured.

    It points at a closed port so "unreachable" stays unreachable when a LoadCoach happens to
    be listening on the default one; nothing else is set.
    """
    monkeypatch.setenv("PROMPTCADENCE_LOADCOACH__BASE_URL", "http://127.0.0.1:9")
    application = bootstrap()
    with TestClient(application.app, base_url="http://localhost") as test_client:
        yield test_client


def test_server_boots_with_zero_configuration_and_serves_degraded_health(
    client: TestClient,
) -> None:
    """Development plan Phase 1 acceptance criterion 1: zero configuration, degraded not dead."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    names = {component["name"] for component in body["components"]}
    assert names == {"database", "loadcoach", "tools"}


def test_database_migrates_on_first_boot(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    database_component = next(c for c in response.json()["components"] if c["name"] == "database")
    assert database_component["status"] == "ok"
    assert "at head" in database_component["detail"]


def test_loadcoach_component_degraded_never_unavailable(client: TestClient) -> None:
    """ADR-0045 rule 3: LoadCoach is required for execution, never for startup."""
    response = client.get("/api/v1/health")
    loadcoach_component = next(c for c in response.json()["components"] if c["name"] == "loadcoach")
    assert loadcoach_component["status"] == "degraded"
    assert loadcoach_component["status"] != "unavailable"


def test_health_response_has_request_id_header(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert "X-Request-ID" in response.headers


def test_version_endpoint_unauthenticated(client: TestClient) -> None:
    """ADR-0026 §5: version negotiation works before any credential is established."""
    response = client.get("/api/v1/version")
    assert response.status_code == 200
    body = response.json()
    assert body["application"] == "promptcadence"
    assert body["api_version"] == "v1"


def test_system_status_endpoint(client: TestClient) -> None:
    response = client.get("/api/v1/system/status")
    assert response.status_code == 200
    body = response.json()
    assert body["active_trajectories"] == []
    assert body["max_concurrent_trajectories"] == 1


def test_wrong_host_header_rejected_with_421(client: TestClient) -> None:
    """ADR-0026 §1: DNS-rebinding defence — an unrecognized Host header is refused."""
    response = client.get("/api/v1/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 421
    body = response.json()
    assert body["error"]["code"] == "MISDIRECTED_REQUEST"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


def test_loopback_host_variants_all_accepted() -> None:
    application = bootstrap()
    with TestClient(application.app, base_url="http://localhost") as test_client:
        for host in ("localhost", "127.0.0.1"):
            response = test_client.get("/api/v1/health", headers={"Host": host})
            assert response.status_code == 200, host


def test_request_id_is_echoed_back(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "my-custom-id-123"})
    assert response.headers["X-Request-ID"] == "my-custom-id-123"


def test_unrecognized_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "has spaces! invalid"})
    assert response.headers["X-Request-ID"] != "has spaces! invalid"


def test_unknown_route_returns_standard_error_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/no-such-route")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# --- ADR-0026 / ADR-0049: the database-backed refusal half, only reachable through bootstrap() --


def test_non_loopback_bind_without_an_active_token_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPTCADENCE_SERVER__HOST", "promptcadence.example")
    monkeypatch.setenv("PROMPTCADENCE_SERVER__ALLOWED_HOSTS", "promptcadence.example")
    with pytest.raises(InsecureBindingError, match="active API token"):
        bootstrap()


def test_manual_approval_without_an_approve_scoped_token_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPTCADENCE_APPROVAL__MODE", "manual")
    with pytest.raises(InsecureBindingError, match="approve-scoped"):
        bootstrap()
