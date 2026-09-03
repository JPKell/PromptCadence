"""promptcadence.domain.plan — the plan, its committed schema, and the five rules of §4.1.

The planner asks LoadCoach for JSON and PromptCadence validates the answer against **its own**
schema: LoadCoach never applies a caller's schema (ADR-0041), so validation and its bounded
corrective retry belong here, exactly as IdeaPress does it.

:data:`PLAN_SCHEMA` is the committed contract — the same document is written beside this module as
``plan.schema.json`` and a golden test asserts the two are byte-identical, so the file a prompt
ships and the rules this module enforces cannot drift apart. Validation itself is written in
Python rather than executed from the schema: this phase adds no runtime dependency, and — more to
the point — a generic validator's "does not match schema at ``$.steps[2].tier``" is precisely the
message that makes P7's corrective retry loop. **The phase's named risk is a schema too strict for
a local planner, and the mitigation available here is legibility**: every issue names the step and
the field, and every issue found is reported together, because a retry budget of two attempts is
spent immediately by a validator that reports one problem at a time.

The verbatim document is not optional and not a convenience. Lifecycle §4.1 requires the plan to
be persisted verbatim alongside its validated form, so :class:`Plan` carries the source text and
refuses to exist if the digest it holds is not the digest of that text. What the model proposed
and what execution is held to are two facts, and both survive.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from baseaicore import DataClassification, ValidationError, canonical_json, sha256_of

from promptcadence.domain.errors import PlanInvalidError

__all__ = [
    "PLAN_SCHEMA",
    "PLAN_SCHEMA_ID",
    "Plan",
    "PlanIssue",
    "PlanIssueReason",
    "PlanStep",
    "schema_document",
    "validate_plan_document",
]

PLAN_SCHEMA_ID: Final = "https://promptcadence.local/schemas/plan/1.0.json"

PLAN_SCHEMA: Final[Mapping[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": PLAN_SCHEMA_ID,
    "title": "PromptCadence plan",
    "description": (
        "A plan of steps over a DAG. Tools, tier and data_classification are declarations that "
        "approval turns into one immutable ExecutionIntent per step; description and "
        "expected_turns are advisory."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": ["steps"],
    "properties": {
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "step_id",
                    "description",
                    "depends_on",
                    "tools",
                    "tier",
                    "data_classification",
                    "expected_turns",
                ],
                "properties": {
                    "step_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "description": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 64},
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 64},
                    },
                    "tier": {"type": "string", "minLength": 1, "maxLength": 60},
                    "data_classification": {
                        "type": "string",
                        "enum": ["public", "internal", "confidential"],
                    },
                    "expected_turns": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        }
    },
}
"""The committed plan schema. Mirrored byte-identically by ``plan.schema.json`` beside it."""

_STEP_FIELDS: Final[frozenset[str]] = frozenset(
    PLAN_SCHEMA["properties"]["steps"]["items"]["required"]
)
_MAX_STEP_ID_LENGTH: Final = 64
_MAX_EXPECTED_TURNS: Final = 100


class PlanIssueReason(StrEnum):
    """Why one part of a plan document was refused. Closed, so P7 can branch on it."""

    NOT_AN_OBJECT = "not_an_object"
    NOT_JSON = "not_json"
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    WRONG_TYPE = "wrong_type"
    EMPTY_PLAN = "empty_plan"
    TOO_MANY_STEPS = "too_many_steps"
    STEP_ID_INVALID = "step_id_invalid"
    DUPLICATE_STEP_ID = "duplicate_step_id"
    SELF_DEPENDENCY = "self_dependency"
    UNKNOWN_DEPENDENCY = "unknown_dependency"
    DEPENDENCY_CYCLE = "dependency_cycle"
    TOOL_NOT_ALLOWLISTED = "tool_not_allowlisted"
    TIER_NOT_CONFIGURED = "tier_not_configured"
    CLASSIFICATION_UNKNOWN = "classification_unknown"
    CLASSIFICATION_LAUNDERING = "classification_laundering"
    EXPECTED_TURNS_INVALID = "expected_turns_invalid"


@dataclass(frozen=True, slots=True)
class PlanIssue:
    """One reason a plan document was refused, named precisely enough to be fixable.

    Attributes:
        reason: The closed category.
        field_name: Which field failed, dotted from the step (``"tier"``, ``"depends_on"``) or
            from the document (``"steps"``).
        step_id: The step it failed in, or ``None`` for a document-level issue. Present whenever
            the document was well-formed enough to name one — an issue P7 cannot locate is an
            issue P7 cannot correct.
        message: A human sentence naming the step and the field. It never quotes a step
            description: that is model output, and issues travel into ``details``, logs and API
            envelopes.
    """

    reason: PlanIssueReason
    field_name: str
    step_id: str | None
    message: str

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form carried in ``PlanInvalidError.details`` and in goldens."""
        return {
            "reason": self.reason.value,
            "field": self.field_name,
            "step_id": self.step_id,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One step of a validated plan.

    ``tools``, ``tier`` and ``data_classification`` are **declarations**: approval turns each into
    one immutable :class:`~promptcadence.domain.intent.ExecutionIntent`, and that intent — not this
    step — is what a turn is held to. ``description`` and ``expected_turns`` are advisory; they
    inform estimates and the explanation, and nothing gates on them.

    Attributes:
        step_id: Unique within the plan.
        description: The model's own words. Advisory, and never copied into an event body.
        depends_on: Steps that must commit first. Together these form the DAG.
        tools: The tools this step declares it needs, a subset of the trajectory allowlist.
        tier: The tier it declares, which must be configured.
        data_classification: The step's declared classification, at or below the trajectory's.
        expected_turns: The planner's estimate of tool round trips. Advisory.
    """

    step_id: str
    description: str
    depends_on: tuple[str, ...]
    tools: tuple[str, ...]
    tier: str
    data_classification: DataClassification
    expected_turns: int

    def as_canonical(self) -> dict[str, Any]:
        """Return the validated mapping form, used in goldens and in the explanation."""
        return {
            "step_id": self.step_id,
            "description": self.description,
            "depends_on": list(self.depends_on),
            "tools": list(self.tools),
            "tier": self.tier,
            "data_classification": self.data_classification.value,
            "expected_turns": self.expected_turns,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """A validated plan, inseparable from the document it was validated from.

    ``raw_document`` and ``document_sha256`` are fields rather than store columns because
    lifecycle §4.1's "persisted verbatim alongside its validated form" is only true if the two
    cannot be separated. ``__post_init__`` recomputes the digest, so a plan whose validated form
    does not belong to its source text cannot exist — construction is safe, and constructing one
    by hand is merely pointless rather than dangerous.

    Attributes:
        steps: The validated steps, in document order.
        raw_document: The planner's answer, verbatim.
        document_sha256: The digest of ``raw_document``, recomputed at construction.

    Raises:
        ValidationError: If the plan is empty, a step id repeats, or the digest does not match the
            document. Emptiness is refused here as well as in
            :func:`validate_plan_document` on purpose: emptiness cannot pass a gate (ADR-0042, the
            IdeaPress M7 lesson), and a `Plan` assembled by a later phase must not be the way in.
    """

    steps: tuple[PlanStep, ...]
    raw_document: str
    document_sha256: str

    def __post_init__(self) -> None:
        """Refuse an empty plan, a duplicate step id or a digest that is not this document's."""
        if not self.steps:
            message = "a plan with no steps is invalid; emptiness cannot pass a gate"
            raise ValidationError(message, details={"field": "steps"})
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            message = "a plan cannot hold two steps with the same step_id"
            raise ValidationError(message, details={"field": "steps"})
        expected = sha256_of(self.raw_document)
        if self.document_sha256 != expected:
            message = (
                "document_sha256 is not the digest of raw_document; a validated plan must belong "
                "to the document it was validated from"
            )
            raise ValidationError(message, details={"field": "document_sha256"})

    @property
    def step_ids(self) -> tuple[str, ...]:
        """The step ids, in document order."""
        return tuple(step.step_id for step in self.steps)

    def step(self, step_id: str) -> PlanStep:
        """Return the named step.

        Args:
            step_id: The step's id.

        Returns:
            The step.

        Raises:
            ValidationError: If the plan has no such step.
        """
        for candidate in self.steps:
            if candidate.step_id == step_id:
                return candidate
        message = f"plan has no step {step_id!r}"
        raise ValidationError(message, details={"field": "step_id", "step_id": step_id})

    def ready_steps(self, committed: Set[str]) -> tuple[PlanStep, ...]:
        """Return the steps whose dependencies have all committed and which have not themselves.

        The ready set the ``LoopController`` dispatches from (lifecycle §8.4). Dispatch policy —
        ``max_concurrent_steps``, at most one local step in flight — is the controller's, not this
        function's: here the DAG is only asked what *could* run.

        Args:
            committed: The ids of steps that have committed.

        Returns:
            The ready steps, in document order, so serial dispatch is deterministic.
        """
        return tuple(
            step
            for step in self.steps
            if step.step_id not in committed and set(step.depends_on) <= set(committed)
        )

    def topological_order(self) -> tuple[str, ...]:
        """Return the step ids in a dependency-respecting order, ties broken by document order.

        Returns:
            Every step id exactly once, each after all of its dependencies. Validation has already
            proved the graph is acyclic, so this always terminates.
        """
        committed: set[str] = set()
        order: list[str] = []
        while len(order) < len(self.steps):
            ready = self.ready_steps(committed)
            for step in ready:
                order.append(step.step_id)
                committed.add(step.step_id)
        return tuple(order)

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form used in goldens and in the explanation document."""
        return {
            "steps": [step.as_canonical() for step in self.steps],
            "document_sha256": self.document_sha256,
        }


def validate_plan_document(
    raw_document: str,
    *,
    trajectory_allowlist: Set[str],
    trajectory_classification: DataClassification,
    configured_tiers: Set[str],
    max_plan_steps: int,
) -> Plan:
    """Validate a planner's answer against the plan schema and lifecycle §4.1's five rules.

    Every issue found is collected before anything is raised. P7's corrective retry has a bounded
    budget (default 2), and a validator that surfaces one problem per attempt spends that budget
    on bookkeeping rather than on correction.

    The five rules, each enforced rather than assumed:

    * ``depends_on`` forms a DAG; a cycle is refused.
    * A step's declared classification may not exceed the trajectory's — **a plan cannot launder
      confidential data into an ``internal`` step**, which is the rule with real consequences: an
      unchecked step classification would let a plan route confidential work onto a remote tier
      that admits ``internal``.
    * Declared tools exist in the trajectory allowlist, and declared tiers are configured.
    * An empty plan is invalid.
    * The document is retained verbatim on the returned :class:`Plan`.

    Args:
        raw_document: The planner's answer, verbatim, as JSON text.
        trajectory_allowlist: The tools the caller permitted for this trajectory. The allowlist is
            the caller's, not the model's, so a tool outside it is refused rather than approved.
        trajectory_classification: What the caller declared the trajectory to be.
        configured_tiers: The tier names in the trajectory's tier snapshot.
        max_plan_steps: ``planning.max_plan_steps``.

    Returns:
        The validated :class:`Plan`, carrying the verbatim document and its digest.

    Raises:
        PlanInvalidError: If the document is not JSON, not an object, empty, over the step limit,
            or breaks any rule above. ``details["issues"]`` lists every issue as a mapping;
            ``details["issue_count"]`` counts them. No step description appears in ``details``.
    """
    issues: list[PlanIssue] = []
    parsed = _parse_document(raw_document, issues)
    if parsed is None:
        raise _refuse(issues)

    raw_steps = _extract_steps(parsed, issues, max_plan_steps=max_plan_steps)
    if raw_steps is None:
        raise _refuse(issues)

    steps = tuple(
        step
        for index, raw_step in enumerate(raw_steps)
        if (step := _validate_step(raw_step, index, issues)) is not None
    )
    _validate_step_ids(steps, issues)
    _validate_declarations(
        steps,
        issues,
        trajectory_allowlist=trajectory_allowlist,
        trajectory_classification=trajectory_classification,
        configured_tiers=configured_tiers,
    )
    _validate_dag(steps, issues)

    if issues:
        raise _refuse(issues)
    return Plan(steps=steps, raw_document=raw_document, document_sha256=sha256_of(raw_document))


def _refuse(issues: Sequence[PlanIssue]) -> PlanInvalidError:
    """Build the refusal carrying every issue found, in the order they were found."""
    summary = "; ".join(issue.message for issue in issues[:5])
    if len(issues) > 5:
        summary += f"; and {len(issues) - 5} more"
    message = f"plan is invalid: {summary}"
    return PlanInvalidError(
        message,
        details={
            "issues": [issue.as_canonical() for issue in issues],
            "issue_count": len(issues),
        },
    )


def _parse_document(raw_document: str, issues: list[PlanIssue]) -> dict[str, Any] | None:
    """Parse the document, recording an issue and returning ``None`` if it is not an object."""
    try:
        parsed = json.loads(raw_document)
    except json.JSONDecodeError as exc:
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.NOT_JSON,
                field_name="$",
                step_id=None,
                message=f"the plan document is not valid JSON (line {exc.lineno})",
            )
        )
        return None
    if not isinstance(parsed, dict):
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.NOT_AN_OBJECT,
                field_name="$",
                step_id=None,
                message=f"the plan document must be a JSON object, got {type(parsed).__name__}",
            )
        )
        return None
    for key in parsed:
        if key != "steps":
            issues.append(
                PlanIssue(
                    reason=PlanIssueReason.UNKNOWN_FIELD,
                    field_name=str(key),
                    step_id=None,
                    message=f"the plan document has no field {key!r}",
                )
            )
    return parsed


def _extract_steps(
    parsed: Mapping[str, Any], issues: list[PlanIssue], *, max_plan_steps: int
) -> list[Any] | None:
    """Return the raw step list, or ``None`` after recording why it cannot be used."""
    if "steps" not in parsed:
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.MISSING_FIELD,
                field_name="steps",
                step_id=None,
                message="the plan document has no 'steps' field",
            )
        )
        return None
    raw_steps = parsed["steps"]
    if not isinstance(raw_steps, list):
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.WRONG_TYPE,
                field_name="steps",
                step_id=None,
                message=f"'steps' must be an array, got {type(raw_steps).__name__}",
            )
        )
        return None
    if not raw_steps:
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.EMPTY_PLAN,
                field_name="steps",
                step_id=None,
                message="a plan with no steps is invalid; emptiness cannot pass a gate",
            )
        )
        return None
    if len(raw_steps) > max_plan_steps:
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.TOO_MANY_STEPS,
                field_name="steps",
                step_id=None,
                message=(
                    f"the plan has {len(raw_steps)} steps; planning.max_plan_steps is "
                    f"{max_plan_steps}"
                ),
            )
        )
        return None
    return raw_steps


def _validate_step(  # noqa: C901 — one branch per schema field; splitting it hides the shape
    raw_step: Any, index: int, issues: list[PlanIssue]
) -> PlanStep | None:
    """Validate one step's shape, recording every issue and returning ``None`` if unusable."""
    position = f"steps[{index}]"
    if not isinstance(raw_step, dict):
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.WRONG_TYPE,
                field_name=position,
                step_id=None,
                message=f"{position} must be an object, got {type(raw_step).__name__}",
            )
        )
        return None

    raw_id = raw_step.get("step_id")
    step_id = raw_id if isinstance(raw_id, str) else None
    label = step_id or position
    before = len(issues)

    for name in sorted(_STEP_FIELDS - set(raw_step)):
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.MISSING_FIELD,
                field_name=name,
                step_id=step_id,
                message=f"step {label} has no {name!r} field",
            )
        )
    for name in sorted(set(raw_step) - _STEP_FIELDS):
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.UNKNOWN_FIELD,
                field_name=name,
                step_id=step_id,
                message=f"step {label} has an unknown field {name!r}",
            )
        )
    if step_id is not None and not (0 < len(step_id) <= _MAX_STEP_ID_LENGTH):
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.STEP_ID_INVALID,
                field_name="step_id",
                step_id=None,
                message=(
                    f"{position} has a step_id of {len(step_id)} characters; "
                    f"1 to {_MAX_STEP_ID_LENGTH} are allowed"
                ),
            )
        )
    if "step_id" in raw_step and step_id is None:
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.WRONG_TYPE,
                field_name="step_id",
                step_id=None,
                message=f"{position} has a non-string step_id",
            )
        )

    description = _string(raw_step, "description", label, step_id, issues)
    depends_on = _string_list(raw_step, "depends_on", label, step_id, issues)
    tools = _string_list(raw_step, "tools", label, step_id, issues)
    tier = _string(raw_step, "tier", label, step_id, issues)
    classification = _classification(raw_step, label, step_id, issues)
    expected_turns = _expected_turns(raw_step, label, step_id, issues)

    if len(issues) != before:
        return None
    assert step_id is not None  # noqa: S101 — a missing step_id was recorded above
    assert description is not None and tier is not None  # noqa: S101 — same
    assert depends_on is not None and tools is not None  # noqa: S101 — same
    assert classification is not None and expected_turns is not None  # noqa: S101 — same
    return PlanStep(
        step_id=step_id,
        description=description,
        depends_on=tuple(depends_on),
        tools=tuple(tools),
        tier=tier,
        data_classification=classification,
        expected_turns=expected_turns,
    )


