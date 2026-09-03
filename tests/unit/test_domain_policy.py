"""Tests for promptcadence.domain.policy: the automatic verdict, the gates, and the version.

The last of those is the point of ``test_the_ruleset_digest_pins_this_modules_decisions``: the
``approval_policy_version`` persisted on every trajectory must change when the rules change, and
"remember to bump it" is not enforcement.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from baseaicore import DataClassification, Money, ValidationError, canonical_json, sha256_of

from promptcadence.domain.errors import ErrorCode
from promptcadence.domain.plan import Plan, PlanStep, validate_plan_document
from promptcadence.domain.policy import (
    APPROVAL_RULESET_DIGEST,
    ApprovalMode,
    ApprovalPolicy,
    BudgetHeadroom,
    EstimateSource,
    GateVerdict,
    PartialPricing,
    ReapprovalScope,
    StepEstimate,
    StepOutcome,
    StepVerdict,
    TrajectoryOutcome,
    VerdictReason,
    evaluate_gates,
    evaluate_plan,
    evaluate_step,
    requires_human_approval,
)
from promptcadence.domain.tiers import TierPolicy
from promptcadence.domain.trajectory import TrajectoryDeclaration

_GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"
_ALLOWLIST = frozenset({"read_file", "list_dir"})
_TIERS = frozenset({"local_fast", "local_large", "remote_cheap", "remote_frontier"})
_USD = "USD"


def _plan(*steps: dict[str, Any]) -> Plan:
    """Validate a plan document made of the given step dictionaries."""
    document = json.dumps({"steps": list(steps)})
    return validate_plan_document(
        document,
        trajectory_allowlist=_ALLOWLIST,
        trajectory_classification=DataClassification.CONFIDENTIAL,
        configured_tiers=_TIERS,
        max_plan_steps=20,
    )


def _step_doc(step_id: str = "s1", **overrides: Any) -> dict[str, Any]:
    """One well-formed step document."""
    return {
        "step_id": step_id,
        "description": "do the thing",
        "depends_on": [],
        "tools": ["read_file"],
        "tier": "local_fast",
        "data_classification": "internal",
        "expected_turns": 2,
        **overrides,
    }


def _raw_step(**overrides: Any) -> PlanStep:
    """A ``PlanStep`` built directly, bypassing document validation.

    Needed for the tier-not-configured rule: a plan document naming an unconfigured tier is
    already refused by :func:`validate_plan_document`, so the only way this rule fires in
    production is a configuration edit between drafting and approval — which is exactly why the
    approver checks it against the trajectory's own snapshot rather than trusting validation.
    """
    defaults: dict[str, Any] = {
        "step_id": "s1",
        "description": "do the thing",
        "depends_on": (),
        "tools": ("read_file",),
        "tier": "local_fast",
        "data_classification": DataClassification.INTERNAL,
        "expected_turns": 2,
    }
    return PlanStep(**{**defaults, **overrides})


def _estimate(tokens: int = 2_000, nanos: int | None = None) -> StepEstimate:
    """A configured-default estimate, priced only when asked."""
    money = Money(currency=_USD, nanos=nanos) if nanos is not None else None
    return StepEstimate(tokens, money_estimate=money)


# --------------------------------------------------------------------------------------------
# Value-object invariants
# --------------------------------------------------------------------------------------------


def test_a_historical_estimate_must_name_its_samples() -> None:
    """A "historical" estimate with nothing behind it is a default wearing a better label."""
    with pytest.raises(ValidationError, match="at least one sample"):
        StepEstimate(10, source=EstimateSource.HISTORICAL)
    assert StepEstimate(10, source=EstimateSource.HISTORICAL, sample_count=20).sample_count == 20


def test_headroom_mirrors_the_ledger_verdict_and_keeps_the_honesty_counts() -> None:
    """F1's adapter must be a copy, not an interpretation (LoadLedger ``CeilingVerdict``)."""
    names = {field.name for field in dataclasses.fields(BudgetHeadroom)}
    assert {
        "exceeded",
        "money_remaining",
        "tokens_remaining",
        "unpriced_debit_count",
        "untotalled_debit_count",
        "unmetered_debit_count",
    } <= names


def test_headroom_refuses_an_untotalled_count_outside_the_unpriced_set() -> None:
    with pytest.raises(ValidationError, match="subset"):
        BudgetHeadroom(
            scope="day", exceeded=False, unpriced_debit_count=1, untotalled_debit_count=2
        )


