"""promptcadence.domain.intent — the ExecutionIntent, and why no turn can exist without one.

This is the load-bearing wall. Spec §11's contract 1 says there is **no code path that executes a
turn without an immutable envelope to check it against**, and ADR-0056 makes that structural
rather than procedural: approval's output is not a verdict on a document, it is the minting of one
immutable intent per approved step, and the bypass path mints one default intent from
``TierPolicy`` before its first turn. Both paths then run the same code, which is what lets the
contract-1 diff test compare two record sets that differ only in the plan rows.

**How "no turn without an intent" is enforced, structurally.**
:class:`~promptcadence.domain.threads.Turn` takes its ``provenance`` positionally with no default,
so a turn cannot be constructed without one. PromptCadence's provenance is
:class:`TurnProvenance`, which declares the intent as a ``dataclasses.InitVar``: constructing one
*requires handing over an* :class:`ExecutionIntent` *object*, and ``intent_id`` and
``intent_revision`` are derived from it rather than passed in. There is no private constructor to
find, no factory to forget and no flag to be out of; omitting the intent is a ``TypeError``. The
InitVar leaves no trace — it is not a field, so it is absent from ``==``, from ``repr`` and from
``as_canonical``, and a turn carries an intent *reference*, never the envelope itself.

That is the same mechanism CutCtx uses to make its compaction invariants the only path, chosen for
the same reason: a validating factory can be bypassed, and a "proof of validation" carried on an
object is forgeable in Python. Making the intent part of construction removes the bypass instead
of policing it.

**Three minting paths, and no fourth.** :func:`mint_for_step` from an approved plan verdict,
:func:`mint_bypass_default` from tier-policy defaults, and :func:`supersede` from an existing
revision. Direct construction is *safe* — ``__post_init__`` validates totally, so a hand-built
intent that is correct is accepted and one that lies is refused — but ``test_domain_intent.py``
walks the source of every module under ``src/promptcadence/`` and fails if any of them constructs
an :class:`ExecutionIntent` outside this module. A fourth path is a CI failure, not a review note.

**Immutable, superseded rather than edited.** A redline resolves at minting: the intent carries
the substituted tier and the plan keeps what the model proposed. A scoped re-approval mints
revision *n+1* with ``supersedes`` pointing at *n*, and *n* is retained. ``revision > 1`` with no
``supersedes`` is refused at construction, so a later revision cannot come into existence except
through :func:`supersede`, which needs the predecessor object in hand.
"""

from __future__ import annotations

from collections.abc import Sequence, Set
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Final

from baseaicore import DataClassification, Money, ValidationError

from promptcadence.domain.events import EventType
from promptcadence.domain.plan import PlanStep
from promptcadence.domain.policy import (
    ApprovalPolicy,
    EstimateSource,
    GateVerdict,
    StepEstimate,
    StepOutcome,
    StepVerdict,
    evaluate_gates,
)
from promptcadence.domain.threads import Turn
from promptcadence.domain.tiers import EgressClass, Tier, TierPolicy
from promptcadence.domain.trajectory import TrajectoryDeclaration

__all__ = [
    "BYPASS_STEP_ID",
    "GOVERNED_INTENT_FIELDS",
    "RECORD_INTENT_FIELDS",
    "ExecutionIntent",
    "GovernedTurn",
    "IntentMinted",
    "MintKind",
    "MintedBy",
    "TurnProvenance",
    "mint_bypass_default",
    "mint_for_step",
    "supersede",
]

BYPASS_STEP_ID: Final = "loop"
"""The synthetic ``step_id`` the bypass path's single default intent carries (ADR-0056 §1).

Synthetic rather than absent: a nullable ``step_id`` would make every query and every join in the
explanation branch on the mode, which is the branching ADR-0056 exists to collapse.
"""

