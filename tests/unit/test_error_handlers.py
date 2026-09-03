"""Tests for promptcadence.web.app's exception handlers: the standard error envelope.

No Phase 1 route can yet raise a :class:`~baseaicore.SuiteError` with application-specific details,
a body-validation failure, or an unhandled exception — those arrive with the routes that accept
input, in later phases. The envelope machinery itself is Phase 1's own deliverable ("MirrorWall
base: envelopes"), so these tests mount a throwaway route on the built app to prove each handler,
the same technique the app's own OpenAPI/docs routes use to stay decoupled from any one router.
"""

from __future__ import annotations

from baseaicore import ConfigurationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

from promptcadence.config import load_settings
from promptcadence.web.app import create_app


class _Body(BaseModel):
    name: str


def _client_with_test_routes() -> TestClient:
    settings = load_settings().settings
    app = create_app(settings)

    @app.get("/api/v1/_test/suite-error")
    def _raise_suite_error() -> None:
        raise ConfigurationError("deliberate test failure", details={"field": "x"})

    @app.get("/api/v1/_test/crash")
    def _raise_unhandled() -> None:
        raise RuntimeError("deliberate crash")

    @app.post("/api/v1/_test/body")
    def _accept_body(body: _Body) -> dict[str, str]:
        return {"name": body.name}

    return TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)


def test_suite_error_becomes_the_standard_envelope() -> None:
    with _client_with_test_routes() as client:
        response = client.get("/api/v1/_test/suite-error")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "CONFIGURATION_ERROR"
    assert body["error"]["details"] == {"field": "x"}
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


def test_unhandled_exception_becomes_internal_error_never_a_raw_traceback() -> None:
    with _client_with_test_routes() as client:
        response = client.get("/api/v1/_test/crash")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "RuntimeError" not in body["error"]["message"]


def test_body_validation_failure_names_the_field() -> None:
    with _client_with_test_routes() as client:
        response = client.post("/api/v1/_test/body", json={})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["fields"][0]["path"] == "name"


def test_body_validation_success_passes_through() -> None:
    with _client_with_test_routes() as client:
        response = client.post("/api/v1/_test/body", json={"name": "ok"})
    assert response.status_code == 200
    assert response.json() == {"name": "ok"}