def _string(
    raw_step: Mapping[str, Any],
    name: str,
    label: str,
    step_id: str | None,
    issues: list[PlanIssue],
) -> str | None:
    """Return a required non-empty string field, recording an issue when it is not one."""
    value = raw_step.get(name)
    if isinstance(value, str) and value:
        return value
    if name in raw_step:
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.WRONG_TYPE,
                field_name=name,
                step_id=step_id,
                message=f"step {label} field {name!r} must be a non-empty string",
            )
        )
    return None


def _string_list(
    raw_step: Mapping[str, Any],
    name: str,
    label: str,
    step_id: str | None,
    issues: list[PlanIssue],
) -> list[str] | None:
    """Return a required array-of-strings field, recording an issue when it is not one."""
    value = raw_step.get(name)
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return list(value)
    if name in raw_step:
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.WRONG_TYPE,
                field_name=name,
                step_id=step_id,
                message=f"step {label} field {name!r} must be an array of non-empty strings",
            )
        )
    return None


def _classification(
    raw_step: Mapping[str, Any], label: str, step_id: str | None, issues: list[PlanIssue]
) -> DataClassification | None:
    """Return the step's declared classification, recording an issue for an unknown level."""
    if "data_classification" not in raw_step:
        return None
    value = raw_step["data_classification"]
    try:
        return DataClassification(str(value))
    except ValueError:
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.CLASSIFICATION_UNKNOWN,
                field_name="data_classification",
                step_id=step_id,
                message=(
                    f"step {label} declares data_classification {value!r}; the levels are "
                    "'public', 'internal' and 'confidential' (ADR-0046)"
                ),
            )
        )
        return None


