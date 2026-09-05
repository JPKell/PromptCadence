"""Gate B: approval in three modes, minting as its output, on both paths.

``manual`` holds a planned plan **and** a bypassed trajectory (§0.2(5) — the hole this row
closes); ``hybrid`` mints the ungated steps and parks at the point a gated step becomes ready;
a grant mints under the approver's identity and a bypass-gate grant **supersedes**; a denial
halts with the denial recorded; a timeout is never a grant; grants are idempotent per request;
a scoped re-approval mints revision *n+1* for that step only; a ceiling raise is a grant with a
budget. The invariant under every test: **no turn executes under an intent whose gate fired and
whose grant is not in the record.**
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from baseaicore import DataClassification, ValidationError
from sqlalchemy import select
from tests.fakes.harness import (
    PRICING_DOCUMENT,
    LoopHarness,
    open_harness,
    plan_document,
    remote_tier_env,
    step,
)
from tests.fakes.loadcoach_app import ScriptedGeneration

from promptcadence.config import Settings, load_settings
from promptcadence.domain.errors import ApprovalInvalidStateError, ErrorCode
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models
from promptcadence.services.approvals import Approver, BudgetRaise
from promptcadence.services.pricing import PricingCatalog, load_pricing_records

OPS = Approver(token_id="01TOKENOPS00000000000000AA", name="ops")  # noqa: S106 — a test id


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "2")
    return load_settings().settings


@pytest.fixture
def pricing_file(tmp_path: Path) -> Path:
    path = tmp_path / "remote_cheap.pricing.json"
    path.write_text(PRICING_DOCUMENT, encoding="utf-8")
    return path


def _assert_no_turn_ran_under_an_ungranted_gate(harness: LoopHarness, trajectory_id: str) -> None:
    """The assertion §0.3 says to write first."""
    with harness.database.read() as session:
        intents = {
            (row.intent_id, row.revision): row
            for row in session.execute(
                select(models.ExecutionIntent).where(
                    models.ExecutionIntent.trajectory_id == trajectory_id
                )
            ).scalars()
        }
    for record in harness.service.turns(trajectory_id):
        if record.turn.role.value != "assistant":
            continue
        row = intents[(record.turn.provenance.intent_id, record.turn.provenance.intent_revision)]
        gated = bool(row.gate_json.get("egress_gated") or row.gate_json.get("cost_gated"))
        manual = harness.settings.approval.mode == "manual"
        if gated or manual:
            assert row.approval_request_id is not None, (
                f"turn {record.turn.turn_id} ran under {row.intent_id}@{row.revision}, whose gate "
                "fired with no grant in the record"
            )
            assert row.minted_by.startswith("approver:")


# --------------------------------------------------------------------------------------------
# manual
# --------------------------------------------------------------------------------------------


def test_manual_holds_a_planned_trajectory_and_a_grant_mints_every_step_under_the_approver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, PROMPTCADENCE_APPROVAL__MODE="manual")
    with open_harness(settings) as harness:
        harness.script_plan(plan_document(step("s1"), step("s2", depends_on=["s1"])))
        trajectory_id = harness.submit_planned()
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.AWAITING_APPROVAL
        assert harness.events(trajectory_id) == [
            "trajectory.created",
            "trajectory.claimed",
            "plan.drafted",
            "approval.requested",
        ]
        view = harness.service.get(trajectory_id)
        assert view.lease_owner is None and "manual" in (view.halted_reason or "")
        (pending,) = harness.approvals.pending()
        assert pending.kind.value == "plan" and pending.reason == "manual_mode"
        assert pending.step_ids == ("s1", "s2")
        assert pending.expires_at == pending.created_at + timedelta(hours=24)
        with harness.database.read() as session:
            assert session.execute(select(models.ExecutionIntent)).scalars().all() == []
            approval = session.execute(select(models.PlanApproval)).scalar_one()
        assert approval.outcome == "gated"

        outcome = harness.approvals.grant(trajectory_id, approver=OPS)
        assert outcome.state is TrajectoryState.EXECUTING and len(outcome.minted) == 2
        assert {intent.minted_by.as_recorded() for intent in outcome.minted} == {
            f"approver:{OPS.token_id}"
        }
        assert {intent.approval_request_id for intent in outcome.minted} == {pending.request_id}
        assert harness.events(trajectory_id)[-3:] == [
            "approval.granted",
            "intent.minted",
            "intent.minted",
        ]
        again = harness.approvals.grant(trajectory_id, approver=OPS)
        assert again.already_resolved and again.minted == ()

        harness.script(ScriptedGeneration(text="s1 done"), ScriptedGeneration(text="s2 done"))
        assert harness.resume(trajectory_id) is TrajectoryState.COMPLETED
        _assert_no_turn_ran_under_an_ungranted_gate(harness, trajectory_id)
        assert harness.approvals.pending() == []


def test_manual_holds_a_bypassed_trajectory_and_the_grant_supersedes_the_default_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§0.2(5), §0.3: the bypass removes planning, never approval of gated egress."""
    settings = _settings(monkeypatch, PROMPTCADENCE_APPROVAL__MODE="manual")
    with open_harness(settings) as harness:
        trajectory_id = harness.submit_bypass()
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.AWAITING_APPROVAL
        assert harness.fake.requests == [], "no turn ran before a person answered"
        assert harness.events(trajectory_id) == [
            "trajectory.created",
            "trajectory.claimed",
            "intent.minted",
            "approval.requested",
        ]
        (pending,) = harness.approvals.pending()
        assert pending.kind.value == "bypass_gate" and pending.step_ids == ("loop",)
        assert pending.reason == "manual_mode"

        outcome = harness.approvals.grant(trajectory_id, approver=OPS)
        (revision_two,) = outcome.minted
        assert revision_two.revision == 2 and revision_two.supersedes == 1
        assert revision_two.minted_by.as_recorded() == f"approver:{OPS.token_id}"
        assert revision_two.approval_request_id == pending.request_id
        with harness.database.read() as session:
            rows = (
                session.execute(
                    select(models.ExecutionIntent).order_by(models.ExecutionIntent.revision)
                )
                .scalars()
                .all()
            )
        assert [(row.revision, row.minted_by) for row in rows] == [
            (1, "bypass_default"),
            (2, f"approver:{OPS.token_id}"),
        ], "revision 1 is retained as the gated envelope nobody executed under"

        harness.script(ScriptedGeneration(text="done"))
        assert harness.resume(trajectory_id) is TrajectoryState.COMPLETED
        turns = harness.service.turns(trajectory_id)
        assert turns[-1].turn.provenance.intent_revision == 2
        _assert_no_turn_ran_under_an_ungranted_gate(harness, trajectory_id)