def test_a_floor_is_reported_as_one_and_strict_pricing_makes_it_bind() -> None:
    """ADR-0069: under STRICT, an amount that cannot be shown to be under the cap is over it."""
    floor = BudgetHeadroom(
        scope="day",
        exceeded=False,
        money_remaining=Money(currency=_USD, nanos=1),
        unpriced_debit_count=3,
        untotalled_debit_count=2,
    )
    assert floor.money_is_floor is True
    assert floor.binds is False
    strict = dataclasses.replace(floor, partial_pricing=PartialPricing.STRICT)
    assert strict.binds is True


def test_a_strict_ceiling_does_not_bind_on_an_unpriced_local_debit() -> None:
    """A local model's debit is unpriced, not untotalled; STRICT fires on the latter only."""
    local_only = BudgetHeadroom(
        scope="trajectory",
        exceeded=False,
        unpriced_debit_count=4,
        untotalled_debit_count=0,
        partial_pricing=PartialPricing.STRICT,
    )
    assert local_only.binds is False


def test_the_approval_policy_refuses_an_unbounded_wait_or_a_gate_that_fires_on_everything() -> None:
    with pytest.raises(ValidationError, match="request_timeout_hours"):
        ApprovalPolicy(request_timeout_hours=0)
    with pytest.raises(ValidationError, match="gate_step_cost"):
        ApprovalPolicy(gate_step_cost=Money(currency=_USD, nanos=0))


def test_a_rejected_verdict_names_a_code_and_no_tier() -> None:
    with pytest.raises(ValidationError, match="error code"):
        StepVerdict(
            step_id="s1",
            outcome=StepOutcome.REJECTED,
            reason=VerdictReason.BUDGET_EXCEEDED,
            declared_tier="local_fast",
            estimate=_estimate(),
        )
    with pytest.raises(ValidationError, match="names an approved tier"):
        StepVerdict(
            step_id="s1",
            outcome=StepOutcome.REJECTED,
            reason=VerdictReason.BUDGET_EXCEEDED,
            declared_tier="local_fast",
            estimate=_estimate(),
            approved_tier="local_fast",
            error_code=ErrorCode.BUDGET_EXCEEDED,
        )
    with pytest.raises(ValidationError, match="names no tier to run on"):
        StepVerdict(
            step_id="s1",
            outcome=StepOutcome.APPROVED,
            reason=VerdictReason.AUTO_APPROVED,
            declared_tier="local_fast",
            estimate=_estimate(),
        )


# --------------------------------------------------------------------------------------------
# The gates
# --------------------------------------------------------------------------------------------


def test_a_local_tier_never_fires_the_egress_gate(
    tier_policy: TierPolicy, approval_policy: ApprovalPolicy
) -> None:
    gate = evaluate_gates(
        (tier_policy.snapshot.require("local_fast"),),
        classification=DataClassification.CONFIDENTIAL,
        estimate=_estimate(),
        policy=approval_policy,
    )
    assert gate.egress_gated is False
    assert gate.egress_classification is None


def test_the_egress_gate_fires_at_or_above_the_configured_level(
    tier_policy: TierPolicy,
) -> None:
    remote = tier_policy.snapshot.require("remote_cheap")
    at_internal = ApprovalPolicy(gate_egress_at=DataClassification.INTERNAL)
    assert (
        evaluate_gates(
            (remote,),
            classification=DataClassification.PUBLIC,
            estimate=_estimate(),
            policy=at_internal,
        ).egress_gated
        is False
    )
    assert (
        evaluate_gates(
            (remote,),
            classification=DataClassification.INTERNAL,
            estimate=_estimate(),
            policy=at_internal,
        ).egress_gated
        is True
    )


def test_the_cost_gate_fires_above_the_configured_amount(tier_policy: TierPolicy) -> None:
    policy = ApprovalPolicy(gate_step_cost=Money(currency=_USD, nanos=1_000_000_000))
    local = (tier_policy.snapshot.require("local_fast"),)
    assert (
        evaluate_gates(
            local,
            classification=DataClassification.PUBLIC,
            estimate=_estimate(nanos=1_000_000_000),
            policy=policy,
        ).cost_gated
        is False
    ), "spending exactly the gate is not above it"
    assert (
        evaluate_gates(
            local,
            classification=DataClassification.PUBLIC,
            estimate=_estimate(nanos=1_000_000_001),
            policy=policy,
        ).cost_gated
        is True
    )


