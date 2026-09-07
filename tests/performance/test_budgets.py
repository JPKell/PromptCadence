"""Spec §15's budgets, ceiling-asserted and target-reported (ADR-0097).

Nine of the ten rows, in-process against the fake LoadCoach; the tenth — added latency per SSE
event — needs a real socket and lives in ``test_sse_gap.py``. Every test takes the median over
``_MEASURED`` iterations after ``_WARMUP``, fails when the median exceeds the row's **ceiling**,
and prints median, p95 and the target for the release handoff. A missed ceiling is a finding for
the operator, never a wider number here.

Marked ``performance`` and excluded from the default gate.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from baseaicore import DataClassification, Money, TokenUsage, new_id
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from tests.fakes.harness import LoopHarness, open_harness
from tests.fakes.loadcoach_app import ScriptedGeneration, build_fake_app

from promptcadence.config import load_settings
from promptcadence.domain.plan import validate_plan_document
from promptcadence.domain.policy import ApprovalMode, ApprovalPolicy, StepEstimate, evaluate_plan
from promptcadence.domain.tiers import EgressClass, Tier, TierPolicy, TierSnapshot
from promptcadence.domain.trajectory import TrajectoryDeclaration, TrajectoryState
from promptcadence.infrastructure.db import models
from promptcadence.infrastructure.loadcoach import LoadCoachClient, Message
from promptcadence.services.compaction import budget_for, build_chain, to_transcript
from promptcadence.services.trajectories import TrajectorySubmission
from promptcadence.services.worker import recover

pytestmark = pytest.mark.performance

_WARMUP = 3
_MEASURED = 20
_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _report(row: str, samples: list[float], *, target_ms: float, ceiling_ms: float) -> None:
    """Print the row's numbers and assert the ceiling on the median (ADR-0097 rules 1–2)."""
    ordered = sorted(samples)
    median = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    print(  # noqa: T201 — the measured numbers are this test's whole output
        f"\n§15 {row}: median {median:.2f} ms, p95 {p95:.2f} ms, max {ordered[-1]:.2f} ms "
        f"(target {target_ms:g} ms, ceiling {ceiling_ms:g} ms, n={len(samples)})"
    )
    assert median <= ceiling_ms, f"{row}: median {median:.2f} ms exceeds the ceiling {ceiling_ms}"


def _timed(call: Callable[[], Any], *, iterations: int = _WARMUP + _MEASURED) -> list[float]:
    samples: list[float] = []
    for index in range(iterations):
        started = time.perf_counter()
        call()
        elapsed = (time.perf_counter() - started) * 1000.0
        if index >= _WARMUP:
            samples.append(elapsed)
    return samples


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopHarness]:
    # The two-run overhead measurement needs a step that may make 17 tool round trips.
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__MAX_TURNS_PER_STEP", "20")
    with open_harness(load_settings().settings) as built:
        yield built


def _call(index: int) -> dict[str, object]:
    return {
        "call_index": index,
        "id": f"c{index}",
        "name": "list_dir",
        "arguments_fragment": '{"path": "."}',
    }


# --- Row 1: trajectory admission (accepted → persisted) ≤ 50 ms / 200 ms ---------------------


def test_trajectory_admission(harness: LoopHarness) -> None:
    counter = iter(range(10_000))
    samples = _timed(
        lambda: harness.service.submit(
            TrajectorySubmission(task=f"task {next(counter)}", bypass_planning=True)
        )
    )
    _report("trajectory admission", samples, target_ms=50, ceiling_ms=200)


# --- Row 2: plan approval evaluation, 20 steps ≤ 20 ms / 100 ms -------------------------------