def _expected_turns(
    raw_step: Mapping[str, Any], label: str, step_id: str | None, issues: list[PlanIssue]
) -> int | None:
    """Return the advisory turn estimate, recording an issue when it is not a sane integer."""
    value = raw_step.get("expected_turns")
    if "expected_turns" not in raw_step:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not (1 <= value <= _MAX_EXPECTED_TURNS)
    ):
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.EXPECTED_TURNS_INVALID,
                field_name="expected_turns",
                step_id=step_id,
                message=(
                    f"step {label} field 'expected_turns' must be an integer from 1 to "
                    f"{_MAX_EXPECTED_TURNS}"
                ),
            )
        )
        return None
    return value


def _validate_step_ids(steps: Sequence[PlanStep], issues: list[PlanIssue]) -> None:
    """Record an issue for every repeated step id."""
    seen: set[str] = set()
    for step in steps:
        if step.step_id in seen:
            issues.append(
                PlanIssue(
                    reason=PlanIssueReason.DUPLICATE_STEP_ID,
                    field_name="step_id",
                    step_id=step.step_id,
                    message=f"step id {step.step_id!r} appears more than once",
                )
            )
        seen.add(step.step_id)


def _validate_declarations(
    steps: Sequence[PlanStep],
    issues: list[PlanIssue],
    *,
    trajectory_allowlist: Set[str],
    trajectory_classification: DataClassification,
    configured_tiers: Set[str],
) -> None:
    """Record an issue for each undeclarable tool, unconfigured tier or laundered level."""
    for step in steps:
        for tool in step.tools:
            if tool not in trajectory_allowlist:
                issues.append(
                    PlanIssue(
                        reason=PlanIssueReason.TOOL_NOT_ALLOWLISTED,
                        field_name="tools",
                        step_id=step.step_id,
                        message=(
                            f"step {step.step_id} declares tool {tool!r}, which is not in the "
                            "trajectory allowlist"
                        ),
                    )
                )
        if step.tier not in configured_tiers:
            issues.append(
                PlanIssue(
                    reason=PlanIssueReason.TIER_NOT_CONFIGURED,
                    field_name="tier",
                    step_id=step.step_id,
                    message=f"step {step.step_id} declares tier {step.tier!r}, which is not "
                    "configured",
                )
            )
        if step.data_classification > trajectory_classification:
            issues.append(
                PlanIssue(
                    reason=PlanIssueReason.CLASSIFICATION_LAUNDERING,
                    field_name="data_classification",
                    step_id=step.step_id,
                    message=(
                        f"step {step.step_id} declares {step.data_classification.value!r}, above "
                        f"the trajectory's {trajectory_classification.value!r}; a plan cannot "
                        "launder data into a step of its own choosing"
                    ),
                )
            )


