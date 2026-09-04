"""promptcadence.domain.policy — the auto approval verdict, and the gates every mint is checked at.

`PlanApprover`'s automatic verdict is a pure function of the tier policy, the egress policy and
the ledger's headroom (lifecycle §4.2), and its output is **data**: one verdict per step plus a
trajectory-level verdict. Approval's real output is the minting of intents
(:mod:`promptcadence.domain.intent`, ADR-0049 rule 5); this module decides *what* may be minted,
and it decides it identically for all three modes — the modes differ only in who authorises it.

Three things are settled here that the documents leave open, and each is recorded in the handoff
because later phases must not relitigate them.

**The auto policy grants no fallback tiers.** Lifecycle §3 says a step approved for one tier "does
not silently climb"; escalation is explicit and goes through the ``tier_escalation`` deviation and
a scoped re-approval that mints a superseding revision carrying the next tier. Granting fallbacks
up front would make that path dead code and would pre-approve egress nobody asked for. A human
approver (P7) and :func:`~promptcadence.domain.intent.supersede` can grant them; the automatic
policy does not.

**The version is derived, not declared.** ``approval_policy_version`` is persisted on every
trajectory, so a policy change that did not change the version would silently reinterpret the
record. "Remember to bump it" is not enforcement, so :attr:`ApprovalPolicy.version` is a digest
over two halves: the configured values (automatic — a changed gate changes the digest) and
:data:`APPROVAL_RULESET_DIGEST`, which is a digest of this module's *decisions* over a fixed
corpus and is asserted by ``tests/unit/test_domain_policy.py``. Change the logic and that test
fails with the new digest; the only way to make it pass is to edit the constant, and editing the
constant necessarily changes every deployment's version. The other half of pinning a decision is
the trajectory's :class:`~promptcadence.domain.tiers.TierSnapshot`, which fixes what the tiers were.

**Headroom is PromptCadence's own shape.** ``loadledger`` is not a dependency until P5, and
:class:`BudgetHeadroom` mirrors ``loadledger.CeilingVerdict`` field for field — including its
three honesty counts — so F1's adapter is a copy rather than an interpretation. Those counts are
load-bearing: approving a step against a **floor** while believing it a total is exactly the
failure ADR-0069 exists to prevent, so a floor is carried into the verdict and rendered as "at
least", and under ``partial_pricing = "strict"`` an amount that cannot be shown to be under the
cap binds as though it were over it.

Human approval *behaviour* is P7's. What is here is the mode enum, the gate definitions and the
verdict shapes, so P7 fills in behaviour rather than inventing vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Final

from baseaicore import DataClassification, Money, ValidationError, sha256_of

from promptcadence.domain.errors import ErrorCode
from promptcadence.domain.events import EventType
from promptcadence.domain.plan import Plan, PlanStep
from promptcadence.domain.tiers import Tier, TierPolicy, most_permissive
from promptcadence.domain.trajectory import TrajectoryDeclaration

__all__ = [
    "APPROVAL_RULESET_DIGEST",
    "ApprovalDenied",
    "ApprovalGranted",
    "ApprovalMode",
    "ApprovalPolicy",
    "ApprovalRequested",
    "BudgetDebited",
    "BudgetHeadroom",
    "EstimateSource",
    "GateVerdict",
    "PartialPricing",
    "PlanApproved",
    "PlanRejected",
    "PlanVerdict",
    "ReapprovalScope",
    "StepEstimate",
    "StepOutcome",
    "StepVerdict",
    "TrajectoryOutcome",
    "VerdictReason",
    "evaluate_gates",
    "evaluate_plan",
    "evaluate_step",
    "requires_human_approval",
]

APPROVAL_RULESET_DIGEST: Final = (
    "sha256:fa4714c24b80095c3989734d0591b5b8b15e0547465a672d4b97b8a4767e9c1b"
)
"""Digest of this module's decisions over the fixed corpus in ``tests/unit/test_domain_policy.py``.

