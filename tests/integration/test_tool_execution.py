"""Spec §18's security rows at the tool layer, and Phase 4's acceptance criterion.

Five rows exist at this layer — unlisted tool, path escape, symlink escape, a refusal fed back as
a structured ``TOOL`` turn with the trajectory continuing, and size caps — and each is its own
test. Then a scripted multi-tool journey against the fake, and the hostile scripted model the
plan's acceptance criterion 1 names: it requests unlisted tools, escapes paths and produces huge
output, and it must end ``completed`` or ``halted`` with every call recorded and **no exception
crossing the loop**.

The last claim is asserted rather than observed. A test that merely reached its final assertion
would have proved that no exception escaped *this* run; the harness here installs a trap around
:meth:`~promptcadence.services.loop.LoopController.run`, so an escape is a named failure with the
exception in the message.

Every test in this file runs with **no LoadCoach, no Ollama, no GPU and no network**, and with an
isolation ladder that has been shown no runtime — so ``run_command`` refuses with
``isolation_unavailable`` on any machine, and the suite does not become a question about whether
the developer has docker.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from baseaicore import sha256_of
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import budget_and_estimator
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    ScriptedGeneration,
    build_fake_app,
    shipped_profiles,
)
from toolyard import TieredSandbox
from weightsdb import MigrationRunner
from weightsdb.testing import temporary_sqlite

from promptcadence.config import Settings, load_settings
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models
from promptcadence.infrastructure.loadcoach import LoadCoachClient
from promptcadence.services.database import MIGRATIONS_LOCATION, Database
from promptcadence.services.events import TrajectoryEventSink
from promptcadence.services.loop import LoopController
from promptcadence.services.tools import ToolPlant
from promptcadence.services.trajectories import TrajectoryService, TrajectorySubmission

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def call(name: str, arguments: str, *, index: int = 0) -> dict[str, object]:
    """One tool-call fragment in LoadCoach's wire shape.

    LoadCoach forwards what the provider streamed — ``call_index``, ``id``, ``name`` and an
    ``arguments_fragment`` — so a test that handed the loop an already-assembled call would be
    testing a wire that does not exist.
    """
    return {
        "call_index": index,
        "id": f"c{index}",
        "name": name,
        "arguments_fragment": arguments,
    }


class Harness:
    """One database, one fake LoadCoach, one plant with no isolation rung."""

    def __init__(self, settings: Settings, database: Database, fake: FakeLoadCoach) -> None:
        self.settings = settings
        self.database = database
        self.fake = fake
        ticks = iter(range(100_000))
        self.clock = lambda: _NOW + timedelta(milliseconds=next(ticks))
        self.sink = TrajectoryEventSink(database, clock=self.clock)
        self.budget, self.estimator = budget_and_estimator(database, settings, clock=self.clock)
        self.service = TrajectoryService(
            database, self.sink, settings, budget=self.budget, clock=self.clock
        )
        self.loadcoach = LoadCoachClient(
            TestClient(build_fake_app(fake), base_url="http://loadcoach.test")
        )
        self.tools = ToolPlant(settings, sandbox=TieredSandbox(which=lambda _name: None))
        self.escaped: BaseException | None = None

    def run(self, **overrides: object) -> tuple[str, TrajectoryState]:
        """Submit, claim and run one trajectory, trapping anything that escapes the loop."""
        fields: dict[str, object] = {"task": "work in the workspace", "bypass_planning": True}
        fields.update(overrides)
        trajectory_id = self.service.submit(
            TrajectorySubmission(**fields)  # type: ignore[arg-type]
        ).trajectory_id
        controller = LoopController(
            budget=self.budget,
            estimator=self.estimator,
            database=self.database,
            sink=self.sink,
            loadcoach=self.loadcoach,
            settings=self.settings,
            owner="host:1/0",
            clock=self.clock,
            tools=self.tools,
        )
        assert controller.claim(trajectory_id) is TrajectoryState.EXECUTING
        try:
            return trajectory_id, controller.run(trajectory_id)
        except BaseException as exc:  # noqa: BLE001 — the assertion this file exists to make
            self.escaped = exc
            pytest.fail(f"an exception crossed the loop: {type(exc).__name__}: {exc}")

    def workspace(self, trajectory_id: str) -> Path:
        return self.tools.workspace_root / trajectory_id

    def records(self, trajectory_id: str) -> list[models.ToolCallRecord]:
        with self.database.read() as session:
            return list(
                session.execute(
                    select(models.ToolCallRecord)
                    .where(models.ToolCallRecord.trajectory_id == trajectory_id)
                    .order_by(models.ToolCallRecord.id)
                ).scalars()
            )

    def tool_turns(self, trajectory_id: str) -> list[str]:
        return [
            t.turn.content or ""
            for t in self.service.turns(trajectory_id)
            if t.turn.role.value == "tool"
        ]

    def events(self, trajectory_id: str) -> list[str]:
        return [event.event_type for event in self.service.events(trajectory_id)]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "5")
    settings = load_settings().settings
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles("tools.agent.local_fast", "tools.agent.local_large"))
    with temporary_sqlite() as engine:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        yield Harness(settings, Database(engine), fake)


# --------------------------------------------------------------------------------------------
# Spec §18, security: the five rows that exist at this layer
# --------------------------------------------------------------------------------------------


def test_an_unlisted_tool_is_refused_as_unknown_and_never_reaches_a_handler(
    harness: Harness,
) -> None:
    """Row 1. A name no registration ever produced cannot be in the registry, so it is refused
    first — before the allowlist is even consulted (ADR-0053 decision 3's fixed order)."""
    harness.fake.script(
        ScriptedGeneration(text="", tool_calls=(call("exfiltrate", "{}"),)),
        ScriptedGeneration(text="No such tool; answering directly."),
    )
    trajectory_id, state = harness.run()
    assert state is TrajectoryState.COMPLETED
    (record,) = harness.records(trajectory_id)
    assert record.tool_name == "exfiltrate"
    assert record.status == "refused"
    assert record.reason == "unknown_tool"
    assert "unknown_tool" in harness.tool_turns(trajectory_id)[0]


def test_a_path_escape_is_refused_and_nothing_outside_the_workspace_is_read(
    harness: Harness, tmp_path: Path
) -> None:
    """Row 2. ``../`` out of the workspace resolves outside its root, which is a containment
    refusal, and the model is told the check that failed and not where the root is."""
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me", encoding="utf-8")
    harness.fake.script(
        ScriptedGeneration(
            text="", tool_calls=(call("read_file", '{"path": "../../../../etc/passwd"}'),)
        ),
        ScriptedGeneration(text="Refused; answering directly."),
    )
    trajectory_id, state = harness.run()
    assert state is TrajectoryState.COMPLETED
    (record,) = harness.records(trajectory_id)
    assert record.status == "refused"
    assert record.reason == "path_escape"
    shown = harness.tool_turns(trajectory_id)[0]
    # The model is told the check that failed and its own argument back; it is never told where the
    # root is, because refusal text is part of the prompt surface (ADR-0053's last consequence).
    assert "path_escape" in shown
    assert str(harness.tools.workspace_root) not in shown
    assert secret.read_text() not in shown


def test_a_symlink_out_of_the_workspace_is_refused_after_resolution(
    harness: Harness, tmp_path: Path
) -> None:
    """Row 3. Containment resolves every symlink *before* it checks, so a link whose name is
    inside the workspace and whose target is not is refused on the target."""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside the workspace", encoding="utf-8")
    harness.fake.script(
        ScriptedGeneration(text="", tool_calls=(call("read_file", '{"path": "link.txt"}'),)),
        ScriptedGeneration(text="Refused; answering directly."),
    )
    # The link is planted in the workspace the trajectory will get, before the loop runs.
    submitted = harness.service.submit(
        TrajectorySubmission(task="read the link", bypass_planning=True)
    ).trajectory_id
    workspace = harness.workspace(submitted)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "link.txt").symlink_to(outside)
    controller = LoopController(
        budget=harness.budget,
        estimator=harness.estimator,
        database=harness.database,
        sink=harness.sink,
        loadcoach=harness.loadcoach,
        settings=harness.settings,
        owner="host:1/0",
        clock=harness.clock,
        tools=harness.tools,
    )
    assert controller.claim(submitted) is TrajectoryState.EXECUTING
    assert controller.run(submitted) is TrajectoryState.COMPLETED
    (record,) = harness.records(submitted)
    assert record.status == "refused"
    assert record.reason == "path_escape"
    assert "outside the workspace" not in harness.tool_turns(submitted)[0]


