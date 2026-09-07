"""Security Standards §14, item by item, for PromptCadence (spec §14, development plan Phase 9).

Every checklist item ends as a named test. The ones this file holds directly are the HTTP-edge
controls Phase 9 wired — the body cap, same-origin, the ordering of Host validation before
authentication, a principal on every route — and the no-secret-in-logs sweep. The ones already
held elsewhere are named in :data:`CHECKLIST_MAP`, and the last test asserts every entry still
resolves to a callable, so an item cannot quietly lose its test in a rename.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    ScriptedGeneration,
    build_fake_app,
    shipped_profiles,
)

from promptcadence.config import Settings, load_settings
from promptcadence.infrastructure.db.models import ApiToken
from promptcadence.services.runtime import build_runtime
from promptcadence.services.tokens import create_token, revoke_token
from promptcadence.web import auth as auth_module
from promptcadence.web.app import create_app

# A fixture credential the sweep looks for. Built by repetition so a secret scanner sees the
# low-entropy placeholder it is, not a leaked key.
LOADCOACH_KEY = "lc-" + "fixture" * 4
_TERMINAL = {"completed", "halted", "failed", "cancelled", "rejected"}


def _fake() -> FakeLoadCoach:
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles("tools.agent.local_fast", "tools.agent.local_large"))
    fake.set_default(ScriptedGeneration(text="a plain answer"))
    return fake


def _serve(
    settings: Settings,
    fake: FakeLoadCoach | None = None,
    *,
    loadcoach_headers: dict[str, str] | None = None,
) -> TestClient:
    loadcoach_http = TestClient(
        build_fake_app(fake or _fake()), base_url="http://loadcoach.fake", headers=loadcoach_headers
    )
    app = create_app(
        settings, runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http)
    )
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with _serve(load_settings().settings) as running:
        yield running


@pytest.fixture
def tokened(client: TestClient) -> tuple[TestClient, str, str]:
    """A loopback server with a read token, a write token and a revoked admin token."""
    runtime = cast("FastAPI", client.app).state.runtime
    now = datetime.now(UTC)
    reader = create_token(runtime.database, name="reader", scopes=["read"], now=now).token
    writer = create_token(runtime.database, name="writer", scopes=["write"], now=now).token
    create_token(runtime.database, name="gone", scopes=["admin"], now=now)
    revoke_token(runtime.database, name="gone", now=now)
    return client, reader, writer


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


# §14: oversize body rejected before buffering


def test_an_oversize_body_is_413_before_it_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPTCADENCE_SERVER__MAX_BODY_BYTES", "1024")
    with _serve(load_settings().settings) as client:
        declared = client.post(
            "/api/v1/trajectories",
            content=b"x" * 4096,
            headers={"Content-Type": "application/json", "Content-Length": "4096"},
        )
        assert declared.status_code == 413
        assert declared.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"

        def chunks() -> Iterator[bytes]:
            for _ in range(8):
                yield b"y" * 512

        streamed = client.post(
            "/api/v1/trajectories",
            content=chunks(),
            headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
        )
        assert streamed.status_code == 413
        small = client.post("/api/v1/trajectories", json={"task": "fits", "bypass_planning": True})
        assert small.status_code == 202  # parsed and queued


# §14: a forged form post is CSRF_FAILED (held by the console suite); a cross-origin JSON post
# is rejected


def test_a_cross_origin_json_post_is_refused_and_a_same_origin_one_is_not(
    client: TestClient,
) -> None:
    body = {"task": "hello", "bypass_planning": True}
    foreign = client.post(
        "/api/v1/trajectories", json=body, headers={"Origin": "http://evil.example"}
    )
    assert foreign.status_code == 403
    assert foreign.json()["error"]["code"] == "CSRF_FAILED"
    null_origin = client.post("/api/v1/trajectories", json=body, headers={"Origin": "null"})
    assert null_origin.status_code == 403
    same = client.post("/api/v1/trajectories", json=body, headers={"Origin": "http://127.0.0.1"})
    assert same.status_code == 202
    scripted = client.post("/api/v1/trajectories", json=body)  # no Origin: a script or the CLI
    assert scripted.status_code == 202
    listed = client.get("/api/v1/trajectories", headers={"Origin": "http://evil.example"})
    assert listed.status_code == 200  # a read is not a write


# §14: an unexpected Host is 421 on both binds, and the refusal precedes authentication


def test_host_validation_precedes_authentication_on_both_binds(
    tokened: tuple[TestClient, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, reader, _ = tokened
    response = client.get("/api/v1/health", headers={"Host": "evil.example", **_auth("nope")})
    assert response.status_code == 421
    assert response.json()["error"]["code"] == "MISDIRECTED_REQUEST"

    monkeypatch.setenv("PROMPTCADENCE_SERVER__HOST", "192.0.2.10")
    monkeypatch.setenv("PROMPTCADENCE_SERVER__ALLOWED_HOSTS", "cadence.test")
    lan_settings = load_settings().settings
    # ``create_app`` is a pure function of the settings; the token-before-bind refusal lives in
    # ``bootstrap``, which is what lets this test build a LAN-bound app over an empty token table.
    with TestClient(_serve(lan_settings).app, base_url="http://cadence.test") as lan:
        rebinding = lan.get("/api/v1/health", headers={"Host": "evil.example", **_auth("nope")})
        assert rebinding.status_code == 421
        # A non-loopback bind is never open: with no token at all the answer is 401, not the
        # loopback principal.
        assert lan.get("/api/v1/health").status_code == 401
        assert lan.get("/api/v1/version").status_code == 200


# §14: authenticated endpoints reject missing, malformed, revoked and wrong-scope tokens; every
# route except /version resolves a principal; /version answers without one while /health does not


def _api_routes(app: FastAPI) -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1"):
            routes.extend((method, route.path) for method in sorted(route.methods or ()))
    return routes


def test_every_api_route_except_version_resolves_a_principal(
    tokened: tuple[TestClient, str, str],
) -> None:
    client, _, _ = tokened
    placeholder = "01PLACEHOLDER000000000000A"
    for method, template in _api_routes(cast("FastAPI", client.app)):
        path = template.replace("{trajectory_id}", placeholder).replace("{name}", "read_file")
        response = client.request(method, path, json={} if method == "POST" else None)
        if path == "/api/v1/version":
            assert response.status_code == 200, path
            continue
        assert response.status_code == 401, (method, path, response.status_code)
        assert response.json()["error"]["code"] == "UNAUTHORIZED", (method, path)


def test_missing_malformed_revoked_and_wrong_scope_tokens_are_refused(
    tokened: tuple[TestClient, str, str],
) -> None:
    client, reader, writer = tokened
    runtime = cast("FastAPI", client.app).state.runtime
    with runtime.database.read() as session:
        gone = session.execute(select(ApiToken).where(ApiToken.name == "gone")).scalar_one()
        assert gone.revoked_at is not None
    for headers in (
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic cmVhZDpyZWFk"},
        {"Authorization": "Bearer not-a-token"},
    ):
        refused = client.get("/api/v1/health", headers=headers)
        assert refused.status_code == 401, headers
    # The right scope passes; the wrong one is 403 with both scopes named — submit ≠ approve.
    assert client.get("/api/v1/health", headers=_auth(reader)).status_code == 200
    forbidden = client.post(
        "/api/v1/trajectories", json={"task": "t", "bypass_planning": True}, headers=_auth(reader)
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["details"] == {"required": "write", "held": ["read"]}
    assert client.get("/api/v1/trajectories", headers=_auth(writer)).status_code == 403
    accepted = client.post(
        "/api/v1/trajectories", json={"task": "t", "bypass_planning": True}, headers=_auth(writer)
    )
    assert accepted.status_code == 202
    trajectory_id = accepted.json()["trajectory_id"]
    cannot_approve = client.post(
        f"/api/v1/trajectories/{trajectory_id}/approve", headers=_auth(writer)
    )
    assert cannot_approve.status_code == 403


# §14: the stored value is a hash and the comparison is constant-time


def test_the_row_holds_a_hash_and_the_comparison_is_constant_time(
    tokened: tuple[TestClient, str, str],
) -> None:
    client, reader, _ = tokened
    runtime = cast("FastAPI", client.app).state.runtime
    with runtime.database.read() as session:
        row = session.execute(select(ApiToken).where(ApiToken.name == "reader")).scalar_one()
    assert row.token_sha256 == "sha256:" + hashlib.sha256(reader.encode()).hexdigest()
    assert reader not in json.dumps({k: str(v) for k, v in vars(row).items()})
    source = inspect.getsource(auth_module.resolve_principal)
    assert "hmac.compare_digest" in source and "==" not in source.split("compare_digest")[1][:200]


# §14: log output contains no secret for a request that carried one — the presented bearer, and
# the LoadCoach API key this application itself presents on every call it makes


def test_logs_events_and_the_explanation_never_carry_a_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LC_KEY", LOADCOACH_KEY)
    monkeypatch.setenv("PROMPTCADENCE_LOADCOACH__API_KEY_ENV", "LC_KEY")
    monkeypatch.setenv("PROMPTCADENCE_LOGGING__LEVEL", "DEBUG")
    settings = load_settings().settings
    caplog.set_level(logging.DEBUG)
    # The fake is reached through an injected client, so the header the real client would build
    # from ``api_key_env`` is placed on it here: the key rides every request, as in production.
    with _serve(settings, loadcoach_headers=_auth(LOADCOACH_KEY)) as client:
        runtime = cast("FastAPI", client.app).state.runtime
        bearer = create_token(
            runtime.database, name="ops", scopes=["admin"], now=datetime.now(UTC)
        ).token
        client.get("/api/v1/health", headers=_auth("wrong-secret-value"))
        created = client.post(
            "/api/v1/trajectories",
            json={"task": "answer plainly", "bypass_planning": True},
            headers=_auth(bearer),
        )
        trajectory_id = created.json()["trajectory_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            view = client.get(f"/api/v1/trajectories/{trajectory_id}", headers=_auth(bearer))
            if view.json()["state"] in _TERMINAL:
                break
            time.sleep(0.02)
        explanation = client.get(
            f"/api/v1/trajectories/{trajectory_id}/explanation", headers=_auth(bearer)
        ).text
        status_body = client.get("/api/v1/system/status", headers=_auth(bearer)).text
        health_body = client.get("/api/v1/health", headers=_auth(bearer)).text
        events = json.dumps([e.as_json() for e in runtime.trajectories.events(trajectory_id)])
    logs = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for secret in (LOADCOACH_KEY, bearer, "wrong-secret-value"):
        assert secret not in logs, secret
        assert secret not in explanation and secret not in events
        assert secret not in status_body and secret not in health_body


# §14: no HTTP endpoint accepts a filesystem path or an upload; tool paths are ToolYard's


def test_no_http_endpoint_accepts_a_filesystem_path_or_an_upload() -> None:
    app = create_app(load_settings().settings)
    suspicious = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for parameter in route.dependant.query_params + route.dependant.path_params:
            if any(word in parameter.name for word in ("path", "file", "dir", "upload")):
                suspicious.append((route.path, parameter.name))
        for body_field in route.dependant.body_params:
            annotation = getattr(body_field, "type_", None) or body_field.field_info.annotation
            fields = getattr(annotation, "model_fields", {}) or {}
            for name in fields:
                if any(word in name for word in ("path", "file", "dir", "upload")):
                    suspicious.append((route.path, name))
    assert suspicious == []


# The items held elsewhere, by name, so a rename cannot orphan one.

CHECKLIST_MAP: dict[str, tuple[str, str]] = {
    "path traversal rejected (workspace escape)": (
        "tests.integration.test_tool_execution",
        "test_a_path_escape_is_refused_and_nothing_outside_the_workspace_is_read",
    ),
    "symlink escape rejected after resolution": (
        "tests.integration.test_tool_execution",
        "test_a_symlink_out_of_the_workspace_is_refused_after_resolution",
    ),
    "unlisted tool never reaches a handler": (
        "tests.integration.test_tool_execution",
        "test_an_unlisted_tool_is_refused_as_unknown_and_never_reaches_a_handler",
    ),
    "sandbox unavailable => run_command refuses, never executes": (
        "tests.integration.test_tool_execution",
        "test_a_command_is_refused_where_the_host_has_no_isolation_rung",
    ),
    "fetch outside the allowlist refused before any bytes": (
        "tests.integration.test_egress",
        "test_a_fetch_to_a_non_allowlisted_host_is_refused_and_recorded",
    ),
    "non-loopback bind without a token refuses to start": (
        "tests.e2e.test_server_boot",
        "test_non_loopback_bind_without_an_active_token_refused",
    ),
    "non-loopback bind without allowed_hosts refuses to start": (
        "tests.unit.test_config",
        "test_non_loopback_named_host_requires_allowed_hosts",
    ),
    "0.0.0.0 without the exposure acknowledgement refuses to start": (
        "tests.unit.test_config",
        "test_lan_exposure_without_acknowledgement_refused",
    ),
    "wrong Host header is 421 on a loopback bind": (
        "tests.e2e.test_server_boot",
        "test_wrong_host_header_rejected_with_421",
    ),
    "GET /version answers without a credential": (
        "tests.e2e.test_server_boot",
        "test_version_endpoint_unauthenticated",
    ),
    "token stored as a hash, shown once": (
        "tests.unit.test_tokens_and_auth",
        "test_a_token_is_issued_once_stored_as_a_hash_and_listed_without_its_secret",
    ),
    "once a token exists a bearer is required and checked": (
        "tests.unit.test_tokens_and_auth",
        "test_once_a_token_exists_a_bearer_is_required_and_checked",
    ),
    "submit != approve, end to end": (
        "tests.e2e.test_approval_surfaces",
        "test_the_approve_scope_is_enforced_once_a_token_exists",
    ),
    "loopback with no tokens is open and records approver:loopback": (
        "tests.e2e.test_console",
        "test_the_inbox_grants_and_the_record_says_who",
    ),
    "forged form post is CSRF_FAILED": (
        "tests.e2e.test_console",
        "test_a_post_without_the_token_is_refused",
    ),
    "mismatched CSRF token is refused": (
        "tests.e2e.test_console",
        "test_a_post_with_a_mismatched_token_is_refused",
    ),
    "the console needs a token once one exists": (
        "tests.e2e.test_console",
        "test_the_console_needs_a_token_once_one_exists",
    ),
    "hostile model output stored and rendered without effect": (
        "tests.e2e.test_console",
        "test_model_output_is_escaped_and_never_rendered_as_markup",
    ),
    "untrusted content escaped on every page": (
        "tests.accessibility.test_ui_checklist",
        "test_untrusted_content_is_escaped_on_every_page",
    ),
    "no template marks a record value safe": (
        "tests.accessibility.test_ui_checklist",
        "test_no_template_marks_a_record_value_safe",
    ),
}


def test_every_remaining_checklist_item_is_held_by_a_named_test() -> None:
    for item, (module_name, function_name) in CHECKLIST_MAP.items():
        module = importlib.import_module(module_name)
        assert callable(getattr(module, function_name, None)), (item, module_name, function_name)
    assert json.dumps(sorted(CHECKLIST_MAP))  # the map is data, serializable for the report