Not a hand-maintained version number: the test recomputes it and fails with the new value when a
rule changes, and it feeds :attr:`ApprovalPolicy.version`, so the version cannot be forgotten.
"""


class ApprovalMode(StrEnum):
    """Who authorises the minting of an intent (ADR-0049 rule 1)."""

    AUTO = "auto"
    HYBRID = "hybrid"
    MANUAL = "manual"


class ReapprovalScope(StrEnum):
    """How much of a drift the deviation policy asks about (lifecycle §5, ``planning``)."""

    ON_TIER_OR_CLASSIFICATION_CHANGE = "on_tier_or_classification_change"
    ANY_DEVIATION = "any_deviation"


class PartialPricing(StrEnum):
    """How a money ceiling binds when a response could not be fully priced (ADR-0069).

    ``FLOOR`` accumulates what was priced and says so; "exceeded" is certain and "under budget" is
    not. ``STRICT`` reverses that for a budget that must not be crossed: an amount that cannot be
    shown to be under the cap is treated as over it.
    """

    FLOOR = "floor"
    STRICT = "strict"


class EstimateSource(StrEnum):
    """Where a step estimate came from (lifecycle §6). Recorded, never inferred.

    A model-generated cost guess is never a member and never an input: a number the model invented
    must not size the budget that constrains the model (ADR-0047).
    """

    HISTORICAL = "historical"
    CONFIGURED_DEFAULT = "configured_default"


class StepOutcome(StrEnum):
    """One step's verdict (lifecycle §4.2)."""

    APPROVED = "approved"
    REDLINED = "redlined"
    REJECTED = "rejected"


class TrajectoryOutcome(StrEnum):
    """The trajectory-level verdict over a whole plan."""

    APPROVED = "approved"
    GATED = "gated"
    REJECTED = "rejected"


class VerdictReason(StrEnum):
    """Why a step got the verdict it got. Closed, so the UI and P7 can both switch on it."""

    AUTO_APPROVED = "auto_approved"
    TIER_NOT_CONFIGURED = "tier_not_configured"
    TIER_CEILING_SUBSTITUTION = "tier_ceiling_substitution"
    NO_ADMITTING_TIER = "no_admitting_tier"
    TIER_UNAVAILABLE = "tier_unavailable"
    UNPRICED_EGRESS = "unpriced_egress"
    BUDGET_EXCEEDED = "budget_exceeded"
    ESTIMATES_EXCEED_HEADROOM = "estimates_exceed_headroom"
    GATED_EGRESS = "gated_egress"
    GATED_STEP_COST = "gated_step_cost"
    MANUAL_MODE = "manual_mode"


