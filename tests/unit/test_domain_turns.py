"""Tests for promptcadence.domain.turns: contract 6 as a matrix, and its golden.

``LENGTH``, ``ERROR`` and absence are never success — asserted for every combination the
function can see, so a new branch cannot make one of them complete quietly.
"""

from __future__ import annotations

from itertools import product
from pathlib import Path

import pytest
from baseaicore import canonical_json

from promptcadence.domain.errors import ErrorCode
from promptcadence.domain.threads import FinishReason
from promptcadence.domain.turns import FinishOutcome, TurnCompleted, TurnStarted, decide_finish

_GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "finish_decisions.json"


def test_stop_completes_and_says_so() -> None:
    decision = decide_finish(
        finish_reason=FinishReason.STOP, schema_validated=False, tool_calls_requested=0
    )
    assert decision.outcome is FinishOutcome.COMPLETE
    assert decision.error_code is None
    assert "stop" in decision.cause


def test_a_schema_validated_result_completes_without_a_finish_reason() -> None:
    """The one declared success on today's wire, which renders no finish_reason."""
    decision = decide_finish(finish_reason=None, schema_validated=True, tool_calls_requested=0)
    assert decision.outcome is FinishOutcome.COMPLETE


@pytest.mark.parametrize("reason", [FinishReason.LENGTH, FinishReason.ERROR])
def test_length_and_error_never_read_as_success(reason: FinishReason) -> None:
    decision = decide_finish(finish_reason=reason, schema_validated=False, tool_calls_requested=0)
    assert decision.outcome is FinishOutcome.HALT
    assert decision.error_code is ErrorCode.LOADCOACH_ERROR
    assert reason.value in decision.cause


def test_absence_is_handled_explicitly_and_names_the_gap() -> None:
    decision = decide_finish(finish_reason=None, schema_validated=False, tool_calls_requested=0)
    assert decision.outcome is FinishOutcome.HALT
    assert "no finish_reason" in decision.cause
    assert "D2_HANDOFF" in decision.cause


def test_an_undeclared_reason_halts_naming_it() -> None:
    decision = decide_finish(
        finish_reason=None,
        schema_validated=False,
        tool_calls_requested=0,
        undeclared_reason="content_filter",
    )
    assert decision.outcome is FinishOutcome.HALT
    assert "content_filter" in decision.cause


def test_tool_calls_continue_declared_or_not() -> None:
    declared = decide_finish(
        finish_reason=FinishReason.TOOL_CALLS, schema_validated=False, tool_calls_requested=2
    )
    implied = decide_finish(finish_reason=None, schema_validated=False, tool_calls_requested=1)
    assert declared.outcome is implied.outcome is FinishOutcome.CONTINUE


def test_schema_validation_does_not_rescue_a_length_finish() -> None:
    """A declared LENGTH wins over a passed schema check: the answer was cut off."""
    decision = decide_finish(
        finish_reason=FinishReason.LENGTH, schema_validated=True, tool_calls_requested=0
    )
    assert decision.outcome is FinishOutcome.HALT


def test_every_halt_names_a_cause_and_a_code() -> None:
    reasons = [None, *FinishReason]
    for reason, schema, tools, undeclared in product(reasons, (False, True), (0, 1), (None, "x")):
        decision = decide_finish(
            finish_reason=reason,
            schema_validated=schema,
            tool_calls_requested=tools,
            undeclared_reason=undeclared,
        )
        assert decision.cause.strip()
        if decision.outcome is FinishOutcome.HALT:
            assert decision.error_code is not None
        else:
            assert decision.error_code is None


def test_finish_decision_golden() -> None:
    """Every cell of the matrix, byte-identical on re-derivation (lifecycle §10)."""
    reasons = [None, *FinishReason]
    cases = {}
    for reason, schema, tools, undeclared in product(reasons, (False, True), (0, 1), (None, "x")):
        decision = decide_finish(
            finish_reason=reason,
            schema_validated=schema,
            tool_calls_requested=tools,
            undeclared_reason=undeclared,
        )
        name = reason.value if reason else "absent"
        key = f"{name}|schema={schema}|tools={tools}|undeclared={undeclared}"
        cases[key] = {
            "outcome": decision.outcome.value,
            "error_code": decision.error_code.value if decision.error_code else None,
        }
    produced = canonical_json(cases) + "\n"
    if not _GOLDEN.exists():  # pragma: no cover — first run writes the golden
        _GOLDEN.write_text(produced, encoding="utf-8")
    assert produced == _GOLDEN.read_text(encoding="utf-8")


def test_the_two_turn_events_carry_ids_and_numbers_only() -> None:
    started = TurnStarted(
        trajectory_id="t",
        turn_id="u",
        sequence=2,
        tier="local_fast",
        task_profile="tools.agent.local_fast",
        intent_id="i",
        intent_revision=1,
    )
    assert started.as_canonical()["turn_id"] == "u"
    completed = TurnCompleted(
        trajectory_id="t",
        turn_id="u",
        sequence=2,
        tier="local_fast",
        model_canonical_id="m",
        loadcoach_job_id="j",
        finish_reason=None,
        schema_validated=False,
        input_tokens=1,
        output_tokens=2,
        loadcoach_ms=3,
        overhead_ms=4,
        decision=FinishOutcome.HALT,
    )
    assert completed.as_canonical()["decision"] == "halt"
    assert "text" not in completed.as_canonical()
