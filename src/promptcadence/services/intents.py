"""promptcadence.services.intents — intents as rows, and rows back into intents by re-minting.

Two halves, and the second is the one worth reading.

**Rows.** :func:`intent_row` maps a minted envelope onto ``execution_intents`` and
:func:`intent_document` renders a row in the same canonical form the envelope renders itself in,
so the two can be compared byte for byte.

**Re-minting, never rehydrating.** There is no path that constructs an
:class:`~promptcadence.domain.intent.ExecutionIntent` from a row
(``test_no_module_mints_an_intent_outside_domain_intent``), and that is deliberate: a row is a
claim about what was minted, and a claim is checked by minting again from the recorded inputs and
comparing. :func:`rebuild_intents` does exactly that for every revision of every intent a
trajectory holds — the bypass default from the declaration and the tier snapshot, a step's
revision 1 from the plan step and the verdict the approval recorded, and every later revision by
superseding the one before it with the fields the row says it carried. A revision the re-mint
cannot reproduce — a configuration change since the claim, an edited row — refuses to run rather
than running turns under an envelope nobody minted.

**Step budgets.** Lifecycle §5's ``budget_overrun`` row says a step's intent budget is its
estimate times two, and :func:`step_budgets` is that one multiplication, so the number lives in one
place and the re-mint reproduces it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from baseaicore import DataClassification, Money, ValidationError
from sqlalchemy import select

from promptcadence.domain.errors import ErrorCode
from promptcadence.domain.intent import (
    BYPASS_STEP_ID,
    ExecutionIntent,
    MintedBy,
    MintKind,
    mint_bypass_default,
    mint_for_step,
    supersede,
)
from promptcadence.domain.plan import Plan, PlanStep
from promptcadence.domain.policy import (
    EstimateSource,
    GateVerdict,
    PlanVerdict,
    StepEstimate,
    StepOutcome,
    StepVerdict,
    TrajectoryOutcome,
    VerdictReason,
)
from promptcadence.infrastructure.db import models
from promptcadence.infrastructure.db.models import ExecutionIntent as ExecutionIntentRow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.orm import Session

    from promptcadence.domain.policy import ApprovalPolicy
    from promptcadence.domain.tiers import TierPolicy
    from promptcadence.domain.trajectory import TrajectoryDeclaration
    from promptcadence.services.views import TrajectoryView

__all__ = [
    "STEP_BUDGET_MULTIPLIER",
    "RecordedPlan",
    "intent_document",
    "intent_row",
    "live_intents",
    "mint_step_intent",
    "minted_by_from_recorded",
    "plan_step_from_row",
    "rebuild_intents",
    "recorded_plan",
    "step_budgets",
    "step_verdict_from_document",
]

STEP_BUDGET_MULTIPLIER: Final = 2
"""Lifecycle §5: a step's intent budget is its estimate × 2; past it is ``budget_overrun``."""


def step_budgets(estimate: StepEstimate) -> tuple[int, Money | None]:
    """Return the ``(token_budget, money_budget)`` slice an estimate mints into.

    Args:
        estimate: The step's estimate, with its labelled source.

    Returns:
        The token slice, at least 1 so an estimate of zero still permits a turn, and the money
        slice or ``None`` when the estimate priced nothing (local work: ``UNSUPPORTED``, never
        ``$0.00``).
    """
    tokens = max(estimate.token_estimate * STEP_BUDGET_MULTIPLIER, 1)
    money = (
        Money(
            currency=estimate.money_estimate.currency,
            nanos=estimate.money_estimate.nanos * STEP_BUDGET_MULTIPLIER,
        )
        if estimate.money_estimate is not None and estimate.money_estimate.nanos > 0
        else None
    )
    return tokens, money


