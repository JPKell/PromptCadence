"""Row WM2: the MirrorWall 0.3 surfaces this application opted into (design brief §6).

The tab strip only when ``[console] url`` is set, the status dot on the System and Tiers pages,
the dense trajectory list, and a running trajectory's log pane fed by
``/api/v1/trajectories/{id}/log`` — the same events as ``/stream``, as ``log`` frames closed with
``log.closed``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.e2e.test_bypass_journey import _wait_terminal, fake  # noqa: F401 — a fixture
from tests.fakes.loadcoach_app import FakeLoadCoach, build_fake_app

from promptcadence.config import load_settings
from promptcadence.services.runtime import build_runtime
from promptcadence.web.app import create_app

CONSOLE = "https://jordan-main.local:8769"


def _client(loadcoach: FakeLoadCoach) -> TestClient:
    loadcoach_http = TestClient(build_fake_app(loadcoach), base_url="http://loadcoach.fake")
    app = create_app(
        load_settings().settings,
        runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http),
    )
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def client(fake: FakeLoadCoach, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:  # noqa: F811
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "2")
    with _client(fake) as running:
        yield running


def test_no_tab_strip_without_a_console_url(client: TestClient) -> None:
    page = client.get("/system").text
    assert 'class="app-tabs"' not in page and 'class="app-tab"' not in page


def test_the_tab_strip_links_the_console_and_the_peers_through_it(
    fake: FakeLoadCoach,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPTCADENCE_CONSOLE__URL", CONSOLE + "/")
    with _client(fake) as client:
        page = client.get("/system").text
    assert f'<a href="{CONSOLE}" class="app-tab">' in page
    assert '<a href="/" class="app-tab" aria-current="page">' in page
    for peer in ("freeweight", "loadcoach", "ideapress"):
        assert f'<a href="{CONSOLE}/apps/{peer}" class="app-tab">' in page


def test_status_dots_on_the_system_and_tiers_pages(client: TestClient) -> None:
    system = client.get("/system").text
    components = client.get("/api/v1/health").json()["components"]
    assert system.count('class="status-dot"') == len(components)
    tiers = client.get("/tiers").text
    assert tiers.count('class="status-dot"') >= 1


def test_the_trajectory_list_is_dense(client: TestClient) -> None:
    client.post("/api/v1/trajectories", json={"task": "t", "bypass_planning": True})
    assert 'data-density="dense"' in client.get("/trajectories").text


def test_the_trajectory_log_is_the_stream_as_log_frames(client: TestClient) -> None:
    trajectory_id = client.post(
        "/api/v1/trajectories", json={"task": "t", "bypass_planning": True}
    ).json()["trajectory_id"]
    view = _wait_terminal(client, trajectory_id)
    assert view["state"] == "completed", view
    body = ""
    with client.stream("GET", f"/api/v1/trajectories/{trajectory_id}/log") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        for chunk in response.iter_text():
            body += chunk
            if "event: log.closed" in body:
                break
    assert 'event: log\ndata: <div class="log-pane-line" data-level=' in body
    assert "trajectory.created" in body and "trajectory.completed" in body
    assert "egress.evaluated" in body
    assert body.rstrip().endswith("event: log.closed\ndata: {}")
    # A finished trajectory's page carries the record, not a pane, and no htmx.
    page = client.get(f"/trajectories/{trajectory_id}").text
    assert "data-log-pane" not in page and "vendor/htmx" not in page
