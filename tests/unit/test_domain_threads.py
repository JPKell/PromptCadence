"""Tests for promptcadence.domain.threads: the package-shaped thread, turn and snapshot.

The constraint this module is under cannot be caught by accident — spec §10 requires no
PromptCadence vocabulary in the types — so it is asserted directly, over the field names, rather
than left to review.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from baseaicore import UNSUPPORTED, TokenUsage, ValidationError

from promptcadence.domain import threads
from promptcadence.domain.threads import (
    FinishReason,
    Thread,
    ThreadSnapshot,
    Turn,
    TurnRole,
    build_snapshot,
)

_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
_PROMPTCADENCE_VOCABULARY = frozenset(
    {
        "trajectory_id",
        "tier",
        "intent_id",
        "intent_revision",
        "step_id",
        "approval_request_id",
        "plan_id",
        "deviation",
        "classification",
        "data_classification",
    }
)


def _turn(sequence: int = 1, **kwargs: object) -> Turn[None]:
    """A minimal package-shaped turn with no provenance attached."""
    # kwargs are the declared keyword-only fields of Turn.
    return Turn(f"t{sequence}", "th1", sequence, TurnRole.ASSISTANT, None, **kwargs)  # type: ignore[arg-type]


def test_no_type_here_carries_promptcadence_vocabulary() -> None:
    """The ThreadRack rejection's whole condition: extraction must be a move, not a rewrite.

    Spec §10 requires these types to be built "as if they were a package (no PromptCadence
    vocabulary in the types)". That erodes one convenient field at a time, and no behavioural test
    would catch it, so the field names are the assertion.
    """
    for cls in (Thread, Turn, ThreadSnapshot):
        names = {f.name for f in dataclasses.fields(cls)}
        offending = names & _PROMPTCADENCE_VOCABULARY
        assert not offending, (
            f"{cls.__name__} carries PromptCadence vocabulary {sorted(offending)}. "
            "Attach host-specific facts through the generic `provenance` field instead."
        )


def test_a_turn_cannot_be_built_without_a_provenance() -> None:
    """``provenance`` is positional with no default, which is what makes the host's rule bite."""
    with pytest.raises(TypeError):
        Turn("t1", "th1", 1, TurnRole.ASSISTANT)  # type: ignore[call-arg]  # the guard under test


def test_a_thread_refuses_an_empty_identifier_or_a_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="thread_id"):
        Thread("  ", "owner", _NOW)
    with pytest.raises(ValidationError, match="owner_id"):
        Thread("th1", "", _NOW)
    with pytest.raises(ValidationError, match="timezone-aware"):
        Thread("th1", "owner", datetime(2026, 9, 2, 12, 0))  # noqa: DTZ001 — the refusal


def test_a_turn_refuses_a_sequence_below_one() -> None:
    with pytest.raises(ValidationError, match="sequence"):
        Turn("t1", "th1", 0, TurnRole.ASSISTANT, None)


def test_a_tool_turn_must_name_the_call_it_answers_and_others_must_not() -> None:
    """A tool result separated from its call is exactly what compaction must never produce."""
    assert Turn("t1", "th1", 1, TurnRole.TOOL, None, tool_call_id="c1").tool_call_id == "c1"
    with pytest.raises(ValidationError, match="tool_call_id"):
        Turn("t1", "th1", 1, TurnRole.TOOL, None)
    with pytest.raises(ValidationError, match="only a tool turn"):
        Turn("t1", "th1", 1, TurnRole.ASSISTANT, None, tool_call_id="c1")


def test_finish_reason_has_no_member_that_could_be_mistaken_for_success() -> None:
    """Spec §11 contract 6: only a declared STOP is success, and absence is ``None``."""
    assert set(FinishReason) == {
        FinishReason.STOP,
        FinishReason.LENGTH,
        FinishReason.TOOL_CALLS,
        FinishReason.ERROR,
    }
    assert not any(member.value in {"unknown", "complete", "done"} for member in FinishReason)


def test_a_snapshot_refuses_a_foreign_turn() -> None:
    foreign = Turn("t1", "other", 1, TurnRole.ASSISTANT, None)
    with pytest.raises(ValidationError, match="belongs to thread"):
        ThreadSnapshot(thread_id="th1", turns=(foreign,), taken_at=_NOW)


def test_a_snapshot_refuses_an_out_of_order_or_duplicated_transcript() -> None:
    with pytest.raises(ValidationError, match="ascend"):
        ThreadSnapshot(thread_id="th1", turns=(_turn(2), _turn(1)), taken_at=_NOW)
    with pytest.raises(ValidationError, match="ascend"):
        build_snapshot("th1", [_turn(1), _turn(1)], taken_at=_NOW)


def test_build_snapshot_orders_by_sequence() -> None:
    snapshot = build_snapshot("th1", [_turn(3), _turn(1), _turn(2)], taken_at=_NOW)
    assert [turn.sequence for turn in snapshot.turns] == [1, 2, 3]


def test_usage_keeps_an_unreported_class_unsupported_rather_than_zero() -> None:
    """ADR-0016 inside the transcript: "not reported" and "none used" are different facts."""
    turn = _turn(usage=TokenUsage(input_tokens=12))
    assert turn.usage is not None
    assert turn.usage.input_tokens == 12
    assert turn.usage.output_tokens is UNSUPPORTED


def test_the_store_port_offers_no_update_and_no_delete() -> None:
    """A transcript that can be rewritten cannot be the authoritative record (contract 2)."""
    surface = {name for name in vars(threads.ThreadStore) if not name.startswith("_")}
    assert surface == {
        "create_thread",
        "get_thread",
        "next_sequence",
        "append_turn",
        "turns",
        "snapshot",
    }