def test_a_denial_halts_with_the_denial_recorded_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, PROMPTCADENCE_APPROVAL__MODE="manual")
    with open_harness(settings) as harness:
        trajectory_id = harness.submit_bypass()
        harness.claim_and_run(trajectory_id)
        denied = harness.approvals.deny(trajectory_id, approver=OPS, reason="not today")
        assert denied.status.value == "denied" and denied.resolution_reason == "not today"
        assert denied.approver_token_id == OPS.token_id
        view = harness.service.get(trajectory_id)
        assert view.state is TrajectoryState.HALTED
        assert view.error_code == ErrorCode.APPROVAL_REQUIRED.value
        assert "not today" in (view.halted_reason or "")
        assert harness.events(trajectory_id)[-2:] == ["approval.denied", "trajectory.halted"]
        (event,) = harness.event_data(trajectory_id, "approval.denied")
        assert event["timed_out"] is False and event["approver_token_id"] == OPS.token_id
        assert harness.approvals.deny(trajectory_id, approver=OPS).status.value == "denied"
        with pytest.raises(ApprovalInvalidStateError):
            harness.approvals.grant(trajectory_id, approver=OPS)


def test_a_pending_request_expires_by_its_persisted_clock_and_a_timeout_is_never_a_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, PROMPTCADENCE_APPROVAL__MODE="manual")
    with open_harness(settings) as harness:
        trajectory_id = harness.submit_bypass()
        harness.claim_and_run(trajectory_id)
        assert harness.approvals.expire(now=harness.clock()) == ()
        harness.clock.advance(timedelta(hours=23))
        assert harness.approvals.expire(now=harness.clock()) == ()
        harness.clock.advance(timedelta(hours=2))
        (expired,) = harness.approvals.expire(now=harness.clock())
        view = harness.service.get(trajectory_id)
        assert view.state is TrajectoryState.HALTED
        assert "request_timeout_hours" in (view.halted_reason or "")
        assert harness.events(trajectory_id)[-2:] == ["approval.denied", "trajectory.halted"]
        (event,) = harness.event_data(trajectory_id, "approval.denied")
        assert event["timed_out"] is True and event["approval_request_id"] == expired
        assert harness.approvals.expire(now=harness.clock()) == (), "idempotent"
        assert harness.fake.requests == []