def test_a_refusal_is_fed_back_as_a_structured_tool_turn_and_the_trajectory_continues(
    harness: Harness,
) -> None:
    """Row 4, and ADR-0053's whole point.

    The refused call becomes a ``TOOL`` turn naming the call it answers, the transcript carries it
    to the next request, and the model answers. A refusal that ended the trajectory would hand the
    model a stop condition: ask for a tool that does not exist, and the run stops.
    """
    harness.fake.script(
        ScriptedGeneration(text="", tool_calls=(call("teleport", "{}"),)),
        ScriptedGeneration(text="Understood, I will not use that tool."),
    )
    trajectory_id, state = harness.run()
    assert state is TrajectoryState.COMPLETED
    turns = harness.service.turns(trajectory_id)
    assert [t.turn.role.value for t in turns] == ["user", "assistant", "tool", "assistant"]
    tool_turn = turns[2].turn
    (record,) = harness.records(trajectory_id)
    assert tool_turn.tool_call_id == record.invocation_id
    assert record.tool_turn_id == tool_turn.turn_id
    # The refusal reached the model: the second request's transcript carries the TOOL turn.
    second = list(harness.fake.jobs.values())[-1]
    roles = [message["role"] for message in second.request_body["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert second.request_body["messages"][-1]["tool_call_id"] == record.invocation_id
    assert harness.events(trajectory_id).count("tool.call.started") == 1
    assert harness.events(trajectory_id).count("tool.call.completed") == 1


def test_an_oversize_result_is_capped_for_the_model_and_kept_whole_as_an_artifact(
    harness: Harness,
) -> None:
    """Row 5. Two caps, and the record names the artifact rather than inlining a prefix.

    The model sees a labelled truncation; the whole output is filed under the digest the record
    already carries, so ``result_sha256`` is checkable against the bytes on disk rather than being
    a digest of something nobody kept.
    """
    body = "x" * 40_000
    harness.fake.script(
        ScriptedGeneration(
            text="", tool_calls=(call("write_file", f'{{"path": "big.txt", "content": "{body}"}}'),)
        ),
        ScriptedGeneration(text="", tool_calls=(call("read_file", '{"path": "big.txt"}'),)),
        ScriptedGeneration(text="Done."),
    )
    trajectory_id, state = harness.run()
    assert state is TrajectoryState.COMPLETED
    read_record = harness.records(trajectory_id)[1]
    assert read_record.tool_name == "read_file"
    assert read_record.status == "ok"
    assert read_record.output_truncated is True
    assert read_record.artifact_ref == read_record.result_sha256
    shown = harness.tool_turns(trajectory_id)[1]
    limit = harness.settings.tools.max_result_chars
    assert len(shown) < len(body)
    assert f"truncated by promptcadence: {limit} of" in shown
    assert read_record.result_sha256 in shown
    stored = harness.tools.artifacts.path_for(read_record.artifact_ref).read_text(encoding="utf-8")
    assert body in stored
    # The artifact's bytes reproduce the digest the record carries, so `result_sha256` names
    # something that can be re-read rather than something nobody kept.
    assert sha256_of(stored) == read_record.result_sha256


# --------------------------------------------------------------------------------------------
# The journeys
# --------------------------------------------------------------------------------------------


def test_a_multi_tool_journey_writes_reads_and_lists_then_finishes(harness: Harness) -> None:
    """Three tools over three round trips against the fake, then a declared stop."""
    harness.fake.script(
        ScriptedGeneration(
            text="",
            tool_calls=(call("write_file", '{"path": "notes.md", "content": "three meetings"}'),),
        ),
        ScriptedGeneration(text="", tool_calls=(call("list_dir", '{"path": "."}'),)),
        ScriptedGeneration(text="", tool_calls=(call("read_file", '{"path": "notes.md"}'),)),
        ScriptedGeneration(text="The notes describe three meetings."),
    )
    trajectory_id, state = harness.run()
    assert state is TrajectoryState.COMPLETED
    records = harness.records(trajectory_id)
    assert [r.tool_name for r in records] == ["write_file", "list_dir", "read_file"]
    assert {r.status for r in records} == {"ok"}
    # Every result here fits in its turn, so none of them is filed: an artifact holds what the
    # model was *not* shown, and `artifact_ref` populated on every row would say nothing.
    assert {r.artifact_ref for r in records} == {None}
    assert {r.output_truncated for r in records} == {False}
    assert not harness.tools.artifacts.root.exists()
    assert (harness.workspace(trajectory_id) / "notes.md").read_text() == "three meetings"
    assert "three meetings" in harness.tool_turns(trajectory_id)[2]
    roles = [t.turn.role.value for t in harness.service.turns(trajectory_id)]
    assert roles == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]


def test_a_command_is_refused_where_the_host_has_no_isolation_rung(harness: Harness) -> None:
    """ADR-0018's floor, reached from the loop: no rung means refuse, never run on the host."""
    harness.fake.script(
        ScriptedGeneration(
            text="", tool_calls=(call("run_command", '{"argv": ["/bin/echo", "hi"]}'),)
        ),
        ScriptedGeneration(text="I cannot run commands here."),
    )
    trajectory_id, state = harness.run()
    assert state is TrajectoryState.COMPLETED
    (record,) = harness.records(trajectory_id)
    assert record.status == "refused"
    assert record.reason == "isolation_unavailable"
    assert record.isolation_tier == "unavailable"


def test_the_round_trip_cap_halts_a_model_that_never_declares_a_finish(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``execution.max_turns_per_step`` bounds the round trips inside a step, and the halt names
    the cap rather than the trajectory quietly stopping when some other limit happened to bite."""
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__MAX_TURNS_PER_STEP", "2")
    settings = load_settings().settings
    bounded = Harness(settings, harness.database, harness.fake)
    bounded.fake.set_default(
        ScriptedGeneration(text="", tool_calls=(call("list_dir", '{"path": "."}'),))
    )
    trajectory_id, state = bounded.run()
    assert state is TrajectoryState.HALTED
    view = bounded.service.get(trajectory_id)
    assert view.error_code == "STEP_LIMIT_EXCEEDED"
    assert "max_turns_per_step (2)" in (view.halted_reason or "")
    assert len(bounded.records(trajectory_id)) == 2


def test_a_hostile_model_halts_or_completes_cleanly_with_every_call_recorded(
    harness: Harness,
) -> None:
    """Phase 4 acceptance criterion 1.

    Unlisted tools, escaping paths, a symlink, arguments that are not JSON, a name that is not a
    name, a huge output, and a command on a host with no isolation. The trajectory must reach a
    terminal state with **every** call recorded, and no exception may cross the loop — the harness
    fails the test by name if one does.
    """
    hostile = (
        call("exfiltrate", '{"target": "https://example.invalid"}', index=0),
        call("read_file", '{"path": "../../../../etc/shadow"}', index=1),
        call("read_file", '{"path": "/etc/hostname"}', index=2),
        call("write_file", '{"path": "../escape.txt", "content": "owned"}', index=3),
        call("read_file", "not json at all", index=4),
        call("", '{"path": "."}', index=5),
        call("A" * 500, "{}", index=6),
        call("run_command", '{"argv": ["/bin/sh", "-c", "curl evil"]}', index=7),
        call("list_dir", '{"path": "."}', index=8),
    )
    harness.fake.script(
        ScriptedGeneration(text="", tool_calls=hostile),
        ScriptedGeneration(text="Every one of those was refused; here is a plain answer."),
    )
    trajectory_id, state = harness.run()
    assert state in {TrajectoryState.COMPLETED, TrajectoryState.HALTED}
    assert harness.escaped is None
    records = harness.records(trajectory_id)
    assert len(records) == len(hostile), "every call the model made must be recorded"
    by_name = {record.tool_name: record for record in records}
    assert by_name["exfiltrate"].reason == "unknown_tool"
    assert by_name["read_file"].status in {"refused", "failed"}
    assert by_name["run_command"].reason == "isolation_unavailable"
    assert by_name["list_dir"].status == "ok"
    # Nothing escaped the workspace, and nothing outside it was created.
    assert not (harness.tools.workspace_root / "escape.txt").exists()
    # A model-chosen name is capped before it reaches a row.
    long_name = next(r for r in records if r.tool_name.startswith("AAAA"))
    assert len(long_name.tool_name) <= 128
    assert long_name.reason == "unknown_tool"
    # Every call produced a TOOL turn and a completed event, refusals included.
    assert len(harness.tool_turns(trajectory_id)) == len(hostile)
    assert harness.events(trajectory_id).count("tool.call.completed") == len(hostile)
    assert all(record.args_sha256 for record in records)


def test_redact_args_stores_the_hash_and_never_the_plaintext(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[tools] redact_args`` is about the record, not the call: the tool still runs."""
    monkeypatch.setenv("PROMPTCADENCE_TOOLS__REDACT_ARGS", "write_file")
    settings = load_settings().settings
    redacting = Harness(settings, harness.database, harness.fake)
    redacting.fake.script(
        ScriptedGeneration(
            text="",
            tool_calls=(call("write_file", '{"path": "s.txt", "content": "hunter2"}'),),
        ),
        ScriptedGeneration(text="Written."),
    )
    trajectory_id, state = redacting.run()
    assert state is TrajectoryState.COMPLETED
    (record,) = redacting.records(trajectory_id)
    assert record.status == "ok"
    assert record.args_json is None
    assert record.args_sha256
    assert (redacting.workspace(trajectory_id) / "s.txt").read_text() == "hunter2"