@dataclass(frozen=True, slots=True)
class StepEstimate:
    """What a step is expected to cost, and where that expectation came from.

    Computed by the layered estimator (lifecycle §6), which is LoadLedger's at P5 — this module
    takes an estimate, it never makes one, so no budget arithmetic of the estimator's kind lives
    here.

    Attributes:
        token_estimate: Expected tokens.
        money_estimate: Expected money, or ``None`` for a local tier, whose cost is
            ``UNSUPPORTED`` and never ``$0.00`` (ADR-0030).
        source: Whether it came from observed history or the configured per-tier default.
        sample_count: How many observations backed a historical estimate; ``0`` for a configured
            default. It appears in the approval record so a reader can weigh the number.

    Raises:
        ValidationError: If the token estimate is negative, or a historical estimate claims no
            samples — a "historical" estimate with nothing behind it is a configured default
            wearing a better label.
    """

    token_estimate: int
    money_estimate: Money | None = field(default=None, kw_only=True)
    source: EstimateSource = field(default=EstimateSource.CONFIGURED_DEFAULT, kw_only=True)
    sample_count: int = field(default=0, kw_only=True)

    def __post_init__(self) -> None:
        """Refuse a negative estimate or a historical estimate with no observations."""
        if self.token_estimate < 0:
            message = f"token_estimate must not be negative, got {self.token_estimate}"
            raise ValidationError(message, details={"field": "token_estimate"})
        if self.source is EstimateSource.HISTORICAL and self.sample_count < 1:
            message = "a historical estimate must name at least one sample"
            raise ValidationError(message, details={"field": "sample_count"})

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form recorded in the approval record and in goldens."""
        return {
            "token_estimate": self.token_estimate,
            "money_estimate": (
                self.money_estimate.as_canonical() if self.money_estimate is not None else None
            ),
            "source": self.source.value,
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True, slots=True)
class BudgetHeadroom:
    """What one active ceiling has left, with the counts that say how sure that is.

    Field-for-field the shape of ``loadledger.CeilingVerdict`` (LoadLedger spec §7), so F1's
    adapter is a copy. The three counts are not decoration: while ``money_remaining`` is derived
    from a floor, ``exceeded`` being ``True`` is certain and ``False`` is not, and an approval that
    dropped the counts would be approving against a number it believed to be a total.

    Attributes:
        scope: Which ceiling this is — ``"trajectory"``, ``"day"``, or ``"project:<name>"``. Free
            text because LoadLedger's scopes are labels, and the most restrictive verdict binds
            regardless of which one it is.
        exceeded: Whether the balance is strictly past a bound this ceiling sets.
        money_remaining: What is left of the money cap, or ``None`` when it binds no money. An
            **upper** bound whenever ``unpriced_debit_count`` is non-zero.
        tokens_remaining: What is left of the token cap, or ``None`` when it binds no tokens.
        unpriced_debit_count: Debits that added less than their full cost. Non-zero makes
            ``money_remaining`` a floor-derived upper bound.
        untotalled_debit_count: The subset carrying an estimate that did not total — what a
            ``STRICT`` ceiling fires on.
        unmetered_debit_count: Debits leaving a token class unreported, making the token balance a
            floor too.
        partial_pricing: How this ceiling binds an incomplete price.

    Raises:
        ValidationError: If a count is negative, or ``untotalled_debit_count`` exceeds
            ``unpriced_debit_count`` — it is a subset of it, and a count that violates that is a
            mis-adapted verdict, not a tighter budget.
    """

    scope: str
    exceeded: bool
    money_remaining: Money | None = field(default=None, kw_only=True)
    tokens_remaining: int | None = field(default=None, kw_only=True)
    unpriced_debit_count: int = field(default=0, kw_only=True)
    untotalled_debit_count: int = field(default=0, kw_only=True)
    unmetered_debit_count: int = field(default=0, kw_only=True)
    partial_pricing: PartialPricing = field(default=PartialPricing.FLOOR, kw_only=True)

    def __post_init__(self) -> None:
        """Refuse a negative count or an untotalled count outside the unpriced set."""
        for name in ("unpriced_debit_count", "untotalled_debit_count", "unmetered_debit_count"):
            if getattr(self, name) < 0:
                message = f"{name} must not be negative"
                raise ValidationError(message, details={"field": name})
        if self.untotalled_debit_count > self.unpriced_debit_count:
            message = (
                "untotalled_debit_count is a subset of unpriced_debit_count; "
                f"got {self.untotalled_debit_count} of {self.unpriced_debit_count}"
            )
            raise ValidationError(message, details={"field": "untotalled_debit_count"})

    @property
    def money_is_floor(self) -> bool:
        """Whether the money figure is a floor — render it as "at least", never as a figure."""
        return self.unpriced_debit_count > 0

    @property
    def tokens_are_floor(self) -> bool:
        """Whether the token figure is a floor, because a class went unreported."""
        return self.unmetered_debit_count > 0

    @property
    def binds(self) -> bool:
        """Whether this ceiling refuses further work right now.

        ``True`` when the ceiling is already exceeded, and — under ``STRICT`` — when some debit in
        the window carried an estimate that did not total, because an amount that cannot be shown
        to be under the cap is treated as over it (ADR-0069).
        """
        if self.exceeded:
            return True
        return self.partial_pricing is PartialPricing.STRICT and self.untotalled_debit_count > 0

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form recorded in the approval record and in goldens."""
        return {
            "scope": self.scope,
            "exceeded": self.exceeded,
            "money_remaining": (
                self.money_remaining.as_canonical() if self.money_remaining is not None else None
            ),
            "tokens_remaining": self.tokens_remaining,
            "unpriced_debit_count": self.unpriced_debit_count,
            "untotalled_debit_count": self.untotalled_debit_count,
            "unmetered_debit_count": self.unmetered_debit_count,
            "partial_pricing": self.partial_pricing.value,
        }


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """The configured approval rules, and the version every trajectory records.

    Attributes:
        mode: ``auto``, ``hybrid`` or ``manual``.
        gate_egress_at: In ``hybrid``, egress at or above this level pauses for a person.
        gate_step_cost: In ``hybrid``, an estimated step cost above this pauses for a person.
            ``None`` disables the cost gate; it is never ``Money.zero()``, which would gate
            everything.
        request_timeout_hours: After this, a pending request halts the trajectory. A timeout is
            never a grant (ADR-0049 rule 4).
        reapproval_scope: How much of a drift the deviation policy asks about.

    Raises:
        ValidationError: If the timeout is not positive, or the cost gate is not positive. An
            unbounded wait is an operational leak, and a zero cost gate gates every step, which is
            the "gate that fires constantly is the gate nobody reads" failure ADR-0049 names.
    """

    mode: ApprovalMode = ApprovalMode.AUTO
    gate_egress_at: DataClassification = DataClassification.INTERNAL
    gate_step_cost: Money | None = None
    request_timeout_hours: float = 24.0
    reapproval_scope: ReapprovalScope = ReapprovalScope.ON_TIER_OR_CLASSIFICATION_CHANGE

    def __post_init__(self) -> None:
        """Refuse a non-positive timeout or a cost gate that would fire on every step."""
        if self.request_timeout_hours <= 0:
            message = (
                f"request_timeout_hours must be positive, got {self.request_timeout_hours}; an "
                "unbounded pending approval is a trajectory nobody is reminded of"
            )
            raise ValidationError(message, details={"field": "request_timeout_hours"})
        if self.gate_step_cost is not None and self.gate_step_cost.nanos <= 0:
            message = (
                "gate_step_cost must be positive when set; a zero gate fires on every step, and "
                "'no cost gate' is None"
            )
            raise ValidationError(message, details={"field": "gate_step_cost"})

    @property
    def version(self) -> str:
        """The ``approval_policy_version`` persisted on every trajectory.

        A digest over the configured values *and* :data:`APPROVAL_RULESET_DIGEST`, so both a
        changed gate and a changed rule change it. Nothing about it is remembered by a person.
        """
        return "sha256:" + sha256_of(
            {"ruleset": APPROVAL_RULESET_DIGEST, "policy": self.as_canonical()}
        )

    def as_canonical(self) -> dict[str, Any]:
        """Return the configured values in the form the version digest is computed over."""
        return {
            "mode": self.mode.value,
            "gate_egress_at": self.gate_egress_at.value,
            "gate_step_cost": (
                self.gate_step_cost.as_canonical() if self.gate_step_cost is not None else None
            ),
            "request_timeout_hours": self.request_timeout_hours,
            "reapproval_scope": self.reapproval_scope.value,
        }


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """What the approval gates said, evaluated at minting against the most permissive tier.

    ADR-0056 rule 4 in object form. ``gating_tier`` is the tier the gates were actually evaluated
    against, which is why the record can explain a gate that fired on a step that "was going to
    run locally anyway": the fallback it permitted is named right here.

    Attributes:
        egress_gated: Whether what could leave reaches ``gate_egress_at``.
        cost_gated: Whether the estimate exceeds ``gate_step_cost``.
        gating_tier: The most permissive tier in the set, by
            :func:`~promptcadence.domain.tiers.most_permissive`'s stated ordering.
        egress_classification: What would actually leave on that tier, or ``None`` when it is
            local and nothing leaves.
    """

    egress_gated: bool
    cost_gated: bool
    gating_tier: str
    egress_classification: DataClassification | None = None

    @property
    def gated(self) -> bool:
        """Whether either gate fired."""
        return self.egress_gated or self.cost_gated

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form carried on an intent and in goldens."""
        return {
            "egress_gated": self.egress_gated,
            "cost_gated": self.cost_gated,
            "gating_tier": self.gating_tier,
            "egress_classification": (
                self.egress_classification.value if self.egress_classification is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class StepVerdict:
    """One step's automatic verdict (lifecycle §4.2).

    Attributes:
        step_id: The step.
        outcome: ``approved``, ``redlined`` or ``rejected``.
        reason: Why, from the closed set.
        declared_tier: What the plan proposed. Preserved on a redline — the plan keeps the
            original, the intent carries the substitution (ADR-0056 rule 3).
        approved_tier: What execution is held to, or ``None`` on a rejection.
        fallback_tiers: Pre-approved fallbacks. Always empty under the automatic policy; see this
            module's docstring.
        estimate: The step estimate this verdict was rendered against.
        gate: What the gates said, or ``None`` when the step was rejected before gates applied.
        requires_human_approval: Whether a person must authorise the minting.
        error_code: The spec §13 code a rejection surfaces as, or ``None``.
        headroom_is_floor: Whether any active ceiling's money figure was a floor when this verdict
            was rendered. Carried so the record never shows an approval against an incomplete sum
            as though the sum were complete (ADR-0069).
    """

    step_id: str
    outcome: StepOutcome
    reason: VerdictReason
    declared_tier: str
    estimate: StepEstimate
    approved_tier: str | None = field(default=None, kw_only=True)
    fallback_tiers: tuple[str, ...] = field(default=(), kw_only=True)
    gate: GateVerdict | None = field(default=None, kw_only=True)
    requires_human_approval: bool = field(default=False, kw_only=True)
    error_code: ErrorCode | None = field(default=None, kw_only=True)
    headroom_is_floor: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        """Refuse a verdict that approves nothing to run on, or rejects while naming a tier."""
        if self.outcome is StepOutcome.REJECTED:
            if self.approved_tier is not None:
                message = f"step {self.step_id} was rejected but names an approved tier"
                raise ValidationError(message, details={"field": "approved_tier"})
            if self.error_code is None:
                message = f"step {self.step_id} was rejected with no error code (spec §13)"
                raise ValidationError(message, details={"field": "error_code"})
        elif self.approved_tier is None:
            message = f"step {self.step_id} was {self.outcome.value} but names no tier to run on"
            raise ValidationError(message, details={"field": "approved_tier"})

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form recorded in ``plan_approvals`` and in goldens."""
        return {
            "step_id": self.step_id,
            "outcome": self.outcome.value,
            "reason": self.reason.value,
            "declared_tier": self.declared_tier,
            "approved_tier": self.approved_tier,
            "fallback_tiers": list(self.fallback_tiers),
            "estimate": self.estimate.as_canonical(),
            "gate": self.gate.as_canonical() if self.gate is not None else None,
            "requires_human_approval": self.requires_human_approval,
            "error_code": self.error_code.value if self.error_code is not None else None,
            "headroom_is_floor": self.headroom_is_floor,
        }


