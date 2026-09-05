"""§6: gates are evaluated against the MOST permissive tier in the intent's set (ADR-0056 rule 4).

A ``local_*`` primary with a ``remote_*`` fallback must gate as remote, on every minting path a
fallback can arrive by — a hand-built verdict, and a supersession.
"""

from __future__ import annotations

from datetime import datetime

from baseaicore import DataClassification

from promptcadence.domain.intent import (
    MintedBy,
    MintKind,
    mint_bypass_default,
    mint_for_step,
    supersede,
)
from promptcadence.domain.plan import PlanStep
from promptcadence.domain.policy import (
    ApprovalMode,
    ApprovalPolicy,
    StepEstimate,
    StepOutcome,
    StepVerdict,
    VerdictReason,
    requires_human_approval,
)
from promptcadence.domain.tiers import TierPolicy
from promptcadence.domain.trajectory import TrajectoryDeclaration


def _hybrid(policy: ApprovalPolicy) -> ApprovalPolicy:
    return ApprovalPolicy(
        mode=ApprovalMode.HYBRID,
        gate_egress_at=policy.gate_egress_at,
        gate_step_cost=policy.gate_step_cost,
    )


def test_a_remote_fallback_gates_a_local_primary_at_minting(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    hybrid = _hybrid(approval_policy)
    step = PlanStep(
        step_id="s1",
        description="x",
        depends_on=(),
        tools=("read_file",),
        tier="local_fast",
        data_classification=DataClassification.INTERNAL,
        expected_turns=1,
    )
    verdict = StepVerdict(
        step_id="s1",
        outcome=StepOutcome.APPROVED,
        reason=VerdictReason.AUTO_APPROVED,
        declared_tier="local_fast",
        estimate=StepEstimate(1000),
        approved_tier="local_fast",
        fallback_tiers=("remote_cheap",),
    )
    minted = mint_for_step(
        intent_id="01INTENT00000000000000000A",
        declaration=declaration,
        step=step,
        verdict=verdict,
        tier_policy=tier_policy,
        policy=hybrid,
        minted_by=MintedBy(MintKind.POLICY),
        minted_at=minted_at,
        max_turns=8,
        token_budget=2000,
    )
    assert minted.gate.gating_tier == "remote_cheap"
    assert minted.gate.egress_gated is True
    assert minted.permitted_egress_class.value == "remote"
    assert requires_human_approval(minted.gate, mode=ApprovalMode.HYBRID) is True


def test_a_supersession_that_adds_a_remote_fallback_is_gated_as_remote(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    hybrid = _hybrid(approval_policy)
    first = mint_bypass_default(
        intent_id="01INTENT00000000000000000B",
        declaration=declaration,
        tier_policy=tier_policy,
        policy=hybrid,
        minted_at=minted_at,
    )
    assert first.gate.gated is False, "a local-only envelope gates nothing"
    second = supersede(
        first,
        tier_policy=tier_policy,
        policy=hybrid,
        minted_by=MintedBy(MintKind.APPROVER, approver_token_id="01TOKEN"),  # noqa: S106
        minted_at=minted_at,
        fallback_tiers=("remote_cheap",),
        approval_request_id="01REQUEST",
    )
    assert second.gate.gating_tier == "remote_cheap"
    assert second.gate.egress_gated is True
    assert requires_human_approval(second.gate, mode=ApprovalMode.HYBRID) is True
