"""``GET``/``PUT /settings`` over HTTP (spec §7.1), and the Settings page (ADR-0094).

What a reader should take from this file: the refusals name what they refused. A
security-relevant key is ``403 FORBIDDEN`` **naming the key**, an unknown one is
``400 VALIDATION_ERROR`` naming it and listing what may change, and a valid key from a principal
without ``admin`` is ``403`` — three different answers, none of them a silent ignore.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.fakes.loadcoach_app import FakeLoadCoach, build_fake_app, shipped_profiles

from promptcadence.config import load_settings
from promptcadence.services.runtime import build_runtime
from promptcadence.services.tokens import create_token
from promptcadence.web.app import create_app

_PROFILES = ("tools.agent.local_fast", "tools.agent.local_large", "tools.plan")


@pytest.fixture
def client() -> Iterator[TestClient]:
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles(*_PROFILES))
    settings = load_settings().settings
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        settings, runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http)
    )
    with TestClient(app, base_url="http://127.0.0.1") as running:
        yield running


def _document(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/v1/settings")
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json())


def test_the_document_names_the_effective_value_its_definition_and_the_config_only_keys(
    client: TestClient,
) -> None:
    document = _document(client)
    assert document["settings"]["execution.step_retries"] == 1
    definition = document["definitions"]["execution.step_retries"]
    assert definition["type"] == "int"
    assert (definition["minimum"], definition["maximum"]) == (0, 10)
    assert definition["source"] == "configuration" and definition["stored"] is None
    assert "server.host" in document["config_only"]
    assert "execution.step_retries" not in document["config_only"]


def test_a_put_stores_the_value_and_the_answer_shows_it_as_database_sourced(
    client: TestClient,
) -> None:
    response = client.put("/api/v1/settings", json={"execution.step_retries": 4})
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["settings"]["execution.step_retries"] == 4
    assert document["definitions"]["execution.step_retries"]["source"] == "database"
    assert _document(client)["settings"]["execution.step_retries"] == 4, "durable, not per-request"


def test_a_security_relevant_key_is_forbidden_naming_it_and_stores_nothing(
    client: TestClient,
) -> None:
    response = client.put(
        "/api/v1/settings",
        json={"execution.step_retries": 9, "budget.daily_money_ceiling": {"nanos": 1}},
    )
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "FORBIDDEN"
    assert "budget.daily_money_ceiling" in error["message"]
    assert error["details"]["key"] == "budget.daily_money_ceiling"
    assert _document(client)["settings"]["execution.step_retries"] == 1, "refused whole"


@pytest.mark.parametrize(
    "key", ["server.host", "tools.workspace_root", "tiers.local_fast.remote", "approval.mode"]
)
def test_every_kind_of_security_relevant_key_is_refused_by_name(
    client: TestClient, key: str
) -> None:
    response = client.put("/api/v1/settings", json={key: "anything"})
    assert response.status_code == 403
    assert key in response.json()["error"]["message"]


def test_an_unknown_key_names_it_and_lists_what_can_be_changed(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"execution.max_steps": 40})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "execution.max_steps" in error["message"]
    assert "execution.step_retries" in error["details"]["runtime_changeable"]


def test_a_value_outside_its_bounds_is_refused_with_the_range(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"compaction.threshold": 2.0})
    assert response.status_code == 400
    assert "between 0.01 and 1.0" in response.json()["error"]["message"]


def test_a_stored_row_shadowed_by_the_environment_is_shown_as_shadowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__STEP_RETRIES", "3")
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles(*_PROFILES))
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        load_settings().settings,
        runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        answered = client.put("/api/v1/settings", json={"execution.step_retries": 8}).json()
        assert answered["settings"]["execution.step_retries"] == 3, "the environment wins"
        definition = answered["definitions"]["execution.step_retries"]
        assert definition["stored"] == 8, "the row is kept and shown"
        assert definition["shadowed_by"] == "env PROMPTCADENCE_EXECUTION__STEP_RETRIES"


def test_the_scopes_are_read_to_look_and_admin_to_change(client: TestClient) -> None:
    runtime = cast("FastAPI", client.app).state.runtime
    now = datetime.now(UTC)
    reader = create_token(runtime.database, name="reader", scopes=["read", "write"], now=now)
    administrator = create_token(runtime.database, name="ops", scopes=["admin"], now=now)
    as_reader = {"Authorization": f"Bearer {reader.token}"}

    assert client.get("/api/v1/settings").status_code == 401, "a token exists now"
    assert client.get("/api/v1/settings", headers=as_reader).status_code == 200, "read may look"
    refused = client.put("/api/v1/settings", json={"execution.step_retries": 2}, headers=as_reader)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "FORBIDDEN"
    assert refused.json()["error"]["details"]["required"] == "admin"
    allowed = client.put(
        "/api/v1/settings",
        json={"execution.step_retries": 2},
        headers={"Authorization": f"Bearer {administrator.token}"},
    )
    assert allowed.status_code == 200
