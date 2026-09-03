"""promptcadence.domain.deviation — the closed taxonomy, and the one comparison behind it.

After every turn, one pure function runs identically in both paths:
``compare(turn_facts, intent) -> deviations``. **One source, no branching on mode** — a comparison
that branched on whether a trajectory was planned could not be the evidence that planned and
bypassed trajectories are governed alike; it would be the claim restated (ADR-0056).

**Why the taxonomy is closed.** There is exactly one category per group of intent fields a turn
can contradict, plus one for a promise contradicted after the fact. The set of contradictable
fields is :data:`~promptcadence.domain.intent.GOVERNED_INTENT_FIELDS`, and
:data:`CATEGORY_INTENT_FIELDS` maps each category onto the fields it is about; a test asserts the
two agree exactly and that every named field really is a field of ``ExecutionIntent``. So a new
category requires a new intent field, which is a new governance dimension and its own ADR — never
a schema tweak.

Closure has a second half, and it is the one prose leaves out: **``compare`` must not be able to
see a fact no intent field covers.** :class:`TurnFacts` is therefore a closed shape too, and its
field list is asserted by a test for the same reason. In particular it carries **no trajectory
ceiling**: a trajectory-level ceiling crossing is not a deviation, it is the budget machinery's
own halt or park (lifecycle §5, §6). A deviation is always a statement about one turn against one
intent, and if a ceiling crossing could reach this function the taxonomy would not be closed.

**Severity is not configurable**, so it is a property derived from the category rather than a
field that could disagree with it. A ``violation`` is an unconditional halt and is never
re-approvable; nothing in :func:`disposition` can make one conditional, and a test asserts that
over every scope.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Final

from baseaicore import DataClassification, Money, ProviderKind, ValidationError

from promptcadence.domain.events import EventType
from promptcadence.domain.intent import ExecutionIntent
from promptcadence.domain.policy import ReapprovalScope
from promptcadence.domain.tiers import EgressClass

__all__ = [
    "CATEGORY_INTENT_FIELDS",
    "DISPOSITIONS",
    "SEVERITIES",
    "Deviation",
    "DeviationCategory",
    "DeviationDetected",
    "DeviationSeverity",
    "Disposition",
    "ExecutionSubject",
    "TierServiceFailure",
    "TurnFacts",
    "compare",
    "disposition",
]


class DeviationCategory(StrEnum):
    """The six categories of lifecycle §5, and there cannot be a seventh without a new field."""

    TIER_VIOLATION = "tier_violation"
    TIER_ESCALATION = "tier_escalation"
    CLASSIFICATION_EXCEEDED = "classification_exceeded"
    UNDECLARED_TOOL = "undeclared_tool"
    BUDGET_OVERRUN = "budget_overrun"
    TURN_OVERRUN = "turn_overrun"


class DeviationSeverity(StrEnum):
    """Two severities, not configurable (lifecycle §5).

    ``VIOLATION`` — the executed reality contradicted an already-made promise. Unconditional halt,
    never re-approvable. ``DRIFT`` — the model or the environment wants something the intent does
    not cover; the disposition follows ``reapproval_scope``.
    """

    VIOLATION = "violation"
    DRIFT = "drift"


class Disposition(StrEnum):
    """What the deviation policy does about one deviation."""

    HALT = "halt"
    SCOPED_REAPPROVAL = "scoped_reapproval"
    CONTINUE_RECORDED = "continue_recorded"
    REFUSED_NOT_REAPPROVABLE = "refused_not_reapprovable"


class TierServiceFailure(StrEnum):
    """Why the intent's tiers could not serve the turn (lifecycle §5, spec §13)."""

    NO_ELIGIBLE_MODEL = "no_eligible_model"
    TIER_UNAVAILABLE = "tier_unavailable"


CATEGORY_INTENT_FIELDS: Final[Mapping[DeviationCategory, frozenset[str]]] = {
    DeviationCategory.TIER_VIOLATION: frozenset(
        {"approved_tier", "fallback_tiers", "permitted_egress_class"}
    ),
    DeviationCategory.TIER_ESCALATION: frozenset(
        {"approved_tier", "fallback_tiers", "permitted_egress_class"}
    ),
    DeviationCategory.CLASSIFICATION_EXCEEDED: frozenset({"max_classification"}),
    DeviationCategory.UNDECLARED_TOOL: frozenset({"approved_tools"}),
    DeviationCategory.BUDGET_OVERRUN: frozenset({"token_budget", "money_budget"}),
    DeviationCategory.TURN_OVERRUN: frozenset({"max_turns"}),
}
"""Which intent fields each category is about. The closure argument, made checkable."""

