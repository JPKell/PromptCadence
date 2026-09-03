"""Tests for promptcadence.domain.errors: the vocabulary is the specification's, not an invention.

The list is read out of the mirrored ``docs/apps/promptcadence/spec.md`` rather than written down
here, so a code added to one side and not the other fails the suite instead of diverging quietly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from baseaicore import SuiteError, ValidationError

from promptcadence.domain.errors import (
    ApprovalInvalidStateError,
    ClassificationInvalidError,
    DeviationHaltedError,
    ErrorCode,
    IllegalTransitionError,
    PlanInvalidError,
    TierNotConfiguredError,
    TierUnavailableError,
    UnpricedEgressRefusedError,
)

_SPEC = Path(__file__).resolve().parents[2] / "docs/apps/promptcadence/spec.md"
_CLASSES = (
    PlanInvalidError,
    ClassificationInvalidError,
    TierNotConfiguredError,
    TierUnavailableError,
    UnpricedEgressRefusedError,
    ApprovalInvalidStateError,
    DeviationHaltedError,
)


def _spec_codes() -> set[str]:
    """Every code in spec §13's opening code block."""
    text = _SPEC.read_text(encoding="utf-8")
    section = text[text.index("## 13. Error behaviour") :]
    start = section.index("```text") + len("```text")
    block = section[start : section.index("```", start)]
    return set(re.findall(r"\b[A-Z][A-Z_]{3,}\b", block))


def test_the_error_code_enum_is_exactly_spec_thirteens_list() -> None:
    assert {member.value for member in ErrorCode} == _spec_codes()


@pytest.mark.parametrize("error_class", _CLASSES, ids=lambda cls: cls.__name__)
def test_every_error_class_carries_a_spec_thirteen_code(error_class: type[SuiteError]) -> None:
    assert error_class.code in {member.value for member in ErrorCode}


@pytest.mark.parametrize("error_class", _CLASSES, ids=lambda cls: cls.__name__)
def test_every_error_class_is_a_suite_error_with_structured_details(
    error_class: type[SuiteError],
) -> None:
    raised = error_class("something went wrong", details={"field": "tier"})
    assert isinstance(raised, SuiteError)
    assert raised.details == {"field": "tier"}
    assert "tier" not in str(raised), "details are for machines; the message is for humans"


def test_the_validation_shaped_errors_stay_catchable_as_validation_errors() -> None:
    """Callers that catch ``ValidationError`` should not have to learn two names for one failure."""
    assert issubclass(PlanInvalidError, ValidationError)
    assert issubclass(ClassificationInvalidError, ValidationError)
    assert issubclass(TierNotConfiguredError, ValidationError)


def test_an_illegal_transition_is_deliberately_not_a_spec_thirteen_code() -> None:
    """It is an internal defect; a service maps it onto the caller-facing code for the operation."""
    assert IllegalTransitionError.code == "TRANSITION_NOT_PERMITTED"
    assert IllegalTransitionError.code not in {member.value for member in ErrorCode}