def test_plan_approval_evaluation_over_twenty_steps() -> None:
    steps = [
        {
            "step_id": f"s{index}",
            "description": f"step {index}",
            "depends_on": [f"s{index - 1}"] if index else [],
            "tools": ["read_file"],
            "tier": "local_fast",
            "data_classification": "internal",
            "expected_turns": 2,
        }
        for index in range(20)
    ]
    plan = validate_plan_document(
        json.dumps({"steps": steps}),
        trajectory_allowlist=frozenset({"read_file", "list_dir"}),
        trajectory_classification=DataClassification.CONFIDENTIAL,
        configured_tiers=frozenset({"local_fast", "local_large"}),
        max_plan_steps=20,
    )
    snapshot = TierSnapshot(
        tiers=(
            Tier(
                name="local_fast",
                task_profile="tools.agent.local_fast",
                egress_class=EgressClass.LOCAL,
                max_data_classification=None,
                context_budget_tokens=16_384,
            ),
            Tier(
                name="local_large",
                task_profile="tools.agent.local_large",
                egress_class=EgressClass.LOCAL,
                max_data_classification=None,
                context_budget_tokens=32_768,
            ),
        ),
        default_tier="local_fast",
        escalation_order=("local_fast", "local_large"),
    )
    declaration = TrajectoryDeclaration(
        trajectory_id="01TRAJECTORY0000000000000A",
        classification=DataClassification.INTERNAL,
        tool_allowlist=frozenset({"read_file", "list_dir"}),
        token_budget=1_000_000,
        money_budget=Money(currency="USD", nanos=5_000_000_000),
        max_turns=8,
    )
    policy = ApprovalPolicy(
        mode=ApprovalMode.AUTO,
        gate_egress_at=DataClassification.INTERNAL,
        gate_step_cost=Money(currency="USD", nanos=1_000_000_000),
    )
    estimates = {f"s{index}": StepEstimate(2_000, money_estimate=None) for index in range(20)}
    samples = _timed(
        lambda: evaluate_plan(
            plan,
            declaration=declaration,
            tier_policy=TierPolicy(snapshot=snapshot),
            policy=policy,
            estimates=estimates,
        )
    )
    _report("plan approval evaluation, 20 steps", samples, target_ms=20, ceiling_ms=100)


# --- Rows 3 and 4: per-turn overhead ≤ 25/100 ms; tool dispatch overhead ≤ 10/50 ms ----------