GOVERNED_INTENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "approved_tier",
        "fallback_tiers",
        "permitted_egress_class",
        "max_classification",
        "approved_tools",
        "token_budget",
        "money_budget",
        "max_turns",
    }
)
"""The intent fields a turn's facts can contradict — the closed taxonomy's other half.

There is exactly one deviation category per field group here, plus one for a promise contradicted
after the fact, so the category set is finite by construction (ADR-0056 §5). A test asserts that
this set and :data:`RECORD_INTENT_FIELDS` together cover every field of
:class:`ExecutionIntent`, which is what forces a *decision* when a field is added: a governed field
needs a category, a disposition row and — per ADR-0056's revisit trigger — its own ADR.
"""

RECORD_INTENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "intent_id",
        "trajectory_id",
        "step_id",
        "revision",
        "supersedes",
        "budget_source",
        "budget_sample_count",
        "minted_by",
        "minted_at",
        "approval_request_id",
        "gate",
    }
)
"""The intent fields that identify, date and explain it. Nothing a turn can contradict."""


class MintKind(StrEnum):
    """Who authorised a minting — ``minted_by`` in lifecycle §4.3's field list."""

    POLICY = "policy"
    APPROVER = "approver"
    BYPASS_DEFAULT = "bypass_default"


@dataclass(frozen=True, slots=True)
class MintedBy:
    """The authority behind one minting, so the record says who allowed every turn.

    Attributes:
        kind: Policy, a person, or the bypass default.
        approver_token_id: The approving token's identity. Required for
            :attr:`MintKind.APPROVER` and refused otherwise — a policy mint attributed to a person
            would put a name on a decision nobody made.

    Raises:
        ValidationError: If an approver mint names no token, or a non-approver mint names one.
    """

    kind: MintKind
    approver_token_id: str | None = None

    def __post_init__(self) -> None:
        """Refuse an unattributed approver mint, or an attributed automatic one."""
        if self.kind is MintKind.APPROVER and not (self.approver_token_id or "").strip():
            message = "an approver minting must name the approving token identity (ADR-0049)"
            raise ValidationError(message, details={"field": "approver_token_id"})
        if self.kind is not MintKind.APPROVER and self.approver_token_id is not None:
            message = f"a {self.kind.value} minting must not name an approver"
            raise ValidationError(message, details={"field": "approver_token_id"})

    def as_recorded(self) -> str:
        """Return the string stored in ``execution_intents.minted_by``.

        Returns:
            ``"policy"``, ``"bypass_default"``, or ``"approver:<token id>"`` — lifecycle §4.3's
            ``"policy" | approver token identity | "bypass_default"``, with the approver's
            identity prefixed so the three cannot be confused for one another.
        """
        if self.kind is MintKind.APPROVER:
            return f"approver:{self.approver_token_id}"
        return self.kind.value


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """The approved envelope exactly one turn at a time is checked against (lifecycle §4.3).

    Immutable, revisioned, and never edited. Construct one through :func:`mint_for_step`,
    :func:`mint_bypass_default` or :func:`supersede`; see the module docstring for why there is no
    fourth path and how that is enforced.

    Attributes:
        intent_id: Stable across every revision. The primary key is ``(intent_id, revision)``.
        trajectory_id: The trajectory this governs.
        step_id: The approved step, or :data:`BYPASS_STEP_ID` on the bypass path.
        revision: 1 for a first minting, *n+1* for a supersession.
        supersedes: The revision this replaces. ``None`` exactly when ``revision`` is 1.
        approved_tier: The tier execution is held to — the substitution on a redline, never the
            proposal.
        fallback_tiers: Ordered, pre-approved alternatives. Empty under the automatic policy;
            escalation is explicit (lifecycle §3).
        permitted_egress_class: The most permissive egress class any permitted tier has, resolved
            at minting. Carried on the intent so
            :func:`~promptcadence.domain.deviation.compare` needs no tier table to tell a remote
            answer on a local-only envelope from an ordinary one.
        approved_tools: A frozen subset of the trajectory allowlist.
        max_classification: At or below the trajectory's declaration.
        token_budget: The step's token slice.
        money_budget: Its money slice, or ``None`` for local work whose cost is ``UNSUPPORTED``
            rather than ``$0.00`` (ADR-0030).
        budget_source: Where the estimate behind those budgets came from.
        budget_sample_count: How many observations backed it.
        max_turns: How many tool round trips this grant covers. Exceeding it is a deviation
            (``turn_overrun``), never a silent continuation.
        minted_by: The authority.
        minted_at: When, timezone-aware.
        approval_request_id: The human grant that gated it, when one did.
        gate: What the gates said at minting, evaluated against the most permissive permitted tier
            (ADR-0056 rule 4).

    Raises:
        ValidationError: If an identifier is empty, the revision and ``supersedes`` disagree, the
            tier set repeats a tier, a budget or turn count is not positive, ``minted_at`` is
            naive, or an approver minting names no approval request. Validation is **total** on
            purpose: it is what makes constructing one by hand pointless rather than merely
            discouraged.
    """

    intent_id: str
    trajectory_id: str
    step_id: str
    revision: int
    supersedes: int | None
    approved_tier: str
    fallback_tiers: tuple[str, ...]
    permitted_egress_class: EgressClass
    approved_tools: frozenset[str]
    max_classification: DataClassification
    token_budget: int
    money_budget: Money | None
    budget_source: EstimateSource
    budget_sample_count: int
    max_turns: int
    minted_by: MintedBy
    minted_at: datetime
    approval_request_id: str | None
    gate: GateVerdict

    def __post_init__(self) -> None:  # noqa: C901 — total validation; each branch is one invariant
        """Refuse every intent that could not have been minted by one of the three paths."""
        for name in ("intent_id", "trajectory_id", "step_id", "approved_tier"):
            if not str(getattr(self, name)).strip():
                message = f"{name} must not be empty"
                raise ValidationError(message, details={"field": name})
        if self.revision < 1:
            message = f"revision starts at 1, got {self.revision}"
            raise ValidationError(message, details={"field": "revision"})
        if self.revision == 1 and self.supersedes is not None:
            message = "revision 1 supersedes nothing; there is no earlier envelope to replace"
            raise ValidationError(message, details={"field": "supersedes"})
        if self.revision > 1 and self.supersedes != self.revision - 1:
            message = (
                f"revision {self.revision} must supersede revision {self.revision - 1}, "
                f"got {self.supersedes}. A revision comes into existence only by supersession."
            )
            raise ValidationError(message, details={"field": "supersedes"})
        if self.approved_tier in self.fallback_tiers:
            message = f"tier {self.approved_tier!r} is both the approved tier and a fallback"
            raise ValidationError(message, details={"field": "fallback_tiers"})
        if len(set(self.fallback_tiers)) != len(self.fallback_tiers):
            message = "fallback_tiers must not repeat a tier"
            raise ValidationError(message, details={"field": "fallback_tiers"})
        if self.token_budget < 1:
            message = f"token_budget must be positive, got {self.token_budget}"
            raise ValidationError(message, details={"field": "token_budget"})
        if self.money_budget is not None and self.money_budget.nanos <= 0:
            message = "money_budget must be positive when set; 'no money budget' is None"
            raise ValidationError(message, details={"field": "money_budget"})
        if self.max_turns < 1:
            message = f"max_turns must be positive, got {self.max_turns}"
            raise ValidationError(message, details={"field": "max_turns"})
        if self.budget_sample_count < 0:
            message = "budget_sample_count must not be negative"
            raise ValidationError(message, details={"field": "budget_sample_count"})
        if self.minted_at.tzinfo is None or self.minted_at.utcoffset() is None:
            message = "minted_at must be timezone-aware"
            raise ValidationError(message, details={"field": "minted_at"})
        if self.minted_by.kind is MintKind.APPROVER and self.approval_request_id is None:
            message = "an approver minting must name the approval request that granted it"
            raise ValidationError(message, details={"field": "approval_request_id"})
        if self.gate.gating_tier not in self.permitted_tiers:
            message = (
                f"gates were evaluated against {self.gate.gating_tier!r}, which this intent does "
                "not permit; gates evaluate against the most permissive tier in the set "
                "(ADR-0056 rule 4)"
            )
            raise ValidationError(message, details={"field": "gate"})

    @property
    def permitted_tiers(self) -> tuple[str, ...]:
        """The tiers this intent permits: the approved tier, then its fallbacks, in order."""
        return (self.approved_tier, *self.fallback_tiers)

    @property
    def is_bypass_default(self) -> bool:
        """Whether this is the bypass path's default intent."""
        return self.minted_by.kind is MintKind.BYPASS_DEFAULT

    def permits_tool(self, tool: str) -> bool:
        """Whether this intent covers a tool call by that name."""
        return tool in self.approved_tools

    def provenance(self, *, trajectory_id: str, tier: str) -> TurnProvenance:
        """Return the provenance a turn under this intent must carry.

        The ergonomic entry to the structural guard: a turn's provenance can only be built from an
        intent object, and this is the intended way to build it.

        Args:
            trajectory_id: The trajectory the turn belongs to; must be this intent's.
            tier: The tier the turn was dispatched on. **Not** validated against
                :attr:`permitted_tiers` — a turn that ran on a tier outside the envelope is
                precisely the ``tier_violation`` that must be *recorded*, and refusing to record it
                would delete the evidence (lifecycle §5).

        Returns:
            The provenance, carrying ``(intent_id, revision)`` derived from this intent.

        Raises:
            ValidationError: If the trajectory does not match. That is a wiring error, not a
                governance event.
        """
        return TurnProvenance(trajectory_id=trajectory_id, tier=tier, intent=self)

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form persisted in ``execution_intents`` and used in goldens."""
        return {
            "intent_id": self.intent_id,
            "trajectory_id": self.trajectory_id,
            "step_id": self.step_id,
            "revision": self.revision,
            "supersedes": self.supersedes,
            "approved_tier": self.approved_tier,
            "fallback_tiers": list(self.fallback_tiers),
            "permitted_egress_class": self.permitted_egress_class.value,
            "approved_tools": sorted(self.approved_tools),
            "max_classification": self.max_classification.value,
            "token_budget": self.token_budget,
            "money_budget": (
                self.money_budget.as_canonical() if self.money_budget is not None else None
            ),
            "budget_source": self.budget_source.value,
            "budget_sample_count": self.budget_sample_count,
            "max_turns": self.max_turns,
            "minted_by": self.minted_by.as_recorded(),
            "minted_at": self.minted_at.isoformat(),
            "approval_request_id": self.approval_request_id,
            "gate": self.gate.as_canonical(),
        }


@dataclass(frozen=True, slots=True)
class TurnProvenance:
    """What every PromptCadence turn carries: which envelope it ran under, and on which tier.

    ``intent`` is an ``InitVar``, not a field. Building this **requires** an
    :class:`ExecutionIntent` instance, and ``intent_id``/``intent_revision`` are read off it rather
    than supplied, so they cannot name an envelope that does not exist. The InitVar is not part of
    ``==``, ``repr`` or :meth:`as_canonical`: a turn records a reference, never the envelope.

    Attributes:
        trajectory_id: The trajectory the turn belongs to.
        tier: The tier the turn was dispatched on. Recorded as observed, never corrected.
        intent_id: Derived from the intent.
        intent_revision: Derived from the intent, so the explanation shows which envelope each
            turn ran under and why a new revision appeared (ADR-0056 §3).

    Raises:
        ValidationError: If the intent governs a different trajectory.
        TypeError: If ``intent`` is omitted. That is the guard, and it is the whole point.
    """

    trajectory_id: str
    tier: str
    intent: InitVar[ExecutionIntent]
    intent_id: str = field(init=False)
    intent_revision: int = field(init=False)

    def __post_init__(self, intent: ExecutionIntent) -> None:
        """Derive the intent reference, refusing an intent from another trajectory."""
        if intent.trajectory_id != self.trajectory_id:
            message = (
                f"intent {intent.intent_id} governs trajectory {intent.trajectory_id}, "
                f"not {self.trajectory_id}"
            )
            raise ValidationError(message, details={"field": "trajectory_id"})
        object.__setattr__(self, "intent_id", intent.intent_id)
        object.__setattr__(self, "intent_revision", intent.revision)

    @classmethod
    def rehydrate(
        cls, *, trajectory_id: str, tier: str, intent_id: str, intent_revision: int
    ) -> TurnProvenance:
        """Rebuild the provenance of a turn that was **already** written, from its row.

        The one deliberate exception to "you must hold an intent", and it is not a way around the
        rule. A persisted turn's row *is* the evidence that an envelope existed when it ran; the
        alternative — re-reading the intent to reconstruct a turn already committed — would make
        reading depend on a revision that may since have been superseded, and would report the
        wrong envelope for that turn.

        It is not a governing path: it produces provenance for a turn that exists, never for one
        about to. ``tests/unit/test_domain_intent.py`` walks the source of every module under
        ``src/promptcadence/`` and fails if this is called anywhere but
        ``promptcadence.infrastructure``.

        Args:
            trajectory_id: From the row.
            tier: From the row.
            intent_id: From the row.
            intent_revision: From the row.

        Returns:
            The provenance the row recorded.

        Raises:
            ValidationError: If the recorded reference is incomplete.
        """
        if not intent_id.strip() or intent_revision < 1:
            message = (
                f"turn row records an incomplete envelope reference "
                f"({intent_id!r}, {intent_revision}); every executed turn records "
                "(intent_id, revision) (ADR-0056 §3)"
            )
            raise ValidationError(message, details={"field": "intent_id"})
        rebuilt = object.__new__(cls)
        object.__setattr__(rebuilt, "trajectory_id", trajectory_id)
        object.__setattr__(rebuilt, "tier", tier)
        object.__setattr__(rebuilt, "intent_id", intent_id)
        object.__setattr__(rebuilt, "intent_revision", intent_revision)
        return rebuilt

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form the ``turns`` row is built from."""
        return {
            "trajectory_id": self.trajectory_id,
            "tier": self.tier,
            "intent_id": self.intent_id,
            "intent_revision": self.intent_revision,
        }