def mint_step_intent(
    *,
    intent_id: str,
    declaration: TrajectoryDeclaration,
    step: PlanStep,
    verdict: StepVerdict,
    tier_policy: TierPolicy,
    policy: ApprovalPolicy,
    minted_by: MintedBy,
    minted_at: datetime,
    max_turns: int,
    approval_request_id: str | None = None,
) -> ExecutionIntent:
    """Mint one approved step's revision 1 with its budget slice derived from the verdict.

    The one place the planned path calls :func:`~promptcadence.domain.intent.mint_for_step`, so
    the slice arithmetic and the turn allowance come from here on the mint and on the re-mint.

    Args:
        intent_id: The new intent's identity.
        declaration: What the caller declared.
        step: The plan step.
        verdict: Its approval verdict, carrying the estimate and the tier execution is held to.
        tier_policy: The trajectory's tier snapshot.
        policy: The approval policy.
        minted_by: Who authorised it.
        minted_at: When.
        max_turns: ``[execution] max_turns_per_step``.
        approval_request_id: The grant that gated it, when one did.

    Returns:
        Revision 1.
    """
    tokens, money = step_budgets(verdict.estimate)
    return mint_for_step(
        intent_id=intent_id,
        declaration=declaration,
        step=step,
        verdict=verdict,
        tier_policy=tier_policy,
        policy=policy,
        minted_by=minted_by,
        minted_at=minted_at,
        max_turns=max_turns,
        token_budget=tokens,
        money_budget=money,
        approval_request_id=approval_request_id,
    )


def minted_by_from_recorded(value: str) -> MintedBy:
    """Rebuild the authority from ``execution_intents.minted_by``.

    Args:
        value: ``"policy"``, ``"bypass_default"`` or ``"approver:<token id>"``.

    Returns:
        The authority.

    Raises:
        ValidationError: If the string is none of the three shapes.
    """
    if value == MintKind.POLICY.value:
        return MintedBy(MintKind.POLICY)
    if value == MintKind.BYPASS_DEFAULT.value:
        return MintedBy(MintKind.BYPASS_DEFAULT)
    prefix = f"{MintKind.APPROVER.value}:"
    if value.startswith(prefix) and value[len(prefix) :].strip():
        return MintedBy(MintKind.APPROVER, approver_token_id=value[len(prefix) :])
    message = f"execution_intents.minted_by holds {value!r}, which names no minting authority"
    raise ValidationError(message, details={"field": "minted_by"})


def intent_row(intent: ExecutionIntent) -> ExecutionIntentRow:
    """Map a minted intent onto its ``execution_intents`` row.

    The row class is imported under an alias: ``test_no_module_mints_an_intent_outside_domain_
    intent`` walks every call named ``ExecutionIntent`` in the package, and constructing the ORM
    row must not read as minting the domain envelope — which it is not.
    """
    return ExecutionIntentRow(
        intent_id=intent.intent_id,
        revision=intent.revision,
        trajectory_id=intent.trajectory_id,
        step_id=intent.step_id,
        supersedes=intent.supersedes,
        approved_tier=intent.approved_tier,
        fallback_tiers_json=list(intent.fallback_tiers),
        permitted_egress_class=intent.permitted_egress_class.value,
        approved_tools_json=sorted(intent.approved_tools),
        max_classification=intent.max_classification.value,
        token_budget=intent.token_budget,
        money_budget_currency=intent.money_budget.currency if intent.money_budget else None,
        money_budget_nanos=intent.money_budget.nanos if intent.money_budget else None,
        budget_source=intent.budget_source.value,
        budget_sample_count=intent.budget_sample_count,
        max_turns=intent.max_turns,
        minted_by=intent.minted_by.as_recorded(),
        minted_at=intent.minted_at,
        approval_request_id=intent.approval_request_id,
        gate_json=intent.gate.as_canonical(),
    )


def intent_document(row: ExecutionIntentRow) -> dict[str, Any]:
    """The recorded intent in its canonical form, for comparison with a re-minted one."""
    return {
        "intent_id": row.intent_id,
        "trajectory_id": row.trajectory_id,
        "step_id": row.step_id,
        "revision": row.revision,
        "supersedes": row.supersedes,
        "approved_tier": row.approved_tier,
        "fallback_tiers": list(row.fallback_tiers_json),
        "permitted_egress_class": row.permitted_egress_class,
        "approved_tools": sorted(str(tool) for tool in row.approved_tools_json),
        "max_classification": row.max_classification,
        "token_budget": row.token_budget,
        "money_budget": _money_document(row),
        "budget_source": row.budget_source,
        "budget_sample_count": row.budget_sample_count,
        "max_turns": row.max_turns,
        "minted_by": row.minted_by,
        "minted_at": row.minted_at.isoformat(),
        "approval_request_id": row.approval_request_id,
        "gate": dict(row.gate_json),
    }