def test_the_cost_gate_does_not_fire_on_an_unpriced_estimate(tier_policy: TierPolicy) -> None:
    """A local model's cost is ``UNSUPPORTED``; treating it as ``$0`` or as huge both mislead."""
    gate = evaluate_gates(
        (tier_policy.snapshot.require("local_fast"),),
        classification=DataClassification.PUBLIC,
        estimate=_estimate(),
        policy=ApprovalPolicy(gate_step_cost=Money(currency=_USD, nanos=1)),
    )
    assert gate.cost_gated is False


@pytest.mark.parametrize(
    ("mode", "gated", "expected"),
    [
        (ApprovalMode.AUTO, False, False),
        (ApprovalMode.AUTO, True, False),
        (ApprovalMode.HYBRID, False, False),
        (ApprovalMode.HYBRID, True, True),
        (ApprovalMode.MANUAL, False, True),
        (ApprovalMode.MANUAL, True, True),
    ],
)
def test_requires_human_approval_over_every_mode_and_gate_state(
    mode: ApprovalMode, gated: bool, expected: bool
) -> None:
    gate = GateVerdict(egress_gated=gated, cost_gated=False, gating_tier="t")
    assert requires_human_approval(gate, mode=mode) is expected


# --------------------------------------------------------------------------------------------
# Step verdicts
# --------------------------------------------------------------------------------------------


def test_a_step_on_an_admitting_configured_tier_is_approved(
    declaration: TrajectoryDeclaration, tier_policy: TierPolicy, approval_policy: ApprovalPolicy
) -> None:
    plan = _plan(_step_doc())
    verdict = evaluate_step(
        plan.steps[0],
        declaration=declaration,
        tier_policy=tier_policy,
        policy=approval_policy,
        estimate=_estimate(),
    )
    assert verdict.outcome is StepOutcome.APPROVED
    assert verdict.approved_tier == "local_fast"
    assert verdict.fallback_tiers == (), "the automatic policy grants no fallbacks"


def test_a_tier_the_snapshot_does_not_define_is_rejected(
    declaration: TrajectoryDeclaration, tier_policy: TierPolicy, approval_policy: ApprovalPolicy
) -> None:
    verdict = evaluate_step(
        _raw_step(tier="gpt_9"),
        declaration=declaration,
        tier_policy=tier_policy,
        policy=approval_policy,
        estimate=_estimate(),
    )
    assert verdict.outcome is StepOutcome.REJECTED
    assert verdict.error_code is ErrorCode.TIER_NOT_CONFIGURED


def test_a_tier_that_cannot_admit_is_redlined_to_the_first_that_can(
    declaration: TrajectoryDeclaration, tier_policy: TierPolicy, approval_policy: ApprovalPolicy
) -> None:
    """Lifecycle §4.2's own example: "remote_cheap -> local_large, classification ceiling"."""
    plan = _plan(_step_doc(tier="remote_frontier", data_classification="confidential"))
    verdict = evaluate_step(
        plan.steps[0],
        declaration=declaration,
        tier_policy=tier_policy,
        policy=approval_policy,
        estimate=_estimate(),
    )
    assert verdict.outcome is StepOutcome.REDLINED
    assert verdict.reason is VerdictReason.TIER_CEILING_SUBSTITUTION
    assert verdict.declared_tier == "remote_frontier"
    assert verdict.approved_tier == "local_fast"


def test_an_unpriced_remote_tier_is_refused_rather_than_treated_as_free(
    declaration: TrajectoryDeclaration, tier_policy: TierPolicy, approval_policy: ApprovalPolicy
) -> None:
    """Spec §11 contract 5: a ceiling cannot bind what cannot be priced."""
    unpriced = dataclasses.replace(
        tier_policy,
        snapshot=dataclasses.replace(
            tier_policy.snapshot,
            tiers=tuple(
                dataclasses.replace(tier, pricing_source="")
                if tier.name == "remote_cheap"
                else tier
                for tier in tier_policy.snapshot.tiers
            ),
        ),
    )
    plan = _plan(_step_doc(tier="remote_cheap"))
    verdict = evaluate_step(
        plan.steps[0],
        declaration=declaration,
        tier_policy=unpriced,
        policy=approval_policy,
        estimate=_estimate(),
    )
    assert verdict.outcome is StepOutcome.REJECTED
    assert verdict.error_code is ErrorCode.UNPRICED_EGRESS_REFUSED


