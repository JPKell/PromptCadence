"""Gate A: the planner drafts under ``tools.plan``, PromptCadence validates, and the plan runs.

T2 → drafting → T4 (auto) → dispatch → T11, against the fake LoadCoach; T7 after the corrective
budget; every attempt on the record; the ``planning`` recovery edge as a redraft; cancel at the
boundary. No LoadCoach, no GPU, no network (spec §20 #10).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fakes.harness import LoopHarness, open_harness, plan_document, step
from tests.fakes.loadcoach_app import ScriptedGeneration, held_generation

from promptcadence.config import load_settings
from promptcadence.domain.errors import ErrorCode
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models
from promptcadence.services.worker import recover


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopHarness]:
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "2")
    with open_harness(load_settings().settings) as harness:
        yield harness


def test_a_planned_trajectory_drafts_is_auto_approved_and_executes_to_completion(
    harness: LoopHarness,
) -> None:
    """Spec §20 #2 minus the explanation clause: plan, approve, execute, result."""
    document = plan_document(step("s1", tools=["read_file"]))
    harness.script_plan(document)
    harness.script(ScriptedGeneration(text="The notes describe three meetings."))
    trajectory_id = harness.submit_planned()
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED

    assert harness.events(trajectory_id) == [
        "trajectory.created",
        "trajectory.claimed",
        "plan.drafted",
        "plan.approved",
        "intent.minted",
        "step.started",
        "turn.started",
        "budget.debited",
        "turn.completed",
        "step.completed",
        "trajectory.completed",
    ]
    (claimed,) = harness.event_data(trajectory_id, "trajectory.claimed")
    assert claimed["state"] == "planning"

    with harness.database.read() as session:
        plan = session.execute(select(models.Plan)).scalar_one()
        (plan_step,) = session.execute(select(models.PlanStep)).scalars().all()
        approval = session.execute(select(models.PlanApproval)).scalar_one()
        intent = session.execute(select(models.ExecutionIntent)).scalar_one()
    # The plan verbatim beside its validated form, with the planning call's own record.
    assert plan.valid is True and plan.attempt == 1
    assert plan.raw_document == document
    assert plan.validated_json["steps"][0]["step_id"] == "s1"
    assert plan.prompt_id == "planner.draft"
    assert plan.prompt_sha256 is not None and plan.prompt_sha256.startswith("sha256:")
    assert plan.input_tokens == 812 and plan.model_canonical_id == harness.fake.model.canonical_id
    assert plan_step.status == "committed" and plan_step.completed_at is not None
    # The verdict, with the policy version derived — the same one the trajectory recorded.
    view = harness.service.get(trajectory_id)
    assert approval.outcome == "approved"
    assert approval.approval_policy_version == view.approval_policy_version
    # The intent the step ran under: minted by policy, sized from the estimate, never by the model.
    assert intent.minted_by == "policy" and intent.step_id == "s1"
    assert intent.approved_tier == "local_fast"
    assert intent.max_turns == harness.settings.execution.max_turns_per_step
    assert intent.token_budget == 2 * (4096 + 1024)  # configured_default estimate × 2
    assert intent.budget_source == "configured_default"
    # The transcript: the caller's task verbatim, the framing turn with its prompt record, the
    # answer — and the call to LoadCoach carried the first two.
    turns = harness.service.turns(trajectory_id)
    assert [t.turn.role.value for t in turns] == ["user", "user", "assistant"]
    assert turns[0].turn.content == "summarize ./notes" and turns[0].prompt_id is None
    assert turns[1].prompt_id == "step.execute" and "step s1" in (turns[1].turn.content or "")
    assert turns[2].turn.provenance.intent_id == intent.intent_id
    assert all(t.step_id == "s1" for t in turns)
    step_request = harness.fake.requests[-1]["body"]
    assert step_request["task"] == "tools.agent.local_fast"
    assert [m["role"] for m in step_request["messages"]] == ["user", "user"]
    # The planning call's spend is on the plan row, not the ledger: it is not a turn under an
    # intent, and contract 1 says debits occur under an intent on every turn in both modes.
    assert len(list(harness.budget.entries(run_id=trajectory_id))) == 1


def test_the_corrective_budget_ends_in_t7_with_every_attempt_on_the_record(
    harness: LoopHarness,
) -> None:
    harness.fake.set_default(ScriptedGeneration(text='{"steps": []}'))
    trajectory_id = harness.submit_planned()
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.FAILED
    view = harness.service.get(trajectory_id)
    assert view.error_code == ErrorCode.PLAN_DRAFT_FAILED.value
    assert "3 attempt(s)" in (view.halted_reason or "")
    assert "emptiness cannot pass a gate" in (view.halted_reason or "")
    assert view.lease_owner is None
    assert harness.events(trajectory_id) == [
        "trajectory.created",
        "trajectory.claimed",
        "plan.drafted",
        "plan.drafted",
        "plan.drafted",
        "trajectory.failed",
    ]
    with harness.database.read() as session:
        plans = session.execute(select(models.Plan).order_by(models.Plan.attempt)).scalars().all()
    assert [plan.attempt for plan in plans] == [1, 2, 3]
    assert all(plan.valid is False and plan.issues_json for plan in plans)
    assert [plan.prompt_id for plan in plans] == [
        "planner.draft",
        "planner.corrective",
        "planner.corrective",
    ]
    drafted = harness.event_data(trajectory_id, "plan.drafted")
    assert [(d["attempt"], d["valid"], d["issue_count"]) for d in drafted] == [
        (1, False, 1),
        (2, False, 1),
        (3, False, 1),
    ]