type GovernedTurn = Turn[TurnProvenance]
"""A PromptCadence turn: a package-shaped turn whose provenance names the envelope it ran under."""


def mint_for_step(
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
    token_budget: int,
    money_budget: Money | None = None,
    approval_request_id: str | None = None,
) -> ExecutionIntent:
    """Mint the intent one approved plan step executes under.

    The redline is resolved **here**: the intent carries ``verdict.approved_tier``, which is the
    substitution when the approver made one, while ``step.tier`` keeps what the model proposed.
    Both facts survive, in the places they belong (ADR-0056 rule 3).

    Args:
        intent_id: The new intent's identity.
        declaration: What the caller declared. The intent's tools and classification are bounded
            by it.
        step: The plan step, for its declared tools and classification.
        verdict: The approval verdict, for the tier execution is held to.
        tier_policy: The trajectory's tier snapshot, for resolving egress classes.
        policy: The approval policy, for re-evaluating the gates against the minted tier set.
        minted_by: Who authorised this.
        minted_at: When, timezone-aware.
        max_turns: How many tool round trips this grant covers.
        token_budget: The step's token slice.
        money_budget: The step's money slice, or ``None``.
        approval_request_id: The grant that gated it, when one did.

    Returns:
        Revision 1 of the step's intent.

    Raises:
        ValidationError: If the verdict rejected the step — a rejection mints nothing — or if the
            step declares a tool the trajectory does not allow, or a classification above the
            trajectory's. The last two have already been refused by plan validation; they are
            re-checked here because minting is the moment the envelope becomes binding, and an
            intent wider than its declaration would be a grant nobody gave.
    """
    if verdict.outcome is StepOutcome.REJECTED or verdict.approved_tier is None:
        message = f"step {step.step_id} was rejected; a rejection mints no intent"
        raise ValidationError(message, details={"field": "verdict", "step_id": step.step_id})
    tools = frozenset(step.tools)
    _require_subset(tools, declaration.tool_allowlist, step.step_id)
    if step.data_classification > declaration.classification:
        message = (
            f"step {step.step_id} declares {step.data_classification.value!r}, above the "
            f"trajectory's {declaration.classification.value!r}"
        )
        raise ValidationError(message, details={"field": "max_classification"})
    return _mint(
        intent_id=intent_id,
        trajectory_id=declaration.trajectory_id,
        step_id=step.step_id,
        revision=1,
        supersedes=None,
        approved_tier=verdict.approved_tier,
        fallback_tiers=verdict.fallback_tiers,
        approved_tools=tools,
        max_classification=step.data_classification,
        token_budget=token_budget,
        money_budget=money_budget,
        estimate=verdict.estimate,
        max_turns=max_turns,
        minted_by=minted_by,
        minted_at=minted_at,
        approval_request_id=approval_request_id,
        tier_policy=tier_policy,
        policy=policy,
    )


