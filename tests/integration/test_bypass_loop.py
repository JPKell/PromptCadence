"""The bypass loop against the fake LoadCoach: claim, turn, finish, halt, cancel, fence.

Integration by nature — a migrated SQLite database and the in-process fake — and in the default
suite (spec §18): no LoadCoach, no GPU, no network.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from baseaicore import DataClassification
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import budget_and_estimator, egress_for
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    ScriptedError,
    ScriptedGeneration,
    build_fake_app,
    schema_profile,
    shipped_profiles,
)
from toolyard import TieredSandbox
from weightsdb import MigrationRunner
from weightsdb.testing import temporary_sqlite

from promptcadence.config import Settings, load_settings
from promptcadence.domain.errors import ErrorCode
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models
from promptcadence.infrastructure.loadcoach import LoadCoachClient
from promptcadence.services.database import MIGRATIONS_LOCATION, Database
from promptcadence.services.events import TrajectoryEventSink
from promptcadence.services.loop import LoopController, RunSignals
from promptcadence.services.pricing import PricingCatalog
from promptcadence.services.tools import ToolPlant
from promptcadence.services.trajectories import TrajectoryService, TrajectorySubmission

_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class Harness:
    """Everything one loop test needs, over one database and one fake."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        fake: FakeLoadCoach,
        *,
        pricing: PricingCatalog | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.fake = fake
        ticks = iter(range(100_000))
        self.clock = lambda: _NOW + timedelta(milliseconds=next(ticks))
        self.sink = TrajectoryEventSink(database, clock=self.clock)
        self.budget, self.estimator = budget_and_estimator(
            database, settings, clock=self.clock, pricing=pricing
        )
        self.egress = egress_for(database, clock=self.clock)
        self.service = TrajectoryService(
            database, self.sink, settings, budget=self.budget, clock=self.clock
        )
        self.loadcoach = LoadCoachClient(
            TestClient(build_fake_app(fake), base_url="http://loadcoach.test")
        )
        # No isolation rung, deterministically: the probe's view of the host is shaped by the
        # injected `which` (D1's seam), so `run_command` refuses with `isolation_unavailable` here
        # whether or not the machine running the suite has docker. The rung itself is ToolYard's to
        # test; what this suite asserts is that its refusal is a result the loop feeds back.
        self.tools = ToolPlant(settings, sandbox=TieredSandbox(which=lambda _name: None))

    def controller(self, owner: str = "host:1/0") -> LoopController:
        return LoopController(
            budget=self.budget,
            estimator=self.estimator,
            egress=self.egress,
            database=self.database,
            sink=self.sink,
            loadcoach=self.loadcoach,
            settings=self.settings,
            owner=owner,
            clock=self.clock,
            tools=self.tools,
        )

    def submit(self, **overrides: object) -> str:
        fields: dict[str, object] = {"task": "summarize ./notes", "bypass_planning": True}
        fields.update(overrides)
        submission = TrajectorySubmission(**fields)  # type: ignore[arg-type]
        return self.service.submit(submission).trajectory_id

    def events(self, trajectory_id: str) -> list[str]:
        return [event.event_type for event in self.service.events(trajectory_id)]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "2")
    settings = load_settings().settings
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles("tools.agent.local_fast", "tools.agent.local_large"))
    with temporary_sqlite() as engine:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        yield Harness(settings, Database(engine), fake)


@pytest.fixture
def schema_harness(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> Iterator[Harness]:
    """A configuration whose default tier's profile validates a schema: the completing case."""
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_FAST__TASK_PROFILE", "structured.answer")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_LARGE__TASK_PROFILE", "tools.agent.local_large")
    settings = load_settings().settings
    fake = FakeLoadCoach()
    fake.register_profile(schema_profile("structured.answer"))
    fake.register_profile(*shipped_profiles("tools.agent.local_large"))
    with temporary_sqlite() as engine:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        yield Harness(settings, Database(engine), fake)


# --------------------------------------------------------------------------------------------
# Claim
# --------------------------------------------------------------------------------------------


