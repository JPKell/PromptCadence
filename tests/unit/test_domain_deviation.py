"""Tests for promptcadence.domain.deviation: the matrix, exhaustive by construction.

The matrix is parametrized over the enums rather than written out, so a seventh category or a
third scope with no disposition row fails the suite the day it appears rather than slipping
through as an untested cell.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any

import pytest
from baseaicore import DataClassification, Money, ProviderKind, ValidationError, canonical_json

from promptcadence.domain.deviation import (
    CATEGORY_INTENT_FIELDS,
    DISPOSITIONS,
    SEVERITIES,
    Deviation,
    DeviationCategory,
    DeviationDetected,
    DeviationSeverity,
    Disposition,
    ExecutionSubject,
    TierServiceFailure,
    TurnFacts,
    compare,
    disposition,
)
from promptcadence.domain.intent import (
    GOVERNED_INTENT_FIELDS,
    ExecutionIntent,
    MintedBy,
    MintKind,
    mint_bypass_default,
    mint_for_step,
)
from promptcadence.domain.plan import PlanStep
from promptcadence.domain.policy import (
    ApprovalPolicy,
    ReapprovalScope,
    StepEstimate,
    StepOutcome,
    StepVerdict,
    VerdictReason,
)
from promptcadence.domain.tiers import EgressClass, TierPolicy
from promptcadence.domain.trajectory import TrajectoryDeclaration

_GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"
_LOCAL = ExecutionSubject(
    model_canonical_id="ollama/qwen3:8b@sha256:" + "a" * 64,
    provider_kind=ProviderKind.OLLAMA,
    egress_class=EgressClass.LOCAL,
)
_REMOTE = ExecutionSubject(
    model_canonical_id="openai_compatible/gpt@sha256:" + "b" * 64,
    provider_kind=ProviderKind.OPENAI_COMPATIBLE,
    egress_class=EgressClass.REMOTE,
    provider_name="frontier",
)


@pytest.fixture
def intent(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> ExecutionIntent:
    """A local-only step intent approving ``read_file`` only, inside a two-tool allowlist.

    Narrower than the trajectory allowlist on purpose: that gap is what makes the
    ``undeclared_tool`` row's two-way split reachable at all — a tool the caller permitted but
    this step's envelope did not.
    """
    return mint_for_step(
        intent_id="01INTENT00000000000000000A",
        declaration=declaration,
        step=PlanStep(
            step_id="s1",
            description="read the notes",
            depends_on=(),
            tools=("read_file",),
            tier="local_fast",
            data_classification=DataClassification.INTERNAL,
            expected_turns=2,
        ),
        verdict=StepVerdict(
            step_id="s1",
            outcome=StepOutcome.APPROVED,
            reason=VerdictReason.AUTO_APPROVED,
            declared_tier="local_fast",
            estimate=StepEstimate(2_000),
            approved_tier="local_fast",
        ),
        tier_policy=tier_policy,
        policy=approval_policy,
        minted_by=MintedBy(MintKind.POLICY),
        minted_at=minted_at,
        max_turns=8,
        token_budget=100_000,
    )


def _facts(**overrides: Any) -> TurnFacts:
    """A turn that stayed entirely inside the fixture intent's envelope."""
    defaults: dict[str, Any] = {
        "turn_id": "01TURN0000000000000000000A",
        "executed_tier": "local_fast",
        "subject": _LOCAL,
        "observed_classification": DataClassification.INTERNAL,
        "turns_used": 1,
        "step_tokens_spent": 500,
        "requested_tools": ("read_file",),
        "trajectory_allowlist": frozenset({"read_file", "list_dir"}),
        "finish_declared": True,
    }
    return TurnFacts(**{**defaults, **overrides})


# --------------------------------------------------------------------------------------------
# Closure
# --------------------------------------------------------------------------------------------