def test_a_trajectory_parks_on_exactly_one_pending_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, PROMPTCADENCE_APPROVAL__MODE="manual")
    with open_harness(settings) as harness:
        trajectory_id = harness.submit_bypass()
        harness.claim_and_run(trajectory_id)
        view = harness.service.get(trajectory_id)
        from promptcadence.domain.policy import VerdictReason
        from promptcadence.services.approvals import ApprovalKind

        with pytest.raises(ApprovalInvalidStateError), harness.sink.write() as (session, events):
            harness.approvals.request(
                session,
                events,
                view=view,
                kind=ApprovalKind.PLAN,
                reason=VerdictReason.MANUAL_MODE,
                step_ids=("loop",),
                detail=None,
                now=harness.clock(),
            )


# --------------------------------------------------------------------------------------------
# hybrid
# --------------------------------------------------------------------------------------------


def _hybrid(monkeypatch: pytest.MonkeyPatch, pricing_file: Path) -> Settings:
    return _settings(
        monkeypatch,
        PROMPTCADENCE_APPROVAL__MODE="hybrid",
        **remote_tier_env(str(pricing_file)),
    )


def test_hybrid_runs_the_ungated_step_first_and_parks_when_the_gated_step_becomes_ready(
    monkeypatch: pytest.MonkeyPatch, pricing_file: Path
) -> None:
    """Spec §20 #7: a step needing internal egress pauses, is listed, and a deny halts."""
    settings = _hybrid(monkeypatch, pricing_file)
    with open_harness(settings, remote_provider=True) as harness:
        harness.script_plan(
            plan_document(
                step("s1", tools=[], data_classification="internal"),
                step(
                    "s2",
                    depends_on=["s1"],
                    tier="remote_cheap",
                    data_classification="internal",
                    tools=[],
                ),
            )
        )
        harness.script(ScriptedGeneration(text="s1 done"))
        trajectory_id = harness.submit_planned(classification=DataClassification.INTERNAL)
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.AWAITING_APPROVAL
        assert harness.events(trajectory_id) == [
            "trajectory.created",
            "trajectory.claimed",
            "plan.drafted",
            "plan.approved",
            "intent.minted",  # s1 only
            "step.started",
            "turn.started",
            "budget.debited",
            "turn.completed",
            "step.completed",
            "approval.requested",  # s2 became ready and its gate fired
        ]
        (minted,) = harness.event_data(trajectory_id, "intent.minted")
        assert minted["step_id"] == "s1" and minted["gated"] is False
        (pending,) = harness.approvals.pending()
        assert pending.kind.value == "gated_step" and pending.step_ids == ("s2",)
        assert pending.reason == "gated_egress"
        with harness.database.read() as session:
            approval = session.execute(select(models.PlanApproval)).scalar_one()
        verdicts = {v["step_id"]: v for v in approval.verdict_json["steps"]}
        assert verdicts["s2"]["requires_human_approval"] is True
        assert verdicts["s2"]["gate"]["egress_gated"] is True
        assert verdicts["s2"]["gate"]["gating_tier"] == "remote_cheap"

        denied = harness.approvals.deny(trajectory_id, approver=OPS, reason="keep it local")
        assert denied.status.value == "denied"
        view = harness.service.get(trajectory_id)
        assert view.state is TrajectoryState.HALTED
        assert view.error_code == ErrorCode.APPROVAL_REQUIRED.value
        assert "keep it local" in (view.halted_reason or "")
        assert len(harness.fake.requests) == 2, "the plan and s1; s2 never ran"
        _assert_no_turn_ran_under_an_ungranted_gate(harness, trajectory_id)


