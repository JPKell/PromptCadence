"""Tests for promptcadence.domain.plan: the committed schema and lifecycle §4.1's five rules.

The goldens here are the phase's acceptance criterion 2 for plan validation: the same document
validates the same way every time, and a refusal names which step and which field failed — the
only mitigation available in this phase for the plan schema's named risk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from baseaicore import DataClassification, ValidationError, canonical_json, sha256_of

from promptcadence.domain.errors import PlanInvalidError
from promptcadence.domain.plan import (
    PLAN_SCHEMA,
    Plan,
    PlanIssueReason,
    PlanStep,
    schema_document,
    validate_plan_document,
)

_SCHEMA_FILE = Path(__file__).resolve().parents[2] / "src/promptcadence/domain/plan.schema.json"
_GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"
_ALLOWLIST = frozenset({"read_file", "list_dir", "write_file"})
_TIERS = frozenset({"local_fast", "local_large", "remote_cheap"})


def _step(step_id: str, **overrides: Any) -> dict[str, Any]:
    """One well-formed step document, with named overrides."""
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


def _document(*steps: dict[str, Any]) -> str:
    """A plan document as the planner would return it."""
    return json.dumps({"steps": list(steps)})


def _validate(document: str, **overrides: Any) -> Plan:
    """Validate against the standard fixture allowlist, classification and tier set."""
    kwargs: dict[str, Any] = {
        "trajectory_allowlist": _ALLOWLIST,
        "trajectory_classification": DataClassification.CONFIDENTIAL,
        "configured_tiers": _TIERS,
        "max_plan_steps": 20,
    }
    return validate_plan_document(document, **{**kwargs, **overrides})


def _reasons(document: str, **overrides: Any) -> list[str]:
    """Validate, expecting a refusal, and return the issue reasons it named."""
    with pytest.raises(PlanInvalidError) as caught:
        _validate(document, **overrides)
    issues = caught.value.details["issues"]
    return [issue["reason"] for issue in issues]


def test_the_committed_schema_file_matches_the_module(tmp_path: Path) -> None:
    """The schema a prompt ships and the schema this module enforces cannot drift apart."""
    assert _SCHEMA_FILE.read_text(encoding="utf-8") == schema_document()


def test_the_schema_and_the_validator_agree_on_the_step_fields() -> None:
    """Both sides of "committed and golden-tested": the file, and what the code requires.

    The validator is hand-written (this phase adds no runtime dependency, and a generic
    validator's message is what makes a corrective retry loop), so its field set is asserted
    against the schema's rather than assumed to match it.
    """
    required = set(PLAN_SCHEMA["properties"]["steps"]["items"]["required"])
    declared = set(PLAN_SCHEMA["properties"]["steps"]["items"]["properties"])
    assert required == declared
    assert required == {field.name for field in PlanStep.__dataclass_fields__.values()}


def test_a_valid_plan_validates_and_is_byte_identical_on_re_derivation() -> None:
    document = _document(_step("s1"), _step("s2", depends_on=["s1"], tools=["list_dir"]))
    first = _validate(document)
    second = _validate(document)
    assert canonical_json(first.as_canonical()) == canonical_json(second.as_canonical())
    assert first.step_ids == ("s1", "s2")
    assert first.document_sha256 == sha256_of(document)


def test_the_verbatim_document_travels_with_the_validated_form() -> None:
    """Lifecycle §4.1: persisted verbatim *alongside* its validated form, inseparably."""
    document = _document(_step("s1"))
    plan = _validate(document)
    assert plan.raw_document == document
    with pytest.raises(ValidationError, match="document_sha256"):
        Plan(steps=plan.steps, raw_document="{}", document_sha256=plan.document_sha256)


def test_an_empty_plan_is_invalid_because_emptiness_cannot_pass_a_gate() -> None:
    """The IdeaPress M7 lesson, and the reason ADR-0042 exists."""
    assert _reasons('{"steps": []}') == [PlanIssueReason.EMPTY_PLAN]
    with pytest.raises(ValidationError, match="emptiness cannot pass a gate"):
        Plan(steps=(), raw_document="{}", document_sha256=sha256_of("{}"))


def test_a_cycle_is_refused_and_the_refusal_names_the_steps_in_it() -> None:
    """Naming the members is what lets a corrective retry break the cycle rather than redraft."""
    document = _document(
        _step("s1", depends_on=["s3"]),
        _step("s2", depends_on=["s1"]),
        _step("s3", depends_on=["s2"]),
    )
    with pytest.raises(PlanInvalidError) as caught:
        _validate(document)
    (issue,) = caught.value.details["issues"]
    assert issue["reason"] == PlanIssueReason.DEPENDENCY_CYCLE
    assert "s1 -> " in issue["message"] or " -> s1" in issue["message"]
    assert issue["step_id"] in {"s1", "s2", "s3"}


def test_a_self_dependency_is_refused() -> None:
    assert _reasons(_document(_step("s1", depends_on=["s1"]))) == [PlanIssueReason.SELF_DEPENDENCY]


def test_a_dangling_dependency_is_refused() -> None:
    assert _reasons(_document(_step("s1", depends_on=["s9"]))) == [
        PlanIssueReason.UNKNOWN_DEPENDENCY
    ]


def test_a_plan_cannot_launder_confidential_data_into_a_lower_step() -> None:
    """The nastiest rule: an unchecked step classification routes confidential work remotely."""
    document = _document(_step("s1", data_classification="confidential"))
    assert _reasons(document, trajectory_classification=DataClassification.INTERNAL) == [
        PlanIssueReason.CLASSIFICATION_LAUNDERING
    ]
    assert _validate(document, trajectory_classification=DataClassification.CONFIDENTIAL)


def test_a_step_may_declare_a_classification_below_the_trajectorys() -> None:
    plan = _validate(
        _document(_step("s1", data_classification="public")),
        trajectory_classification=DataClassification.INTERNAL,
    )
    assert plan.steps[0].data_classification is DataClassification.PUBLIC


def test_a_tool_outside_the_callers_allowlist_is_refused() -> None:
    assert _reasons(_document(_step("s1", tools=["rm_rf"]))) == [
        PlanIssueReason.TOOL_NOT_ALLOWLISTED
    ]


def test_an_unconfigured_tier_is_refused() -> None:
    assert _reasons(_document(_step("s1", tier="gpt_9"))) == [PlanIssueReason.TIER_NOT_CONFIGURED]


def test_an_unknown_classification_level_is_refused_rather_than_coerced() -> None:
    assert _reasons(_document(_step("s1", data_classification="secret"))) == [
        PlanIssueReason.CLASSIFICATION_UNKNOWN
    ]


def test_a_duplicate_step_id_is_refused() -> None:
    assert PlanIssueReason.DUPLICATE_STEP_ID in _reasons(_document(_step("s1"), _step("s1")))


def test_the_step_limit_is_enforced() -> None:
    document = _document(*(_step(f"s{index}") for index in range(6)))
    assert _reasons(document, max_plan_steps=5) == [PlanIssueReason.TOO_MANY_STEPS]


def test_a_document_that_is_not_json_or_not_an_object_is_refused() -> None:
    assert _reasons("not json at all") == [PlanIssueReason.NOT_JSON]
    assert _reasons("[1, 2]") == [PlanIssueReason.NOT_AN_OBJECT]
    assert _reasons('{"plan": []}') == [
        PlanIssueReason.UNKNOWN_FIELD,
        PlanIssueReason.MISSING_FIELD,
    ]
    assert _reasons('{"steps": {}}') == [PlanIssueReason.WRONG_TYPE]


def test_a_missing_or_unknown_step_field_is_refused_and_named() -> None:
    step = _step("s1")
    del step["tier"]
    step["priority"] = "high"
    with pytest.raises(PlanInvalidError) as caught:
        _validate(_document(step))
    issues = caught.value.details["issues"]
    assert {issue["reason"] for issue in issues} == {
        PlanIssueReason.MISSING_FIELD,
        PlanIssueReason.UNKNOWN_FIELD,
    }
    assert all(issue["step_id"] == "s1" for issue in issues)
    assert {issue["field"] for issue in issues} == {"tier", "priority"}


def test_a_wrong_type_is_named_field_by_field() -> None:
    document = _document(_step("s1", tools="read_file", expected_turns=0, description=""))
    reasons = _reasons(document)
    assert PlanIssueReason.WRONG_TYPE in reasons
    assert PlanIssueReason.EXPECTED_TURNS_INVALID in reasons


def test_expected_turns_refuses_a_boolean_masquerading_as_an_integer() -> None:
    """``True`` is an ``int`` in Python; a plan saying ``expected_turns: true`` means nothing."""
    assert PlanIssueReason.EXPECTED_TURNS_INVALID in _reasons(
        _document(_step("s1", expected_turns=True))
    )


def test_every_issue_is_reported_together_not_one_per_attempt() -> None:
    """A bounded corrective budget is spent immediately by a one-problem-at-a-time validator."""
    document = _document(
        _step("s1", tools=["rm_rf"], tier="gpt_9", data_classification="confidential"),
        _step("s2", depends_on=["nowhere"]),
    )
    with pytest.raises(PlanInvalidError) as caught:
        _validate(document, trajectory_classification=DataClassification.INTERNAL)
    assert caught.value.details["issue_count"] >= 4


def test_a_refusal_never_carries_a_step_description() -> None:
    """``details`` travels into logs and API envelopes; a description is model output."""
    secret = "the customer list is at /srv/private.csv"  # noqa: S105 — a description, not a token
    document = _document(_step("s1", description=secret, tier="gpt_9"))
    with pytest.raises(PlanInvalidError) as caught:
        _validate(document)
    assert secret not in json.dumps(caught.value.details)
    assert secret not in str(caught.value)


def test_ready_steps_and_topological_order_respect_the_dag() -> None:
    document = _document(
        _step("s1"),
        _step("s2", depends_on=["s1"]),
        _step("s3", depends_on=["s1"]),
        _step("s4", depends_on=["s2", "s3"]),
    )
    plan = _validate(document)
    assert [step.step_id for step in plan.ready_steps(frozenset())] == ["s1"]
    assert [step.step_id for step in plan.ready_steps(frozenset({"s1"}))] == ["s2", "s3"]
    assert [step.step_id for step in plan.ready_steps(frozenset({"s1", "s2"}))] == ["s3"]
    assert plan.topological_order() == ("s1", "s2", "s3", "s4")


def test_step_lookup_refuses_an_unknown_step() -> None:
    plan = _validate(_document(_step("s1")))
    assert plan.step("s1").step_id == "s1"
    with pytest.raises(ValidationError, match="no step"):
        plan.step("s9")


def test_plan_validation_goldens() -> None:
    """The determinism golden for plan validation (acceptance criterion 2).

    One entry per named case in the development plan's test list, plus the laundering case, all
    re-derived and compared byte for byte.
    """
    cases: dict[str, Any] = {}
    valid = _document(_step("s1"), _step("s2", depends_on=["s1"]))
    cases["valid"] = _validate(valid).as_canonical()
    for name, document, classification in (
        ("empty", '{"steps": []}', DataClassification.CONFIDENTIAL),
        (
            "cyclic",
            _document(_step("s1", depends_on=["s2"]), _step("s2", depends_on=["s1"])),
            DataClassification.CONFIDENTIAL,
        ),
        (
            "laundering",
            _document(_step("s1", data_classification="confidential")),
            DataClassification.INTERNAL,
        ),
        ("unknown_tool", _document(_step("s1", tools=["rm_rf"])), DataClassification.CONFIDENTIAL),
        ("unknown_tier", _document(_step("s1", tier="gpt_9")), DataClassification.CONFIDENTIAL),
    ):
        with pytest.raises(PlanInvalidError) as caught:
            _validate(document, trajectory_classification=classification)
        cases[name] = caught.value.details
    golden = _GOLDEN_DIR / "plan_validation.json"
    produced = canonical_json(cases)
    if not golden.exists():  # pragma: no cover — first run writes the golden
        golden.write_text(produced + "\n", encoding="utf-8")
    assert produced + "\n" == golden.read_text(encoding="utf-8")