@dataclass(frozen=True, slots=True)
class PlanVerdict:
    """The trajectory-level verdict plus every step's, with the policy version behind it.

    Attributes:
        outcome: ``approved`` when every step may mint now, ``gated`` when at least one needs a
            person first, ``rejected`` when the plan cannot run.
        steps: One verdict per step, in plan order.
        approval_policy_version: :attr:`ApprovalPolicy.version` at the moment of the verdict.
        headroom: The ceilings the verdict was rendered against, recorded so a later reader sees
            the numbers rather than the conclusion.
    """

    outcome: TrajectoryOutcome
    steps: tuple[StepVerdict, ...]
    approval_policy_version: str
    headroom: tuple[BudgetHeadroom, ...] = ()

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form recorded in ``plan_approvals`` and in goldens."""
        return {
            "outcome": self.outcome.value,
            "steps": [verdict.as_canonical() for verdict in self.steps],
            "approval_policy_version": self.approval_policy_version,
            "headroom": [item.as_canonical() for item in self.headroom],
        }

    def verdict_for(self, step_id: str) -> StepVerdict:
        """Return the verdict for one step.

        Args:
            step_id: The step.

        Returns:
            Its verdict.

        Raises:
            ValidationError: If the plan verdict has none for that step.
        """
        for verdict in self.steps:
            if verdict.step_id == step_id:
                return verdict
        message = f"no verdict for step {step_id!r}"
        raise ValidationError(message, details={"field": "step_id", "step_id": step_id})


def evaluate_gates(
    tiers: Sequence[Tier],
    *,
    classification: DataClassification,
    estimate: StepEstimate,
    policy: ApprovalPolicy,
) -> GateVerdict:
    """Evaluate the approval gates against the most permissive tier the set permits.

    ADR-0056 rule 4: **not** against the first choice. A step approved for ``local_fast`` with
    ``remote_frontier`` in its fallbacks would otherwise pass an egress gate that only looked at
    the primary and then escalate past it silently, so a fallback that never fires still had to be
    permitted.

    Args:
        tiers: The tiers the intent permits, approved tier first.
        classification: The work's classification, which bounds what could actually leave.
        estimate: The step estimate the cost gate is compared against.
        policy: The configured gates.

    Returns:
        What each gate said, and the tier they were evaluated against.

    Raises:
        ValidationError: If the tier set is empty.
    """
    tier = most_permissive(tiers, classification=classification)
    leaving = tier.egress_classification(classification)
    egress_gated = leaving is not None and leaving >= policy.gate_egress_at
    cost_gated = (
        policy.gate_step_cost is not None
        and estimate.money_estimate is not None
        and estimate.money_estimate > policy.gate_step_cost
    )
    return GateVerdict(
        egress_gated=egress_gated,
        cost_gated=cost_gated,
        gating_tier=tier.name,
        egress_classification=leaving,
    )


def requires_human_approval(gate: GateVerdict, *, mode: ApprovalMode) -> bool:
    """Whether a person must authorise this minting.

    Args:
        gate: What the gates said.
        mode: The configured approval mode.

    Returns:
        ``False`` in ``auto`` — the policy verdict applies directly. ``True`` in ``manual`` for
        everything. In ``hybrid``, ``True`` exactly when a gate fired. Gates fire on the bypass
        path too (ADR-0048): the mode is a property of what the intent permits, not of how the
        work was planned.
    """
    match mode:
        case ApprovalMode.AUTO:
            return False
        case ApprovalMode.MANUAL:
            return True
        case ApprovalMode.HYBRID:
            return gate.gated


def evaluate_step(
    step: PlanStep,
    *,
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    policy: ApprovalPolicy,
    estimate: StepEstimate,
    headroom_is_floor: bool = False,
) -> StepVerdict:
    """Render one step's automatic verdict.

    The rules, in order, each traceable to a document:

    1. The declared tier must be configured in the trajectory's snapshot (lifecycle §4.1).
    2. The tier must admit the step's classification (lifecycle §2). When it does not, the first
       tier in ``escalation_order`` that admits is substituted — a **redline**, with the plan
       keeping the original (lifecycle §4.2). When none admits, the step is rejected: there is no
       tier this work may run on, and running it somewhere else is the egress this application
       exists to prevent.
    3. A remote tier must name a pricing source. Unpriced egress is refused, not free — a ceiling
       cannot bind what cannot be priced (spec §11 contract 5, ADR-0030).
    4. The tier must be available. Until LC-E1 lands, every remote tier reports
       ``loadcoach_has_no_remote_provider`` (lifecycle §3), so a plan proposing one is rejected
       with that reason rather than approved and then failed at dispatch.
    5. The gates are evaluated against the most permissive tier the verdict permits, and the mode
       decides whether that means a person.

    Args:
        step: The plan step.
        declaration: What the caller declared. The step's classification is at or below it —
            :func:`~promptcadence.domain.plan.validate_plan_document` has already refused a plan
            that launders, and this function does not restate that check (ADR-0042).
        tier_policy: The trajectory's tier snapshot plus what LoadCoach has registered.
        policy: The configured approval rules.
        estimate: What this step is expected to cost.
        headroom_is_floor: Whether any active ceiling's money figure was a floor.

    Returns:
        The step's verdict.
    """
    known = set(tier_policy.snapshot.by_name)
    if step.tier not in known:
        return StepVerdict(
            step_id=step.step_id,
            outcome=StepOutcome.REJECTED,
            reason=VerdictReason.TIER_NOT_CONFIGURED,
            declared_tier=step.tier,
            estimate=estimate,
            error_code=ErrorCode.TIER_NOT_CONFIGURED,
            headroom_is_floor=headroom_is_floor,
        )

    declared = tier_policy.snapshot.require(step.tier)
    resolved = declared
    reason = VerdictReason.AUTO_APPROVED
    outcome = StepOutcome.APPROVED
    if not declared.admits(step.data_classification):
        admitting = tier_policy.admitting_tiers(step.data_classification)
        if not admitting:
            return StepVerdict(
                step_id=step.step_id,
                outcome=StepOutcome.REJECTED,
                reason=VerdictReason.NO_ADMITTING_TIER,
                declared_tier=step.tier,
                estimate=estimate,
                error_code=ErrorCode.EGRESS_DENIED,
                headroom_is_floor=headroom_is_floor,
            )
        resolved = admitting[0]
        reason = VerdictReason.TIER_CEILING_SUBSTITUTION
        outcome = StepOutcome.REDLINED

    if resolved.is_remote and not resolved.pricing_source.strip():
        return StepVerdict(
            step_id=step.step_id,
            outcome=StepOutcome.REJECTED,
            reason=VerdictReason.UNPRICED_EGRESS,
            declared_tier=step.tier,
            estimate=estimate,
            error_code=ErrorCode.UNPRICED_EGRESS_REFUSED,
            headroom_is_floor=headroom_is_floor,
        )

    availability = tier_policy.availability(resolved.name)
    if not availability.available:
        return StepVerdict(
            step_id=step.step_id,
            outcome=StepOutcome.REJECTED,
            reason=VerdictReason.TIER_UNAVAILABLE,
            declared_tier=step.tier,
            estimate=estimate,
            error_code=ErrorCode.TIER_UNAVAILABLE,
            headroom_is_floor=headroom_is_floor,
        )

    gate = evaluate_gates(
        (resolved,),
        classification=step.data_classification,
        estimate=estimate,
        policy=policy,
    )
    needs_human = requires_human_approval(gate, mode=policy.mode)
    if needs_human and reason is VerdictReason.AUTO_APPROVED:
        reason = _gate_reason(gate, mode=policy.mode)
    return StepVerdict(
        step_id=step.step_id,
        outcome=outcome,
        reason=reason,
        declared_tier=step.tier,
        estimate=estimate,
        approved_tier=resolved.name,
        gate=gate,
        requires_human_approval=needs_human,
        headroom_is_floor=headroom_is_floor,
    )


def _gate_reason(gate: GateVerdict, *, mode: ApprovalMode) -> VerdictReason:
    """Return the reason a step needs a person, preferring the gate that actually fired."""
    if mode is ApprovalMode.MANUAL:
        return VerdictReason.MANUAL_MODE
    if gate.egress_gated:
        return VerdictReason.GATED_EGRESS
    return VerdictReason.GATED_STEP_COST


def evaluate_plan(
    plan: Plan,
    *,
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    policy: ApprovalPolicy,
    estimates: Mapping[str, StepEstimate],
    headroom: Sequence[BudgetHeadroom] = (),
) -> PlanVerdict:
    """Render the automatic verdict over a whole plan.

    Budget is evaluated once, at the plan level, because lifecycle §6 approves a plan "only if the
    sum of step estimates fits the remaining ceilings" — a per-step check would approve every step
    individually and the plan collectively over budget. Any ceiling that already binds rejects the
    plan outright; then the summed estimates are compared against what each ceiling has left. The
    comparison is a sum and a ``>``; the *estimator* is LoadLedger's (P5), and nothing here
    invents a number.

    Args:
        plan: The validated plan.
        declaration: What the caller declared.
        tier_policy: The trajectory's tier snapshot plus what LoadCoach has registered.
        policy: The configured approval rules.
        estimates: One estimate per step id.
        headroom: Every active ceiling's verdict. Empty means budget is not being evaluated, which
            is the case in Phase 2 tests and until LoadLedger arrives at P5.

    Returns:
        The trajectory-level verdict with one step verdict each, and the policy version.

    Raises:
        ValidationError: If a step has no estimate. Approving a step whose cost nobody estimated
            would put an unbounded step inside a bounded plan.
    """
    missing = [step.step_id for step in plan.steps if step.step_id not in estimates]
    if missing:
        message = f"no estimate for step(s) {', '.join(missing)}"
        raise ValidationError(message, details={"field": "estimates", "step_ids": missing})

    binding = [item for item in headroom if item.binds]
    is_floor = any(item.money_is_floor for item in headroom)
    if binding:
        return _reject_all(
            plan,
            estimates=estimates,
            reason=VerdictReason.BUDGET_EXCEEDED,
            code=ErrorCode.BUDGET_EXCEEDED,
            policy=policy,
            headroom=headroom,
            headroom_is_floor=is_floor,
        )

    if not _estimates_fit(plan, estimates=estimates, headroom=headroom):
        return _reject_all(
            plan,
            estimates=estimates,
            reason=VerdictReason.ESTIMATES_EXCEED_HEADROOM,
            code=ErrorCode.BUDGET_EXCEEDED,
            policy=policy,
            headroom=headroom,
            headroom_is_floor=is_floor,
        )

    verdicts = tuple(
        evaluate_step(
            step,
            declaration=declaration,
            tier_policy=tier_policy,
            policy=policy,
            estimate=estimates[step.step_id],
            headroom_is_floor=is_floor,
        )
        for step in plan.steps
    )
    if any(verdict.outcome is StepOutcome.REJECTED for verdict in verdicts):
        outcome = TrajectoryOutcome.REJECTED
    elif any(verdict.requires_human_approval for verdict in verdicts):
        outcome = TrajectoryOutcome.GATED
    else:
        outcome = TrajectoryOutcome.APPROVED
    return PlanVerdict(
        outcome=outcome,
        steps=verdicts,
        approval_policy_version=policy.version,
        headroom=tuple(headroom),
    )


def _estimates_fit(
    plan: Plan, *, estimates: Mapping[str, StepEstimate], headroom: Sequence[BudgetHeadroom]
) -> bool:
    """Whether the summed step estimates fit inside every active ceiling's remainder."""
    total_tokens = sum(estimates[step.step_id].token_estimate for step in plan.steps)
    money_estimates = [
        estimates[step.step_id].money_estimate
        for step in plan.steps
        if estimates[step.step_id].money_estimate is not None
    ]
    total_money: Money | None = None
    for amount in money_estimates:
        assert amount is not None  # noqa: S101 — filtered above
        total_money = amount if total_money is None else total_money + amount
    for item in headroom:
        if item.tokens_remaining is not None and total_tokens > item.tokens_remaining:
            return False
        if (
            item.money_remaining is not None
            and total_money is not None
            and total_money > item.money_remaining
        ):
            return False
    return True


