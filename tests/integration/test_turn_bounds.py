"""``turn_overrun`` is decided before ``STEP_LIMIT_EXCEEDED`` halts (spec §13, lifecycle §5).

G3 §9b asked which fires first when both could. The lifecycle's answer, held here: a step whose
turn reaches the intent's ``max_turns`` without a declared finish is a drift and parks for the
scoped re-approval that may extend it; ``STEP_LIMIT_EXCEEDED`` is the halt for the bound nobody can
extend by approval — ``max_turns_per_step`` tool round trips spent with no declared finish.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select
from tests.fakes.harness import LoopHarness, open_harness
from tests.fakes.loadcoach_app import ScriptedGeneration

from promptcadence.config import load_settings
from promptcadence.domain.errors import ErrorCode
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models


def _call(index: int) -> dict[str, object]:
    return {"call_index": 0, "id": f"c{index}", "name": "list_dir", "arguments_fragment": "{}"}


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopHarness]:
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__MAX_TURNS_PER_STEP", "1")
    with open_harness(load_settings().settings) as built:
        yield built


def test_reaching_max_turns_without_a_finish_parks_as_turn_overrun_before_any_halt(
    harness: LoopHarness,
) -> None:
    """The intent's ``max_turns`` is the caller's 1: the first tool-calling answer is the drift."""
    harness.script(ScriptedGeneration(text="", tool_calls=(_call(0),)))
    trajectory_id = harness.submit_bypass(max_turns=1)
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.AWAITING_APPROVAL
    with harness.database.read() as session:
        categories = list(
            session.execute(
                select(models.Deviation.category).where(
                    models.Deviation.trajectory_id == trajectory_id
                )
            ).scalars()
        )
        request = session.execute(
            select(models.ApprovalRequest).where(
                models.ApprovalRequest.trajectory_id == trajectory_id
            )
        ).scalar_one()
    assert categories == ["turn_overrun"]
    assert request.kind == "reapproval" and request.status == "pending"
    view = harness.service.get(trajectory_id)
    assert view.error_code is None, "a park is not a halt"
    # The requested call was not run before the park: the re-approval decides the envelope first.
    with harness.database.read() as session:
        assert session.execute(select(models.ToolCallRecord)).scalars().all() == []


def test_the_round_trip_cap_halts_with_step_limit_exceeded_when_max_turns_is_not_yet_reached(
    harness: LoopHarness,
) -> None:
    """The caller allows three turns; the configured cap allows one tool round trip."""
    harness.script(
        ScriptedGeneration(text="", tool_calls=(_call(0),)),
        ScriptedGeneration(text="", tool_calls=(_call(1),)),
    )
    trajectory_id = harness.submit_bypass(max_turns=3)
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.HALTED
    view = harness.service.get(trajectory_id)
    assert view.error_code == ErrorCode.STEP_LIMIT_EXCEEDED.value
    assert "max_turns_per_step (1)" in (view.halted_reason or "")
    with harness.database.read() as session:
        categories = list(
            session.execute(
                select(models.Deviation.category).where(
                    models.Deviation.trajectory_id == trajectory_id
                )
            ).scalars()
        )
        records = session.execute(select(models.ToolCallRecord)).scalars().all()
    assert "turn_overrun" not in categories, "two turns of three is not an overrun"
    assert len(records) == 1, "the first round trip ran; the second was the one over the cap"