def test_every_category_names_real_intent_fields_and_together_they_cover_the_governed_set() -> None:
    """The closure argument: one category per contradictable field group, plus the after-the-fact
    one.

    If this fails, either a governed intent field has no category (a deviation nobody can record)
    or a category names a field that does not exist (a category nothing can trigger).
    """
    declared = {field.name for field in dataclasses.fields(ExecutionIntent)}
    named: set[str] = set()
    for category, fields in CATEGORY_INTENT_FIELDS.items():
        assert fields <= declared, f"{category} names fields ExecutionIntent does not have"
        named |= fields
    assert named == GOVERNED_INTENT_FIELDS
    assert set(CATEGORY_INTENT_FIELDS) == set(DeviationCategory)


def test_turn_facts_carries_no_trajectory_level_ceiling() -> None:
    """A ceiling crossing is the budget machinery's halt or park, never a deviation (§5, §6).

    If one could be passed to ``compare``, the taxonomy would not be closed: it would either be
    ignored or grow a seventh category. The field list is therefore the assertion.
    """
    names = {field.name for field in dataclasses.fields(TurnFacts)}
    assert names == {
        "turn_id",
        "executed_tier",
        "subject",
        "observed_classification",
        "turns_used",
        "step_tokens_spent",
        "requested_tools",
        "trajectory_allowlist",
        "tier_service_failure",
        "step_money_spent",
        "step_money_is_floor",
        "finish_declared",
    }
    forbidden = {"trajectory_budget", "daily_ceiling", "headroom", "ledger", "project_ceiling"}
    assert not names & forbidden


# --------------------------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------------------------


def test_the_disposition_table_covers_every_category_by_scope_cell() -> None:
    """Exhaustive by construction: a new category with no row fails here, not in production."""
    assert set(DISPOSITIONS) == set(product(DeviationCategory, ReapprovalScope))
    assert set(SEVERITIES) == set(DeviationCategory)


@pytest.mark.parametrize(("category", "scope"), list(product(DeviationCategory, ReapprovalScope)))
def test_every_matrix_cell_resolves_to_a_disposition(
    category: DeviationCategory, scope: ReapprovalScope
) -> None:
    deviation = Deviation(category=category, intent_id="i1", intent_revision=1, turn_id="t1")
    assert isinstance(disposition(deviation, scope=scope), Disposition)


@pytest.mark.parametrize("scope", list(ReapprovalScope))
def test_a_violation_halts_unconditionally_and_is_never_reapprovable(
    scope: ReapprovalScope,
) -> None:
    """Nothing in the disposition logic may make a violation conditional (lifecycle §5)."""
    for category, severity in SEVERITIES.items():
        if severity is not DeviationSeverity.VIOLATION:
            continue
        deviation = Deviation(category=category, intent_id="i1", intent_revision=1, turn_id="t1")
        assert disposition(deviation, scope=scope) is Disposition.HALT
        assert deviation.is_reapprovable is False


def test_severity_is_derived_and_cannot_be_set_to_disagree_with_its_category() -> None:
    deviation = Deviation(
        category=DeviationCategory.TIER_VIOLATION, intent_id="i1", intent_revision=1, turn_id="t1"
    )
    assert deviation.severity is DeviationSeverity.VIOLATION
    assert "severity" not in {field.name for field in dataclasses.fields(Deviation)}


