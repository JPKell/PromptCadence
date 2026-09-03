"""Tests for promptcadence.domain.intent — including the guard behind spec §11 contract 1.

Three things are proved here rather than asserted in prose:

1. **A turn cannot be constructed without an intent.** Structural, via ``TurnProvenance``'s
   ``InitVar``, and shown to bite against a loop written the way a hurried one would be.
2. **There is no fourth minting path.** An AST walk over every module in the package.
3. **Gates evaluate against the most permissive tier**, so a pre-approved fallback cannot smuggle
   egress past a hybrid gate (ADR-0056 rule 4).
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from baseaicore import DataClassification, Money, ValidationError, canonical_json

from promptcadence.domain.errors import ErrorCode, TierNotConfiguredError
from promptcadence.domain.intent import (
    BYPASS_STEP_ID,
    GOVERNED_INTENT_FIELDS,
    RECORD_INTENT_FIELDS,
    ExecutionIntent,
    IntentMinted,
    MintedBy,
    MintKind,
    TurnProvenance,
    mint_bypass_default,
    mint_for_step,
    supersede,
)
from promptcadence.domain.plan import PlanStep
from promptcadence.domain.policy import (
    ApprovalMode,
    ApprovalPolicy,
    EstimateSource,
    StepEstimate,
    StepOutcome,
    StepVerdict,
    VerdictReason,
)
from promptcadence.domain.threads import Turn, TurnRole
from promptcadence.domain.tiers import EgressClass, TierPolicy
from promptcadence.domain.trajectory import TrajectoryDeclaration

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src/promptcadence"
_GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"


def _plan_step(**overrides: Any) -> PlanStep:
    """A plan step declaring one allowlisted tool on a local tier."""
    defaults: dict[str, Any] = {
        "step_id": "s1",
        "description": "read the notes",
        "depends_on": (),
        "tools": ("read_file",),
        "tier": "local_fast",
        "data_classification": DataClassification.INTERNAL,
        "expected_turns": 2,
    }
    return PlanStep(**{**defaults, **overrides})


def _verdict(**overrides: Any) -> StepVerdict:
    """An approved verdict for ``_plan_step()``."""
    defaults: dict[str, Any] = {
        "step_id": "s1",
        "outcome": StepOutcome.APPROVED,
        "reason": VerdictReason.AUTO_APPROVED,
        "declared_tier": "local_fast",
        "estimate": StepEstimate(2_000, source=EstimateSource.HISTORICAL, sample_count=42),
        "approved_tier": "local_fast",
    }
    return StepVerdict(**{**defaults, **overrides})


def _for_step(
    base_declaration: TrajectoryDeclaration,
    base_tier_policy: TierPolicy,
    base_policy: ApprovalPolicy,
    base_minted_at: datetime,
    **overrides: Any,
) -> ExecutionIntent:
    """Mint from an approved plan step, with named overrides."""
    kwargs: dict[str, Any] = {
        "intent_id": "01INTENT00000000000000000A",
        "declaration": base_declaration,
        "step": _plan_step(),
        "verdict": _verdict(),
        "tier_policy": base_tier_policy,
        "policy": base_policy,
        "minted_by": MintedBy(MintKind.POLICY),
        "minted_at": base_minted_at,
        "max_turns": 4,
        "token_budget": 20_000,
    }
    return mint_for_step(**{**kwargs, **overrides})


# --------------------------------------------------------------------------------------------
# The field partition: the closure argument, made mechanical
# --------------------------------------------------------------------------------------------


def test_every_intent_field_is_either_governed_or_record_keeping() -> None:
    """Adding a field forces a decision, which is what keeps the deviation taxonomy closed.

    A governed field is one a turn can contradict, so it needs a deviation category, a disposition
    row and — per ADR-0056's revisit trigger — its own ADR. This test is where an author is told
    that, at CI time, rather than after the fact.
    """
    declared = {field.name for field in dataclasses.fields(ExecutionIntent)}
    assert GOVERNED_INTENT_FIELDS | RECORD_INTENT_FIELDS == declared
    assert not GOVERNED_INTENT_FIELDS & RECORD_INTENT_FIELDS


# --------------------------------------------------------------------------------------------
# Guard 1: no turn without an intent
# --------------------------------------------------------------------------------------------


def test_a_turn_provenance_cannot_be_built_without_the_intent_it_ran_under() -> None:
    """The structural half of contract 1: omitting the intent is a ``TypeError``, not a note."""
    with pytest.raises(TypeError):
        TurnProvenance(trajectory_id="tr1", tier="local_fast")  # type: ignore[call-arg]  # the guard


def test_the_intent_is_an_initvar_and_leaves_no_trace_on_the_turn(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    """A turn records a reference, never the envelope: intents are read from their own rows."""
    intent = _for_step(declaration, tier_policy, approval_policy, minted_at)
    provenance = intent.provenance(trajectory_id=declaration.trajectory_id, tier="local_fast")
    assert "intent" not in {field.name for field in dataclasses.fields(TurnProvenance)}
    assert "ExecutionIntent" not in repr(provenance)
    assert set(provenance.as_canonical()) == {
        "trajectory_id",
        "tier",
        "intent_id",
        "intent_revision",
    }
    assert provenance.intent_id == intent.intent_id
    assert provenance.intent_revision == intent.revision


def test_a_hurried_loop_that_skips_minting_cannot_produce_a_turn(
    declaration: TrajectoryDeclaration,
) -> None:
    """A loop written the way a hurried one would be. Every one of its own tests would pass.

    It cannot append a turn, because there is no provenance it can build and no ``Turn``
    constructor that will take none.
    """

    class SkipsTheIntent:
        """A bypass loop that resolved a tier and went straight to executing."""

        def turn_for(self, tier: str) -> Turn[TurnProvenance]:
            """Build the turn it wants to record."""
            provenance = TurnProvenance(  # type: ignore[call-arg]  # exactly the omission under test
                trajectory_id=declaration.trajectory_id, tier=tier
            )
            return Turn("t1", "th1", 1, TurnRole.ASSISTANT, provenance)

    with pytest.raises(TypeError):
        SkipsTheIntent().turn_for("local_fast")


def test_provenance_refuses_an_intent_from_another_trajectory(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    intent = _for_step(declaration, tier_policy, approval_policy, minted_at)
    with pytest.raises(ValidationError, match="governs trajectory"):
        intent.provenance(trajectory_id="01OTHER0000000000000000000", tier="local_fast")


def test_provenance_records_a_tier_outside_the_envelope_rather_than_refusing_it(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    """A turn on a tier the intent never permitted is the violation that must be *recorded*.

    Refusing to build its provenance would delete the evidence of exactly the event lifecycle §5
    halts on.
    """
    intent = _for_step(declaration, tier_policy, approval_policy, minted_at)
    provenance = intent.provenance(trajectory_id=declaration.trajectory_id, tier="remote_frontier")
    assert provenance.tier == "remote_frontier"
    assert "remote_frontier" not in intent.permitted_tiers


def test_rehydrate_rebuilds_a_written_turns_provenance_and_refuses_an_incomplete_reference() -> (
    None
):
    rebuilt = TurnProvenance.rehydrate(
        trajectory_id="tr1", tier="local_fast", intent_id="i1", intent_revision=3
    )
    assert (rebuilt.intent_id, rebuilt.intent_revision) == ("i1", 3)
    with pytest.raises(ValidationError, match="incomplete envelope"):
        TurnProvenance.rehydrate(
            trajectory_id="tr1", tier="local_fast", intent_id="", intent_revision=1
        )


# --------------------------------------------------------------------------------------------
# Guard 2: no fourth minting path
# --------------------------------------------------------------------------------------------


def _constructor_sites(name: str) -> dict[str, list[int]]:
    """Return every module under ``src/promptcadence`` calling ``name(...)``, with its lines."""
    sites: dict[str, list[int]] = {}
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == name)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
            )
        ]
        if lines:
            sites[str(path.relative_to(_SOURCE_ROOT))] = lines
    return sites


def test_no_module_mints_an_intent_outside_domain_intent() -> None:
    """The second guard: a fourth minting path is a CI failure, not a review note.

    Direct construction is *safe* — ``__post_init__`` validates totally — but it skips the gate
    evaluation and the egress resolution that ``_mint`` performs, so a path around it would write
    an envelope whose gate verdict nobody computed. Later phases add modules that mint; this test
    is what tells their author there is one way in.
    """
    assert set(_constructor_sites("ExecutionIntent")) <= {"domain/intent.py"}


def test_rehydrate_is_called_only_from_infrastructure() -> None:
    """The one path that builds provenance without an intent is confined to reading rows back."""
    callers = set(_constructor_sites("rehydrate"))
    assert callers <= {"domain/intent.py", "infrastructure/threads.py"}


def test_direct_construction_is_pointless_rather_than_dangerous(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    """A hand-built intent that is correct is accepted; one that lies about anything is not."""
    minted = _for_step(declaration, tier_policy, approval_policy, minted_at)
    assert dataclasses.replace(minted) == minted
    for broken in (
        {"revision": 2, "supersedes": None},
        {"revision": 1, "supersedes": 1},
        {"revision": 3, "supersedes": 1},
        {"token_budget": 0},
        {"max_turns": 0},
        {"fallback_tiers": ("local_fast",)},
        {"intent_id": "   "},
        {"minted_at": datetime(2026, 9, 2, 12, 0)},  # noqa: DTZ001 — the refusal under test
        {"money_budget": Money(currency="USD", nanos=0)},
    ):
        with pytest.raises(ValidationError):
            dataclasses.replace(minted, **broken)


def test_an_intent_cannot_be_gated_against_a_tier_it_does_not_permit(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    minted = _for_step(declaration, tier_policy, approval_policy, minted_at)
    with pytest.raises(ValidationError, match="most permissive"):
        dataclasses.replace(
            minted, gate=dataclasses.replace(minted.gate, gating_tier="remote_cheap")
        )


def test_an_intent_is_immutable(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    minted = _for_step(declaration, tier_policy, approval_policy, minted_at)
    with pytest.raises(dataclasses.FrozenInstanceError):
        minted.approved_tier = "remote_frontier"  # type: ignore[misc]  # the guard under test


# --------------------------------------------------------------------------------------------
# The three paths
# --------------------------------------------------------------------------------------------


def test_minting_from_an_approved_step_carries_the_steps_declarations(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    minted = _for_step(declaration, tier_policy, approval_policy, minted_at)
    assert minted.step_id == "s1"
    assert minted.revision == 1
    assert minted.supersedes is None
    assert minted.approved_tools == frozenset({"read_file"})
    assert minted.max_classification is DataClassification.INTERNAL
    assert minted.permitted_egress_class is EgressClass.LOCAL
    assert minted.minted_by.as_recorded() == "policy"


def test_a_redline_resolves_at_minting_and_the_plan_keeps_the_proposal(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    """ADR-0056 rule 3: two facts, in the places they belong."""
    step = _plan_step(tier="remote_cheap", data_classification=DataClassification.CONFIDENTIAL)
    verdict = _verdict(
        outcome=StepOutcome.REDLINED,
        reason=VerdictReason.TIER_CEILING_SUBSTITUTION,
        declared_tier="remote_cheap",
        approved_tier="local_large",
    )
    minted = _for_step(
        declaration,
        tier_policy,
        approval_policy,
        minted_at,
        declaration=dataclasses.replace(
            declaration, classification=DataClassification.CONFIDENTIAL
        ),
        step=step,
        verdict=verdict,
    )
    assert minted.approved_tier == "local_large"
    assert step.tier == "remote_cheap"
    assert verdict.declared_tier == "remote_cheap"


def test_a_rejected_verdict_mints_nothing(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    rejected = StepVerdict(
        step_id="s1",
        outcome=StepOutcome.REJECTED,
        reason=VerdictReason.NO_ADMITTING_TIER,
        declared_tier="remote_frontier",
        estimate=StepEstimate(10),
        error_code=ErrorCode.EGRESS_DENIED,
    )
    with pytest.raises(ValidationError, match="mints no intent"):
        _for_step(declaration, tier_policy, approval_policy, minted_at, verdict=rejected)


def test_minting_refuses_a_step_wider_than_the_callers_declaration(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    """An intent wider than its declaration would be a grant nobody gave."""
    with pytest.raises(ValidationError, match="outside the trajectory allowlist"):
        _for_step(
            declaration,
            tier_policy,
            approval_policy,
            minted_at,
            step=_plan_step(tools=("read_file", "run_command")),
        )
    with pytest.raises(ValidationError, match="above the trajectory"):
        _for_step(
            declaration,
            tier_policy,
            approval_policy,
            minted_at,
            step=_plan_step(data_classification=DataClassification.CONFIDENTIAL),
        )


def test_the_bypass_default_comes_entirely_from_the_declaration_and_tier_policy(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    """ADR-0056 §2: after this returns, the loop holds exactly what the planned path holds."""
    minted = mint_bypass_default(
        intent_id="01BYPASS0000000000000000A0",
        declaration=declaration,
        tier_policy=tier_policy,
        policy=approval_policy,
        minted_at=minted_at,
    )
    assert minted.step_id == BYPASS_STEP_ID
    assert minted.approved_tier == tier_policy.snapshot.default_tier
    assert minted.approved_tools == frozenset(declaration.tool_allowlist)
    assert minted.max_classification is declaration.classification
    assert minted.token_budget == declaration.token_budget
    assert minted.money_budget == declaration.money_budget
    assert minted.max_turns == declaration.max_turns
    assert minted.is_bypass_default
    assert minted.minted_by.as_recorded() == "bypass_default"


def test_the_bypass_default_honours_a_configured_tier_override(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    minted = mint_bypass_default(
        intent_id="01BYPASS0000000000000000A0",
        declaration=declaration,
        tier_policy=tier_policy,
        policy=approval_policy,
        minted_at=minted_at,
        tier_override="local_large",
    )
    assert minted.approved_tier == "local_large"
    with pytest.raises(TierNotConfiguredError):
        mint_bypass_default(
            intent_id="01BYPASS0000000000000000A0",
            declaration=declaration,
            tier_policy=tier_policy,
            policy=approval_policy,
            minted_at=minted_at,
            tier_override="gpt_9",
        )


def test_supersession_is_the_only_way_a_later_revision_exists(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    first = _for_step(declaration, tier_policy, approval_policy, minted_at)
    second = supersede(
        first,
        tier_policy=tier_policy,
        policy=approval_policy,
        minted_by=MintedBy(MintKind.APPROVER, "01TOKEN000000000000000000"),
        minted_at=minted_at,
        approved_tier="local_large",
        approval_request_id="01REQUEST00000000000000000",
    )
    assert second.revision == 2
    assert second.supersedes == 1
    assert second.intent_id == first.intent_id
    assert second.approved_tier == "local_large"
    assert first.approved_tier == "local_fast", "the superseded revision is retained unchanged"
    third = supersede(
        second,
        tier_policy=tier_policy,
        policy=approval_policy,
        minted_by=MintedBy(MintKind.POLICY),
        minted_at=minted_at,
    )
    assert (third.revision, third.supersedes) == (3, 2)


def test_supersession_inherits_every_field_it_is_not_given(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    """A re-approval that widens one dimension must not silently reset the others."""
    first = _for_step(declaration, tier_policy, approval_policy, minted_at)
    second = supersede(
        first,
        tier_policy=tier_policy,
        policy=approval_policy,
        minted_by=MintedBy(MintKind.POLICY),
        minted_at=minted_at,
        max_turns=12,
    )
    assert second.max_turns == 12
    for field_name in ("approved_tier", "approved_tools", "max_classification", "token_budget"):
        assert getattr(second, field_name) == getattr(first, field_name)


# --------------------------------------------------------------------------------------------
# Guard 3: gates against the most permissive tier
# --------------------------------------------------------------------------------------------


def test_a_pre_approved_remote_fallback_cannot_smuggle_egress_past_a_hybrid_gate(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    minted_at: datetime,
) -> None:
    """ADR-0056 rule 4, with a local primary and a remote fallback — the smuggling route itself."""
    hybrid = ApprovalPolicy(mode=ApprovalMode.HYBRID, gate_egress_at=DataClassification.INTERNAL)
    local_only = _for_step(declaration, tier_policy, hybrid, minted_at)
    assert local_only.gate.gated is False
    assert local_only.permitted_egress_class is EgressClass.LOCAL

    with_fallback = _for_step(
        declaration,
        tier_policy,
        hybrid,
        minted_at,
        verdict=_verdict(fallback_tiers=("remote_cheap",)),
    )
    assert with_fallback.approved_tier == "local_fast"
    assert with_fallback.gate.gating_tier == "remote_cheap"
    assert with_fallback.gate.egress_gated is True
    assert with_fallback.permitted_egress_class is EgressClass.REMOTE


def test_supersession_re_evaluates_the_gates(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    minted_at: datetime,
) -> None:
    """ADR-0049 rule 3: a gate fires at *every* minting, including a drift-triggered re-mint."""
    hybrid = ApprovalPolicy(mode=ApprovalMode.HYBRID, gate_egress_at=DataClassification.INTERNAL)
    first = _for_step(declaration, tier_policy, hybrid, minted_at)
    assert first.gate.gated is False
    escalated = supersede(
        first,
        tier_policy=tier_policy,
        policy=hybrid,
        minted_by=MintedBy(MintKind.POLICY),
        minted_at=minted_at,
        fallback_tiers=("remote_cheap",),
    )
    assert escalated.gate.egress_gated is True


# --------------------------------------------------------------------------------------------
# minted_by, the event, and the goldens
# --------------------------------------------------------------------------------------------


def test_minted_by_records_who_and_refuses_a_mismatch() -> None:
    assert MintedBy(MintKind.POLICY).as_recorded() == "policy"
    assert MintedBy(MintKind.BYPASS_DEFAULT).as_recorded() == "bypass_default"
    assert MintedBy(MintKind.APPROVER, "tok1").as_recorded() == "approver:tok1"
    with pytest.raises(ValidationError, match="approving token"):
        MintedBy(MintKind.APPROVER)
    with pytest.raises(ValidationError, match="must not name an approver"):
        MintedBy(MintKind.POLICY, "tok1")


def test_an_approver_minting_must_name_the_request_that_granted_it(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    with pytest.raises(ValidationError, match="approval request"):
        _for_step(
            declaration,
            tier_policy,
            approval_policy,
            minted_at,
            minted_by=MintedBy(MintKind.APPROVER, "tok1"),
            approval_request_id=None,
        )


def test_the_minted_event_carries_the_shape_and_no_content(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    minted = _for_step(declaration, tier_policy, approval_policy, minted_at)
    body = IntentMinted.of(minted)
    assert body.event_type.value == "intent.minted"
    assert body.intent_id == minted.intent_id
    assert body.minted_by == "policy"
    assert "description" not in body.as_canonical()


def test_intent_minting_goldens(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    """All four minting cases, byte-identical on re-derivation (acceptance criterion 2)."""
    hybrid = ApprovalPolicy(mode=ApprovalMode.HYBRID, gate_egress_at=DataClassification.INTERNAL)
    plan_minted = _for_step(declaration, tier_policy, approval_policy, minted_at)
    confidential = dataclasses.replace(declaration, classification=DataClassification.CONFIDENTIAL)
    redlined = _for_step(
        declaration,
        tier_policy,
        approval_policy,
        minted_at,
        declaration=confidential,
        step=_plan_step(tier="remote_cheap", data_classification=DataClassification.CONFIDENTIAL),
        verdict=_verdict(
            outcome=StepOutcome.REDLINED,
            reason=VerdictReason.TIER_CEILING_SUBSTITUTION,
            declared_tier="remote_cheap",
            approved_tier="local_large",
        ),
    )
    bypass = mint_bypass_default(
        intent_id="01BYPASS0000000000000000A0",
        declaration=declaration,
        tier_policy=tier_policy,
        policy=hybrid,
        minted_at=minted_at,
    )
    superseded = supersede(
        plan_minted,
        tier_policy=tier_policy,
        policy=hybrid,
        minted_by=MintedBy(MintKind.APPROVER, "01TOKEN000000000000000000"),
        minted_at=minted_at,
        fallback_tiers=("remote_cheap",),
        max_turns=12,
        approval_request_id="01REQUEST00000000000000000",
    )
    cases = {
        "plan": plan_minted.as_canonical(),
        "redline": redlined.as_canonical(),
        "bypass_default": bypass.as_canonical(),
        "superseding_revision": superseded.as_canonical(),
        "events": {
            name: IntentMinted.of(intent).as_canonical()
            for name, intent in (
                ("plan", plan_minted),
                ("redline", redlined),
                ("bypass_default", bypass),
                ("superseding_revision", superseded),
            )
        },
    }
    golden = _GOLDEN_DIR / "intent_minting.json"
    produced = canonical_json(cases)
    if not golden.exists():  # pragma: no cover — first run writes the golden
        golden.write_text(produced + "\n", encoding="utf-8")
    assert produced + "\n" == golden.read_text(encoding="utf-8")


def test_direct_construction_refuses_the_remaining_invariants(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> None:
    """The rest of the total validation, so hand-building an intent stays pointless."""
    minted = _for_step(declaration, tier_policy, approval_policy, minted_at)
    for broken, expected in (
        ({"revision": 0, "supersedes": None}, "revision starts at 1"),
        ({"fallback_tiers": ("local_large", "local_large")}, "must not repeat"),
        ({"budget_sample_count": -1}, "budget_sample_count"),
        ({"trajectory_id": " "}, "trajectory_id"),
        ({"step_id": ""}, "step_id"),
        ({"approved_tier": " "}, "approved_tier"),
    ):
        with pytest.raises(ValidationError, match=expected):
            dataclasses.replace(minted, **broken)