SEVERITIES: Final[Mapping[DeviationCategory, DeviationSeverity]] = {
    DeviationCategory.TIER_VIOLATION: DeviationSeverity.VIOLATION,
    DeviationCategory.TIER_ESCALATION: DeviationSeverity.DRIFT,
    DeviationCategory.CLASSIFICATION_EXCEEDED: DeviationSeverity.DRIFT,
    DeviationCategory.UNDECLARED_TOOL: DeviationSeverity.DRIFT,
    DeviationCategory.BUDGET_OVERRUN: DeviationSeverity.DRIFT,
    DeviationCategory.TURN_OVERRUN: DeviationSeverity.DRIFT,
}
"""Lifecycle §5's severity column. Fixed, not configured."""

_SCOPED = Disposition.SCOPED_REAPPROVAL
_CONTINUE = Disposition.CONTINUE_RECORDED

DISPOSITIONS: Final[Mapping[tuple[DeviationCategory, ReapprovalScope], Disposition]] = {
    (category, scope): dispositions[scope]
    for category, dispositions in {
        DeviationCategory.TIER_VIOLATION: {
            ReapprovalScope.ON_TIER_OR_CLASSIFICATION_CHANGE: Disposition.HALT,
            ReapprovalScope.ANY_DEVIATION: Disposition.HALT,
        },
        DeviationCategory.TIER_ESCALATION: {
            ReapprovalScope.ON_TIER_OR_CLASSIFICATION_CHANGE: _SCOPED,
            ReapprovalScope.ANY_DEVIATION: _SCOPED,
        },
        DeviationCategory.CLASSIFICATION_EXCEEDED: {
            ReapprovalScope.ON_TIER_OR_CLASSIFICATION_CHANGE: _SCOPED,
            ReapprovalScope.ANY_DEVIATION: _SCOPED,
        },
        DeviationCategory.UNDECLARED_TOOL: {
            ReapprovalScope.ON_TIER_OR_CLASSIFICATION_CHANGE: _CONTINUE,
            ReapprovalScope.ANY_DEVIATION: _SCOPED,
        },
        DeviationCategory.BUDGET_OVERRUN: {
            ReapprovalScope.ON_TIER_OR_CLASSIFICATION_CHANGE: _CONTINUE,
            ReapprovalScope.ANY_DEVIATION: _SCOPED,
        },
        DeviationCategory.TURN_OVERRUN: {
            ReapprovalScope.ON_TIER_OR_CLASSIFICATION_CHANGE: _SCOPED,
            ReapprovalScope.ANY_DEVIATION: _SCOPED,
        },
    }.items()
    for scope in ReapprovalScope
}
"""Lifecycle §5's two disposition columns, one entry per category x scope cell.

The one refinement the table states in prose rather than in a column is handled by
:func:`disposition`: an ``undeclared_tool`` naming a tool outside the **trajectory** allowlist is
:attr:`Disposition.REFUSED_NOT_REAPPROVABLE` under either scope, because the allowlist is the
caller's, not the model's.
"""