def test_the_matrix_matches_lifecycle_five_cell_by_cell() -> None:
    """Transcribed from the table, so a change to either side has to be a deliberate edit."""
    on_change = ReapprovalScope.ON_TIER_OR_CLASSIFICATION_CHANGE
    any_deviation = ReapprovalScope.ANY_DEVIATION
    expected = {
        (DeviationCategory.TIER_VIOLATION, on_change): Disposition.HALT,
        (DeviationCategory.TIER_VIOLATION, any_deviation): Disposition.HALT,
        (DeviationCategory.TIER_ESCALATION, on_change): Disposition.SCOPED_REAPPROVAL,
        (DeviationCategory.TIER_ESCALATION, any_deviation): Disposition.SCOPED_REAPPROVAL,
        (DeviationCategory.CLASSIFICATION_EXCEEDED, on_change): Disposition.SCOPED_REAPPROVAL,
        (DeviationCategory.CLASSIFICATION_EXCEEDED, any_deviation): Disposition.SCOPED_REAPPROVAL,
        (DeviationCategory.UNDECLARED_TOOL, on_change): Disposition.CONTINUE_RECORDED,
        (DeviationCategory.UNDECLARED_TOOL, any_deviation): Disposition.SCOPED_REAPPROVAL,
        (DeviationCategory.BUDGET_OVERRUN, on_change): Disposition.CONTINUE_RECORDED,
        (DeviationCategory.BUDGET_OVERRUN, any_deviation): Disposition.SCOPED_REAPPROVAL,
        (DeviationCategory.TURN_OVERRUN, on_change): Disposition.SCOPED_REAPPROVAL,
        (DeviationCategory.TURN_OVERRUN, any_deviation): Disposition.SCOPED_REAPPROVAL,
    }
    assert dict(DISPOSITIONS) == expected


@pytest.mark.parametrize("scope", list(ReapprovalScope))
def test_a_tool_outside_the_callers_allowlist_is_refused_under_either_scope(
    scope: ReapprovalScope,
) -> None:
    """The table's one prose refinement: the allowlist is the caller's, not the model's."""
    outside = Deviation(
        category=DeviationCategory.UNDECLARED_TOOL,
        intent_id="i1",
        intent_revision=1,
        turn_id="t1",
        tools=("rm_rf",),
        outside_trajectory_allowlist=True,
    )
    assert disposition(outside, scope=scope) is Disposition.REFUSED_NOT_REAPPROVABLE
    assert outside.is_reapprovable is False


# --------------------------------------------------------------------------------------------
# compare()
# --------------------------------------------------------------------------------------------


def test_a_compliant_turn_produces_no_deviations(intent: ExecutionIntent) -> None:
    assert compare(_facts(), intent) == ()


def test_a_tier_outside_the_intent_is_a_violation(intent: ExecutionIntent) -> None:
    (found,) = compare(_facts(executed_tier="local_large"), intent)
    assert found.category is DeviationCategory.TIER_VIOLATION
    assert found.severity is DeviationSeverity.VIOLATION
    assert found.executed_tier == "local_large"
    assert found.permitted_tiers == ("local_fast",)


def test_a_remote_answer_on_a_local_only_envelope_is_a_violation(
    intent: ExecutionIntent,
) -> None:
    """Spec §11 contract 4: the tier constraint is verified, not assumed."""
    (found,) = compare(_facts(subject=_REMOTE), intent)
    assert found.category is DeviationCategory.TIER_VIOLATION
    assert found.subject is _REMOTE


