"""The committed OpenAPI snapshot matches the application (packaging standards §6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from promptcadence.config import Settings
from promptcadence.web.app import create_app

SNAPSHOT = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"

pytestmark = pytest.mark.contract


def current_openapi() -> dict[str, Any]:
    """The schema this build serves, with the volatile version field pinned by the build."""
    document = create_app(Settings()).openapi()
    return cast("dict[str, Any]", json.loads(json.dumps(document, sort_keys=True)))


def test_the_committed_snapshot_matches_the_application() -> None:
    assert SNAPSHOT.is_file(), "docs/openapi.json is missing; regenerate it"
    committed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert committed == current_openapi(), (
        "docs/openapi.json drifted; regenerate with "
        "python -c 'from tests.contract.test_openapi_snapshot import write; write()'"
    )


def test_every_documented_endpoint_is_in_the_snapshot() -> None:
    """Spec §7.1's list, minus the two this build does not serve (see docs/troubleshooting.md)."""
    paths = set(current_openapi()["paths"])
    for path in (
        "/api/v1/health",
        "/api/v1/version",
        "/api/v1/system/status",
        "/api/v1/trajectories",
        "/api/v1/trajectories/{trajectory_id}",
        "/api/v1/trajectories/{trajectory_id}/stream",
        "/api/v1/trajectories/{trajectory_id}/cancel",
        "/api/v1/trajectories/{trajectory_id}/explanation",
        "/api/v1/trajectories/{trajectory_id}/approve",
        "/api/v1/trajectories/{trajectory_id}/deny",
        "/api/v1/trajectories/{trajectory_id}/turns",
        "/api/v1/trajectories/{trajectory_id}/plan",
        "/api/v1/trajectories/{trajectory_id}/intents",
        "/api/v1/approvals",
        "/api/v1/tiers",
        "/api/v1/tools",
        "/api/v1/tools/{name}",
        "/api/v1/ledger",
        "/api/v1/ledger/entries",
        "/api/v1/egress-decisions",
    ):
        assert path in paths, path
    assert "/api/v1/settings" not in paths, "unbuilt in 1.0 — a spec §7.1 promise, recorded"


def write() -> None:
    """Regenerate the snapshot."""
    SNAPSHOT.write_text(json.dumps(current_openapi(), indent=2, sort_keys=True) + "\n")