def _validate_dag(steps: Sequence[PlanStep], issues: list[PlanIssue]) -> None:
    """Record an issue for every dangling dependency, self-dependency or cycle."""
    known = {step.step_id for step in steps}
    edges: dict[str, tuple[str, ...]] = {}
    for step in steps:
        for dependency in step.depends_on:
            if dependency == step.step_id:
                issues.append(
                    PlanIssue(
                        reason=PlanIssueReason.SELF_DEPENDENCY,
                        field_name="depends_on",
                        step_id=step.step_id,
                        message=f"step {step.step_id} depends on itself",
                    )
                )
            elif dependency not in known:
                issues.append(
                    PlanIssue(
                        reason=PlanIssueReason.UNKNOWN_DEPENDENCY,
                        field_name="depends_on",
                        step_id=step.step_id,
                        message=(
                            f"step {step.step_id} depends on {dependency!r}, which is not a step "
                            "of this plan"
                        ),
                    )
                )
        edges[step.step_id] = tuple(d for d in step.depends_on if d in known and d != step.step_id)

    cycle = _find_cycle(edges)
    if cycle is not None:
        issues.append(
            PlanIssue(
                reason=PlanIssueReason.DEPENDENCY_CYCLE,
                field_name="depends_on",
                step_id=cycle[0],
                message="depends_on must form a DAG; these steps form a cycle: "
                + " -> ".join([*cycle, cycle[0]]),
            )
        )


def _find_cycle(edges: Mapping[str, tuple[str, ...]]) -> tuple[str, ...] | None:
    """Return one cycle in the dependency graph, or ``None``.

    Reporting the *members* rather than merely "there is a cycle" is what lets a corrective retry
    fix it: the planner is told which steps to break apart, not that its whole answer was wrong.
    """
    unvisited, visiting, done = 0, 1, 2
    state = dict.fromkeys(edges, unvisited)
    stack: list[str] = []

    def walk(node: str) -> tuple[str, ...] | None:
        state[node] = visiting
        stack.append(node)
        for dependency in edges[node]:
            if state[dependency] == visiting:
                return tuple(stack[stack.index(dependency) :])
            if state[dependency] == unvisited:
                found = walk(dependency)
                if found is not None:
                    return found
        stack.pop()
        state[node] = done
        return None

    for node in edges:
        if state[node] == unvisited:
            found = walk(node)
            if found is not None:
                return found
    return None


def schema_document() -> str:
    """Return the committed plan schema as the exact text ``plan.schema.json`` holds.

    The file is what a prompt ships to a planner; this function is what the golden test compares
    it against, so the schema the model is shown and the schema this module enforces cannot drift.
    """
    return canonical_json(PLAN_SCHEMA) + "\n"