class _TimingClient(TestClient):
    """Measures every request at the transport boundary — the LoadCoach time to subtract."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.spent_ms = 0.0

    def request(self, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        started = time.perf_counter()
        response: httpx.Response = super().request(method, url, **kwargs)
        self.spent_ms += (time.perf_counter() - started) * 1000.0
        return response


def _measured_run(harness: LoopHarness, answers: list[ScriptedGeneration]) -> tuple[float, int]:
    """Run one bypass trajectory; return (PromptCadence overhead ms, tool calls executed).

    Overhead is wall time minus the time spent inside LoadCoach's transport minus the tools' own
    ``duration_ms`` — the three figures spec §15 says are always reported separately.
    """
    timing = _TimingClient(build_fake_app(harness.fake), base_url="http://loadcoach.test")
    harness.loadcoach = LoadCoachClient(timing)
    harness.fake.script(*answers)
    trajectory_id = harness.submit_bypass(max_turns=len(answers) + 2)
    controller = harness.controller()
    assert controller.claim(trajectory_id) is TrajectoryState.EXECUTING
    started = time.perf_counter()
    state = controller.run(trajectory_id)
    wall_ms = (time.perf_counter() - started) * 1000.0
    assert state is TrajectoryState.COMPLETED, state
    with harness.database.read() as session:
        tool_ms = sum(
            session.execute(
                select(models.ToolCallRecord.duration_ms).where(
                    models.ToolCallRecord.trajectory_id == trajectory_id
                )
            ).scalars()
        )
        calls = session.execute(
            select(models.ToolCallRecord.id).where(
                models.ToolCallRecord.trajectory_id == trajectory_id
            )
        ).scalars()
        count = len(list(calls))
    return wall_ms - timing.spent_ms - float(tool_ms), count


def test_per_turn_overhead_and_tool_dispatch_overhead(harness: LoopHarness) -> None:
    """Two runs, two unknowns.

    Run A: 18 turns, one ``list_dir`` each (17 calls) — the most the configured ``max_turns``
    cap of 20 admits. Run B: 2 turns, the first with 17 calls. With ``o_t`` the per-turn
    overhead and ``o_c`` the per-call dispatch overhead, ``A = 18·o_t + 17·o_c`` and
    ``B = 2·o_t + 17·o_c``, so ``o_t = (A − B) / 16`` and ``o_c = (B − 2·o_t) / 17``. Each pair
    of runs is repeated and the medians are used.
    """
    turn_samples: list[float] = []
    call_samples: list[float] = []
    for index in range(_WARMUP + 7):
        a_overhead, a_calls = _measured_run(
            harness,
            [ScriptedGeneration(text="", tool_calls=(_call(turn),)) for turn in range(17)]
            + [ScriptedGeneration(text="done")],
        )
        b_overhead, b_calls = _measured_run(
            harness,
            [
                ScriptedGeneration(text="", tool_calls=tuple(_call(turn) for turn in range(17))),
                ScriptedGeneration(text="done"),
            ],
        )
        assert (a_calls, b_calls) == (17, 17)
        per_turn = (a_overhead - b_overhead) / 16.0
        per_call = (b_overhead - 2.0 * per_turn) / 17.0
        if index >= _WARMUP:
            turn_samples.append(max(per_turn, 0.0))
            call_samples.append(max(per_call, 0.0))
    _report(
        "per-turn overhead excluding LoadCoach time", turn_samples, target_ms=25, ceiling_ms=100
    )
    _report(
        "tool dispatch overhead excluding tool runtime", call_samples, target_ms=10, ceiling_ms=50
    )


# --- Row 5: ledger debit, ceilings evaluated ≤ 5 ms / 20 ms ----------------------------------


def test_ledger_debit_with_ceilings_evaluated(harness: LoopHarness) -> None:
    trajectory_id = harness.submit_bypass(project=None)
    view = harness.service.get(trajectory_id)
    priced = harness.budget.price(
        tier="local_fast",
        canonical_id=harness.fake.model.canonical_id,
        usage=TokenUsage(input_tokens=812, output_tokens=1104),
        at=_NOW,
    )

    def debit() -> None:
        with harness.sink.write() as (session, events):
            body = harness.budget.debit(
                session, view=view, turn_id=new_id(), tier="local_fast", priced=priced, at=_NOW
            )
            events.append(trajectory_id, body, now=_NOW)

    samples = _timed(debit)
    _report("ledger debit, ceilings evaluated", samples, target_ms=5, ceiling_ms=20)


# --- Row 6: compaction plan, 200-turn transcript ≤ 50 ms / 200 ms ----------------------------


def test_compaction_plan_over_two_hundred_turns(harness: LoopHarness) -> None:
    body = "The meeting covered the migration plan in considerable detail. " * 3
    messages: list[Message] = [Message(role="user", content="summarize ./notes")]
    origins: list[str] = [new_id()]
    for index in range(200):
        role = "assistant" if index % 2 == 0 else "tool"
        messages.append(
            Message(role=role, content=body, tool_call_id=f"c{index}" if role == "tool" else None)
        )
        origins.append(new_id())
    tier = Tier(
        name="local_fast",
        task_profile="tools.agent.local_fast",
        egress_class=EgressClass.LOCAL,
        max_data_classification=None,
        context_budget_tokens=4_096,
    )
    settings = harness.settings.compaction
    chain = build_chain(settings)
    budget = budget_for(tier, settings)
    transcript = to_transcript(tuple(messages), tuple(origins))
    samples = _timed(lambda: chain.decide(transcript, budget))
    _report("compaction plan, 200-turn transcript", samples, target_ms=50, ceiling_ms=200)


# --- Rows 8 and 9: explanation retrieval ≤ 25/100 ms; materialization of 500 turns ≤ 2/10 s ---


def _five_hundred_turn_trajectory(harness: LoopHarness) -> str:
    """A completed bypass trajectory with 500 assistant turns and their two events each."""
    trajectory_id = harness.submit_bypass()
    controller = harness.controller()
    assert controller.claim(trajectory_id) is TrajectoryState.EXECUTING
    with harness.database.write() as session:
        intent = session.execute(
            select(models.ExecutionIntent).where(
                models.ExecutionIntent.trajectory_id == trajectory_id
            )
        ).scalar_one()
        thread_id = new_id()
        session.add(
            models.Thread(
                id=thread_id, trajectory_id=trajectory_id, step_id="loop", created_at=_NOW
            )
        )
        next_sequence = harness.sink.next_sequence(session, trajectory_id)
        for index in range(500):
            turn_id = new_id()
            at = _NOW + timedelta(milliseconds=index)
            session.add(
                models.Turn(
                    id=turn_id,
                    thread_id=thread_id,
                    trajectory_id=trajectory_id,
                    sequence=index + 1,
                    role="assistant",
                    tier="local_fast",
                    model_canonical_id=harness.fake.model.canonical_id,
                    content_text=f"turn {index}",
                    content_hash="sha256:" + "0" * 64,
                    finish_reason="tool_calls",
                    intent_id=intent.intent_id,
                    intent_revision=intent.revision,
                    input_tokens=800,
                    output_tokens=100,
                    loadcoach_ms=250.0,
                    overhead_ms=3.0,
                    loadcoach_job_id=f"job-{index}",
                    created_at=at,
                )
            )
            for event_type in ("turn.started", "turn.completed"):
                session.add(
                    models.Event(
                        id=new_id(),
                        trajectory_id=trajectory_id,
                        sequence=next_sequence,
                        event_type=event_type,
                        timestamp=at,
                        data_json={"turn_id": turn_id, "sequence": index + 1},
                    )
                )
                next_sequence += 1
        session.execute(
            update(models.Trajectory)
            .where(models.Trajectory.id == trajectory_id)
            .values(status="completed", completed_at=_NOW, lease_owner=None)
        )
    return trajectory_id


def test_explanation_materialization_of_five_hundred_turns_and_its_retrieval(
    harness: LoopHarness,
) -> None:
    trajectory_id = _five_hundred_turn_trajectory(harness)
    builder = harness.controller().explanations

    materialize_samples: list[float] = []
    for _ in range(3):
        builder.drop_revisions(trajectory_id)
        started = time.perf_counter()
        revision = builder.materialize(trajectory_id, now=_NOW)
        materialize_samples.append((time.perf_counter() - started) * 1000.0)
        assert revision.turn_count == 500
    _report(
        "explanation materialization, 500-turn trajectory",
        materialize_samples,
        target_ms=2_000,
        ceiling_ms=10_000,
    )

    def read() -> None:
        assert builder.read(trajectory_id).source == "materialized"

    samples = _timed(read)
    _report(
        "explanation retrieval, terminal (materialized), 500 turns",
        samples,
        target_ms=25,
        ceiling_ms=100,
    )


# --- Row 10: recovery of 100 in-flight trajectories at startup ≤ 2 s / 10 s -------------------


def test_recovery_of_one_hundred_in_flight_trajectories(harness: LoopHarness) -> None:
    ids = [harness.submit_bypass() for _ in range(100)]
    with harness.database.write() as session:
        session.execute(
            update(models.Trajectory)
            .where(models.Trajectory.id.in_(ids))
            .values(
                status=TrajectoryState.EXECUTING.value,
                lease_owner="ghost:9/0",
                lease_expires_at=_NOW - timedelta(seconds=1),
            )
        )
    controller = harness.controller("host:1/recovery")
    started = time.perf_counter()
    summary = recover(
        controller, harness.database, owner_prefix="host:1", now=_NOW, only_expired=False
    )
    elapsed = (time.perf_counter() - started) * 1000.0
    assert summary.touched == 100, summary.as_json()
    _report(
        "recovery of 100 in-flight trajectories at startup",
        [elapsed],
        target_ms=2_000,
        ceiling_ms=10_000,
    )