def test_t3_claims_in_one_write_with_the_intent_and_both_events(
    harness: Harness,
) -> None:
    trajectory_id = harness.submit()
    controller = harness.controller()
    assert controller.claim(trajectory_id) is TrajectoryState.EXECUTING
    view = harness.service.get(trajectory_id)
    assert view.state is TrajectoryState.EXECUTING
    assert view.lease_owner == "host:1/0"
    assert view.lease_expires_at is not None
    assert harness.events(trajectory_id) == [
        "trajectory.created",
        "trajectory.claimed",
        "intent.minted",
    ]
    # Phase 7: the thread opens at the step's first dispatch (``step.started``), on both paths,
    # so the claim leaves the intent and nothing to converse in yet.
    assert harness.service.turns(trajectory_id) == []
    with harness.database.read() as session:
        intent = session.execute(select(models.ExecutionIntent)).scalar_one()
    assert intent.minted_by == "bypass_default"
    assert intent.step_id == "loop"
    assert intent.trajectory_id == trajectory_id


def test_a_second_worker_cannot_claim_the_same_trajectory(harness: Harness) -> None:
    trajectory_id = harness.submit()
    assert harness.controller("host:1/0").claim(trajectory_id) is TrajectoryState.EXECUTING
    assert harness.controller("host:1/1").claim(trajectory_id) is None
    assert harness.controller("host:1/1").next_queued() is None


def test_a_planned_trajectory_is_claimed_for_planning(harness: Harness) -> None:
    """T2: the claim takes the lease and nothing more; drafting is ``run``'s (Phase 7)."""
    trajectory_id = harness.submit(bypass_planning=None)
    assert harness.service.get(trajectory_id).bypass_planning is False
    assert harness.controller().claim(trajectory_id) is TrajectoryState.PLANNING
    view = harness.service.get(trajectory_id)
    assert view.lease_owner == "host:1/0"
    assert harness.events(trajectory_id) == ["trajectory.created", "trajectory.claimed"]


def test_the_tier_pin_is_honoured_by_the_minted_intent(harness: Harness) -> None:
    trajectory_id = harness.submit(tier="local_large")
    harness.controller().claim(trajectory_id)
    with harness.database.read() as session:
        intent = session.execute(select(models.ExecutionIntent)).scalar_one()
    assert intent.approved_tier == "local_large"


# --------------------------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------------------------


def _claim_and_run(harness: Harness, **overrides: object) -> tuple[str, TrajectoryState]:
    trajectory_id = harness.submit(**overrides)
    controller = harness.controller()
    assert controller.claim(trajectory_id) is TrajectoryState.EXECUTING
    return trajectory_id, controller.run(trajectory_id)


def test_a_text_profile_turn_completes_on_a_declared_stop(harness: Harness) -> None:
    """Contract 6's first clause: the provider declared ``stop``, so the turn completes."""
    trajectory_id, state = _claim_and_run(harness)
    assert state is TrajectoryState.COMPLETED
    view = harness.service.get(trajectory_id)
    assert view.error_code is None
    assert view.completed_at is not None
    assert harness.events(trajectory_id) == [
        "trajectory.created",
        "trajectory.claimed",
        "intent.minted",
        "step.started",
        "turn.started",
        "budget.debited",
        "turn.completed",
        "step.completed",
        "trajectory.completed",
    ]
    turns = harness.service.turns(trajectory_id)
    assert [t.turn.role.value for t in turns] == ["user", "assistant"]
    assistant = turns[1]
    assert assistant.turn.content == "The notes describe three meetings."
    assert assistant.loadcoach_job_id is not None
    assert assistant.turn.provenance.tier == "local_fast"
    assert assistant.turn.usage is not None and assistant.turn.usage.input_tokens == 812
    (job,) = harness.fake.jobs.values()
    assert job.idempotency_key == assistant.turn.turn_id  # the turn id is the key
    assert harness.fake.requests[-1]["body"]["messages"] == [
        {"role": "user", "content": "summarize ./notes"}
    ]
    assert harness.fake.requests[-1]["body"]["task"] == "tools.agent.local_fast"


def test_a_turn_with_no_declared_finish_halts_never_completes(harness: Harness) -> None:
    """The wire of a LoadCoach before 846348b: no finish_reason, and a turn cannot
    complete on none."""
    harness.fake.script(ScriptedGeneration(finish_reason=None))
    trajectory_id, state = _claim_and_run(harness)
    assert state is TrajectoryState.HALTED
    view = harness.service.get(trajectory_id)
    assert view.error_code == ErrorCode.LOADCOACH_ERROR.value
    assert "no finish_reason" in (view.halted_reason or "")
    assert harness.events(trajectory_id)[-2:] == ["turn.completed", "trajectory.halted"]
    roles = [t.turn.role.value for t in harness.service.turns(trajectory_id)]
    assert roles == ["user", "assistant"]  # the turn is recorded exactly as it happened