def _money_document(row: ExecutionIntentRow) -> dict[str, Any] | None:
    if row.money_budget_currency is not None and row.money_budget_nanos is not None:
        return {"currency": row.money_budget_currency, "nanos": row.money_budget_nanos}
    return None


def _money_of(row: ExecutionIntentRow) -> Money | None:
    if row.money_budget_currency is not None and row.money_budget_nanos is not None:
        return Money(currency=row.money_budget_currency, nanos=row.money_budget_nanos)
    return None


def plan_step_from_row(row: models.PlanStep) -> PlanStep:
    """Rebuild a validated plan step from its ``plan_steps`` row."""
    return PlanStep(
        step_id=row.step_id,
        description=row.description,
        depends_on=tuple(str(item) for item in row.depends_on_json),
        tools=tuple(str(item) for item in row.tools_json),
        tier=row.tier,
        data_classification=DataClassification(row.data_classification),
        expected_turns=row.expected_turns,
    )


def step_verdict_from_document(document: Mapping[str, Any]) -> StepVerdict:
    """Rebuild one step's verdict from ``plan_approvals.verdict_json``'s ``steps`` entry.

    The inverse of :meth:`~promptcadence.domain.policy.StepVerdict.as_canonical`, used by the
    re-mint: a step's revision 1 was minted from this verdict, so this is the input to reproduce.

    Raises:
        ValidationError: If the document does not describe a verdict — the same refusals
            construction makes.
    """
    estimate_doc = document["estimate"]
    money = estimate_doc.get("money_estimate")
    estimate = StepEstimate(
        int(estimate_doc["token_estimate"]),
        money_estimate=(
            Money(currency=str(money["currency"]), nanos=int(money["nanos"]))
            if isinstance(money, Mapping)
            else None
        ),
        source=EstimateSource(str(estimate_doc["source"])),
        sample_count=int(estimate_doc["sample_count"]),
    )
    gate_doc = document.get("gate")
    gate = (
        GateVerdict(
            egress_gated=bool(gate_doc["egress_gated"]),
            cost_gated=bool(gate_doc["cost_gated"]),
            gating_tier=str(gate_doc["gating_tier"]),
            egress_classification=(
                DataClassification(str(gate_doc["egress_classification"]))
                if gate_doc.get("egress_classification")
                else None
            ),
        )
        if isinstance(gate_doc, Mapping)
        else None
    )
    error_code = document.get("error_code")
    return StepVerdict(
        step_id=str(document["step_id"]),
        outcome=StepOutcome(str(document["outcome"])),
        reason=VerdictReason(str(document["reason"])),
        declared_tier=str(document["declared_tier"]),
        estimate=estimate,
        approved_tier=(
            str(document["approved_tier"]) if document.get("approved_tier") is not None else None
        ),
        fallback_tiers=tuple(str(name) for name in document.get("fallback_tiers", [])),
        gate=gate,
        requires_human_approval=bool(document.get("requires_human_approval", False)),
        error_code=ErrorCode(str(error_code)) if error_code else None,
        headroom_is_floor=bool(document.get("headroom_is_floor", False)),
    )