def test_hybrid_grant_mints_the_gated_step_under_the_approver_and_it_runs(
    monkeypatch: pytest.MonkeyPatch, pricing_file: Path
) -> None:
    settings = _hybrid(monkeypatch, pricing_file)
    pricing = PricingCatalog(by_tier={"remote_cheap": load_pricing_records(pricing_file)})
    with open_harness(settings, remote_provider=True, pricing=pricing) as harness:
        harness.script_plan(
            plan_document(
                step("s1", tools=[], data_classification="internal"),
                step(
                    "s2",
                    depends_on=["s1"],
                    tier="remote_cheap",
                    data_classification="internal",
                    tools=[],
                ),
            )
        )
        harness.script(ScriptedGeneration(text="s1 done"))
        trajectory_id = harness.submit_planned(classification=DataClassification.INTERNAL)
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.AWAITING_APPROVAL
        (pending,) = harness.approvals.pending()
        outcome = harness.approvals.grant(trajectory_id, approver=OPS)
        (s2,) = outcome.minted
        assert s2.step_id == "s2" and s2.approved_tier == "remote_cheap"
        assert s2.minted_by.as_recorded() == f"approver:{OPS.token_id}"
        assert s2.approval_request_id == pending.request_id
        assert s2.gate.egress_gated is True, "the record says the gate fired, and who granted it"
        harness.script(ScriptedGeneration(text="s2 done"))
        assert harness.resume(trajectory_id) is TrajectoryState.COMPLETED
        turns = harness.service.turns(trajectory_id)
        s2_turns = [t for t in turns if t.step_id == "s2"]
        assert s2_turns[-1].turn.provenance.tier == "remote_cheap"
        assert "s1 done" in (s2_turns[1].turn.content or ""), "the dependency's result framed s2"
        _assert_no_turn_ran_under_an_ungranted_gate(harness, trajectory_id)


def test_hybrid_with_every_step_gated_parks_from_planning(
    monkeypatch: pytest.MonkeyPatch, pricing_file: Path
) -> None:
    settings = _hybrid(monkeypatch, pricing_file)
    with open_harness(settings, remote_provider=True) as harness:
        harness.script_plan(
            plan_document(step("s1", tier="remote_cheap", data_classification="internal", tools=[]))
        )
        trajectory_id = harness.submit_planned(classification=DataClassification.INTERNAL)
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.AWAITING_APPROVAL
        assert harness.events(trajectory_id) == [
            "trajectory.created",
            "trajectory.claimed",
            "plan.drafted",
            "approval.requested",
        ]
        (pending,) = harness.approvals.pending()
        assert pending.kind.value == "gated_step" and pending.step_ids == ("s1",)


def test_hybrid_gates_a_bypassed_trajectory_pinned_to_a_remote_tier(
    monkeypatch: pytest.MonkeyPatch, pricing_file: Path
) -> None:
    """ADR-0049 rule 3: the gate fires at the minting of the default intent."""
    settings = _hybrid(monkeypatch, pricing_file)
    with open_harness(settings) as harness:
        trajectory_id = harness.submit_bypass(
            classification=DataClassification.INTERNAL, tier="remote_cheap"
        )
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.AWAITING_APPROVAL
        (pending,) = harness.approvals.pending()
        assert pending.kind.value == "bypass_gate" and pending.reason == "gated_egress"
        local = harness.submit_bypass(classification=DataClassification.INTERNAL)
        harness.script(ScriptedGeneration(text="done"))
        assert harness.claim_and_run(local) is TrajectoryState.COMPLETED, "ungated: no pause"


# --------------------------------------------------------------------------------------------
# redline, re-approval, ceiling raise
# --------------------------------------------------------------------------------------------


def test_a_redlined_step_runs_on_the_substitute_while_the_plan_keeps_the_original(
    monkeypatch: pytest.MonkeyPatch, pricing_file: Path
) -> None:
    settings = _settings(monkeypatch, **remote_tier_env(str(pricing_file)))
    with open_harness(settings, remote_provider=True) as harness:
        harness.script_plan(plan_document(step("s1", tier="remote_cheap", tools=[])))
        harness.script(ScriptedGeneration(text="done"))
        trajectory_id = harness.submit_planned()  # confidential: remote_cheap does not admit it
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
        with harness.database.read() as session:
            plan_step = session.execute(select(models.PlanStep)).scalar_one()
            intent = session.execute(select(models.ExecutionIntent)).scalar_one()
            approval = session.execute(select(models.PlanApproval)).scalar_one()
        assert plan_step.tier == "remote_cheap"
        assert intent.approved_tier == "local_fast"
        (verdict,) = approval.verdict_json["steps"]
        assert verdict["outcome"] == "redlined"
        assert verdict["reason"] == "tier_ceiling_substitution"
        (approved,) = harness.event_data(trajectory_id, "plan.approved")
        assert approved["redlined_count"] == 1
        assert harness.fake.requests[-1]["body"]["task"] == "tools.agent.local_fast"