def test_a_truncated_answer_halts_never_flows_onward(harness: Harness) -> None:
    """The quiet failure the row was named for: ``length`` is not ``stop``."""
    harness.fake.script(ScriptedGeneration(text="the notes describe", finish_reason="length"))
    trajectory_id, state = _claim_and_run(harness)
    assert state is TrajectoryState.HALTED
    view = harness.service.get(trajectory_id)
    assert view.error_code == ErrorCode.LOADCOACH_ERROR.value
    assert "length" in (view.halted_reason or "")
    completed = harness.service.events(trajectory_id)[-2]
    assert completed.data["finish_reason"] == "length"
    assert completed.data["decision"] == "halt"


def test_a_schema_validated_result_completes_the_trajectory(schema_harness: Harness) -> None:
    """Contract 6's second clause, and the full bypass journey on today's wire."""
    schema_harness.fake.script(ScriptedGeneration(text='{"answer": "three meetings"}'))
    trajectory_id, state = _claim_and_run(schema_harness)
    assert state is TrajectoryState.COMPLETED
    view = schema_harness.service.get(trajectory_id)
    assert view.completed_at is not None
    assert view.lease_owner is None
    assert schema_harness.events(trajectory_id)[-3:] == [
        "turn.completed",
        "step.completed",
        "trajectory.completed",
    ]
    completed = schema_harness.service.events(trajectory_id)[-3]
    assert completed.data["schema_validated"] is True
    assert completed.data["decision"] == "complete"


def test_no_eligible_model_escalates_to_a_scoped_reapproval_or_halts_when_the_order_ends(
    harness: Harness,
) -> None:
    """Spec §13's ``NO_ELIGIBLE_MODEL`` cell, real at Phase 7.

    The intent's tiers cannot serve and the next tier in the escalation order is outside the
    intent: a ``tier_escalation`` deviation (lifecycle §5), whose disposition is a scoped
    re-approval carrying the next tier. When the order is exhausted there is nothing to grant, and
    the trajectory halts with the cause naming the failure.
    """
    harness.fake.script(ScriptedError("NO_ELIGIBLE_MODEL", details={"candidates": []}))
    trajectory_id, state = _claim_and_run(harness)
    assert state is TrajectoryState.AWAITING_APPROVAL
    view = harness.service.get(trajectory_id)
    assert "no_eligible_model" in (view.halted_reason or "")
    assert harness.events(trajectory_id)[-3:] == [
        "turn.started",
        "deviation.detected",
        "approval.requested",
    ]
    with harness.database.read() as session:
        request = session.execute(select(models.ApprovalRequest)).scalar_one()
        deviation = session.execute(select(models.Deviation)).scalar_one()
    assert request.kind == "reapproval"
    assert request.detail_json is not None
    assert request.detail_json["category"] == "tier_escalation"
    assert request.detail_json["next_tier"] == "local_large"
    assert deviation.category == "tier_escalation"

    harness.fake.script(ScriptedError("NO_ELIGIBLE_MODEL", details={"candidates": []}))
    trajectory_id, state = _claim_and_run(harness, tier="local_large")  # last in the order
    assert state is TrajectoryState.HALTED
    view = harness.service.get(trajectory_id)
    assert view.error_code == ErrorCode.TIER_UNAVAILABLE.value
    assert "escalation order is exhausted" in (view.halted_reason or "")


def test_loadcoach_errors_halt_with_the_mapped_code_and_the_original_in_the_cause(
    harness: Harness,
) -> None:
    harness.fake.script(ScriptedError("CONTEXT_LIMIT_EXCEEDED"))
    _, state = _claim_and_run(harness)
    assert harness.service.list(state=TrajectoryState.HALTED)[0][0].error_code == (
        ErrorCode.COMPACTION_FAILED.value
    )

    harness.fake.script(ScriptedError("PROVIDER_TIMEOUT"))
    _, state = _claim_and_run(harness)
    assert harness.service.list(state=TrajectoryState.HALTED)[0][0].error_code == (
        ErrorCode.LOADCOACH_ERROR.value
    )


def test_an_unreachable_loadcoach_fails_the_trajectory_with_the_reason(
    harness: Harness,
) -> None:
    import httpx

    harness.loadcoach = LoadCoachClient(httpx.Client(base_url="http://127.0.0.1:9", timeout=0.2))
    trajectory_id, state = _claim_and_run(harness)
    assert state is TrajectoryState.FAILED
    view = harness.service.get(trajectory_id)
    assert view.error_code == ErrorCode.LOADCOACH_UNAVAILABLE.value