def _reject_all(
    plan: Plan,
    *,
    estimates: Mapping[str, StepEstimate],
    reason: VerdictReason,
    code: ErrorCode,
    policy: ApprovalPolicy,
    headroom: Sequence[BudgetHeadroom],
    headroom_is_floor: bool,
) -> PlanVerdict:
    """Reject every step for one plan-level cause, naming it on each verdict.

    ``PLAN_REJECTED`` always lists every step's verdict and the ceiling that rejected it (spec
    §13): "the plan was refused" without numbers is a defect, and the numbers are the headroom
    carried on the returned verdict.
    """
    return PlanVerdict(
        outcome=TrajectoryOutcome.REJECTED,
        steps=tuple(
            StepVerdict(
                step_id=step.step_id,
                outcome=StepOutcome.REJECTED,
                reason=reason,
                declared_tier=step.tier,
                estimate=estimates[step.step_id],
                error_code=code,
                headroom_is_floor=headroom_is_floor,
            )
            for step in plan.steps
        ),
        approval_policy_version=policy.version,
        headroom=tuple(headroom),
    )


@dataclass(frozen=True, slots=True)
class PlanApproved:
    """``plan.approved`` - T4. Counts and the policy version; never a step description."""

    event_type: ClassVar[EventType] = EventType.PLAN_APPROVED
    trajectory_id: str
    step_count: int
    redlined_count: int
    approval_policy_version: str

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "step_count": self.step_count,
            "redlined_count": self.redlined_count,
            "approval_policy_version": self.approval_policy_version,
        }