def rebuild_intents(
    session: Session,
    *,
    view: TrajectoryView,
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    policy: ApprovalPolicy,
) -> dict[str, tuple[ExecutionIntent, ...]]:
    """Re-mint every recorded intent revision of a trajectory and verify each against its row.

    Args:
        session: An open session.
        view: The trajectory.
        declaration: Its declaration, rebuilt from the row.
        tier_policy: Its recorded tier snapshot, wrapped with today's availability.
        policy: The approval policy as configured now.

    Returns:
        Every chain, keyed by ``step_id``, each as its revisions in order — revision 1 first, the
        live envelope last. Empty when nothing has been minted yet.

    Raises:
        ValidationError: A re-minted revision differs from its row (the approval policy or the
            configuration changed since the minting, or a row was edited), a step intent's plan
            step or verdict cannot be found, or ``minted_by`` names no authority. Turns cannot run
            under an envelope nobody minted.
    """
    rows: Sequence[ExecutionIntentRow] = list(
        session.execute(
            select(ExecutionIntentRow)
            .where(ExecutionIntentRow.trajectory_id == view.trajectory_id)
            .order_by(ExecutionIntentRow.intent_id, ExecutionIntentRow.revision)
        ).scalars()
    )
    if not rows:
        return {}
    steps, verdicts = _plan_inputs(session, view.trajectory_id)
    chains: dict[str, list[ExecutionIntent]] = {}
    by_intent: dict[str, list[ExecutionIntentRow]] = {}
    for row in rows:
        by_intent.setdefault(row.intent_id, []).append(row)
    for intent_id, revisions in by_intent.items():
        previous: ExecutionIntent | None = None
        for row in revisions:
            if previous is None:
                minted = _remint_first(
                    row,
                    view=view,
                    declaration=declaration,
                    tier_policy=tier_policy,
                    policy=policy,
                    steps=steps,
                    verdicts=verdicts,
                )
            else:
                minted = supersede(
                    previous,
                    tier_policy=tier_policy,
                    policy=policy,
                    minted_by=minted_by_from_recorded(row.minted_by),
                    minted_at=row.minted_at,
                    approved_tier=row.approved_tier,
                    fallback_tiers=tuple(str(name) for name in row.fallback_tiers_json),
                    approved_tools={str(tool) for tool in row.approved_tools_json},
                    max_classification=DataClassification(row.max_classification),
                    token_budget=row.token_budget,
                    money_budget=_money_of(row),
                    max_turns=row.max_turns,
                    approval_request_id=row.approval_request_id,
                )
            recorded = intent_document(row)
            if minted.as_canonical() != recorded:
                message = (
                    f"intent {intent_id} revision {row.revision} re-minted from the recorded "
                    "declaration, plan and tier snapshot does not match the envelope this "
                    "trajectory recorded; the approval policy or the configuration has changed "
                    "since it was minted, and turns cannot run under an envelope nobody minted"
                )
                raise ValidationError(
                    message,
                    details={
                        "field": "execution_intents",
                        "intent_id": intent_id,
                        "revision": row.revision,
                        "recorded": recorded,
                        "reminted": minted.as_canonical(),
                    },
                )
            chains.setdefault(row.step_id, []).append(minted)
            previous = minted
    return {step_id: tuple(chain) for step_id, chain in chains.items()}


def live_intents(chains: Mapping[str, Sequence[ExecutionIntent]]) -> dict[str, ExecutionIntent]:
    """The current (highest) revision of every chain, keyed by ``step_id``."""
    return {step_id: chain[-1] for step_id, chain in chains.items() if chain}


def _remint_first(
    row: ExecutionIntentRow,
    *,
    view: TrajectoryView,
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    policy: ApprovalPolicy,
    steps: Mapping[str, PlanStep],
    verdicts: Mapping[str, StepVerdict],
) -> ExecutionIntent:
    """Re-mint revision 1 by whichever of the two first-minting paths the row records."""
    if row.step_id == BYPASS_STEP_ID:
        # The bypass default's budgets are the trajectory's own ceilings *as they were at the
        # minting*. A granted ceiling raise moves the trajectory's ceiling afterwards (and mints
        # a superseding revision from the raised one), so revision 1 is re-minted from the
        # budgets its row recorded; everything else — tier, tools, classification, gates — is
        # re-derived and compared as usual.
        as_minted = dataclasses.replace(
            declaration, token_budget=row.token_budget, money_budget=_money_of(row)
        )
        return mint_bypass_default(
            intent_id=row.intent_id,
            declaration=as_minted,
            tier_policy=tier_policy,
            policy=policy,
            minted_at=row.minted_at,
            tier_override=view.tier_override,
        )
    step = steps.get(row.step_id)
    verdict = verdicts.get(row.step_id)
    if step is None or verdict is None:
        message = (
            f"intent {row.intent_id} governs step {row.step_id!r}, which the recorded plan and "
            "its approval do not describe"
        )
        raise ValidationError(message, details={"field": "step_id", "step_id": row.step_id})
    return mint_step_intent(
        intent_id=row.intent_id,
        declaration=declaration,
        step=step,
        verdict=verdict,
        tier_policy=tier_policy,
        policy=policy,
        minted_by=minted_by_from_recorded(row.minted_by),
        minted_at=row.minted_at,
        max_turns=row.max_turns,
        approval_request_id=row.approval_request_id,
    )