def test_a_requested_tool_call_is_executed_and_its_result_continues_the_turn(
    harness: Harness,
) -> None:
    """Phase 4 replaces Phase 3's placeholder: the call runs, and the loop keeps going.

    The trajectory still does not *complete* on the tool turn — a requested tool is not a declared
    finish — so the second scripted answer is what completes it. That is the placeholder's one
    surviving claim, kept.
    """
    (harness.tools.workspace_root / "x").mkdir(parents=True, exist_ok=True)
    harness.fake.script(
        ScriptedGeneration(
            text="",
            tool_calls=(
                {
                    "call_index": 0,
                    "id": "c1",
                    "name": "list_dir",
                    "arguments_fragment": '{"path": "."}',
                },
            ),
        ),
        ScriptedGeneration(text="The workspace is empty."),
    )
    trajectory_id, state = _claim_and_run(harness)
    assert state is TrajectoryState.COMPLETED
    roles = [t.turn.role.value for t in harness.service.turns(trajectory_id)]
    assert roles == ["user", "assistant", "tool", "assistant"]
    events = harness.events(trajectory_id)
    assert "tool.call.started" in events
    assert "tool.call.completed" in events
    with harness.database.read() as session:
        record = session.execute(select(models.ToolCallRecord)).scalar_one()
    assert record.tool_name == "list_dir"
    assert record.status == "ok"
    assert record.reason is None
    assert record.tool_turn_id is not None


def test_a_tool_outside_the_allowlist_is_refused_and_recorded_and_the_run_continues(
    harness: Harness,
) -> None:
    """Lifecycle §5's prose refinement, now that a call can actually be refused.

    Outside the *trajectory* allowlist the call is refused outright and never re-approvable — but
    that is a statement about the **call**, not about the trajectory. Before tools executed, a
    halt was the only available reading; ToolYard's ``not_allowlisted`` is the other one, and it is
    the one lifecycle §5 wrote down.

    ``write_file`` rather than an invented name on purpose: the refusal order is registry →
    allowlist → … , so a name nothing registers is refused as ``unknown_tool`` before the allowlist
    is consulted, and would test the wrong check. The file it asked for is asserted absent, which
    is the claim that matters — a refusal that still wrote would be the failure this exists to
    prevent.
    """
    harness.fake.script(
        ScriptedGeneration(
            text="",
            tool_calls=(
                {
                    "call_index": 0,
                    "id": "c1",
                    "name": "write_file",
                    "arguments_fragment": '{"path": "out.txt", "content": "x"}',
                },
            ),
        ),
        ScriptedGeneration(text="I cannot do that; answering directly."),
    )
    trajectory_id, state = _claim_and_run(harness, tools=("read_file",))
    assert state is TrajectoryState.COMPLETED
    view = harness.service.get(trajectory_id)
    assert view.error_code is None
    assert "deviation.detected" in harness.events(trajectory_id)
    with harness.database.read() as session:
        deviation = session.execute(select(models.Deviation)).scalar_one()
        record = session.execute(select(models.ToolCallRecord)).scalar_one()
    assert deviation.category == "undeclared_tool"
    assert deviation.reapprovable is False
    assert deviation.disposition == "refused_not_reapprovable"
    assert record.tool_name == "write_file"
    assert record.status == "refused"
    assert record.reason == "not_allowlisted"
    assert not (harness.tools.workspace_root / trajectory_id / "out.txt").exists()
    tool_turn = next(t for t in harness.service.turns(trajectory_id) if t.turn.role.value == "tool")
    assert tool_turn.turn.tool_call_id == record.invocation_id
    assert "not_allowlisted" in (tool_turn.turn.content or "")


def _cheap_estimates(settings: Settings) -> Settings:
    """Shrink the tiers' configured per-step estimates so a small ceiling still admits a step.

    Two different mechanisms watch the same number and this separates them. The **ledger's**
    per-run ceiling refuses a step whose *estimate* would not fit — correct, and what P5 added —
    while the ``budget_overrun`` **deviation** these tests are about compares what a turn actually
    spent against the intent's budget, after the fact. With the shipped 4096+1024 default estimate
    a 1000-token ceiling admits nothing at all, so these tests would never reach the turn whose
    overrun they are asserting on.
    """
    tiers = {
        name: tier.model_copy(
            update={"default_step_input_tokens": 400, "default_step_output_tokens": 100}
        )
        for name, tier in settings.tiers.items()
    }
    return settings.model_copy(update={"tiers": tiers})