def mint_bypass_default(
    *,
    intent_id: str,
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    policy: ApprovalPolicy,
    minted_at: datetime,
    estimate: StepEstimate | None = None,
    tier_override: str | None = None,
) -> ExecutionIntent:
    """Mint the one default intent a bypassed trajectory executes under (ADR-0056 §2).

    Everything comes from the declaration and from tier policy — ``policy.default_tier``, the
    trajectory allowlist, the trajectory's own classification and budget, ``execution.max_steps``
    as ``max_turns``. The bypass removes planning; it removes nothing else, and this function is
    why: after it returns, the loop is holding exactly what the planned path holds.

    Args:
        intent_id: The new intent's identity.
        declaration: What the caller declared.
        tier_policy: The trajectory's tier snapshot; its default tier is where a bypass turn
            starts.
        policy: The approval policy. Gates fire on the bypass path too (ADR-0048), here, at the
            minting of this intent.
        minted_at: When, timezone-aware.
        estimate: The step estimate the cost gate is evaluated against. ``None`` means no money
            estimate is available, which leaves the cost gate unfired rather than assuming a cost.
        tier_override: A per-request tier, when the caller named one. Must be configured.

    Returns:
        Revision 1 of the trajectory's default intent, with ``step_id`` :data:`BYPASS_STEP_ID`.

    Raises:
        TierNotConfiguredError: If ``tier_override`` names a tier the snapshot does not define.
    """
    tier = (
        tier_policy.snapshot.require(tier_override)
        if tier_override is not None
        else tier_policy.default_tier
    )
    return _mint(
        intent_id=intent_id,
        trajectory_id=declaration.trajectory_id,
        step_id=BYPASS_STEP_ID,
        revision=1,
        supersedes=None,
        approved_tier=tier.name,
        fallback_tiers=(),
        approved_tools=frozenset(declaration.tool_allowlist),
        max_classification=declaration.classification,
        token_budget=declaration.token_budget,
        money_budget=declaration.money_budget,
        estimate=estimate if estimate is not None else StepEstimate(0),
        max_turns=declaration.max_turns,
        minted_by=MintedBy(MintKind.BYPASS_DEFAULT),
        minted_at=minted_at,
        approval_request_id=None,
        tier_policy=tier_policy,
        policy=policy,
    )


