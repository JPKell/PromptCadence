"""``GET /egress-decisions`` and ``promptcadence egress list`` (spec §7.1, §7.2, dev plan Phase 6).

Both surfaces exist to answer one question an operator actually asks — *where did this
trajectory's data go* — so the assertions here are about what is **present**, not only about what
was refused. A surface that listed denials and quietly omitted approvals would pass a test written
the other way round while answering the wrong question.

The wire shape is asserted against SetSpec's ``governance.egress_decision`` 1.0 field names rather
than against a projection this application maintains, because the payload is the contract and a
hand-written projection would be a second definition of it (ADR-0051 §4).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from baseaicore import DataClassification
from commissioner import EgressTarget
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from promptcadence.bootstrap import bootstrap
from promptcadence.cli.main import app
from promptcadence.services.database import Database, ensure_ready
from promptcadence.services.egress import EgressService

runner = CliRunner()

_NOW = datetime(2026, 9, 4, 9, 30, tzinfo=UTC)


def _seed(egress: EgressService) -> None:
    """One approval, one denial and one violation — the whole vocabulary, recorded."""
    egress.evaluate(
        run_id="01TRAJECTORYAAAAAAAAAAAAAA",
        source_ref="01TURNAAAAAAAAAAAAAAAAAAAA",
        classification=DataClassification.PUBLIC,
        target=EgressTarget(name="local_fast", remote=False),
    )
    egress.evaluate(
        run_id="01TRAJECTORYAAAAAAAAAAAAAA",
        source_ref="01TURNBBBBBBBBBBBBBBBBBBBB",
        classification=DataClassification.CONFIDENTIAL,
        target=EgressTarget(
            name="remote_cheap",
            remote=True,
            max_data_classification=DataClassification.INTERNAL,
        ),
    )
    egress.record_violation(
        run_id="01TRAJECTORYBBBBBBBBBBBBBB",
        source_ref="01TURNCCCCCCCCCCCCCCCCCCCC",
        classification=DataClassification.PUBLIC,
        target=EgressTarget(name="local_fast", remote=False),
        reason="execution_subject_unverified",
        decided_at=_NOW,
        decision_id="01DECISIONCCCCCCCCCCCCCCCC",
    )


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[TestClient]:
    """The bootstrapped application over a seeded database, pointed at a closed port."""
    database_path = tmp_path / "egress.sqlite3"
    monkeypatch.setenv("PROMPTCADENCE_LOADCOACH__BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PROMPTCADENCE_STORAGE__DATABASE_URL", f"sqlite:///{database_path}")
    with Database.from_url(f"sqlite:///{database_path}") as database:
        ensure_ready(database, auto_migrate=True)
        _seed(EgressService(database, clock=lambda: _NOW))
    with TestClient(bootstrap().app, base_url="http://localhost") as client:
        yield client


# --------------------------------------------------------------------------------------------
# GET /egress-decisions
# --------------------------------------------------------------------------------------------


def test_get_egress_decisions_lists_approvals_denials_and_violations(
    seeded: TestClient,
) -> None:
    """Unfiltered means unfiltered: contract 3 makes an approval as durable as a denial."""
    response = seeded.get("/api/v1/egress-decisions")
    assert response.status_code == 200
    rows = response.json()["items"]
    assert {row["verdict"] for row in rows} == {"approved", "denied", "violation"}


def test_each_row_is_a_governance_egress_decision_payload(seeded: TestClient) -> None:
    """The wire shape is SetSpec's, field for field, not a projection kept in step by hand."""
    rows = seeded.get("/api/v1/egress-decisions").json()["items"]
    denial = next(row for row in rows if row["verdict"] == "denied")
    assert set(denial) == {
        "decision_id",
        "request",
        "verdict",
        "reason",
        "policy_name",
        "policy_version",
        "decided_at",
    }
    assert set(denial["request"]) == {
        "run_id",
        "source_ref",
        "data_classification",
        "target",
        "requested_at",
    }
    assert denial["reason"] == "classification_exceeds_ceiling"
    assert denial["request"]["target"]["remote"] is True
    assert denial["request"]["source_ref"] == "01TURNBBBBBBBBBBBBBBBBBBBB"


def test_the_filters_narrow_by_trajectory_and_by_verdict(seeded: TestClient) -> None:
    by_trajectory = seeded.get(
        "/api/v1/egress-decisions", params={"trajectory_id": "01TRAJECTORYBBBBBBBBBBBBBB"}
    ).json()["items"]
    assert [row["verdict"] for row in by_trajectory] == ["violation"]

    denied = seeded.get("/api/v1/egress-decisions", params={"verdict": "denied"}).json()["items"]
    assert [row["verdict"] for row in denied] == ["denied"]


def test_an_unknown_verdict_is_refused_rather_than_ignored(seeded: TestClient) -> None:
    """A caller who asked for ``verdict=blocked`` must not read an unfiltered list as filtered.

    On this endpoint that mistake means reading approvals as denials, which is the one
    misinterpretation a governance surface cannot afford.
    """
    response = seeded.get("/api/v1/egress-decisions", params={"verdict": "blocked"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------------------------
# promptcadence egress list
# --------------------------------------------------------------------------------------------


def test_egress_list_prints_every_verdict_with_its_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Local mode: no server answering, read straight from the configured database."""
    database_path = tmp_path / "egress-cli.sqlite3"
    monkeypatch.setenv("PROMPTCADENCE_LOADCOACH__BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PROMPTCADENCE_STORAGE__DATABASE_URL", f"sqlite:///{database_path}")
    with Database.from_url(f"sqlite:///{database_path}") as database:
        ensure_ready(database, auto_migrate=True)
        _seed(EgressService(database, clock=lambda: _NOW))

    result = runner.invoke(app, ["egress", "list"])
    assert result.exit_code == 0, result.stdout
    assert "approved" in result.stdout
    assert "denied" in result.stdout
    assert "violation" in result.stdout
    assert "classification_exceeds_ceiling" in result.stdout
    assert "target_not_remote" in result.stdout
    assert "3 decision(s)." in result.stdout


def test_egress_list_json_prints_the_payload_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    database_path = tmp_path / "egress-json.sqlite3"
    monkeypatch.setenv("PROMPTCADENCE_LOADCOACH__BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PROMPTCADENCE_STORAGE__DATABASE_URL", f"sqlite:///{database_path}")
    with Database.from_url(f"sqlite:///{database_path}") as database:
        ensure_ready(database, auto_migrate=True)
        _seed(EgressService(database, clock=lambda: _NOW))

    result = runner.invoke(app, ["egress", "list", "--json", "--verdict", "denied"])
    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    assert [row["verdict"] for row in rows] == ["denied"]
    assert rows[0]["policy_name"] == "OrderedClassificationPolicy"


def test_egress_list_refuses_a_verdict_outside_the_vocabulary() -> None:
    result = runner.invoke(app, ["egress", "list", "--verdict", "blocked"])
    assert result.exit_code == 2
    assert "VALIDATION_ERROR" in result.output


def test_egress_list_says_so_when_nothing_was_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """An empty list is an answer, not an error — a fresh install has governed nothing yet."""
    database_path = tmp_path / "egress-empty.sqlite3"
    monkeypatch.setenv("PROMPTCADENCE_LOADCOACH__BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PROMPTCADENCE_STORAGE__DATABASE_URL", f"sqlite:///{database_path}")
    with Database.from_url(f"sqlite:///{database_path}") as database:
        ensure_ready(database, auto_migrate=True)

    result = runner.invoke(app, ["egress", "list"])
    assert result.exit_code == 0, result.stdout
    assert "No egress decisions recorded." in result.stdout