def test_a_remote_tier_is_rejected_until_loadcoach_registers_a_remote_provider(
    declaration: TrajectoryDeclaration,
    local_only_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
) -> None:
    """Rejected at approval rather than approved and failed at dispatch (lifecycle §3)."""
    plan = _plan(_step_doc(tier="remote_cheap"))
    verdict = evaluate_step(
        plan.steps[0],
        declaration=declaration,
        tier_policy=local_only_policy,
        policy=approval_policy,
        estimate=_estimate(),
    )
    assert verdict.outcome is StepOutcome.REJECTED
    assert verdict.error_code is ErrorCode.TIER_UNAVAILABLE
    assert verdict.reason is VerdictReason.TIER_UNAVAILABLE


def test_a_hybrid_gate_marks_the_step_for_a_person_without_rejecting_it(
    declaration: TrajectoryDeclaration, tier_policy: TierPolicy
) -> None:
    plan = _plan(_step_doc(tier="remote_cheap"))
    verdict = evaluate_step(
        plan.steps[0],
        declaration=declaration,
        tier_policy=tier_policy,
        policy=ApprovalPolicy(mode=ApprovalMode.HYBRID, gate_egress_at=DataClassification.INTERNAL),
        estimate=_estimate(),
    )
    assert verdict.outcome is StepOutcome.APPROVED
    assert verdict.requires_human_approval is True
    assert verdict.reason is VerdictReason.GATED_EGRESS


# --------------------------------------------------------------------------------------------
# Plan verdicts and budget
# --------------------------------------------------------------------------------------------


def test_a_plan_missing_an_estimate_is_refused_rather_than_approved(
    declaration: TrajectoryDeclaration, tier_policy: TierPolicy, approval_policy: ApprovalPolicy
) -> None:
    """An unbounded step inside a bounded plan is the thing approval exists to prevent."""
    plan = _plan(_step_doc("s1"), _step_doc("s2"))
    with pytest.raises(ValidationError, match="no estimate"):
        evaluate_plan(
            plan,
            declaration=declaration,
            tier_policy=tier_policy,
            policy=approval_policy,
            estimates={"s1": _estimate()},
        )


def test_a_binding_ceiling_rejects_the_whole_plan_and_names_it_on_every_step(
    declaration: TrajectoryDeclaration, tier_policy: TierPolicy, approval_policy: ApprovalPolicy
) -> None:
    plan = _plan(_step_doc("s1"), _step_doc("s2"))
    verdict = evaluate_plan(
        plan,
        declaration=declaration,
        tier_policy=tier_policy,
        policy=approval_policy,
        estimates={"s1": _estimate(), "s2": _estimate()},
        headroom=[BudgetHeadroom(scope="day", exceeded=True)],
    )
    assert verdict.outcome is TrajectoryOutcome.REJECTED
    assert {step.reason for step in verdict.steps} == {VerdictReason.BUDGET_EXCEEDED}
    assert verdict.headroom[0].exceeded is True, "the numbers travel with the refusal (spec §13)"


def test_the_sum_of_estimates_must_fit_not_each_step_alone(
    declaration: TrajectoryDeclaration, tier_policy: TierPolicy, approval_policy: ApprovalPolicy
) -> None:
    """Lifecycle §6: a per-step check approves every step and the plan collectively over budget."""
    plan = _plan(_step_doc("s1"), _step_doc("s2"))
    headroom = [BudgetHeadroom(scope="trajectory", exceeded=False, tokens_remaining=3_000)]
    estimates = {"s1": _estimate(2_000), "s2": _estimate(2_000)}
    verdict = evaluate_plan(
        plan,
        declaration=declaration,
        tier_policy=tier_policy,
        policy=approval_policy,
        estimates=estimates,
        headroom=headroom,
    )
    assert verdict.outcome is TrajectoryOutcome.REJECTED
    assert {step.reason for step in verdict.steps} == {VerdictReason.ESTIMATES_EXCEED_HEADROOM}

    roomy = [BudgetHeadroom(scope="trajectory", exceeded=False, tokens_remaining=4_000)]
    assert (
        evaluate_plan(
            plan,
            declaration=declaration,
            tier_policy=tier_policy,
            policy=approval_policy,
            estimates=estimates,
            headroom=roomy,
        ).outcome
        is TrajectoryOutcome.APPROVED
    )


