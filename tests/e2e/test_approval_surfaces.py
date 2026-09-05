"""Phase 7's surfaces over HTTP and the CLI (spec §7.1, §7.2, §17): approve, deny, list,
the plan and intent reads, the status dashboard, tokens and the ``approve`` scope."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.fakes.harness import plan_document, step
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
from promptcadence.services.tokens import create_token
from promptcadence.web.app import create_app

_PROFILES = ("tools.agent.local_fast", "tools.agent.local_large", "tools.plan")


def _serve(fake: FakeLoadCoach) -> TestClient:
    settings = load_settings().settings
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        settings, runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http)
    )
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def fake() -> FakeLoadCoach:
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles(*_PROFILES))
    fake.set_default(ScriptedGeneration(text="three meetings"))
    return fake


@pytest.fixture
def manual(fake: FakeLoadCoach, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PROMPTCADENCE_APPROVAL__MODE", "manual")
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "2")
    with _serve(fake) as client:
        yield client


@pytest.fixture
def auto(fake: FakeLoadCoach, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "2")
    with _serve(fake) as client:
        yield client


def _wait(
    client: TestClient, trajectory_id: str, states: set[str], *, seconds: float = 10
) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while True:
        view: dict[str, Any] = client.get(f"/api/v1/trajectories/{trajectory_id}").json()
        if view["state"] in states or time.monotonic() > deadline:
            return view
        time.sleep(0.02)


def test_the_approve_deny_and_list_surfaces_over_http(manual: TestClient) -> None:
    submitted = manual.post("/api/v1/trajectories", json={"task": "t", "bypass_planning": True})
    trajectory_id = submitted.json()["trajectory_id"]
    assert _wait(manual, trajectory_id, {"awaiting_approval"})["state"] == "awaiting_approval"

    listed = manual.get("/api/v1/approvals").json()["items"]
    assert len(listed) == 1
    (pending,) = listed
    assert pending["trajectory_id"] == trajectory_id
    assert pending["kind"] == "bypass_gate" and pending["status"] == "pending"
    assert pending["age_seconds"] >= 0 and pending["expires_at"]
    status = manual.get("/api/v1/system/status").json()
    assert [p["request_id"] for p in status["pending_approvals"]] == [pending["request_id"]]
    assert "age_seconds" in status["pending_approvals"][0]
    assert status["ledger"]["day"]["scope"] == "day"

    granted = manual.post(f"/api/v1/trajectories/{trajectory_id}/approve")
    assert granted.status_code == 200, granted.text
    body = granted.json()
    assert body["state"] == "executing" and body["already_resolved"] is False
    assert [(m["revision"], m["step_id"]) for m in body["minted"]] == [(2, "loop")]
    assert _wait(manual, trajectory_id, {"completed"})["state"] == "completed"
    again = manual.post(f"/api/v1/trajectories/{trajectory_id}/approve")
    assert again.status_code == 200 and again.json()["already_resolved"] is True

    intents = manual.get(f"/api/v1/trajectories/{trajectory_id}/intents").json()["items"]
    assert [(i["revision"], i["minted_by"]) for i in intents] == [
        (1, "bypass_default"),
        (2, "approver:loopback"),
    ]
    assert manual.get(f"/api/v1/trajectories/{trajectory_id}/plan").json() is None
    assert manual.get("/api/v1/approvals").json()["items"] == []
    resolved = manual.get(
        "/api/v1/approvals", params={"trajectory_id": trajectory_id, "status": "all"}
    ).json()["items"]
    assert resolved[0]["status"] == "granted" and resolved[0]["approver_token_id"] == "loopback"  # noqa: S105

    second = manual.post("/api/v1/trajectories", json={"task": "t", "bypass_planning": True})
    second_id = second.json()["trajectory_id"]
    _wait(manual, second_id, {"awaiting_approval"})
    denied = manual.post(f"/api/v1/trajectories/{second_id}/deny", json={"reason": "no"})
    assert denied.status_code == 200 and denied.json()["state"] == "halted"
    assert denied.json()["request"]["resolution_reason"] == "no"
    view = manual.get(f"/api/v1/trajectories/{second_id}").json()
    assert view["error_code"] == "APPROVAL_REQUIRED" and "no" in view["cause"]
    conflict = manual.post(f"/api/v1/trajectories/{second_id}/approve")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "APPROVAL_INVALID_STATE"
    assert manual.post("/api/v1/trajectories/01ABSENT000000000000000000/approve").status_code == 404


def test_the_approve_scope_is_enforced_once_a_token_exists(manual: TestClient) -> None:
    runtime = cast("FastAPI", manual.app).state.runtime
    now = datetime.now(UTC)
    reader = create_token(runtime.database, name="reader", scopes=["read", "write"], now=now)
    approver = create_token(runtime.database, name="ops", scopes=["approve"], now=now)
    trajectory_id = manual.post(
        "/api/v1/trajectories", json={"task": "t", "bypass_planning": True}
    ).json()["trajectory_id"]
    _wait(manual, trajectory_id, {"awaiting_approval"})

    anonymous = manual.post(f"/api/v1/trajectories/{trajectory_id}/approve")
    assert anonymous.status_code == 401 and anonymous.json()["error"]["code"] == "UNAUTHORIZED"
    forbidden = manual.post(
        f"/api/v1/trajectories/{trajectory_id}/approve",
        headers={"Authorization": f"Bearer {reader.token}"},
    )
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "FORBIDDEN"
    assert manual.get("/api/v1/approvals").status_code == 401, "read needs a token now too"
    granted = manual.post(
        f"/api/v1/trajectories/{trajectory_id}/approve",
        headers={"Authorization": f"Bearer {approver.token}"},
    )
    assert granted.status_code == 200
    intents = manual.get(
        f"/api/v1/trajectories/{trajectory_id}/intents",
    ).json()["items"]
    assert intents[-1]["minted_by"] == f"approver:{approver.record.token_id}"  # noqa: S105


def test_the_planned_journey_over_http_and_the_cli_with_the_plan_and_intent_reads(
    auto: TestClient, fake: FakeLoadCoach, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.script(ScriptedGeneration(text=plan_document(step("s1"), step("s2", depends_on=["s1"]))))
    monkeypatch.setattr(
        trajectory_commands,
        "http_client_factory",
        lambda settings: TestClient(auto.app, base_url="http://127.0.0.1"),
    )
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["run", "summarize the files in ./notes", "--follow"])
    assert result.exit_code == 0, result.output
    assert "plan.drafted — attempt 1, valid, 2 step(s)" in result.stdout
    assert "plan.approved" in result.stdout
    assert "intent.minted — step s1 (policy, revision 1)" in result.stdout
    assert "step.completed — step s2" in result.stdout
    assert "trajectory.completed" in result.stdout
    trajectory_id = result.stdout.split("trajectory   ")[1].split()[0]

    plan = auto.get(f"/api/v1/trajectories/{trajectory_id}/plan").json()
    assert plan["plan_id"] and len(plan["attempts"]) == 1 and plan["attempts"][0]["valid"]
    assert plan["attempts"][0]["prompt"]["prompt_id"] == "planner.draft"
    assert [(s["step_id"], s["status"]) for s in plan["steps"]] == [
        ("s1", "committed"),
        ("s2", "committed"),
    ]
    assert plan["approval"]["outcome"] == "approved"
    intents = auto.get(f"/api/v1/trajectories/{trajectory_id}/intents").json()["items"]
    assert [(i["step_id"], i["minted_by"]) for i in intents] == [("s1", "policy"), ("s2", "policy")]
    turns = auto.get(f"/api/v1/trajectories/{trajectory_id}/turns").json()["items"]
    assert [(t["step_id"], t["role"]) for t in turns] == [
        ("s1", "user"),
        ("s1", "user"),
        ("s1", "assistant"),
        ("s2", "user"),
        ("s2", "user"),
        ("s2", "assistant"),
    ]
    assert turns[1]["prompt_id"] == "step.execute" and turns[0]["prompt_id"] is None

    listed = runner.invoke(cli_main.app, ["approvals", "list", "--json"])
    assert listed.exit_code == 0 and json.loads(listed.stdout)["items"] == []


def test_the_cli_approve_and_deny_commands(
    manual: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        trajectory_commands,
        "http_client_factory",
        lambda settings: TestClient(manual.app, base_url="http://127.0.0.1"),
    )
    runner = CliRunner()
    first = manual.post("/api/v1/trajectories", json={"task": "a", "bypass_planning": True})
    second = manual.post("/api/v1/trajectories", json={"task": "b", "bypass_planning": True})
    ids = [first.json()["trajectory_id"], second.json()["trajectory_id"]]
    for trajectory_id in ids:
        _wait(manual, trajectory_id, {"awaiting_approval"})
    listed = runner.invoke(cli_main.app, ["approvals", "list"])
    assert listed.exit_code == 0 and listed.stdout.count("bypass_gate") == 2
    approved = runner.invoke(cli_main.app, ["approve", ids[0]])
    assert approved.exit_code == 0, approved.output
    assert "state        executing" in approved.stdout and "minted" in approved.stdout
    denied = runner.invoke(cli_main.app, ["deny", ids[1], "--reason", "later"])
    assert denied.exit_code == 0, denied.output
    assert "state        halted" in denied.stdout and "reason       later" in denied.stdout
    assert _wait(manual, ids[0], {"completed"})["state"] == "completed"
    tiers = runner.invoke(cli_main.app, ["tiers", "list"])
    assert (
        tiers.exit_code == 0 and "local_fast" in tiers.stdout and "escalation order" in tiers.stdout
    )
    shown = runner.invoke(cli_main.app, ["tiers", "show", "local_large", "--json"])
    assert (
        shown.exit_code == 0
        and json.loads(shown.stdout)["task_profile"] == "tools.agent.local_large"
    )
    missing = runner.invoke(cli_main.app, ["tiers", "show", "gpt_9"])
    assert missing.exit_code == 2


def test_tiers_check_exits_four_when_no_loadcoach_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPTCADENCE_LOADCOACH__BASE_URL", "http://127.0.0.1:9")
    result = CliRunner().invoke(cli_main.app, ["tiers", "check"])
    assert result.exit_code == 4
    assert "tools.plan" in result.stdout and "unreachable" in result.stdout.lower()


def test_token_create_list_revoke_via_the_cli() -> None:
    runner = CliRunner()
    created = runner.invoke(cli_main.app, ["token", "create", "ops", "--scope", "approve,read"])
    assert created.exit_code == 0, created.output
    assert "token        " in created.stdout and "approve,read" in created.stdout
    listed = runner.invoke(cli_main.app, ["token", "list"])
    assert listed.exit_code == 0 and "ops" in listed.stdout and "active" in listed.stdout
    revoked = runner.invoke(cli_main.app, ["token", "revoke", "ops"])
    assert revoked.exit_code == 0 and "revoked" in revoked.stdout
    missing = runner.invoke(cli_main.app, ["token", "revoke", "ops"])
    assert missing.exit_code == 1
