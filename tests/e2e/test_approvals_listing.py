"""``GET /approvals?status=all`` with no trajectory: every request, newest first, paged (row WPC1).

Asked for by WeightRoomGym, whose Approvals history read PromptCadence's database even while it ran
because this listing did not exist. The existing listings — pending, and per trajectory — keep their
answers, and the tests below hold that too.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.e2e.test_approval_surfaces import _PROFILES, _serve, _wait
from tests.fakes.loadcoach_app import FakeLoadCoach, ScriptedGeneration, shipped_profiles


@pytest.fixture
def manual(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles(*_PROFILES))
    fake.set_default(ScriptedGeneration(text="three meetings"))
    monkeypatch.setenv("PROMPTCADENCE_APPROVAL__MODE", "manual")
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "2")
    with _serve(fake) as client:
        yield client


def _awaiting(client: TestClient) -> str:
    trajectory_id = str(
        client.post("/api/v1/trajectories", json={"task": "t", "bypass_planning": True}).json()[
            "trajectory_id"
        ]
    )
    assert _wait(client, trajectory_id, {"awaiting_approval"})["state"] == "awaiting_approval"
    return trajectory_id


def _approvals(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get("/api/v1/approvals", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_every_request_newest_first_paged_while_the_old_listings_keep_their_answers(
    manual: TestClient,
) -> None:
    first = _awaiting(manual)
    denied = manual.post(f"/api/v1/trajectories/{first}/deny", json={"reason": "not today"})
    assert denied.status_code == 200, denied.text
    second = _awaiting(manual)
    third = _awaiting(manual)

    everything = _approvals(manual, status="all")["items"]
    assert [row["trajectory_id"] for row in everything] == [third, second, first]
    assert [row["status"] for row in everything] == ["pending", "pending", "denied"]

    paged: list[str] = []
    cursor: str | None = None
    while True:
        body = _approvals(manual, status="all", limit=1, **({"cursor": cursor} if cursor else {}))
        assert body["page"]["limit"] == 1
        paged.extend(row["trajectory_id"] for row in body["items"])
        cursor = body["page"]["next_cursor"]
        assert body["page"]["has_more"] is (cursor is not None)
        if cursor is None:
            break
    assert paged == [third, second, first]

    # Unchanged: pending oldest first, and one trajectory's requests whatever became of them.
    assert [row["trajectory_id"] for row in _approvals(manual)["items"]] == [second, third]
    per_trajectory = _approvals(manual, status="all", trajectory_id=first)["items"]
    assert [(row["trajectory_id"], row["status"]) for row in per_trajectory] == [(first, "denied")]


def test_the_limit_is_clamped_to_200_and_a_forged_cursor_is_refused(manual: TestClient) -> None:
    assert _approvals(manual, status="all", limit=5000)["page"]["limit"] == 200
    response = manual.get("/api/v1/approvals", params={"status": "all", "cursor": "forged"})
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "cursor"
