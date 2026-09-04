"""``GET /ledger``, ``GET /ledger/entries`` and ``promptcadence ledger show`` (spec §7).

The assertion that matters most here is not that the endpoints exist. It is that **the three
surfaces render a figure the same way**: an unpriced amount as ``—`` and never ``$0.00``
(ADR-0016, spec §20 criterion 1), and a floor as "at least" and never as a bare number
(ADR-0069). They render it identically because they all go through ``render_money``, and these
tests are what would notice if one of them stopped.

The whole file runs on the fake LoadCoach: no GPU, no Ollama, no network (spec §20 #10).
"""

from __future__ import annotations

import json
import socket
import time
from typing import TYPE_CHECKING, Any

import pytest
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
from promptcadence.services.budget import NOT_PRICED
from promptcadence.services.runtime import build_runtime
from promptcadence.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

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
    monkeypatch.setenv("PROMPTCADENCE_BUDGET__PROJECTS__RESEARCH__TOKEN_CEILING", "100000")
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


def test_get_ledger_reports_the_day_and_every_configured_project(client: TestClient) -> None:
    view = _run(client, project="research")
    assert view["state"] == "completed", view["cause"]
    body = client.get("/api/v1/ledger").json()
    assert body["utc_day"] == body["as_of"][:10]
    assert body["day"]["scope"] == "day"
    assert [project["project"] for project in body["projects"]] == ["research"]
    research = body["projects"][0]
    assert research["tokens_remaining"] == 100_000 - 812 - 104
    assert research["binds"] is False


def test_an_unpriced_position_renders_an_em_dash_and_never_a_zero(client: TestClient) -> None:
    """Spec §20 criterion 1 on the wire. The local tier priced nothing; nothing is not free."""
    _run(client)
    body = client.get("/api/v1/ledger").json()
    assert body["day"]["money_remaining_display"] != NOT_PRICED, "the cap itself is a real figure"
    entries = client.get("/api/v1/ledger/entries").json()["items"]
    assert entries, "the turn was debited"
    trajectory = next(one for one in entries[0]["ceilings"] if one["scope"] == "trajectory")
    assert trajectory["money_is_floor"] is True
    assert trajectory["money_remaining_display"].startswith("at most "), (
        "a *remaining* figure derived from a floor is an upper bound, not a lower one"
    )
    assert "0.00" not in json.dumps(entries), "no fabricated zero anywhere in the document"


def test_get_ledger_entries_carries_usage_and_a_pricing_hash_and_never_a_money_fact(
    client: TestClient,
) -> None:
    """ADR-0030 rule 1 on the wire: the stored facts cross the boundary, the money does not."""
    view = _run(client)
    body = client.get(
        "/api/v1/ledger/entries", params={"trajectory_id": view["trajectory_id"]}
    ).json()["items"]
    assert len(body) == 1
    entry = body[0]
    assert entry["trajectory_id"] == view["trajectory_id"]
    assert entry["usage"]["input"] == 812
    assert entry["usage"]["cache_write"] == "unsupported", "not reported is not zero"
    assert entry["unpriced"] is True
    assert set(entry) == {
        "entry_id",
        "trajectory_id",
        "turn_id",
        "occurred_at",
        "tags",
        "unpriced",
        "pricing_hash",
        "usage",
        "ceilings",
    }
    assert "money_spent" not in entry and "cost" not in entry


def test_get_ledger_entries_filters_by_tag(client: TestClient) -> None:
    _run(client, project="research")
    tagged = client.get("/api/v1/ledger/entries", params={"tag": "tier:local_fast"}).json()["items"]
    assert tagged and all("tier:local_fast" in entry["tags"] for entry in tagged)
    empty = client.get("/api/v1/ledger/entries", params={"tag": "tier:nope"}).json()["items"]
    assert empty == []


def test_ledger_show_prints_the_same_figures_the_api_returns(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI and the API agree because they share one renderer, not because they were compared."""
    _run(client, project="research")
    body = client.get("/api/v1/ledger").json()
    # Local mode, forced: the served application under test is in-process (Starlette's TestClient
    # binds no socket), so pointing the CLI at a closed port is what makes "no server answers"
    # true rather than merely likely — a stray PromptCadence on the default port would otherwise
    # answer from a different database and this test would fail for a reason it is not about.
    monkeypatch.setenv("PROMPTCADENCE_SERVER__PORT", str(_closed_port()))
    result = CliRunner().invoke(cli_app, ["ledger", "show", "--scope", "project", "--json"])
    assert result.exit_code == 0, result.output
    printed = json.loads(result.stdout)
    assert printed["projects"][0]["tokens_remaining"] == body["projects"][0]["tokens_remaining"]
    assert (
        printed["projects"][0]["money_remaining_display"]
        == body["projects"][0]["money_remaining_display"]
    )


def test_ledger_show_text_output_names_the_scope_and_never_prints_a_bare_floor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(client)
    monkeypatch.setenv("PROMPTCADENCE_SERVER__PORT", str(_closed_port()))
    runner = CliRunner()
    day = runner.invoke(cli_app, ["ledger", "show"])
    assert day.exit_code == 0, day.output
    assert "UTC day" in day.stdout
    assert "day " in day.stdout

    tiers = runner.invoke(cli_app, ["ledger", "show", "--scope", "tier"])
    assert tiers.exit_code == 0, tiers.output
    assert "local_fast" in tiers.stdout
    assert "1 debit(s) recorded" in tiers.stdout
    assert "no tier ceiling is configured" in tiers.stdout


def test_ledger_show_refuses_an_unknown_scope_and_a_missing_trajectory(
    client: TestClient,
) -> None:
    runner = CliRunner()
    bad = runner.invoke(cli_app, ["ledger", "show", "--scope", "galaxy"])
    assert bad.exit_code == 2
    assert "VALIDATION_ERROR" in bad.output

    missing = runner.invoke(cli_app, ["ledger", "show", "--scope", "trajectory"])
    assert missing.exit_code == 2
    assert "--trajectory" in missing.output


def test_a_per_request_partial_pricing_override_crosses_the_wire(client: TestClient) -> None:
    created = client.post(
        "/api/v1/trajectories",
        json={
            "task": "strictly budgeted",
            "bypass_planning": True,
            "budget": {"tokens": 5000, "partial_pricing": "strict"},
        },
    )
    assert created.status_code == 202, created.text
    assert created.json()["budget"]["partial_pricing"] == "strict"


def test_an_unknown_partial_pricing_value_is_refused(client: TestClient) -> None:
    refused = client.post(
        "/api/v1/trajectories",
        json={"task": "x", "budget": {"tokens": 5000, "partial_pricing": "whatever"}},
    )
    assert refused.status_code in (400, 422)
