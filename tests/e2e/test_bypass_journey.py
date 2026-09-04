"""Development plan Phase 3, acceptance criterion 1 — the fake half.

``promptcadence run "…" --bypass-planning`` completes against the fake LoadCoach, over HTTP and
over the CLI, with the SSE stream replaying from ``Last-Event-ID``. The live half — the same
journey against a real LoadCoach — is ``tests/live/test_loadcoach_journey.py`` and an operator
step.

The tiers are the shipped defaults (free-text ``tools.agent.*`` profiles) and the fake speaks the
wire of LoadCoach ``846348b``: the provider's declared ``stop`` completes a turn. Two
further tests script the wire of an older LoadCoach (no ``finish_reason``) and a truncated answer
(``length``) and assert each reads as a halt with its cause on the row, never as a completion.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    ScriptedGeneration,
    build_fake_app,
    shipped_profiles,
)
from typer.testing import CliRunner

from promptcadence.cli import main as cli_main
from promptcadence.cli.commands import trajectories as trajectory_commands
from promptcadence.config import load_settings
from promptcadence.services.runtime import build_runtime
from promptcadence.web.app import create_app

_TERMINAL = {"completed", "halted", "failed", "cancelled"}


@pytest.fixture
def fake() -> FakeLoadCoach:
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles("tools.agent.local_fast", "tools.agent.local_large"))
    fake.set_default(ScriptedGeneration(text="the notes describe three meetings"))
    return fake


@pytest.fixture
def client(fake: FakeLoadCoach, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The served application, its worker running, its LoadCoach the in-process fake."""
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "2")
    settings = load_settings().settings
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        settings,
        runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http),
    )
    with TestClient(app, base_url="http://127.0.0.1") as running:
        yield running