@dataclass(frozen=True, slots=True)
class PlanRejected:
    """``plan.rejected`` - T6. Every step's verdict reason, because a bare refusal is a defect."""

    event_type: ClassVar[EventType] = EventType.PLAN_REJECTED
    trajectory_id: str
    reasons: tuple[tuple[str, VerdictReason], ...]
    approval_policy_version: str

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "reasons": [
                {"step_id": step_id, "reason": reason.value} for step_id, reason in self.reasons
            ],
            "approval_policy_version": self.approval_policy_version,
        }


@dataclass(frozen=True, slots=True)
class BudgetDebited:
    """``budget.debited`` — one recorded debit, in this application's own vocabulary.

    ``loadledger.LedgerEntry.as_canonical()`` is the shape the suite's ``budget.debited`` event
    carries (LoadLedger spec §17), and this is that shape said in PromptCadence's terms: the four
    token classes, the pricing hash, and every active ceiling's verdict after the debit.

    **There is no money field, and that is the point** (ADR-0030 rule 1). The stored facts are the
    usage and the ``pricing_hash`` the cost was derived from; a money figure here would be a
    derived number in a record of truth, and a later price correction would have nowhere to go. The
    only money in this body rides inside :attr:`headroom`, where it is a *verdict* — what a ceiling
    said, with the counts that say whether it said it from a floor.

    Attributes:
        trajectory_id: The run the debit was recorded against.
        turn_id: The debit's ``source_ref`` — the turn this spend came from. Reconciliation is
            idempotent by this, so it is the field a reader needs to tie a debit to its turn.
        entry_id: The ledger's own id for the entry.
        tier: The tier that ran the turn, matching the debit's ``tier:<name>`` tag.
        project: The project label, matching its ``project:<name>`` tag, or ``None``.
        usage: The four token classes, ``"unsupported"`` where the provider reported none — never
            ``0``, which would claim a class was used zero times (ADR-0016, ADR-0070).
        unpriced: Whether the debit added less than its full cost: no estimate at all (the local
            case), or one that did not total.
        pricing_hash: The price record the cost was derived from, carried even when the estimate
            came out unpriced — knowing *which* price list failed to price a call is how the gap
            gets closed. ``None`` when no pricing was applied at all.
        headroom: One verdict per active ceiling, in configuration order, as of this debit.
    """

    event_type: ClassVar[EventType] = EventType.BUDGET_DEBITED
    trajectory_id: str
    turn_id: str
    entry_id: str
    tier: str
    project: str | None
    usage: Mapping[str, int | str]
    unpriced: bool
    pricing_hash: str | None
    headroom: tuple[BudgetHeadroom, ...]

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "turn_id": self.turn_id,
            "entry_id": self.entry_id,
            "tier": self.tier,
            "project": self.project,
            "usage": dict(self.usage),
            "unpriced": self.unpriced,
            "pricing_hash": self.pricing_hash,
            "headroom": [one.as_canonical() for one in self.headroom],
        }