def test_an_approval_against_a_floor_records_that_it_was_one(
    declaration: TrajectoryDeclaration, tier_policy: TierPolicy, approval_policy: ApprovalPolicy
) -> None:
    """ADR-0069's failure mode: approving against a floor while believing it a total."""
    plan = _plan(_step_doc())
    verdict = evaluate_plan(
        plan,
        declaration=declaration,
        tier_policy=tier_policy,
        policy=approval_policy,
        estimates={"s1": _estimate()},
        headroom=[
            BudgetHeadroom(
                scope="day",
                exceeded=False,
                money_remaining=Money(currency=_USD, nanos=10_000_000_000),
                unpriced_debit_count=7,
            )
        ],
    )
    assert verdict.outcome is TrajectoryOutcome.APPROVED
    assert all(step.headroom_is_floor for step in verdict.steps)


def test_one_rejected_step_rejects_the_trajectory_and_one_gated_step_gates_it(
    declaration: TrajectoryDeclaration, tier_policy: TierPolicy
) -> None:
    plan = _plan(_step_doc("s1"), _step_doc("s2", tier="remote_cheap"))
    estimates = {"s1": _estimate(), "s2": _estimate()}
    gated = evaluate_plan(
        plan,
        declaration=declaration,
        tier_policy=tier_policy,
        policy=ApprovalPolicy(mode=ApprovalMode.HYBRID, gate_egress_at=DataClassification.INTERNAL),
        estimates=estimates,
    )
    assert gated.outcome is TrajectoryOutcome.GATED

    bad = Plan(
        steps=(plan.steps[0], _raw_step(step_id="s2", tier="gpt_9")),
        raw_document=plan.raw_document,
        document_sha256=plan.document_sha256,
    )
    rejected = evaluate_plan(
        bad,
        declaration=declaration,
        tier_policy=tier_policy,
        policy=ApprovalPolicy(),
        estimates=estimates,
    )
    assert rejected.outcome is TrajectoryOutcome.REJECTED


def test_verdict_lookup_refuses_an_unknown_step(
    declaration: TrajectoryDeclaration, tier_policy: TierPolicy, approval_policy: ApprovalPolicy
) -> None:
    plan = _plan(_step_doc())
    verdict = evaluate_plan(
        plan,
        declaration=declaration,
        tier_policy=tier_policy,
        policy=approval_policy,
        estimates={"s1": _estimate()},
    )
    assert verdict.verdict_for("s1").step_id == "s1"
    with pytest.raises(ValidationError, match="no verdict"):
        verdict.verdict_for("s9")


# --------------------------------------------------------------------------------------------
# The version, and the corpus that pins it
# --------------------------------------------------------------------------------------------


