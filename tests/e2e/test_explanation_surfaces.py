"""``GET /trajectories/{id}/explanation`` and ``promptcadence trajectory explain`` (spec §7).

Both surfaces over one journey on the fake LoadCoach: no GPU, no Ollama, no network. What is
asserted beyond "the endpoint exists" is the pair of properties that make the record trustworthy —
the two surfaces answer with the **same document**, and the document the API returns is the
composition of the rows and not a cache that has drifted from them.

The response headers, not the body, say which path answered. The body is the document, and the
document is a byte-stable composition of the rows: folding "which cache answered" into it would
make two reads of the same rows differ, which is exactly what the equality golden forbids.
"""

from __future__ import annotations

import json
import socket
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    ScriptedGeneration,
    build_fake_app,
    shipped_profiles,
)
from typer.testing import CliRunner

from promptcadence.cli.main import app as cli_app
from promptcadence.config import load_settings
from promptcadence.domain.explanation import SCHEMA_NAME, SCHEMA_VERSION
from promptcadence.services.runtime import build_runtime
from promptcadence.services.tokens import create_token
from promptcadence.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_TERMINAL = {"completed", "halted", "failed", "cancelled", "rejected"}


def _closed_port() -> int:
    """A port nothing is listening on, so an "either"-mode command takes its local path."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def fake() -> FakeLoadCoach:
    served = FakeLoadCoach()
    served.register_profile(*shipped_profiles("tools.agent.local_fast", "tools.agent.local_large"))
    served.set_default(
        ScriptedGeneration(text="the notes describe three meetings", input_tokens=812)
    )
    return served


@pytest.fixture
def client(fake: FakeLoadCoach, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "2")
    settings = load_settings().settings
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        settings, runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http)
    )
    with TestClient(app, base_url="http://127.0.0.1") as running:
        yield running


def _run(client: TestClient, **body: Any) -> dict[str, Any]:
    payload = {"task": "summarize ./notes", "bypass_planning": True, **body}
    created = client.post("/api/v1/trajectories", json=payload)
    assert created.status_code == 202, created.text
    trajectory_id = created.json()["trajectory_id"]
    deadline = time.monotonic() + 10
    while True:
        view: dict[str, Any] = client.get(f"/api/v1/trajectories/{trajectory_id}").json()
        if view["state"] in _TERMINAL or time.monotonic() > deadline:
            return view
        time.sleep(0.02)


def test_the_endpoint_answers_the_document_from_the_materialized_revision(
    client: TestClient,
) -> None:
    view = _run(client)
    assert view["state"] == "completed", view["cause"]
    response = client.get(f"/api/v1/trajectories/{view['trajectory_id']}/explanation")
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["schema"] == SCHEMA_NAME
    assert document["version"] == SCHEMA_VERSION
    assert document["trajectory"]["trajectory_id"] == view["trajectory_id"]
    assert response.headers["X-Explanation-Source"] == "materialized"
    assert response.headers["X-Explanation-Revision"] == "1"
    assert response.headers["X-Explanation-Revision-Cause"] == "terminal"
    assert response.headers["ETag"].strip('"')


def test_an_in_flight_trajectory_is_composed_live(client: TestClient) -> None:
    """A snapshot of a moving record would be stale by the time it returned (lifecycle §9.1)."""
    created = client.post(
        "/api/v1/trajectories", json={"task": "summarize ./notes", "bypass_planning": True}
    )
    trajectory_id = created.json()["trajectory_id"]
    response = client.get(f"/api/v1/trajectories/{trajectory_id}/explanation")
    assert response.status_code == 200
    assert response.headers["X-Explanation-Source"] == "live"
    assert "X-Explanation-Revision" not in response.headers


def test_an_unknown_trajectory_is_a_typed_404(client: TestClient) -> None:
    response = client.get("/api/v1/trajectories/01UNKNOWNUNKNOWNUNKNOWNUNK/explanation")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TRAJECTORY_NOT_FOUND"


def test_the_endpoint_requires_the_read_scope(client: TestClient) -> None:
    """Scopes are enforced on the explanation as on every other record surface (spec §14).

    Loopback with no tokens is open, so the token is created first: once one exists, every scoped
    endpoint needs a bearer, and a bearer without ``read`` is refused.
    """
    view = _run(client)
    runtime = cast("FastAPI", client.app).state.runtime
    writer = create_token(
        runtime.database, name="writer-only", scopes=["write"], now=datetime.now(UTC)
    )

    anonymous = client.get(f"/api/v1/trajectories/{view['trajectory_id']}/explanation")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "UNAUTHORIZED"

    refused = client.get(
        f"/api/v1/trajectories/{view['trajectory_id']}/explanation",
        headers={"Authorization": f"Bearer {writer.token}"},
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "FORBIDDEN"


def test_the_route_is_in_the_generated_openapi(client: TestClient) -> None:
    """Spec §7.1 lists it; the schema a caller reads must too."""
    schema = client.get("/api/v1/openapi.json").json()
    assert "/api/v1/trajectories/{trajectory_id}/explanation" in schema["paths"]


def test_the_cli_and_the_api_answer_the_same_document(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One record, two surfaces. A difference here is a second definition of the document."""
    view = _run(client)
    from_api = client.get(f"/api/v1/trajectories/{view['trajectory_id']}/explanation").json()

    monkeypatch.setenv("PROMPTCADENCE_SERVER__PORT", str(_closed_port()))
    target = tmp_path / "explanation.json"
    result = CliRunner().invoke(
        cli_app,
        ["trajectory", "explain", view["trajectory_id"], "--output", str(target)],
    )
    assert result.exit_code == 0, result.output
    assert "materialized (revision 1)" in result.output
    assert json.loads(target.read_text()) == from_api


def test_the_cli_prints_the_document_with_json(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _run(client)
    monkeypatch.setenv("PROMPTCADENCE_SERVER__PORT", str(_closed_port()))
    result = CliRunner().invoke(cli_app, ["trajectory", "explain", view["trajectory_id"], "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["schema"] == SCHEMA_NAME


def test_rebuild_explanations_reports_an_intact_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rebuilt 0`` over an intact cache is the assertion that the cache was correct."""
    _run(client)
    monkeypatch.setenv("PROMPTCADENCE_SERVER__PORT", str(_closed_port()))
    runner = CliRunner()
    intact = runner.invoke(cli_app, ["db", "rebuild-explanations", "--json"])
    assert intact.exit_code == 0, intact.output
    assert json.loads(intact.output) == {"dropped": 0, "considered": 1, "rebuilt": 0}

    dropped = runner.invoke(cli_app, ["db", "rebuild-explanations", "--drop", "--json"])
    assert intact.exit_code == 0, dropped.output
    assert json.loads(dropped.output) == {"dropped": 1, "considered": 1, "rebuilt": 1}