def supersede(
    previous: ExecutionIntent,
    *,
    tier_policy: TierPolicy,
    policy: ApprovalPolicy,
    minted_by: MintedBy,
    minted_at: datetime,
    approved_tier: str | None = None,
    fallback_tiers: tuple[str, ...] | None = None,
    approved_tools: Set[str] | None = None,
    max_classification: DataClassification | None = None,
    token_budget: int | None = None,
    money_budget: Money | None = None,
    max_turns: int | None = None,
    estimate: StepEstimate | None = None,
    approval_request_id: str | None = None,
) -> ExecutionIntent:
    """Mint revision *n+1*, replacing ``previous`` without editing it.

    The only way a revision above 1 comes into existence, and it needs the predecessor in hand.
    Every unspecified field is inherited, so a re-approval that widens one dimension does not
    silently reset the others. The gates are re-evaluated against the new tier set, because a
    supersession is a minting and ADR-0049 rule 3 fires the gates at every minting — a re-approval
    that added a remote fallback must be gated as remote.

    Args:
        previous: The revision being replaced. It is retained; nothing here mutates it.
        tier_policy: The trajectory's tier snapshot.
        policy: The approval policy, for the gates.
        minted_by: Who authorised this revision.
        minted_at: When, timezone-aware.
        approved_tier: A new tier, or ``None`` to keep the previous one.
        fallback_tiers: New fallbacks, or ``None`` to keep the previous ones.
        approved_tools: A new tool set, or ``None`` to keep the previous one.
        max_classification: A new ceiling, or ``None`` to keep the previous one.
        token_budget: A new token slice, or ``None`` to keep the previous one.
        money_budget: A new money slice. ``None`` keeps the previous one, which means a money
            budget cannot be *removed* by supersession — only replaced. Removing a ceiling is a
            widening nobody would notice in a diff of two revisions.
        max_turns: A new turn allowance, or ``None`` to keep the previous one.
        estimate: The estimate behind the new budgets, or ``None`` to keep the previous source.
        approval_request_id: The grant that authorised this revision.

    Returns:
        Revision ``previous.revision + 1``, with ``supersedes`` pointing at ``previous.revision``.
    """
    inherited = (
        estimate
        if estimate is not None
        else StepEstimate(
            previous.token_budget,
            money_estimate=previous.money_budget,
            source=previous.budget_source,
            sample_count=previous.budget_sample_count,
        )
    )
    return _mint(
        intent_id=previous.intent_id,
        trajectory_id=previous.trajectory_id,
        step_id=previous.step_id,
        revision=previous.revision + 1,
        supersedes=previous.revision,
        approved_tier=approved_tier if approved_tier is not None else previous.approved_tier,
        fallback_tiers=(fallback_tiers if fallback_tiers is not None else previous.fallback_tiers),
        approved_tools=(
            frozenset(approved_tools) if approved_tools is not None else previous.approved_tools
        ),
        max_classification=(
            max_classification if max_classification is not None else previous.max_classification
        ),
        token_budget=token_budget if token_budget is not None else previous.token_budget,
        money_budget=money_budget if money_budget is not None else previous.money_budget,
        estimate=inherited,
        max_turns=max_turns if max_turns is not None else previous.max_turns,
        minted_by=minted_by,
        minted_at=minted_at,
        approval_request_id=approval_request_id,
        tier_policy=tier_policy,
        policy=policy,
    )