@dataclass(frozen=True, slots=True)
class ApprovalRequested:
    """``approval.requested`` - T5 and T10. What is being asked, and by when it expires."""

    event_type: ClassVar[EventType] = EventType.APPROVAL_REQUESTED
    trajectory_id: str
    approval_request_id: str
    step_ids: tuple[str, ...]
    reason: VerdictReason
    expires_at: str

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "approval_request_id": self.approval_request_id,
            "step_ids": list(self.step_ids),
            "reason": self.reason.value,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class ApprovalGranted:
    """``approval.granted`` - T8. Which identity authorised it, so the record says who."""

    event_type: ClassVar[EventType] = EventType.APPROVAL_GRANTED
    trajectory_id: str
    approval_request_id: str
    approver_token_id: str
    minted_intent_ids: tuple[str, ...]

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "approval_request_id": self.approval_request_id,
            "approver_token_id": self.approver_token_id,
            "minted_intent_ids": list(self.minted_intent_ids),
        }


@dataclass(frozen=True, slots=True)
class ApprovalDenied:
    """``approval.denied`` - T9. A denial and a timeout share this shape; ``timed_out`` says so."""

    event_type: ClassVar[EventType] = EventType.APPROVAL_DENIED
    trajectory_id: str
    approval_request_id: str
    timed_out: bool
    approver_token_id: str | None = None

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "approval_request_id": self.approval_request_id,
            "timed_out": self.timed_out,
            "approver_token_id": self.approver_token_id,
        }
