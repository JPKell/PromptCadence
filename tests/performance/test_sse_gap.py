"""Spec §15's tenth row — added latency per SSE event ≤ 5 ms, ceiling 20 ms — over a real socket.

An in-process test client drives the response iterator directly and cannot see the SSE loop's
poll quantisation (LoadCoach's F12 finding), so this test serves the application with uvicorn on
a loopback socket, opens ``GET /trajectories/{id}/stream``, publishes events through the served
process's own sink at a fixed cadence, and measures the client-side arrival of each frame against
the instant it was published. The added latency is that difference. Marked ``performance``.
"""

from __future__ import annotations

import statistics
import threading
import time
from datetime import UTC, datetime

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from tests.fakes.loadcoach_app import FakeLoadCoach, build_fake_app, shipped_profiles

from promptcadence.config import load_settings
from promptcadence.domain.turns import TurnStarted
from promptcadence.services.runtime import build_runtime
from promptcadence.web.app import create_app

pytestmark = pytest.mark.performance

_CADENCE_S = 0.05
_EVENTS = 60
_TARGET_MS = 5.0
_CEILING_MS = 20.0


def test_added_latency_per_sse_event_over_a_real_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    # Manual approval parks the trajectory on its bypass gate, so the worker leaves it in flight
    # and the stream stays open while this test publishes its own events onto it.
    monkeypatch.setenv("PROMPTCADENCE_APPROVAL__MODE", "manual")
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles("tools.agent.local_fast", "tools.agent.local_large"))
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        load_settings().settings,
        runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http),
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, name="promptcadence-gap-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    published: list[float] = []
    arrivals: list[float] = []
    try:
        with httpx.Client(base_url=base, timeout=30.0) as client:
            created = client.post(
                "/api/v1/trajectories", json={"task": "measure me", "bypass_planning": True}
            )
            assert created.status_code == 202, created.text
            trajectory_id = created.json()["trajectory_id"]
            settle = time.monotonic() + 5
            while time.monotonic() < settle:
                view = client.get(f"/api/v1/trajectories/{trajectory_id}").json()
                if view["state"] == "awaiting_approval":
                    break
                time.sleep(0.02)
            assert view["state"] == "awaiting_approval", view
            runtime = app.state.runtime

            def publish() -> None:
                for index in range(_EVENTS):
                    time.sleep(_CADENCE_S)
                    with runtime.sink.write() as (_session, events):
                        events.append(
                            trajectory_id,
                            TurnStarted(
                                trajectory_id=trajectory_id,
                                turn_id=f"01SYNTHETIC{index:015d}",
                                sequence=index + 1,
                                tier="local_fast",
                                task_profile="tools.agent.local_fast",
                                intent_id="01INTENT00000000000000000A",
                                intent_revision=1,
                            ),
                            now=datetime.now(UTC),
                        )
                    published.append(time.perf_counter())

            publisher = threading.Thread(target=publish, name="promptcadence-gap-publisher")
            with client.stream(
                "GET",
                f"/api/v1/trajectories/{trajectory_id}/stream",
                headers={"Accept": "text/event-stream"},
            ) as response:
                assert response.status_code == 200, response.read()
                publisher.start()
                # The worker's own events replay first; a synthetic frame is known by its turn id.
                for line in response.iter_lines():
                    if line.startswith("data:") and "01SYNTHETIC" in line:
                        arrivals.append(time.perf_counter())
                        if len(arrivals) >= _EVENTS:
                            break
            publisher.join(timeout=10)
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert len(arrivals) == _EVENTS, f"{len(arrivals)} of {_EVENTS} frames arrived"
    added_ms = sorted(
        max((arrived - sent) * 1000.0, 0.0)
        for sent, arrived in zip(published, arrivals, strict=True)
    )
    median = statistics.median(added_ms)
    p95 = added_ms[int(len(added_ms) * 0.95) - 1]
    print(  # noqa: T201 — the report
        f"\n§15 added latency per SSE event over TCP: median {median:.2f} ms, p95 {p95:.2f} ms, "
        f"max {added_ms[-1]:.2f} ms over {_EVENTS} events at {_CADENCE_S * 1000:g} ms cadence "
        f"(target {_TARGET_MS:g} ms, ceiling {_CEILING_MS:g} ms)"
    )
    assert median <= _CEILING_MS, f"added SSE latency median {median:.2f} ms exceeds the ceiling"