def _mint(
    *,
    intent_id: str,
    trajectory_id: str,
    step_id: str,
    revision: int,
    supersedes: int | None,
    approved_tier: str,
    fallback_tiers: tuple[str, ...],
    approved_tools: frozenset[str],
    max_classification: DataClassification,
    token_budget: int,
    money_budget: Money | None,
    estimate: StepEstimate,
    max_turns: int,
    minted_by: MintedBy,
    minted_at: datetime,
    approval_request_id: str | None,
    tier_policy: TierPolicy,
    policy: ApprovalPolicy,
) -> ExecutionIntent:
    """Resolve the tier set's egress class, evaluate the gates, and construct the intent.

    The one place :class:`ExecutionIntent` is constructed. Every minting path funnels through it,
    so the gate evaluation and the egress resolution cannot be skipped by one of them.
    """
    tiers: Sequence[Tier] = tuple(
        tier_policy.snapshot.require(name) for name in (approved_tier, *fallback_tiers)
    )
    gate = evaluate_gates(
        tiers, classification=max_classification, estimate=estimate, policy=policy
    )
    permitted_egress = max((tier.egress_class for tier in tiers), key=lambda cls: cls.rank)
    return ExecutionIntent(
        intent_id=intent_id,
        trajectory_id=trajectory_id,
        step_id=step_id,
        revision=revision,
        supersedes=supersedes,
        approved_tier=approved_tier,
        fallback_tiers=tuple(fallback_tiers),
        permitted_egress_class=permitted_egress,
        approved_tools=approved_tools,
        max_classification=max_classification,
        token_budget=token_budget,
        money_budget=money_budget,
        budget_source=estimate.source,
        budget_sample_count=estimate.sample_count,
        max_turns=max_turns,
        minted_by=minted_by,
        minted_at=minted_at,
        approval_request_id=approval_request_id,
        gate=gate,
    )