@dataclass(frozen=True, slots=True)
class ExecutionSubject:
    """Who actually answered the turn — verified against the tier, never assumed.

    Spec §11 contract 4: every LoadCoach response names its execution subject, and PromptCadence
    checks it against the tier that requested it. ``egress_class`` is the verified answer to "did
    this leave the machine?", and it is a **resolved** value rather than something this module
    derives from ``provider_kind``: :class:`baseaicore.ProviderKind` names a runtime
    (``ollama``, ``openai_compatible``, ``llamacpp``, ``vllm``, ``fake``), and
    ``openai_compatible`` covers both a local llama.cpp server and a paid remote endpoint. Kind
    alone therefore cannot answer the egress question, and guessing from it would be the
    "assumed, not verified" failure the contract exists to prevent.

    Resolving it is the fact builder's job at the HTTP boundary (Phase 3): LoadCoach 1.0 serves
    exactly one configured provider, so verifying that the response's provider is the configured
    one *is* the verification, and LC-E1's multi-provider registration is what will make the
    response's provider name the input. See ``C4_HANDOFF.md`` for the amendment this proposes to
    spec §11 contract 4.

    Attributes:
        model_canonical_id: ``provider/name@sha256:digest``, verbatim from the response.
        provider_kind: The runtime that served it.
        egress_class: Whether serving it left this machine.
        provider_name: The registered provider's name once LC-E1 lands; ``None`` before then.
    """

    model_canonical_id: str
    provider_kind: ProviderKind
    egress_class: EgressClass
    provider_name: str | None = None

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form recorded on a deviation and in goldens."""
        return {
            "model_canonical_id": self.model_canonical_id,
            "provider_kind": self.provider_kind.value,
            "egress_class": self.egress_class.value,
            "provider_name": self.provider_name,
        }


@dataclass(frozen=True, slots=True)
class TurnFacts:
    """What one turn actually did — the other half of the closed taxonomy.

    Every field here can contradict exactly one group of
    :data:`~promptcadence.domain.intent.GOVERNED_INTENT_FIELDS`, plus the identifiers that say
    which turn this is. **Nothing else may be added without a matching intent field**, and a test
    asserts the field list, because a fact with no field to contradict is a deviation category
    with no disposition — which is precisely the open taxonomy ADR-0056 rejected.

    What is deliberately **absent**: any trajectory-level ceiling, balance or headroom. Crossing a
    trajectory, per-day or per-project ceiling is the budget machinery's halt or park (lifecycle
    §6), not a deviation. If one could be passed here, ``compare`` would either ignore it or grow a
    seventh category.

    Built by Phase 3 from a LoadCoach response. Nothing in this type is HTTP-shaped, so building
    one requires no client, no envelope and no framework — which is what lets the deviation tests
    run with no fake LoadCoach at all.

    Attributes:
        turn_id: The turn these facts are about.
        executed_tier: The tier the turn was dispatched on, or ``None`` when nothing executed
            because the intent's tiers could not serve.
        subject: Who answered, or ``None`` when nothing executed.
        tier_service_failure: Why the intent's tiers could not serve, when they could not. Set
            exactly when ``executed_tier`` is ``None``.
        requested_tools: The tools the model asked for this turn, in request order.
        trajectory_allowlist: The caller's allowlist, which splits an undeclared tool into a drift
            the application handles and a refusal that is never re-approvable. It is a fact about
            the turn's context, not an intent field, and it refines a disposition rather than
            creating a category.
        observed_classification: The classification of what came back, after any tool result
            raised it (lifecycle §5, operator-flagged paths).
        step_tokens_spent: Tokens spent under this intent so far, including this turn.
        step_money_spent: Money spent under this intent so far, or ``None`` for unpriced local
            work whose cost is ``UNSUPPORTED``, never ``$0.00``.
        step_money_is_floor: Whether ``step_money_spent`` is a floor (ADR-0069). ``compare`` fires
            ``budget_overrun`` only on a strict excess, which stays sound over a floor: "exceeded"
            is certain even when "under budget" is not.
        turns_used: Turns executed under this intent, including this one.
        finish_declared: Whether the provider declared a finish. A model never decides control
            flow: this is a declared ``finish_reason`` or a schema-validated result, never the
            text saying it is done.

    Raises:
        ValidationError: If ``executed_tier`` and ``tier_service_failure`` disagree about whether
            anything executed, or a count is negative.
    """

    turn_id: str
    executed_tier: str | None
    subject: ExecutionSubject | None
    observed_classification: DataClassification
    turns_used: int
    step_tokens_spent: int
    requested_tools: tuple[str, ...] = field(default=(), kw_only=True)
    trajectory_allowlist: frozenset[str] = field(default=frozenset(), kw_only=True)
    tier_service_failure: TierServiceFailure | None = field(default=None, kw_only=True)
    step_money_spent: Money | None = field(default=None, kw_only=True)
    step_money_is_floor: bool = field(default=False, kw_only=True)
    finish_declared: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        """Refuse facts that cannot describe one real turn."""
        if not self.turn_id.strip():
            message = "turn_id must not be empty"
            raise ValidationError(message, details={"field": "turn_id"})
        executed = self.executed_tier is not None
        if executed and self.tier_service_failure is not None:
            message = (
                f"turn {self.turn_id} names both an executed tier and a tier service failure; "
                "either it ran or it could not be served"
            )
            raise ValidationError(message, details={"field": "tier_service_failure"})
        if not executed and self.tier_service_failure is None:
            message = (
                f"turn {self.turn_id} names no executed tier and no service failure; a turn that "
                "did not run must say why"
            )
            raise ValidationError(message, details={"field": "tier_service_failure"})
        if executed and self.subject is None:
            message = (
                f"turn {self.turn_id} ran on {self.executed_tier} but names no execution "
                "subject; the subject is verified, never assumed (spec §11 contract 4)"
            )
            raise ValidationError(message, details={"field": "subject"})
        if self.turns_used < 1:
            message = f"turns_used counts this turn, so it starts at 1; got {self.turns_used}"
            raise ValidationError(message, details={"field": "turns_used"})
        if self.step_tokens_spent < 0:
            message = "step_tokens_spent must not be negative"
            raise ValidationError(message, details={"field": "step_tokens_spent"})


@dataclass(frozen=True, slots=True)
class Deviation:
    """One category-typed statement about one turn against one intent.

    ``severity`` is a property, not a field: lifecycle §5 says severity is not configurable, and a
    field could be set to disagree with its category. Every optional attribute below is the
    evidence for exactly one category; the others stay ``None`` so a golden shows what the
    comparison actually saw.

    Attributes:
        category: Which of the six.
        intent_id: The envelope contradicted.
        intent_revision: Which revision of it.
        turn_id: The turn.
        tools: The tools implicated, for ``undeclared_tool``.
        outside_trajectory_allowlist: Whether those tools are outside the caller's allowlist, which
            makes the refusal permanent rather than re-approvable.
        executed_tier: For the tier categories, the tier that ran (or ``None`` when none did).
        permitted_tiers: What the intent permitted.
        subject: Who answered, for a ``tier_violation``.
        service_failure: Why no tier could serve, for a ``tier_escalation``.
        observed_classification: What came back, for ``classification_exceeded``.
        permitted_classification: The intent's ceiling.
        tokens_spent: For ``budget_overrun``.
        token_budget: The intent's token slice.
        money_spent: For ``budget_overrun``, when priced.
        money_budget: The intent's money slice.
        money_is_floor: Whether the money figure is a floor; rendered "at least", never bare.
        turns_used: For ``turn_overrun``.
        max_turns: The intent's allowance.
    """

    category: DeviationCategory
    intent_id: str
    intent_revision: int
    turn_id: str
    tools: tuple[str, ...] = field(default=(), kw_only=True)
    outside_trajectory_allowlist: bool = field(default=False, kw_only=True)
    executed_tier: str | None = field(default=None, kw_only=True)
    permitted_tiers: tuple[str, ...] = field(default=(), kw_only=True)
    subject: ExecutionSubject | None = field(default=None, kw_only=True)
    service_failure: TierServiceFailure | None = field(default=None, kw_only=True)
    observed_classification: DataClassification | None = field(default=None, kw_only=True)
    permitted_classification: DataClassification | None = field(default=None, kw_only=True)
    tokens_spent: int | None = field(default=None, kw_only=True)
    token_budget: int | None = field(default=None, kw_only=True)
    money_spent: Money | None = field(default=None, kw_only=True)
    money_budget: Money | None = field(default=None, kw_only=True)
    money_is_floor: bool = field(default=False, kw_only=True)
    turns_used: int | None = field(default=None, kw_only=True)
    max_turns: int | None = field(default=None, kw_only=True)

    @property
    def severity(self) -> DeviationSeverity:
        """The severity of this deviation's category. Derived, so it cannot disagree with it."""
        return SEVERITIES[self.category]

    @property
    def is_reapprovable(self) -> bool:
        """Whether a scoped re-approval could ever grant this.

        ``False`` for every violation — a contradicted promise is never re-approvable — and for a
        tool outside the trajectory allowlist, because that allowlist is the caller's.
        """
        if self.severity is DeviationSeverity.VIOLATION:
            return False
        return not self.outside_trajectory_allowlist

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form persisted in ``deviations`` and used in goldens."""
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "intent_id": self.intent_id,
            "intent_revision": self.intent_revision,
            "turn_id": self.turn_id,
            "tools": list(self.tools),
            "outside_trajectory_allowlist": self.outside_trajectory_allowlist,
            "executed_tier": self.executed_tier,
            "permitted_tiers": list(self.permitted_tiers),
            "subject": self.subject.as_canonical() if self.subject is not None else None,
            "service_failure": (
                self.service_failure.value if self.service_failure is not None else None
            ),
            "observed_classification": (
                self.observed_classification.value
                if self.observed_classification is not None
                else None
            ),
            "permitted_classification": (
                self.permitted_classification.value
                if self.permitted_classification is not None
                else None
            ),
            "tokens_spent": self.tokens_spent,
            "token_budget": self.token_budget,
            "money_spent": (
                self.money_spent.as_canonical() if self.money_spent is not None else None
            ),
            "money_budget": (
                self.money_budget.as_canonical() if self.money_budget is not None else None
            ),
            "money_is_floor": self.money_is_floor,
            "turns_used": self.turns_used,
            "max_turns": self.max_turns,
        }


def disposition(deviation: Deviation, *, scope: ReapprovalScope) -> Disposition:
    """What the deviation policy does about one deviation.

    Args:
        deviation: The deviation.
        scope: ``planning.reapproval_scope``.

    Returns:
        The cell of lifecycle §5's table for this category and scope, with the one refinement the
        table states in prose: an ``undeclared_tool`` naming a tool outside the **trajectory**
        allowlist is refused outright and is never re-approvable, under either scope. A
        ``violation`` is :attr:`Disposition.HALT` under every scope, unconditionally — nothing here
        can make one conditional.
    """
    if deviation.severity is DeviationSeverity.VIOLATION:
        return Disposition.HALT
    if (
        deviation.category is DeviationCategory.UNDECLARED_TOOL
        and deviation.outside_trajectory_allowlist
    ):
        return Disposition.REFUSED_NOT_REAPPROVABLE
    return DISPOSITIONS[(deviation.category, scope)]


def compare(turn_facts: TurnFacts, intent: ExecutionIntent) -> tuple[Deviation, ...]:
    """Compare what one turn did against the envelope it ran under.

    Pure, deterministic, and **identical in both paths** — there is no "plan-declared vs
    default-policy" branch, which is the collapse ADR-0056 bought. The returned deviations are in
    :class:`DeviationCategory` declaration order, so a golden is a golden.

    Args:
        turn_facts: What the turn actually did.
        intent: The envelope it ran under.

    Returns:
        Every deviation found; empty when the turn stayed inside its envelope. A turn can produce
        several — a model that escalated *and* asked for an undeclared tool contradicts two
        fields, and reporting only the first would hide one from the record.
    """
    found: list[Deviation] = []
    _tier_violation(turn_facts, intent, found)
    _tier_escalation(turn_facts, intent, found)
    _classification_exceeded(turn_facts, intent, found)
    _undeclared_tools(turn_facts, intent, found)
    _budget_overrun(turn_facts, intent, found)
    _turn_overrun(turn_facts, intent, found)
    return tuple(found)


def _tier_violation(facts: TurnFacts, intent: ExecutionIntent, found: list[Deviation]) -> None:
    """Record the two ways an executed turn contradicts a promise already made about its tier."""
    if facts.executed_tier is None or facts.subject is None:
        return
    outside_intent = facts.executed_tier not in intent.permitted_tiers
    escaped_egress = facts.subject.egress_class > intent.permitted_egress_class
    if not (outside_intent or escaped_egress):
        return
    found.append(
        Deviation(
            category=DeviationCategory.TIER_VIOLATION,
            intent_id=intent.intent_id,
            intent_revision=intent.revision,
            turn_id=facts.turn_id,
            executed_tier=facts.executed_tier,
            permitted_tiers=intent.permitted_tiers,
            subject=facts.subject,
        )
    )


def _tier_escalation(facts: TurnFacts, intent: ExecutionIntent, found: list[Deviation]) -> None:
    """Record that the intent's tiers could not serve, so the next tier is outside the envelope."""
    if facts.tier_service_failure is None:
        return
    found.append(
        Deviation(
            category=DeviationCategory.TIER_ESCALATION,
            intent_id=intent.intent_id,
            intent_revision=intent.revision,
            turn_id=facts.turn_id,
            executed_tier=None,
            permitted_tiers=intent.permitted_tiers,
            service_failure=facts.tier_service_failure,
        )
    )