def test_a_scoped_reapproval_mints_a_superseding_revision_for_that_step_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §20 #8: ``undeclared_tool`` under ``any_deviation``, both revisions retained."""
    settings = _settings(monkeypatch, PROMPTCADENCE_PLANNING__REAPPROVAL_SCOPE="any_deviation")
    with open_harness(settings) as harness:
        (harness.tools.workspace_root / "seed").mkdir(parents=True, exist_ok=True)
        harness.script_plan(plan_document(step("s1", tools=["read_file"]), step("s2", tools=[])))
        harness.script(
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
            )
        )
        trajectory_id = harness.submit_planned()
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.AWAITING_APPROVAL
        assert harness.events(trajectory_id)[-3:] == [
            "turn.completed",
            "deviation.detected",
            "approval.requested",
        ]
        (pending,) = harness.approvals.pending()
        assert pending.kind.value == "reapproval" and pending.step_ids == ("s1",)
        assert pending.detail is not None
        assert pending.detail["category"] == "undeclared_tool"
        assert pending.detail["tools"] == ["list_dir"]

        outcome = harness.approvals.grant(trajectory_id, approver=OPS)
        (revised,) = outcome.minted
        assert revised.step_id == "s1" and revised.revision == 2 and revised.supersedes == 1
        assert revised.approved_tools == frozenset({"read_file", "list_dir"})
        with harness.database.read() as session:
            rows = (
                session.execute(
                    select(models.ExecutionIntent).order_by(
                        models.ExecutionIntent.step_id, models.ExecutionIntent.revision
                    )
                )
                .scalars()
                .all()
            )
        assert [(row.step_id, row.revision) for row in rows] == [("s1", 1), ("s1", 2), ("s2", 1)]

        harness.script(ScriptedGeneration(text="s1 done"), ScriptedGeneration(text="s2 done"))
        assert harness.resume(trajectory_id) is TrajectoryState.COMPLETED
        turns = harness.service.turns(trajectory_id)
        s1 = [t for t in turns if t.step_id == "s1"]
        assert [t.turn.role.value for t in s1] == ["user", "user", "assistant", "tool", "assistant"]
        assert s1[3].turn.provenance.intent_revision == 2, "the pending call ran under the grant"
        assert s1[2].turn.provenance.intent_revision == 1, "the drifted turn keeps its envelope"
        with harness.database.read() as session:
            record = session.execute(select(models.ToolCallRecord)).scalar_one()
        assert record.tool_name == "list_dir" and record.status == "ok"


def test_a_tool_outside_the_trajectory_allowlist_is_never_reapprovable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, PROMPTCADENCE_PLANNING__REAPPROVAL_SCOPE="any_deviation")
    with open_harness(settings) as harness:
        harness.script_plan(plan_document(step("s1", tools=["read_file"])))
        harness.script(
            ScriptedGeneration(
                text="",
                tool_calls=(
                    {
                        "call_index": 0,
                        "id": "c1",
                        "name": "write_file",
                        "arguments_fragment": '{"path": "x", "content": "y"}',
                    },
                ),
            ),
            ScriptedGeneration(text="fine"),
        )
        trajectory_id = harness.submit_planned(tools=("read_file",))
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
        with harness.database.read() as session:
            deviation = session.execute(select(models.Deviation)).scalar_one()
            record = session.execute(select(models.ToolCallRecord)).scalar_one()
        assert deviation.disposition == "refused_not_reapprovable"
        assert record.status == "refused" and record.reason == "not_allowlisted"
        assert harness.approvals.pending() == []


def test_a_ceiling_raise_is_granted_with_a_new_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    with open_harness(settings) as harness:
        trajectory_id = harness.submit_bypass(token_budget=100)  # below the 5120-token estimate
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.AWAITING_APPROVAL
        (pending,) = harness.approvals.pending()
        assert pending.kind.value == "ceiling_raise" and pending.reason == "budget_exceeded"
        assert pending.detail == {"scope": "trajectory", "step_id": "loop"}
        with pytest.raises(ValidationError):
            harness.approvals.grant(trajectory_id, approver=OPS)  # a raise needs a budget
        outcome = harness.approvals.grant(
            trajectory_id, approver=OPS, budget_raise=BudgetRaise(token_ceiling=100_000)
        )
        (revised,) = outcome.minted
        assert revised.revision == 2 and revised.token_budget == 100_000
        assert harness.service.get(trajectory_id).token_budget == 100_000
        harness.script(ScriptedGeneration(text="done"))
        assert harness.resume(trajectory_id) is TrajectoryState.COMPLETED