def test_a_remote_answer_on_an_envelope_that_permits_remote_is_not_a_violation(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    remote_intent = mint_bypass_default(
        intent_id="01INTENT00000000000000000A",
        declaration=declaration,
        tier_policy=tier_policy,
        policy=approval_policy,
        minted_at=minted_at,
        tier_override="remote_cheap",
    )
    assert compare(_facts(executed_tier="remote_cheap", subject=_REMOTE), remote_intent) == ()


def test_tiers_that_cannot_serve_raise_an_escalation_drift(intent: ExecutionIntent) -> None:
    facts = _facts(
        executed_tier=None,
        subject=None,
        tier_service_failure=TierServiceFailure.NO_ELIGIBLE_MODEL,
    )
    (found,) = compare(facts, intent)
    assert found.category is DeviationCategory.TIER_ESCALATION
    assert found.severity is DeviationSeverity.DRIFT
    assert found.service_failure is TierServiceFailure.NO_ELIGIBLE_MODEL


def test_data_above_the_intents_ceiling_is_a_classification_drift(
    intent: ExecutionIntent,
) -> None:
    (found,) = compare(_facts(observed_classification=DataClassification.CONFIDENTIAL), intent)
    assert found.category is DeviationCategory.CLASSIFICATION_EXCEEDED
    assert found.observed_classification is DataClassification.CONFIDENTIAL
    assert found.permitted_classification is DataClassification.INTERNAL


def test_data_below_the_ceiling_is_not_a_deviation(intent: ExecutionIntent) -> None:
    assert compare(_facts(observed_classification=DataClassification.PUBLIC), intent) == ()


def test_undeclared_tools_split_by_the_trajectory_allowlist_refusal_first(
    intent: ExecutionIntent,
) -> None:
    """Two deviations at most, and the permanent refusal is reported before the drift."""
    facts = _facts(requested_tools=("rm_rf", "list_dir", "read_file"))
    outside, inside = compare(facts, intent)
    assert outside.outside_trajectory_allowlist is True
    assert outside.tools == ("rm_rf",)
    assert inside.outside_trajectory_allowlist is False
    assert inside.tools == ("list_dir",)


def test_a_repeated_undeclared_tool_is_reported_once(intent: ExecutionIntent) -> None:
    (found,) = compare(_facts(requested_tools=("list_dir", "list_dir")), intent)
    assert found.tools == ("list_dir",)


def test_spend_past_the_step_slice_is_a_budget_drift(intent: ExecutionIntent) -> None:
    assert compare(_facts(step_tokens_spent=intent.token_budget), intent) == ()
    (found,) = compare(_facts(step_tokens_spent=intent.token_budget + 1), intent)
    assert found.category is DeviationCategory.BUDGET_OVERRUN
    assert found.token_budget == intent.token_budget


def test_a_money_floor_that_already_exceeds_the_slice_still_fires_and_says_it_is_a_floor(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    """ADR-0069: over a floor, "exceeded" is certain even though "under budget" is not."""
    priced = mint_bypass_default(
        intent_id="01INTENT00000000000000000A",
        declaration=dataclasses.replace(
            declaration, money_budget=Money(currency="USD", nanos=1_000_000_000)
        ),
        tier_policy=tier_policy,
        policy=approval_policy,
        minted_at=minted_at,
    )
    facts = _facts(
        step_money_spent=Money(currency="USD", nanos=2_000_000_000), step_money_is_floor=True
    )
    (found,) = compare(facts, priced)
    assert found.category is DeviationCategory.BUDGET_OVERRUN
    assert found.money_is_floor is True


def test_reaching_max_turns_without_a_declared_finish_is_a_turn_overrun(
    intent: ExecutionIntent,
) -> None:
    """A model never decides control flow: only a *declared* finish ends the step."""
    assert compare(_facts(turns_used=intent.max_turns, finish_declared=True), intent) == ()
    assert compare(_facts(turns_used=intent.max_turns - 1, finish_declared=False), intent) == ()
    (found,) = compare(_facts(turns_used=intent.max_turns, finish_declared=False), intent)
    assert found.category is DeviationCategory.TURN_OVERRUN
    assert found.turns_used == intent.max_turns


def test_several_deviations_on_one_turn_are_all_reported_in_category_order(
    intent: ExecutionIntent,
) -> None:
    facts = _facts(
        executed_tier="local_large",
        subject=_REMOTE,
        observed_classification=DataClassification.CONFIDENTIAL,
        requested_tools=("rm_rf", "list_dir"),
        step_tokens_spent=intent.token_budget + 1,
        turns_used=intent.max_turns,
        finish_declared=False,
    )
    assert [deviation.category for deviation in compare(facts, intent)] == [
        DeviationCategory.TIER_VIOLATION,
        DeviationCategory.CLASSIFICATION_EXCEEDED,
        DeviationCategory.UNDECLARED_TOOL,
        DeviationCategory.UNDECLARED_TOOL,
        DeviationCategory.BUDGET_OVERRUN,
        DeviationCategory.TURN_OVERRUN,
    ]


def test_compare_does_not_branch_on_how_the_intent_was_minted(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
    intent: ExecutionIntent,
) -> None:
    """One source, no mode branching — the collapse ADR-0056 bought, asserted on the outputs.

    The bypass default and a plan-minted intent with identical fields produce identical
    deviations, differing only in the ids they name.
    """
    bypassed = mint_bypass_default(
        intent_id=intent.intent_id,
        declaration=dataclasses.replace(
            declaration, tool_allowlist=frozenset({"read_file"}), token_budget=100_000
        ),
        tier_policy=tier_policy,
        policy=approval_policy,
        minted_at=minted_at,
    )
    assert bypassed.minted_by.kind is MintKind.BYPASS_DEFAULT
    assert intent.minted_by.kind is MintKind.POLICY
    facts = _facts(
        executed_tier="local_large",
        requested_tools=("rm_rf", "list_dir"),
        turns_used=8,
        finish_declared=False,
    )
    assert [d.as_canonical() for d in compare(facts, bypassed)] == [
        d.as_canonical() for d in compare(facts, intent)
    ]


# --------------------------------------------------------------------------------------------
# Facts validation, the event, and the golden
# --------------------------------------------------------------------------------------------


def test_turn_facts_refuse_a_turn_that_neither_ran_nor_said_why() -> None:
    with pytest.raises(ValidationError, match="must say why"):
        _facts(executed_tier=None, subject=None)
    with pytest.raises(ValidationError, match="either it ran"):
        _facts(tier_service_failure=TierServiceFailure.TIER_UNAVAILABLE)
    with pytest.raises(ValidationError, match="no execution subject"):
        _facts(subject=None)
    with pytest.raises(ValidationError, match="turns_used"):
        _facts(turns_used=0)
    with pytest.raises(ValidationError, match="turn_id"):
        _facts(turn_id="  ")


def test_the_detected_event_carries_the_category_and_the_disposition(
    intent: ExecutionIntent,
) -> None:
    (found,) = compare(_facts(executed_tier="local_large"), intent)
    body = DeviationDetected.of(
        found,
        trajectory_id=intent.trajectory_id,
        scope=ReapprovalScope.ON_TIER_OR_CLASSIFICATION_CHANGE,
    )
    assert body.event_type.value == "deviation.detected"
    assert body.category is DeviationCategory.TIER_VIOLATION
    assert body.disposition is Disposition.HALT
    assert body.reapprovable is False


def test_deviation_matrix_golden(intent: ExecutionIntent) -> None:
    """Every category x scope cell, with the deviation each is rendered over, byte for byte."""
    facts = _facts(
        executed_tier="local_large",
        subject=_REMOTE,
        observed_classification=DataClassification.CONFIDENTIAL,
        requested_tools=("rm_rf", "list_dir"),
        step_tokens_spent=intent.token_budget + 1,
        turns_used=intent.max_turns,
        finish_declared=False,
    )
    escalation = _facts(
        executed_tier=None,
        subject=None,
        tier_service_failure=TierServiceFailure.TIER_UNAVAILABLE,
    )
    deviations = [*compare(facts, intent), *compare(escalation, intent)]
    cases = {
        "deviations": [deviation.as_canonical() for deviation in deviations],
        "dispositions": {
            f"{category.value}|{scope.value}": DISPOSITIONS[(category, scope)].value
            for category, scope in sorted(
                DISPOSITIONS, key=lambda cell: (cell[0].value, cell[1].value)
            )
        },
        "events": [
            DeviationDetected.of(
                deviation, trajectory_id=intent.trajectory_id, scope=scope
            ).as_canonical()
            for deviation in deviations
            for scope in ReapprovalScope
        ],
    }
    golden = _GOLDEN_DIR / "deviation_matrix.json"
    produced = canonical_json(cases)
    if not golden.exists():  # pragma: no cover — first run writes the golden
        golden.write_text(produced + "\n", encoding="utf-8")
    assert produced + "\n" == golden.read_text(encoding="utf-8")


def test_deviation_matrix_bypass_rows_golden(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    """The plan's named golden: every category, against the intent the **bypass path** mints.

    The golden above renders the matrix over a hand-built intent, which proves the comparison. This
    one renders it over the intent ``mint_bypass_default`` actually produces from ``TierPolicy``,
    which proves something different and is the reason the plan asks for it by name: the full
    lifecycle §5 category set applies to a bypassed trajectory **unchanged**, because the intent is
    the comparison source in both modes and Phase 7 changes only who mints it (ADR-0048, ADR-0056).

    So this file is the baseline Phase 7's contract-1 invariance diff is written against. When a
    planner mints the intent instead, these rows must not move; only the ``intent_id`` and the
    ``minted_by`` kind may. A diff here after P7 is either a real governance change or a bug, and
    either way it should be seen rather than inferred.
    """
    bypassed = mint_bypass_default(
        intent_id="01BYPASSINTENT00000000000",
        declaration=dataclasses.replace(
            declaration, tool_allowlist=frozenset({"read_file", "list_dir"})
        ),
        tier_policy=tier_policy,
        policy=approval_policy,
        minted_at=minted_at,
    )
    assert bypassed.minted_by.kind is MintKind.BYPASS_DEFAULT

    executed = _facts(
        executed_tier="local_large",
        subject=_REMOTE,
        observed_classification=DataClassification.CONFIDENTIAL,
        requested_tools=("rm_rf", "list_dir"),
        step_tokens_spent=(bypassed.token_budget or 0) + 1,
        turns_used=bypassed.max_turns,
        finish_declared=False,
    )
    unserved = _facts(
        executed_tier=None,
        subject=None,
        tier_service_failure=TierServiceFailure.NO_ELIGIBLE_MODEL,
    )
    deviations = [*compare(executed, bypassed), *compare(unserved, bypassed)]

    # Every category the bypass path can raise is actually exercised here; a golden that silently
    # stopped covering one would still pass byte-for-byte.
    assert {deviation.category for deviation in deviations} == set(DeviationCategory)

    cases = {
        "minted_by": bypassed.minted_by.kind.value,
        "deviations": [deviation.as_canonical() for deviation in deviations],
        "severities": {
            deviation.category.value: deviation.severity.value for deviation in deviations
        },
        "dispositions": {
            f"{deviation.category.value}|{scope.value}": disposition(deviation, scope=scope).value
            for deviation in deviations
            for scope in ReapprovalScope
        },
    }
    golden = _GOLDEN_DIR / "deviation_matrix_bypass.json"
    produced = canonical_json(cases)
    if not golden.exists():  # pragma: no cover — first run writes the golden
        golden.write_text(produced + "\n", encoding="utf-8")
    assert produced + "\n" == golden.read_text(encoding="utf-8")


def test_a_tool_outside_the_trajectory_allowlist_is_never_reapprovable(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    """Lifecycle §5's split, on the bypass default intent, under **both** scopes.

    The allowlist is the caller's, not the model's, so a tool outside it is refused outright and no
    re-approval can grant it. A tool inside the allowlist but outside the intent is the other half:
    a drift whose disposition follows ``reapproval_scope``. Asserting both under both scopes is
    what makes "never re-approvable" a claim about the policy rather than about one code path.
    """
    bypassed = mint_bypass_default(
        intent_id="01BYPASSINTENT00000000000",
        declaration=dataclasses.replace(declaration, tool_allowlist=frozenset({"read_file"})),
        tier_policy=tier_policy,
        policy=approval_policy,
        minted_at=minted_at,
    )
    outside = compare(_facts(requested_tools=("rm_rf",)), bypassed)
    (refused,) = [d for d in outside if d.category is DeviationCategory.UNDECLARED_TOOL]
    assert refused.outside_trajectory_allowlist is True
    assert refused.is_reapprovable is False
    for scope in ReapprovalScope:
        assert disposition(refused, scope=scope) is Disposition.REFUSED_NOT_REAPPROVABLE


def test_turn_facts_refuse_a_negative_token_count() -> None:
    with pytest.raises(ValidationError, match="step_tokens_spent"):
        _facts(step_tokens_spent=-1)