def _classification_exceeded(
    facts: TurnFacts, intent: ExecutionIntent, found: list[Deviation]
) -> None:
    """Record data above the intent's ceiling arriving in the turn."""
    if facts.observed_classification <= intent.max_classification:
        return
    found.append(
        Deviation(
            category=DeviationCategory.CLASSIFICATION_EXCEEDED,
            intent_id=intent.intent_id,
            intent_revision=intent.revision,
            turn_id=facts.turn_id,
            observed_classification=facts.observed_classification,
            permitted_classification=intent.max_classification,
        )
    )


def _undeclared_tools(facts: TurnFacts, intent: ExecutionIntent, found: list[Deviation]) -> None:
    """Record tools outside the intent, split by whether the caller's allowlist covers them.

    Two deviations at most, and the outside-the-allowlist one is emitted first: telling a model
    that its call merely needs approval, when no approval could ever grant it, invites a
    re-approval that can only fail (ToolYard's ``not_allowlisted`` before ``not_in_intent``).
    """
    undeclared = _ordered_unique(
        tool for tool in facts.requested_tools if not intent.permits_tool(tool)
    )
    if not undeclared:
        return
    outside = tuple(t for t in undeclared if t not in facts.trajectory_allowlist)
    inside = tuple(t for t in undeclared if t in facts.trajectory_allowlist)
    for tools, is_outside in ((outside, True), (inside, False)):
        if tools:
            found.append(
                Deviation(
                    category=DeviationCategory.UNDECLARED_TOOL,
                    intent_id=intent.intent_id,
                    intent_revision=intent.revision,
                    turn_id=facts.turn_id,
                    tools=tools,
                    outside_trajectory_allowlist=is_outside,
                )
            )