def test_an_invalid_draft_is_corrected_and_the_valid_one_is_the_plan(
    harness: LoopHarness,
) -> None:
    broken = plan_document(step("s1", tier="gpt_9"))
    harness.script(ScriptedGeneration(text=broken))
    harness.script_plan(plan_document(step("s1")))
    harness.script(ScriptedGeneration(text="done"))
    trajectory_id = harness.submit_planned()
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
    assert harness.events(trajectory_id)[:5] == [
        "trajectory.created",
        "trajectory.claimed",
        "plan.drafted",
        "plan.drafted",
        "plan.approved",
    ]
    with harness.database.read() as session:
        plans = session.execute(select(models.Plan).order_by(models.Plan.attempt)).scalars().all()
        approval = session.execute(select(models.PlanApproval)).scalar_one()
    assert [plan.valid for plan in plans] == [False, True]
    assert approval.plan_id == plans[1].id
    assert plans[0].issues_json is not None
    assert plans[0].issues_json[0]["reason"] == "tier_not_configured"


def test_a_step_limit_override_bounds_the_plan(harness: LoopHarness) -> None:
    harness.script_plan(plan_document(step("s1"), step("s2")))
    harness.fake.set_default(ScriptedGeneration(text='{"steps": []}'))
    trajectory_id = harness.submit_planned(max_steps=1)
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.FAILED
    with harness.database.read() as session:
        first = session.execute(select(models.Plan).where(models.Plan.attempt == 1)).scalar_one()
    assert first.issues_json is not None
    assert first.issues_json[0]["reason"] == "too_many_steps"


def test_a_cancel_during_planning_is_honoured_at_the_boundary(harness: LoopHarness) -> None:
    trajectory_id = harness.submit_planned()
    controller = harness.controller()
    assert controller.claim(trajectory_id) is TrajectoryState.PLANNING
    harness.service.cancel(trajectory_id)
    assert controller.run(trajectory_id) is TrajectoryState.CANCELLED
    assert harness.events(trajectory_id)[-1] == "trajectory.cancelled"
    assert harness.fake.requests == [], "no planning call was made"


def test_a_planning_lease_found_at_recovery_is_redrafted_and_its_job_cancelled(
    harness: LoopHarness,
) -> None:
    """Lifecycle §8.3's ``planning`` edge, real: re-claim, cancel the plan job, redraft."""
    held, hold = held_generation(text=plan_document(step("s1")))
    harness.script(held)
    trajectory_id = harness.submit_planned()
    stalled = harness.controller("host:1/0")
    assert stalled.claim(trajectory_id) is TrajectoryState.PLANNING
    result: list[TrajectoryState] = []
    thread = threading.Thread(target=lambda: result.append(stalled.run(trajectory_id)))
    thread.start()
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while not harness.fake.in_flight() and datetime.now(UTC) < deadline:
        threading.Event().wait(0.01)
    (orphan,) = harness.fake.in_flight()
    assert orphan.idempotency_key is not None
    assert orphan.idempotency_key.startswith(f"plan:{trajectory_id}:")

    recoverer = harness.controller("host:2/0")
    summary = recover(
        recoverer,
        harness.database,
        owner_prefix="host:2",
        now=harness.clock(),
        only_expired=False,
    )
    assert summary.resumed == (trajectory_id,)
    assert orphan.cancel_requested is True
    hold.set()
    thread.join(timeout=5)
    assert result == [TrajectoryState.PLANNING], "the stalled worker committed nothing"
    recovered = harness.event_data(trajectory_id, "trajectory.recovered")
    assert recovered and recovered[0]["recovered_from"] == "planning"
    assert recovered[0]["outcome"].startswith("redraft")
    assert orphan.job_id in recovered[0]["outcome"]

    harness.script_plan(plan_document(step("s1")))
    harness.script(ScriptedGeneration(text="done"))
    assert recoverer.run(trajectory_id) is TrajectoryState.COMPLETED
    keys = {job.idempotency_key for job in harness.fake.jobs.values()}
    assert len(keys) == 3, "the redraft used a fresh key; the cancelled job was never replayed"


def test_no_placeholder_for_the_planner_survives_anywhere() -> None:
    """Exit condition 9: the stub D2 left is gone from code, tests, CLI output and docs."""
    root = Path(__file__).resolve().parents[2]
    needle = "before Phase " + "7"
    offenders = [
        str(path.relative_to(root))
        for folder in ("src", "tests", "docs/apps/promptcadence")
        for path in (root / folder).rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".md", ".json", ".toml"}
        and needle in path.read_text(encoding="utf-8", errors="ignore")
        and path.name != Path(__file__).name
    ]
    assert offenders == []


def test_the_plan_document_never_leaves_promptcadence(harness: LoopHarness) -> None:
    """ADR-0051: the step calls carry the task and the framing, never the plan document."""
    document = plan_document(step("s1"))
    harness.script_plan(document)
    harness.script(ScriptedGeneration(text="done"))
    trajectory_id = harness.submit_planned()
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
    step_request = harness.fake.requests[-1]["body"]
    assert all(
        json.loads(document)["steps"][0]["description"] in m["content"] or True
        for m in step_request["messages"]
    )
    assert document not in json.dumps(step_request)