def _require_subset(tools: Set[str], allowlist: Set[str], step_id: str) -> None:
    """Refuse a step declaring a tool the trajectory allowlist does not contain."""
    outside = sorted(tools - allowlist)
    if outside:
        message = (
            f"step {step_id} declares tool(s) {', '.join(outside)} outside the trajectory "
            "allowlist; the allowlist is the caller's, not the model's"
        )
        raise ValidationError(
            message, details={"field": "approved_tools", "step_id": step_id, "tools": outside}
        )


@dataclass(frozen=True, slots=True)
class IntentMinted:
    """``intent.minted`` - written in the same transaction as the transition that caused it.

    ADR-0044: a state change and its event are one write, and minting is part of T3, T4 and T8.
    The body carries the envelope's *shape* — ids, tiers, categories and numbers — and never the
    task, the plan step's description or any model output.
    """

    event_type: ClassVar[EventType] = EventType.INTENT_MINTED
    trajectory_id: str
    intent_id: str
    revision: int
    step_id: str
    approved_tier: str
    fallback_tiers: tuple[str, ...]
    max_classification: DataClassification
    token_budget: int
    max_turns: int
    minted_by: str
    supersedes: int | None = None
    approval_request_id: str | None = None
    gated: bool = False

    @classmethod
    def of(cls, intent: ExecutionIntent) -> IntentMinted:
        """Build the event body announcing one minting.

        Args:
            intent: The intent just minted.

        Returns:
            The body, ready for the same write as its transition.
        """
        return cls(
            trajectory_id=intent.trajectory_id,
            intent_id=intent.intent_id,
            revision=intent.revision,
            step_id=intent.step_id,
            approved_tier=intent.approved_tier,
            fallback_tiers=intent.fallback_tiers,
            max_classification=intent.max_classification,
            token_budget=intent.token_budget,
            max_turns=intent.max_turns,
            minted_by=intent.minted_by.as_recorded(),
            supersedes=intent.supersedes,
            approval_request_id=intent.approval_request_id,
            gated=intent.gate.gated,
        )

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "intent_id": self.intent_id,
            "revision": self.revision,
            "step_id": self.step_id,
            "approved_tier": self.approved_tier,
            "fallback_tiers": list(self.fallback_tiers),
            "max_classification": self.max_classification.value,
            "token_budget": self.token_budget,
            "max_turns": self.max_turns,
            "minted_by": self.minted_by,
            "supersedes": self.supersedes,
            "approval_request_id": self.approval_request_id,
            "gated": self.gated,
        }