def _corpus(tier_policy: TierPolicy, declaration: TrajectoryDeclaration) -> dict[str, Any]:
    """Every decision this module makes, over a fixed set of scenarios.

    Deliberately excludes ``approval_policy_version`` from what is hashed: the version *contains*
    :data:`APPROVAL_RULESET_DIGEST`, so including it would make the digest depend on itself.
    """
    validated = _plan(
        _step_doc("s1"),
        _step_doc("s2", tier="remote_cheap"),
        _step_doc("s3", tier="remote_frontier", data_classification="confidential"),
    )
    plan = Plan(
        steps=(*validated.steps, _raw_step(step_id="s4", tier="gpt_9")),
        raw_document=validated.raw_document,
        document_sha256=validated.document_sha256,
    )
    estimates = {
        "s1": StepEstimate(2_000, source=EstimateSource.HISTORICAL, sample_count=30),
        "s2": StepEstimate(3_000, money_estimate=Money(currency=_USD, nanos=2_000_000_000)),
        "s3": StepEstimate(4_000, money_estimate=Money(currency=_USD, nanos=500_000_000)),
        "s4": StepEstimate(1_000),
    }
    headrooms: dict[str, list[BudgetHeadroom]] = {
        "no_headroom": [],
        "roomy": [
            BudgetHeadroom(
                scope="trajectory",
                exceeded=False,
                tokens_remaining=1_000_000,
                money_remaining=Money(currency=_USD, nanos=50_000_000_000),
            )
        ],
        "exceeded": [BudgetHeadroom(scope="day", exceeded=True)],
        "floor": [
            BudgetHeadroom(
                scope="day",
                exceeded=False,
                tokens_remaining=1_000_000,
                money_remaining=Money(currency=_USD, nanos=50_000_000_000),
                unpriced_debit_count=5,
                untotalled_debit_count=2,
            )
        ],
        "strict_floor": [
            BudgetHeadroom(
                scope="day",
                exceeded=False,
                tokens_remaining=1_000_000,
                unpriced_debit_count=5,
                untotalled_debit_count=2,
                partial_pricing=PartialPricing.STRICT,
            )
        ],
        "tight": [BudgetHeadroom(scope="trajectory", exceeded=False, tokens_remaining=5_000)],
    }
    results: dict[str, Any] = {}
    for mode in ApprovalMode:
        for scope in ReapprovalScope:
            for gate_at in DataClassification:
                policy = ApprovalPolicy(
                    mode=mode,
                    gate_egress_at=gate_at,
                    gate_step_cost=Money(currency=_USD, nanos=1_000_000_000),
                    reapproval_scope=scope,
                )
                for headroom_name, headroom in headrooms.items():
                    for registered in (False, True):
                        key = (
                            f"{mode.value}|{scope.value}|{gate_at.value}|"
                            f"{headroom_name}|remote={registered}"
                        )
                        verdict = evaluate_plan(
                            plan,
                            declaration=declaration,
                            tier_policy=dataclasses.replace(
                                tier_policy, loadcoach_has_remote_provider=registered
                            ),
                            policy=policy,
                            estimates=estimates,
                            headroom=headroom,
                        )
                        results[key] = {
                            "outcome": verdict.outcome.value,
                            "steps": [step.as_canonical() for step in verdict.steps],
                        }
    return results


def test_the_ruleset_digest_pins_this_modules_decisions(
    tier_policy: TierPolicy, declaration: TrajectoryDeclaration
) -> None:
    """Enforcement, not a reminder: changing a rule cannot leave the version unchanged.

    A rule change moves this digest, this test fails and names the new value, and the only way to
    make it pass is to edit ``APPROVAL_RULESET_DIGEST`` in ``policy.py`` — which changes
    :attr:`ApprovalPolicy.version` for every deployment, automatically.
    """
    produced = "sha256:" + sha256_of(_corpus(tier_policy, declaration))
    assert produced == APPROVAL_RULESET_DIGEST, (
        "the approval ruleset changed. Set APPROVAL_RULESET_DIGEST in "
        f"src/promptcadence/domain/policy.py to {produced!r} — that is what makes every "
        "trajectory's approval_policy_version change with it."
    )


def test_the_version_changes_when_any_configured_value_changes(
    approval_policy: ApprovalPolicy,
) -> None:
    baseline = approval_policy.version
    assert baseline.startswith("sha256:")
    for changed in (
        dataclasses.replace(approval_policy, mode=ApprovalMode.HYBRID),
        dataclasses.replace(approval_policy, gate_egress_at=DataClassification.PUBLIC),
        dataclasses.replace(approval_policy, request_timeout_hours=48.0),
        dataclasses.replace(approval_policy, reapproval_scope=ReapprovalScope.ANY_DEVIATION),
        dataclasses.replace(
            approval_policy, gate_step_cost=Money(currency=_USD, nanos=2_000_000_000)
        ),
    ):
        assert changed.version != baseline


def test_the_version_is_stable_for_an_unchanged_policy(approval_policy: ApprovalPolicy) -> None:
    assert approval_policy.version == dataclasses.replace(approval_policy).version


