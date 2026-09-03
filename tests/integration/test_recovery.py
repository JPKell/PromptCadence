"""Recovery after a real ``kill -9`` (lifecycle §8.3, ADR-0036, spec §20 #9), no simulation.

Two child processes die with SIGKILL at the two places a turn can be lost:

* **in flight** — the child is blocked inside ``POST /generate`` (the fake holds the job). The
  recovering process finds the dangling ``turn.started``, finds the job by its idempotency key,
  cancels it, resumes, and the next turn completes. One assistant turn, no orphaned job.
* **after the response, before the commit** — the child received a completed job and killed
  itself before writing the turn. The recovering process finds the completed job by its key and
  reconciles it into the turn row. One assistant turn, one job, no second execution.

The fake LoadCoach is served by uvicorn on a loopback port from the test process, so it survives
the child and the test can inspect and assert on its jobs directly. The child talks to it over a
real socket, as the served application would.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import select
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    ScriptedGeneration,
    build_fake_app,
    text_profile,
)

from promptcadence.config import load_settings
from promptcadence.domain.errors import ErrorCode
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.domain.turns import TurnStarted
from promptcadence.infrastructure.db import models
from promptcadence.infrastructure.loadcoach import LoadCoachClient
from promptcadence.services.database import Database, ensure_ready
from promptcadence.services.events import TrajectoryEventSink
from promptcadence.services.loop import LoopController, ReconcileOutcome
from promptcadence.services.trajectories import TrajectoryService, TrajectorySubmission
from promptcadence.services.worker import RecoverySummary, recover

_CHILD = r"""
import os, signal, sys, threading
from datetime import UTC, datetime
from promptcadence.config import load_settings
from promptcadence.infrastructure.loadcoach import LoadCoachClient
from promptcadence.services.database import Database, ensure_ready
from promptcadence.services.events import TrajectoryEventSink
from promptcadence.services.loop import LoopController, RunSignals
from promptcadence.services.trajectories import TrajectoryService, TrajectorySubmission
from promptcadence.services.worker import LeaseKeeper

mode = sys.argv[1]
settings = load_settings().settings
database = Database.from_url(settings.storage.database_url)
ensure_ready(database, auto_migrate=True)
sink = TrajectoryEventSink(database)
service = TrajectoryService(database, sink, settings)
submission = TrajectorySubmission(task="reconcile me", bypass_planning=True)
trajectory_id = service.submit(submission).trajectory_id


class DiesAfterResponse(LoadCoachClient):
    # The real client, except that the process is killed -9 the instant an answer arrives.

    def generate(self, request):
        response = super().generate(request)
        sys.stdout.write(f"RESPONDED {response.job_id}\n"); sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)  # a real kill -9, before the turn is committed
        return response  # pragma: no cover - never reached