def test_a_budget_overrun_is_recorded_and_continued_under_the_default_scope(
    harness: Harness,
) -> None:
    """Lifecycle §5: under ``on_tier_or_classification_change`` a budget overrun is
    ``continue_recorded`` — an event *and* a row — and the turn's own verdict then decides."""
    harness = Harness(_cheap_estimates(harness.settings), harness.database, harness.fake)
    harness.fake.script(ScriptedGeneration(input_tokens=900, output_tokens=200))
    trajectory_id, state = _claim_and_run(harness, token_budget=1000)
    assert state is TrajectoryState.COMPLETED  # the declared stop decided; the drift did not
    view = harness.service.get(trajectory_id)
    assert view.error_code is None
    assert "deviation.detected" in harness.events(trajectory_id)
    with harness.database.read() as session:
        deviation = session.execute(select(models.Deviation)).scalar_one()
    assert deviation.category == "budget_overrun"
    assert deviation.disposition == "continue_recorded"
    assert deviation.detail_json["tokens_spent"] == 1100


def test_a_budget_overrun_parks_for_scoped_reapproval_under_any_deviation(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under ``any_deviation`` a budget overrun is a scoped re-approval (lifecycle §5), and at
    Phase 7 that is a real T10: one request, scoped to the drifted step, carrying what it asked."""
    monkeypatch.setenv("PROMPTCADENCE_PLANNING__REAPPROVAL_SCOPE", "any_deviation")
    strict = Harness(_cheap_estimates(load_settings().settings), harness.database, harness.fake)
    strict.fake.script(ScriptedGeneration(input_tokens=900, output_tokens=200))
    trajectory_id, state = _claim_and_run(strict, token_budget=1000)
    assert state is TrajectoryState.AWAITING_APPROVAL
    view = strict.service.get(trajectory_id)
    assert "budget_overrun" in (view.halted_reason or "")
    assert view.lease_owner is None
    with strict.database.read() as session:
        request = session.execute(select(models.ApprovalRequest)).scalar_one()
    assert request.status == "pending"
    assert request.kind == "reapproval"
    assert request.step_ids_json == ["loop"]
    assert request.detail_json is not None
    assert request.detail_json["category"] == "budget_overrun"
    assert request.detail_json["tokens_spent"] == 1100


def test_a_remote_answer_on_a_local_tier_is_a_violation_that_halts(harness: Harness) -> None:
    """Contract 4: the subject is verified; a foreign provider kind is read as remote."""
    from tests.fakes.loadcoach_app import FakeModel

    harness.fake.model = FakeModel(
        canonical_id="openai_compatible/gpt@sha256:" + "b" * 64, provider_kind="openai_compatible"
    )
    # The registry still says one kind, so the surface is verifiable; the *answer* names it.
    harness.fake.model = FakeModel(
        canonical_id="openai_compatible/gpt@sha256:" + "b" * 64, provider_kind="ollama"
    )
    trajectory_id, state = _claim_and_run(harness)
    assert state is TrajectoryState.HALTED
    view = harness.service.get(trajectory_id)
    assert view.error_code == ErrorCode.DEVIATION_HALTED.value
    assert "tier_violation" in (view.halted_reason or "")


def test_a_cancel_request_is_honoured_at_the_boundary_in_one_write(harness: Harness) -> None:
    trajectory_id = harness.submit()
    controller = harness.controller()
    controller.claim(trajectory_id)
    harness.service.cancel(trajectory_id)  # sets the flag on a leased trajectory
    assert controller.run(trajectory_id) is TrajectoryState.CANCELLED
    assert harness.events(trajectory_id)[-1] == "trajectory.cancelled"
    assert harness.fake.requests == []  # no turn was started


def test_a_cancel_mid_turn_cancels_the_loadcoach_job_and_the_trajectory(harness: Harness) -> None:
    hold = threading.Event()
    harness.fake.script(ScriptedGeneration(hold=hold))
    trajectory_id = harness.submit()
    controller = harness.controller()
    controller.claim(trajectory_id)
    signals = RunSignals.fresh()
    result: list[TrajectoryState] = []
    thread = threading.Thread(
        target=lambda: result.append(controller.run(trajectory_id, signals=signals))
    )
    thread.start()
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while not harness.fake.in_flight() and datetime.now(UTC) < deadline:
        threading.Event().wait(0.01)
    assert signals.in_flight_turn_id is not None
    harness.service.cancel(trajectory_id)
    renewed, requested = controller.renew_lease(trajectory_id)
    assert renewed and requested
    signals.cancel_requested.set()
    assert controller.cancel_in_flight(signals.in_flight_turn_id) is not None
    thread.join(timeout=5)
    assert result == [TrajectoryState.HALTED] or result == [TrajectoryState.CANCELLED]
    assert not harness.fake.in_flight()
    view = harness.service.get(trajectory_id)
    assert view.state in {TrajectoryState.HALTED, TrajectoryState.CANCELLED}
    assert view.lease_owner is None


def test_a_lost_lease_fences_every_write(harness: Harness) -> None:
    """The recovering worker owns it now; the stalled one cannot commit a turn or an ending."""
    trajectory_id = harness.submit()
    stalled = harness.controller("host:1/0")
    stalled.claim(trajectory_id)
    with harness.database.write() as session:
        row = session.get(models.Trajectory, trajectory_id)
        assert row is not None
        row.lease_owner = "host:2/0"  # recovery elsewhere took it over
    renewed, _ = stalled.renew_lease(trajectory_id)
    assert renewed is False
    assert stalled.run(trajectory_id) is TrajectoryState.EXECUTING  # stopped, committed nothing
    assert harness.fake.requests == []
    assert harness.service.get(trajectory_id).state is TrajectoryState.EXECUTING


def test_a_changed_approval_policy_refuses_to_run_under_an_envelope_nobody_minted(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    trajectory_id = harness.submit()
    harness.controller().claim(trajectory_id)
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__MAX_STEPS", "7")  # the default max_turns moved
    changed = LoopController(
        budget=harness.budget,
        estimator=harness.estimator,
        egress=harness.egress,
        database=harness.database,
        sink=harness.sink,
        loadcoach=harness.loadcoach,
        settings=load_settings().settings,
        owner="host:1/0",
        clock=harness.clock,
    )
    assert changed.run(trajectory_id) is TrajectoryState.FAILED
    assert "nobody minted" in (harness.service.get(trajectory_id).halted_reason or "")


def test_the_recorded_classification_flows_into_the_facts(harness: Harness) -> None:
    trajectory_id, _ = _claim_and_run(harness, classification=DataClassification.PUBLIC)
    assert harness.service.get(trajectory_id).classification is DataClassification.PUBLIC


def test_an_assistant_turn_that_only_requested_tools_replays_natively(harness: Harness) -> None:
    """G2: the turn goes back on the wire as what it was, not as text naming what it did.

    Until LoadCoach's `/generate` carried `tool_calls` on a message this turn could not be replayed
    at all — a provider refuses an assistant turn with neither content nor calls — so it was
    rendered as `[tool_calls]`-prefixed text (G1's workaround, now deleted). The row still keeps
    the empty content the model actually produced.
    """
    (harness.tools.workspace_root / "x").mkdir(parents=True, exist_ok=True)
    harness.fake.script(
        ScriptedGeneration(
            text="",
            tool_calls=(
                {
                    "call_index": 0,
                    "id": "c1",
                    "name": "list_dir",
                    "arguments_fragment": '{"path": "."}',
                },
            ),
        ),
        ScriptedGeneration(text="The workspace is empty."),
    )
    trajectory_id, state = _claim_and_run(harness)
    assert state is TrajectoryState.COMPLETED
    replayed = harness.fake.requests[-1]["body"]["messages"]
    assert [m["role"] for m in replayed] == ["user", "assistant", "tool"]
    assert replayed[1]["content"] == ""
    assert replayed[1]["tool_calls"] == [
        {"id": "c1", "name": "list_dir", "arguments": {"path": "."}}
    ]
    assert "[tool_calls]" not in json.dumps(replayed)
    # The TOOL turn answers the model's own call id, which is what a provider matches on.
    assert replayed[2]["tool_call_id"] == "c1"
    assistant = harness.service.turns(trajectory_id)[1]
    assert assistant.turn.content == "", "the row keeps what the model actually said"


def test_the_offered_tools_are_the_intents_allowlist_and_no_wider(harness: Harness) -> None:
    """Lifecycle §4.3: the model is told about the tools the intent declared, and only those."""
    harness.fake.script(ScriptedGeneration(text="Nothing to do."))
    _claim_and_run(harness)
    offered = harness.fake.requests[-1]["body"].get("tools") or []
    names = {tool["name"] for tool in offered}
    assert names, "a step with an allowlist offers its tools"
    assert names <= set(harness.settings.tools.enabled)
    assert all(tool["parameters"] for tool in offered), "each definition carries its schema"