def _budget_overrun(facts: TurnFacts, intent: ExecutionIntent, found: list[Deviation]) -> None:
    """Record spend past the step's own slice. Strictly greater: spending the slice is not past it.

    Sound over a floor: a floor that already exceeds the budget certainly exceeds it, so a
    ``True`` here is never wrong. A ``False`` is not a claim that the step is under budget, which
    is why ``money_is_floor`` travels onto the deviation and into the record.
    """
    over_tokens = facts.step_tokens_spent > intent.token_budget
    over_money = (
        facts.step_money_spent is not None
        and intent.money_budget is not None
        and facts.step_money_spent > intent.money_budget
    )
    if not (over_tokens or over_money):
        return
    found.append(
        Deviation(
            category=DeviationCategory.BUDGET_OVERRUN,
            intent_id=intent.intent_id,
            intent_revision=intent.revision,
            turn_id=facts.turn_id,
            tokens_spent=facts.step_tokens_spent,
            token_budget=intent.token_budget,
            money_spent=facts.step_money_spent,
            money_budget=intent.money_budget,
            money_is_floor=facts.step_money_is_floor,
        )
    )


def _turn_overrun(facts: TurnFacts, intent: ExecutionIntent, found: list[Deviation]) -> None:
    """Record a step that reached ``max_turns`` without a declared finish."""
    if facts.finish_declared or facts.turns_used < intent.max_turns:
        return
    found.append(
        Deviation(
            category=DeviationCategory.TURN_OVERRUN,
            intent_id=intent.intent_id,
            intent_revision=intent.revision,
            turn_id=facts.turn_id,
            turns_used=facts.turns_used,
            max_turns=intent.max_turns,
        )
    )


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    """Return the values in first-seen order with duplicates removed."""
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True, slots=True)
class DeviationDetected:
    """``deviation.detected`` - emitted for every deviation, including a silently-continued drift.

    Lifecycle §5: every deviation is an event *and* a row. A drift the policy continues past is
    still recorded, because a governance record that only holds the deviations that stopped
    something answers the wrong question.
    """

    event_type: ClassVar[EventType] = EventType.DEVIATION_DETECTED
    trajectory_id: str
    turn_id: str
    intent_id: str
    intent_revision: int
    category: DeviationCategory
    severity: DeviationSeverity
    disposition: Disposition
    reapprovable: bool

    @classmethod
    def of(
        cls, deviation: Deviation, *, trajectory_id: str, scope: ReapprovalScope
    ) -> DeviationDetected:
        """Build the event body for one deviation under one re-approval scope.

        Args:
            deviation: The deviation.
            trajectory_id: The trajectory it happened in.
            scope: ``planning.reapproval_scope``, which decides the disposition.

        Returns:
            The body, carrying the category, the severity and what was done about it.
        """
        return cls(
            trajectory_id=trajectory_id,
            turn_id=deviation.turn_id,
            intent_id=deviation.intent_id,
            intent_revision=deviation.intent_revision,
            category=deviation.category,
            severity=deviation.severity,
            disposition=disposition(deviation, scope=scope),
            reapprovable=deviation.is_reapprovable,
        )

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "turn_id": self.turn_id,
            "intent_id": self.intent_id,
            "intent_revision": self.intent_revision,
            "category": self.category.value,
            "severity": self.severity.value,
            "disposition": self.disposition.value,
            "reapprovable": self.reapprovable,
        }