client_class = DiesAfterResponse if mode == "after_response" else LoadCoachClient
loadcoach = client_class.from_settings(base_url=settings.loadcoach.base_url, timeout_seconds=600)
controller = LoopController(
    database=database, sink=sink, loadcoach=loadcoach, settings=settings, owner="child:1/0"
)
assert controller.claim(trajectory_id) is not None
print(f"READY {trajectory_id}", flush=True)
signals = RunSignals.fresh()
keeper = LeaseKeeper(controller, trajectory_id, interval_seconds=0.5, signals=signals)
keeper.start()
controller.run(trajectory_id, signals=signals)
keeper.stop()
print("DONE", flush=True)
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def served_fake() -> Iterator[tuple[FakeLoadCoach, str]]:
    """The fake LoadCoach on a real loopback socket, outliving any child process."""
    fake = FakeLoadCoach()
    fake.register_profile(text_profile("tools.agent.local_fast"))
    fake.register_profile(text_profile("tools.agent.local_large"))
    fake.set_default(ScriptedGeneration(text="recovered"))
    port = _free_port()
    config = uvicorn.Config(build_fake_app(fake), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="fake-loadcoach", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/api/v1/version", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover — the fake never came up
        pytest.fail("the fake LoadCoach did not start")
    yield fake, base_url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def environment(
    tmp_path: Path, served_fake: tuple[FakeLoadCoach, str], monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """The configuration both the child and the recovering process run under."""
    _, base_url = served_fake
    values = {
        "PROMPTCADENCE_STORAGE__DATABASE_URL": f"sqlite:///{tmp_path / 'shared.sqlite3'}",
        "PROMPTCADENCE_LOADCOACH__BASE_URL": base_url,
        "PROMPTCADENCE_EXECUTION__LEASE_SECONDS": "2",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return {**os.environ, **values}


def _spawn(environment: dict[str, str], mode: str) -> tuple[subprocess.Popen[str], str]:
    child = subprocess.Popen(  # noqa: S603 — our own interpreter, our own script
        [sys.executable, "-c", _CHILD, mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert child.stdout is not None
    line = child.stdout.readline().strip()
    if not line.startswith("READY "):
        child.kill()
        _, err = child.communicate(timeout=10)
        pytest.fail(f"child never claimed: {line!r}\n{err}")
    return child, line.split(" ", 1)[1]


def _kill_minus_nine(child: subprocess.Popen[str]) -> None:
    os.kill(child.pid, signal.SIGKILL)
    child.wait(timeout=10)
    assert child.returncode == -signal.SIGKILL


class Recoverer:
    """The surviving process's handles over the shared database and the served fake."""

    def __init__(self) -> None:
        self.settings = load_settings().settings
        self.database = Database.from_url(self.settings.storage.database_url or "")
        ensure_ready(self.database, auto_migrate=True)
        self.sink = TrajectoryEventSink(self.database)
        self.service = TrajectoryService(self.database, self.sink, self.settings)
        self.loadcoach = LoadCoachClient.from_settings(
            base_url=self.settings.loadcoach.base_url, timeout_seconds=30
        )
        self.controller = LoopController(
            database=self.database,
            sink=self.sink,
            loadcoach=self.loadcoach,
            settings=self.settings,
            owner="parent:2/recovery",
        )

    def recover(self) -> RecoverySummary:
        return recover(
            self.controller,
            self.database,
            owner_prefix="parent:2",
            now=datetime.now(UTC),
            only_expired=False,
        )

    def events(self, trajectory_id: str) -> list[dict[str, Any]]:
        return [e.as_json() for e in self.service.events(trajectory_id)]


def test_kill_minus_nine_with_a_turn_in_flight_cancels_the_orphan_and_resumes(
    environment: dict[str, str], served_fake: tuple[FakeLoadCoach, str]
) -> None:
    fake, _ = served_fake
    hold = threading.Event()
    fake.script(ScriptedGeneration(hold=hold, text="never delivered"))
    child, trajectory_id = _spawn(environment, "in_flight")
    deadline = time.monotonic() + 10
    while not fake.in_flight() and time.monotonic() < deadline:
        time.sleep(0.02)
    (orphan,) = fake.in_flight()
    _kill_minus_nine(child)

    recoverer = Recoverer()
    before = recoverer.service.get(trajectory_id)
    assert before.state is TrajectoryState.EXECUTING
    assert before.lease_owner == "child:1/0"

    summary = recoverer.recover()
    assert summary.resumed == (trajectory_id,)
    assert orphan.state in {"cancelling", "cancelled"}
    assert orphan.cancel_requested is True
    hold.set()  # release the fake's handler, as a real cancel would; it must stay cancelled
    time.sleep(0.1)
    assert fake.jobs[orphan.job_id].state == "cancelled"

    final = recoverer.controller.run(trajectory_id)
    assert final is TrajectoryState.COMPLETED

    turns = recoverer.service.turns(trajectory_id)
    assert [t.turn.role.value for t in turns] == ["user", "assistant"], "no duplicate turn"
    assert turns[1].loadcoach_job_id != orphan.job_id
    assert not fake.in_flight(), "no orphaned job"
    assert len(fake.jobs) == 2  # the cancelled one and the one that completed the trajectory
    types = [e["event_type"] for e in recoverer.events(trajectory_id)]
    assert types == [
        "trajectory.created",
        "trajectory.claimed",
        "intent.minted",
        "turn.started",
        "trajectory.recovered",
        "turn.started",
        "turn.completed",
        "trajectory.completed",
    ]
    recovered = recoverer.events(trajectory_id)[4]["data"]
    assert recovered["outcome"] == f"cancelled_in_flight_job:{orphan.job_id}"


def test_kill_minus_nine_after_the_response_reconciles_the_completed_job_without_a_second_one(
    environment: dict[str, str], served_fake: tuple[FakeLoadCoach, str]
) -> None:
    fake, _ = served_fake
    child, trajectory_id = _spawn(environment, "after_response")
    assert child.stdout is not None
    responded = child.stdout.readline().strip()
    assert responded.startswith("RESPONDED ")
    job_id = responded.split(" ", 1)[1]
    child.wait(timeout=10)
    assert child.returncode == -signal.SIGKILL

    recoverer = Recoverer()
    assert recoverer.service.turns(trajectory_id)[-1].turn.role.value == "user"  # uncommitted
    summary = recoverer.recover()
    assert summary.finished == (trajectory_id,)

    # The turn is reconciled — one job, one assistant turn, the job's own output — and the
    # trajectory then ends on that turn's verdict, read from the job document exactly as it
    # would have been read from the response: since LoadCoach 846348b the document
    # carries `output.finish_reason` and the validation checks, so the declared `stop`
    # completes the trajectory (spec §11 contract 6) — no halt naming the reconciliation.
    final = recoverer.service.get(trajectory_id)
    assert final.state is TrajectoryState.COMPLETED, final.halted_reason
    assert final.error_code is None
    turns = recoverer.service.turns(trajectory_id)
    assert [t.turn.role.value for t in turns] == ["user", "assistant"]
    assert turns[1].loadcoach_job_id == job_id
    assert turns[1].turn.content == "recovered"
    assert turns[1].turn.finish_reason is not None
    assert turns[1].turn.finish_reason.value == "stop"
    assert len(fake.jobs) == 1, "no second execution"
    assert len(fake.jobs_with_key(turns[1].turn.turn_id)) == 1
    assert not fake.in_flight(), "no orphaned job"
    types = [e["event_type"] for e in recoverer.events(trajectory_id)]
    assert types[-3:] == ["turn.completed", "trajectory.recovered", "trajectory.completed"]
    reconciled = recoverer.events(trajectory_id)[-2]["data"]
    assert reconciled["outcome"] == f"reconciled_completed_job:{job_id}"


def test_an_unreconcilable_turn_halts_recovered_after_crash(
    environment: dict[str, str], served_fake: tuple[FakeLoadCoach, str]
) -> None:
    """A dangling turn.started whose key no LoadCoach job holds: halted, with the cause."""
    recoverer = Recoverer()
    trajectory_id = recoverer.service.submit(
        TrajectorySubmission(task="lost", bypass_planning=True)
    ).trajectory_id
    stale = LoopController(
        database=recoverer.database,
        sink=recoverer.sink,
        loadcoach=recoverer.loadcoach,
        settings=recoverer.settings,
        owner="ghost:9/0",
    )
    assert stale.claim(trajectory_id) is TrajectoryState.EXECUTING
    with recoverer.sink.write() as (_, events):
        events.append(
            trajectory_id,
            TurnStarted(
                trajectory_id=trajectory_id,
                turn_id="01TURNTHATNEVERREACHEDLC00",
                sequence=2,
                tier="local_fast",
                task_profile="tools.agent.local_fast",
                intent_id="i",
                intent_revision=1,
            ),
        )
    summary = recoverer.recover()
    assert summary.halted == (trajectory_id,)
    view = recoverer.service.get(trajectory_id)
    assert view.state is TrajectoryState.HALTED
    assert (view.halted_reason or "").startswith("recovered_after_crash")
    assert view.error_code == ErrorCode.LOADCOACH_ERROR.value
    assert recoverer.recover().touched == 0  # idempotent


def test_recovery_is_deferred_when_loadcoach_is_unreachable(
    environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    recoverer = Recoverer()
    trajectory_id = recoverer.service.submit(
        TrajectorySubmission(task="lost", bypass_planning=True)
    ).trajectory_id
    stale = LoopController(
        database=recoverer.database,
        sink=recoverer.sink,
        loadcoach=recoverer.loadcoach,
        settings=recoverer.settings,
        owner="ghost:9/0",
    )
    stale.claim(trajectory_id)
    with recoverer.sink.write() as (_, events):
        events.append(
            trajectory_id,
            TurnStarted(
                trajectory_id=trajectory_id,
                turn_id="01TURNTHATNEVERREACHEDLC01",
                sequence=2,
                tier="local_fast",
                task_profile="tools.agent.local_fast",
                intent_id="i",
                intent_revision=1,
            ),
        )
    unreachable = LoopController(
        database=recoverer.database,
        sink=recoverer.sink,
        loadcoach=LoadCoachClient(httpx.Client(base_url="http://127.0.0.1:9", timeout=0.2)),
        settings=recoverer.settings,
        owner="parent:2/recovery",
    )
    assert unreachable.reconcile(trajectory_id, require_expired=False) is ReconcileOutcome.DEFERRED
    assert recoverer.service.get(trajectory_id).state is TrajectoryState.EXECUTING
    assert recoverer.service.get(trajectory_id).lease_owner == "ghost:9/0"


def test_the_reaper_takes_over_only_an_expired_lease(environment: dict[str, str]) -> None:
    recoverer = Recoverer()
    trajectory_id = recoverer.service.submit(
        TrajectorySubmission(task="live", bypass_planning=True)
    ).trajectory_id
    live = LoopController(
        database=recoverer.database,
        sink=recoverer.sink,
        loadcoach=recoverer.loadcoach,
        settings=recoverer.settings,
        owner="parent:2/0",
    )
    live.claim(trajectory_id)
    assert (
        recover(
            recoverer.controller,
            recoverer.database,
            owner_prefix="parent:2",
            now=datetime.now(UTC),
            only_expired=True,
        ).touched
        == 0
    )
    with recoverer.database.write() as session:
        row = session.execute(
            select(models.Trajectory).where(models.Trajectory.id == trajectory_id)
        ).scalar_one()
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    summary = recover(
        recoverer.controller,
        recoverer.database,
        owner_prefix="parent:2",
        now=datetime.now(UTC),
        only_expired=True,
    )
    assert summary.resumed == (trajectory_id,)
    assert recoverer.service.get(trajectory_id).lease_owner == "parent:2/recovery"