def test_approval_verdict_goldens(
    tier_policy: TierPolicy, declaration: TrajectoryDeclaration
) -> None:
    """The determinism golden for approval evaluation (acceptance criterion 2).

    Six named scenarios rather than the whole 216-case corpus: the corpus is pinned by
    :data:`APPROVAL_RULESET_DIGEST` above, and a golden nobody can read in a diff is a golden
    nobody reviews. These are the cases a reader should be able to check by eye.
    """
    plan = _plan(
        _step_doc("s1"),
        _step_doc("s2", tier="remote_cheap"),
        _step_doc("s3", tier="remote_frontier", data_classification="confidential"),
    )
    estimates = {
        "s1": StepEstimate(2_000, source=EstimateSource.HISTORICAL, sample_count=30),
        "s2": StepEstimate(3_000, money_estimate=Money(currency=_USD, nanos=2_000_000_000)),
        "s3": StepEstimate(4_000, money_estimate=Money(currency=_USD, nanos=500_000_000)),
    }
    roomy = [
        BudgetHeadroom(
            scope="trajectory",
            exceeded=False,
            tokens_remaining=1_000_000,
            money_remaining=Money(currency=_USD, nanos=50_000_000_000),
        )
    ]
    scenarios: dict[str, tuple[ApprovalPolicy, list[BudgetHeadroom], bool]] = {
        "auto_local_only": (ApprovalPolicy(), roomy, False),
        "auto_remote_registered": (ApprovalPolicy(), roomy, True),
        "hybrid_gates_egress": (
            ApprovalPolicy(mode=ApprovalMode.HYBRID, gate_egress_at=DataClassification.INTERNAL),
            roomy,
            True,
        ),
        "hybrid_gates_cost": (
            ApprovalPolicy(
                mode=ApprovalMode.HYBRID,
                gate_egress_at=DataClassification.CONFIDENTIAL,
                gate_step_cost=Money(currency=_USD, nanos=1_000_000_000),
            ),
            roomy,
            True,
        ),
        "manual_holds_everything": (ApprovalPolicy(mode=ApprovalMode.MANUAL), roomy, True),
        "ceiling_exceeded": (
            ApprovalPolicy(),
            [BudgetHeadroom(scope="day", exceeded=True, unpriced_debit_count=3)],
            True,
        ),
    }
    cases = {
        name: evaluate_plan(
            plan,
            declaration=declaration,
            tier_policy=dataclasses.replace(tier_policy, loadcoach_has_remote_provider=registered),
            policy=policy,
            estimates=estimates,
            headroom=headroom,
        ).as_canonical()
        for name, (policy, headroom, registered) in scenarios.items()
    }
    golden = _GOLDEN_DIR / "approval_verdicts.json"
    produced = canonical_json(cases)
    if not golden.exists():  # pragma: no cover — first run writes the golden
        golden.write_text(produced + "\n", encoding="utf-8")
    assert produced + "\n" == golden.read_text(encoding="utf-8")


def test_an_estimate_refuses_a_negative_token_count() -> None:
    with pytest.raises(ValidationError, match="token_estimate"):
        StepEstimate(-1)


def test_headroom_refuses_a_negative_count() -> None:
    with pytest.raises(ValidationError, match="unmetered_debit_count"):
        BudgetHeadroom(scope="day", exceeded=False, unmetered_debit_count=-1)


def test_a_step_no_configured_tier_can_admit_is_rejected_rather_than_placed_somewhere(
    declaration: TrajectoryDeclaration, approval_policy: ApprovalPolicy
) -> None:
    """There is no tier this work may run on, and running it elsewhere is the egress to prevent."""
    from promptcadence.domain.tiers import TierPolicy as _TierPolicy
    from promptcadence.domain.tiers import TierSnapshot as _TierSnapshot

    remote_only = _TierPolicy(
        snapshot=_TierSnapshot(
            tiers=(dataclasses.replace(_plan_tier(), name="remote_frontier"),),
            default_tier="remote_frontier",
            escalation_order=("remote_frontier",),
        ),
        loadcoach_has_remote_provider=True,
    )
    verdict = evaluate_step(
        _raw_step(tier="remote_frontier", data_classification=DataClassification.CONFIDENTIAL),
        declaration=declaration,
        tier_policy=remote_only,
        policy=approval_policy,
        estimate=_estimate(),
    )
    assert verdict.outcome is StepOutcome.REJECTED
    assert verdict.reason is VerdictReason.NO_ADMITTING_TIER
    assert verdict.error_code is ErrorCode.EGRESS_DENIED


def _plan_tier() -> Any:
    """A remote tier ceilinged at ``public``: it admits nothing confidential."""
    from promptcadence.domain.tiers import EgressClass
    from promptcadence.domain.tiers import Tier as _Tier

    return _Tier(
        name="remote_frontier",
        task_profile="tools.agent.remote_frontier",
        egress_class=EgressClass.REMOTE,
        max_data_classification=DataClassification.PUBLIC,
        context_budget_tokens=128_000,
        pricing_source="pricing/remote_frontier.json",
    )