def _wait_terminal(
    client: TestClient, trajectory_id: str, *, timeout_seconds: float = 10
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        view: dict[str, Any] = client.get(f"/api/v1/trajectories/{trajectory_id}").json()
        if view["state"] in _TERMINAL or time.monotonic() > deadline:
            return view
        time.sleep(0.02)


def _frames(
    client: TestClient, trajectory_id: str, *, last_event_id: str | None = None
) -> list[tuple[int, str, dict[str, Any]]]:
    headers = {"Accept": "text/event-stream"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    frames: list[tuple[int, str, dict[str, Any]]] = []
    with client.stream(
        "GET", f"/api/v1/trajectories/{trajectory_id}/stream", headers=headers
    ) as response:
        assert response.status_code == 200
        event_id: int | None = None
        event_type: str | None = None
        for line in response.iter_lines():
            if line.startswith("id:"):
                event_id = int(line[3:].strip())
            elif line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:") and event_id is not None and event_type is not None:
                frames.append((event_id, event_type, json.loads(line[5:].strip())))
                event_id = event_type = None
    return frames


def test_the_bypass_journey_completes_over_http(client: TestClient, fake: FakeLoadCoach) -> None:
    submitted = client.post(
        "/api/v1/trajectories",
        json={
            "task": "summarize the files in ./notes",
            "bypass_planning": True,
            "data_classification": "internal",
        },
    )
    assert submitted.status_code == 202, submitted.text
    trajectory_id = submitted.json()["trajectory_id"]
    assert submitted.json()["state"] == "queued"

    final = _wait_terminal(client, trajectory_id)
    assert final["state"] == "completed", final
    assert final["cause"] == "the provider declared finish_reason=stop"
    assert final["lease"]["owner"] is None

    turns = client.get(f"/api/v1/trajectories/{trajectory_id}/turns").json()["items"]
    assert [turn["role"] for turn in turns] == ["user", "assistant"]
    assert turns[1]["intent_id"] and turns[1]["intent_revision"] == 1
    assert turns[1]["usage"]["cache_read_tokens"] == "unsupported"  # the interim wire, verbatim
    assert turns[1]["loadcoach_job_id"] in fake.jobs

    listed = client.get("/api/v1/trajectories", params={"state": "completed"}).json()
    assert [item["trajectory_id"] for item in listed["items"]] == [trajectory_id]

    status = client.get("/api/v1/system/status").json()
    assert status["active_trajectories"] == []
    assert status["last_recovery"]["touched"] == 0

    cancel = client.post(f"/api/v1/trajectories/{trajectory_id}/cancel")
    assert cancel.status_code == 409
    assert cancel.json()["error"]["code"] == "TRAJECTORY_NOT_CANCELLABLE"


def test_the_stream_replays_from_last_event_id_without_gap_or_duplicate(
    client: TestClient,
) -> None:
    trajectory_id = client.post(
        "/api/v1/trajectories", json={"task": "t", "bypass_planning": True}
    ).json()["trajectory_id"]
    assert _wait_terminal(client, trajectory_id)["state"] == "completed"

    everything = _frames(client, trajectory_id)
    ids = [frame[0] for frame in everything]
    assert ids == list(range(1, len(ids) + 1))  # gap-free from 1
    assert [frame[1] for frame in everything] == [
        "trajectory.created",
        "trajectory.claimed",
        "intent.minted",
        "turn.started",
        "turn.completed",
        "trajectory.completed",
    ]
    envelope = everything[0][2]
    assert envelope["schema"] == "event.envelope"
    assert envelope["generator"]["name"] == "promptcadence"
    assert envelope["payload"]["entity"] == {"kind": "trajectory", "id": trajectory_id}
    assert envelope["payload"]["sequence"] == 1

    resumed = _frames(client, trajectory_id, last_event_id="3")
    assert [frame[0] for frame in resumed] == [4, 5, 6]
    assert resumed[-1][1] == "trajectory.completed"

    garbage = _frames(client, trajectory_id, last_event_id="not-a-number")
    assert [frame[0] for frame in garbage] == ids

    assert client.get("/api/v1/trajectories/01ABSENT000000000000000000/stream").status_code == 404


def test_an_undeclared_finish_halts_visibly_never_completes(
    client: TestClient, fake: FakeLoadCoach
) -> None:
    """The quiet failure, made loud: no declared finish is a halt with its cause on the row."""
    fake.script(ScriptedGeneration(finish_reason=None))  # the wire before 846348b
    trajectory_id = client.post(
        "/api/v1/trajectories", json={"task": "t", "bypass_planning": True, "tier": "local_large"}
    ).json()["trajectory_id"]
    final = _wait_terminal(client, trajectory_id)
    assert final["state"] == "halted"
    assert final["error_code"] == "LOADCOACH_ERROR"
    assert "no finish_reason" in final["cause"]


def test_a_truncated_answer_halts_visibly_never_completes(
    client: TestClient, fake: FakeLoadCoach
) -> None:
    """A ``length`` finish is the row's named failure mode; it reads as a halt, over HTTP."""
    fake.script(ScriptedGeneration(text="the notes descr", finish_reason="length"))
    trajectory_id = client.post(
        "/api/v1/trajectories", json={"task": "t", "bypass_planning": True}
    ).json()["trajectory_id"]
    final = _wait_terminal(client, trajectory_id)
    assert final["state"] == "halted"
    assert final["error_code"] == "LOADCOACH_ERROR"
    assert "finish_reason=length" in final["cause"]
    turns = client.get(f"/api/v1/trajectories/{trajectory_id}/turns").json()["items"]
    assert turns[1]["finish_reason"] == "length"


def test_submission_refusals_use_spec_13_codes(client: TestClient) -> None:
    assert (
        client.post(
            "/api/v1/trajectories", json={"task": "t", "data_classification": "secret"}
        ).json()["error"]["code"]
        == "CLASSIFICATION_INVALID"
    )
    assert (
        client.post("/api/v1/trajectories", json={"task": "t", "project": "nope"}).json()["error"][
            "code"
        ]
        == "PROJECT_UNKNOWN"
    )
    assert (
        client.post("/api/v1/trajectories", json={"task": "t", "tools": ["teleport"]}).json()[
            "error"
        ]["code"]
        == "TOOL_NOT_FOUND"
    )
    assert (
        client.post("/api/v1/trajectories", json={"task": "t", "tier": "gpt_9"}).json()["error"][
            "code"
        ]
        == "TIER_NOT_CONFIGURED"
    )
    assert (
        client.post("/api/v1/trajectories", json={"task": "t", "extra": 1}).json()["error"]["code"]
        == "VALIDATION_ERROR"
    )
    assert client.get("/api/v1/trajectories/01ABSENT000000000000000000").status_code == 404


def test_the_cli_run_follow_completes_with_exit_zero(
    client: TestClient, fake: FakeLoadCoach, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance criterion 1, spelled the way the plan spells it."""
    monkeypatch.setattr(
        trajectory_commands,
        "http_client_factory",
        lambda settings: TestClient(client.app, base_url="http://127.0.0.1"),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        ["run", "summarize the files in ./notes", "--bypass-planning", "--follow", "--json"],
    )
    assert result.exit_code == 0, result.output
    final = json.loads(result.stdout.strip().splitlines()[-1])
    assert final["state"] == "completed"

    text = runner.invoke(cli_main.app, ["run", "another task", "--bypass-planning", "--follow"])
    assert text.exit_code == 0, text.output
    assert "trajectory.completed" in text.stdout
    assert "state        completed" in text.stdout

    fake.script(ScriptedGeneration(text="cut off", finish_reason="length"))
    halted = runner.invoke(
        cli_main.app, ["run", "t", "--bypass-planning", "--tier", "local_large", "--follow"]
    )
    assert halted.exit_code == 5
    assert "finish_reason=length" in halted.stdout

    listed = runner.invoke(cli_main.app, ["trajectory", "list", "--json"])
    assert listed.exit_code == 0
    payload = json.loads(listed.stdout)
    assert payload["source"] == "server"
    assert len(payload["items"]) == 3

    shown = runner.invoke(cli_main.app, ["trajectory", "show", final["trajectory_id"]])
    assert shown.exit_code == 0
    assert "completed" in shown.stdout

    waited = runner.invoke(cli_main.app, ["trajectory", "wait", final["trajectory_id"], "--json"])
    assert waited.exit_code == 0

    cancelled = runner.invoke(cli_main.app, ["trajectory", "cancel", final["trajectory_id"]])
    assert cancelled.exit_code == 1
    assert "TRAJECTORY_NOT_CANCELLABLE" in cancelled.stderr


def test_the_cli_exits_four_when_no_server_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPTCADENCE_SERVER__PORT", "9")  # discard: nothing listens
    result = CliRunner().invoke(cli_main.app, ["run", "t", "--bypass-planning"])
    assert result.exit_code == 4
    assert "not reachable" in result.stderr
    cancel = CliRunner().invoke(cli_main.app, ["trajectory", "cancel", "01X"])
    assert cancel.exit_code == 4