def _plan_inputs(
    session: Session, trajectory_id: str
) -> tuple[dict[str, PlanStep], dict[str, StepVerdict]]:
    """The validated plan's steps and the approval's per-step verdicts, or two empty mappings."""
    recorded = recorded_plan(session, trajectory_id)
    if recorded is None:
        return {}, {}
    steps = {step.step_id: step for step in recorded.plan.steps}
    verdicts = (
        {verdict.step_id: verdict for verdict in recorded.verdict.steps}
        if recorded.verdict is not None
        else {}
    )
    return steps, verdicts


@dataclass(frozen=True, slots=True)
class RecordedPlan:
    """The validated plan a trajectory recorded, with its verdict and each step's state.

    Attributes:
        plan_id: The ``plans`` row.
        plan: The plan, rebuilt from the verbatim document and the step rows — the digest check
            in :class:`~promptcadence.domain.plan.Plan` runs again here, so a row whose document
            and validated form have parted company cannot be executed from.
        verdict: The recorded verdict, or ``None`` before approval ran.
        step_status: Each step's execution state (``pending``, ``running``, ``committed``).
    """

    plan_id: str
    plan: Plan
    verdict: PlanVerdict | None
    step_status: Mapping[str, str]

    @property
    def committed(self) -> frozenset[str]:
        """The steps that have committed."""
        return frozenset(
            step_id for step_id, status in self.step_status.items() if status == "committed"
        )


def recorded_plan(session: Session, trajectory_id: str) -> RecordedPlan | None:
    """Read the trajectory's validated plan, its verdict and its steps' states.

    Args:
        session: An open session.
        trajectory_id: The trajectory.

    Returns:
        The recorded plan, or ``None`` when no drafting attempt validated.

    Raises:
        ValidationError: The rows do not describe a plan — an empty step set, or a document whose
            digest is not the one recorded.
    """
    plan_row = session.execute(
        select(models.Plan)
        .where(models.Plan.trajectory_id == trajectory_id, models.Plan.valid.is_(True))
        .order_by(models.Plan.attempt.desc())
        .limit(1)
    ).scalar_one_or_none()
    if plan_row is None:
        return None
    step_rows = list(
        session.execute(
            select(models.PlanStep)
            .where(models.PlanStep.plan_id == plan_row.id)
            .order_by(models.PlanStep.sequence)
        ).scalars()
    )
    plan = Plan(
        steps=tuple(plan_step_from_row(row) for row in step_rows),
        raw_document=plan_row.raw_document,
        document_sha256=plan_row.document_sha256,
    )
    approval = session.execute(
        select(models.PlanApproval)
        .where(models.PlanApproval.plan_id == plan_row.id)
        .order_by(models.PlanApproval.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    verdict: PlanVerdict | None = None
    if approval is not None:
        document = approval.verdict_json
        verdict = PlanVerdict(
            outcome=TrajectoryOutcome(str(document["outcome"])),
            steps=tuple(step_verdict_from_document(item) for item in document["steps"]),
            approval_policy_version=str(document["approval_policy_version"]),
        )
    return RecordedPlan(
        plan_id=plan_row.id,
        plan=plan,
        verdict=verdict,
        step_status={row.step_id: row.status for row in step_rows},
    )
