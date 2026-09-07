"""The retention sweep (spec §14, P9 gate C): finished words go, the record stays, and it still
explains itself. Workspaces are content and follow content; an in-flight trajectory is never
touched; a second sweep does nothing; ``retain_content`` disables it."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from baseaicore import canonical_json
from sqlalchemy import select
from tests.fakes.harness import LoopHarness, open_harness, plan_document, step
from tests.fakes.loadcoach_app import ScriptedGeneration

from promptcadence.config import load_settings
from promptcadence.domain.explanation import CONTENT_REMOVED
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models
from promptcadence.services.retention import RetentionOutcome, scrub_content
from promptcadence.services.worker import TrajectoryWorker

_LATER = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)  # a day after the harness clock's start


def _call(name: str, arguments: str) -> dict[str, object]:
    return {"call_index": 0, "id": "c0", "name": name, "arguments_fragment": arguments}


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopHarness]:
    with open_harness(load_settings().settings) as built:
        yield built


def _finished(harness: LoopHarness) -> str:
    """A planned trajectory with a plan, a tool call that wrote a file, and a declared finish."""
    harness.script_plan(
        plan_document(step("s1", description="write the summary file", tools=["write_file"]))
    )
    harness.script(
        ScriptedGeneration(
            text="Writing.",
            tool_calls=(_call("write_file", '{"path": "summary.txt", "content": "hunter2"}'),),
        ),
        ScriptedGeneration(text="Done: the file is written."),
    )
    trajectory_id = harness.submit_planned(task="summarize ./notes into a file")
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
    assert (harness.tools.workspace_root / trajectory_id / "summary.txt").exists()
    return trajectory_id


def _sweep(harness: LoopHarness, *, now: datetime = _LATER, hours: int = 24) -> RetentionOutcome:
    controller = harness.controller()
    return scrub_content(
        harness.database,
        now=now,
        retention_hours=hours,
        tools=controller.tools,
        explanations=controller.explanations,
    )


def test_a_finished_trajectory_past_the_retention_loses_its_words_and_keeps_its_record(
    harness: LoopHarness,
) -> None:
    trajectory_id = _finished(harness)
    builder = harness.controller().explanations
    before = builder.read(trajectory_id)
    assert before.source == "materialized" and before.revision is not None

    outcome = _sweep(harness)
    assert outcome.scrubbed == (trajectory_id,)
    assert outcome.workspaces_removed == 1 and outcome.revisions_bumped == 1
    assert not (harness.tools.workspace_root / trajectory_id).exists()

    with harness.database.read() as session:
        row = session.get(models.Trajectory, trajectory_id)
        assert row is not None and row.content_scrubbed_at is not None
        assert row.task == CONTENT_REMOVED
        turns = list(
            session.execute(
                select(models.Turn).where(models.Turn.trajectory_id == trajectory_id)
            ).scalars()
        )
        assert turns and all(t.content_text is None and t.tool_calls_json is None for t in turns)
        assert all(t.content_hash for t in turns), "the digest outlives the words"
        assert all(t.model_canonical_id for t in turns if t.role == "assistant")
        plan = (
            session.execute(select(models.Plan).where(models.Plan.trajectory_id == trajectory_id))
            .scalars()
            .one()
        )
        assert plan.raw_document is None and plan.document_sha256
        assert all(s["description"] == CONTENT_REMOVED for s in plan.validated_json["steps"])
        steps = list(session.execute(select(models.PlanStep)).scalars())
        assert steps and all(s.description == CONTENT_REMOVED for s in steps)
        assert all(s.tools_json == ["write_file"] for s in steps), "the declaration stays"
        records = list(
            session.execute(
                select(models.ToolCallRecord).where(
                    models.ToolCallRecord.trajectory_id == trajectory_id
                )
            ).scalars()
        )
        assert records
        for record in records:
            assert record.args_json is None and record.result_summary is None
            assert record.reason_detail is None
            assert record.args_sha256 and record.result_sha256 and record.status
        assert "hunter2" not in json.dumps(
            [dict(e.data_json) for e in session.execute(select(models.Event)).scalars()]
        )

    # And it still explains itself: a new revision, composed from the scrubbed rows.
    after = builder.read(trajectory_id)
    assert after.source == "materialized" and after.revision is not None
    assert after.revision.revision == before.revision.revision + 1
    assert after.revision.cause == "retention_scrub"
    assert canonical_json(after.document) == canonical_json(builder.compose_live(trajectory_id))
    text = canonical_json(after.document)
    assert "hunter2" not in text and "write the summary file" not in text
    turns_doc = [t for th in after.document["threads"] for t in th["turns"]]
    assert all(t["content"]["removed"] for t in turns_doc)
    assert after.document["ledger_entries"] and after.document["egress_decisions"]
    assert after.document["tool_calls"][0]["arguments"]["removed"] is True


def test_a_second_sweep_does_nothing_and_a_young_trajectory_is_left_alone(
    harness: LoopHarness,
) -> None:
    trajectory_id = _finished(harness)
    # Inside the window: nothing selected, the file is still there.
    young = _sweep(harness, now=harness.clock.now + timedelta(hours=1))
    assert young.scrubbed == ()
    assert (harness.tools.workspace_root / trajectory_id / "summary.txt").exists()
    first = _sweep(harness)
    assert first.scrubbed == (trajectory_id,)
    second = _sweep(harness)
    assert second.scrubbed == () and second.revisions_bumped == 0
    builder = harness.controller().explanations
    revision = builder.read(trajectory_id).revision
    assert revision is not None and revision.revision == 2, "no duplicate revision"


def test_an_in_flight_trajectory_is_never_swept_whatever_its_age(harness: LoopHarness) -> None:
    """A parked trajectory's workspace is what an operator reads while deciding; it stays."""
    # Turn 1 writes the file; turn 2 asks for more tools and reaches ``max_turns`` with no
    # declared finish, which is the ``turn_overrun`` drift: the trajectory parks (lifecycle §5).
    harness.script(
        ScriptedGeneration(
            text="", tool_calls=(_call("write_file", '{"path": "wip.txt", "content": "x"}'),)
        ),
        ScriptedGeneration(text="", tool_calls=(_call("list_dir", '{"path": "."}'),)),
    )
    trajectory_id = harness.submit_bypass(task="keep going", max_turns=2)
    state = harness.claim_and_run(trajectory_id)
    assert state is TrajectoryState.AWAITING_APPROVAL, state
    assert (harness.tools.workspace_root / trajectory_id / "wip.txt").exists()
    outcome = _sweep(harness, now=_LATER + timedelta(days=30))
    assert outcome.scrubbed == ()
    assert (harness.tools.workspace_root / trajectory_id / "wip.txt").exists()
    with harness.database.read() as session:
        row = session.get(models.Trajectory, trajectory_id)
        assert row is not None and row.task == "keep going" and row.content_scrubbed_at is None


def test_retain_content_disables_the_sweep_from_the_worker(
    harness: LoopHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    trajectory_id = _finished(harness)
    monkeypatch.setenv("PROMPTCADENCE_STORAGE__RETAIN_CONTENT", "true")
    retaining = TrajectoryWorker(
        database=harness.database,
        sink=harness.sink,
        loadcoach=harness.loadcoach,
        settings=load_settings().settings,
        tools=harness.tools,
        budget=harness.budget,
        egress=harness.egress,
    )
    assert retaining.sweep_retention(harness.controller(), _LATER) is None
    sweeping = TrajectoryWorker(
        database=harness.database,
        sink=harness.sink,
        loadcoach=harness.loadcoach,
        settings=harness.settings,
        tools=harness.tools,
        budget=harness.budget,
        egress=harness.egress,
    )
    outcome = sweeping.sweep_retention(harness.controller(), _LATER)
    assert outcome is not None and outcome.scrubbed == (trajectory_id,)
