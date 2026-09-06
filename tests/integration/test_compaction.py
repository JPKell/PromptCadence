"""Compaction against the fake LoadCoach: the trigger, the governed summary, and the record.

Lifecycle §7 in a running loop. What each test is really about is the **record**: compaction is a
view and never a deletion, so the proof that it happened at all is the ``compactions`` row and its
``context.compacted`` event, and the proof that it was safe is that every original turn is still
in ``turns`` afterwards.

The tier budgets here are deliberately tiny. A test that needed a real 16 384-token transcript to
cross a threshold would be a slow test measuring the fake's throughput; the arithmetic is the same
at 200 tokens and the assertions are the same sentences.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from cutctx import CompactionExecutor
from sqlalchemy import select
from tests.fakes.harness import LoopHarness, open_harness
from tests.fakes.loadcoach_app import ScriptedGeneration

from promptcadence.config import load_settings
from promptcadence.domain.errors import ErrorCode
from promptcadence.domain.events import EventType
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models
from promptcadence.services.loop import COMPACTION_STEP_PREFIX

_LONG = "The meeting covered the migration plan in considerable detail. " * 6


def _tiers(fast_budget: int, large_budget: int = 4096) -> dict[str, str]:
    """Both local tiers restated: a ``tiers`` table from the environment replaces the defaults."""
    return {
        "PROMPTCADENCE_TIERS__LOCAL_FAST__TASK_PROFILE": "tools.agent.local_fast",
        "PROMPTCADENCE_TIERS__LOCAL_FAST__REMOTE": "false",
        "PROMPTCADENCE_TIERS__LOCAL_FAST__CONTEXT_BUDGET_TOKENS": str(fast_budget),
        "PROMPTCADENCE_TIERS__LOCAL_LARGE__TASK_PROFILE": "tools.agent.local_large",
        "PROMPTCADENCE_TIERS__LOCAL_LARGE__REMOTE": "false",
        "PROMPTCADENCE_TIERS__LOCAL_LARGE__CONTEXT_BUDGET_TOKENS": str(large_budget),
    }


def _harness(monkeypatch: pytest.MonkeyPatch, **env: str) -> Iterator[LoopHarness]:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with open_harness(load_settings().settings) as harness:
        yield harness


@pytest.fixture
def dropping(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopHarness]:
    """A chain with no summarizing policy: masking and dropping alone, so no model call."""
    yield from _harness(
        monkeypatch,
        PROMPTCADENCE_COMPACTION__POLICY_CHAIN="observation_masking,drop_oldest",
        PROMPTCADENCE_COMPACTION__PROTECTED_RECENT_TURNS="1",
        **_tiers(220),
    )


@pytest.fixture
def summarizing(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopHarness]:
    """The shipped chain, on a tier small enough that masking alone cannot fit it."""
    yield from _harness(
        monkeypatch,
        PROMPTCADENCE_COMPACTION__PROTECTED_RECENT_TURNS="1",
        **_tiers(220),
    )


def _call(index: int) -> dict[str, object]:
    """One ``list_dir`` call in LoadCoach's wire shape — fragments, not an assembled call."""
    return {
        "call_index": 0,
        "id": f"c{index}",
        "name": "list_dir",
        "arguments_fragment": '{"path": "."}',
    }


def _long_run(harness: LoopHarness, turns: int, *, token_budget: int | None = None) -> str:
    """A bypass trajectory whose model answers ``turns`` times, the last one a declared stop.

    Every intermediate answer carries a long body **and** a tool call: only a tool request
    continues a step (``decide_finish``), and the body is what makes the transcript grow. The tool
    is ``list_dir``, which needs nothing planted and returns a short result — so what crosses the
    threshold is the model's own output, which is what a real long trajectory looks like.
    """
    harness.script(
        *[ScriptedGeneration(text=_LONG, tool_calls=(_call(index),)) for index in range(turns - 1)],
        ScriptedGeneration(text="Done."),
    )
    return harness.submit_bypass(max_turns=turns + 2, token_budget=token_budget)


def _compactions(harness: LoopHarness, trajectory_id: str) -> list[models.Compaction]:
    with harness.database.read() as session:
        return list(
            session.execute(
                select(models.Compaction)
                .where(models.Compaction.trajectory_id == trajectory_id)
                .order_by(models.Compaction.created_at, models.Compaction.id)
            ).scalars()
        )


# --------------------------------------------------------------------------------------------
# The trigger, and "a view, never a deletion"
# --------------------------------------------------------------------------------------------


def test_a_transcript_over_the_threshold_is_compacted_and_every_turn_stays(
    dropping: LoopHarness,
) -> None:
    """Exit condition 2, and the sentence lifecycle §7 leads with."""
    harness = dropping
    trajectory_id = _long_run(harness, 6)
    harness.claim_and_run(trajectory_id)

    rows = _compactions(harness, trajectory_id)
    assert rows, "the transcript never crossed the threshold, so this test proves nothing"
    row = rows[0]
    assert row.tokens_after_estimate < row.tokens_before
    assert row.tokens_after_estimate <= row.budget_tokens
    assert row.step_id == "loop"
    assert row.summary_turn_id is None, "the chain has no summarizing policy"

    bodies = harness.event_data(trajectory_id, EventType.CONTEXT_COMPACTED.value)
    assert len(bodies) == len(rows)
    assert bodies[0]["compaction_id"] == row.id
    assert bodies[0]["plan_hash"] == row.plan_hash

    # Compaction is a view: every turn the wire lost is still a row.
    with harness.database.read() as session:
        recorded = set(
            session.execute(
                select(models.Turn.id).where(models.Turn.trajectory_id == trajectory_id)
            ).scalars()
        )
    assert set(row.dropped_turn_ids) <= recorded
    assert set(row.masked_turn_ids) <= recorded


def test_the_event_and_the_row_are_one_write(dropping: LoopHarness) -> None:
    """ADR-0044: never a row without its event, and never an event without its row."""
    harness = dropping
    trajectory_id = _long_run(harness, 6)
    harness.claim_and_run(trajectory_id)
    rows = _compactions(harness, trajectory_id)
    bodies = harness.event_data(trajectory_id, EventType.CONTEXT_COMPACTED.value)
    assert [row.id for row in rows] == [body["compaction_id"] for body in bodies]


# --------------------------------------------------------------------------------------------
# The summary: governed, debited, recorded, and out of the step's way
# --------------------------------------------------------------------------------------------


def test_the_summary_runs_under_its_own_superseding_revision_in_its_own_thread(
    summarizing: LoopHarness,
) -> None:
    """Exit condition 3, and ADR-0090's two rules that are visible from outside."""
    harness = summarizing
    trajectory_id = _long_run(harness, 8)
    harness.claim_and_run(trajectory_id)

    rows = [row for row in _compactions(harness, trajectory_id) if row.summary_turn_id]
    assert rows, "no summarization ran, so this test proves nothing"
    row = rows[0]

    with harness.database.read() as session:
        turn = session.get(models.Turn, row.summary_turn_id)
        assert turn is not None
        thread = session.get(models.Thread, turn.thread_id)
        assert thread is not None
        intents = list(
            session.execute(
                select(models.ExecutionIntent)
                .where(models.ExecutionIntent.trajectory_id == trajectory_id)
                .order_by(models.ExecutionIntent.revision)
            ).scalars()
        )

    # Its own thread, so it is never replayed into the transcript it replaced (ADR-0091 rule 3).
    assert thread.step_id.startswith(COMPACTION_STEP_PREFIX)
    assert thread.id != row.thread_id

    # Its own revision, narrowed: one tier, no fallbacks, no tools (ADR-0090 rule 2).
    narrowed = next(item for item in intents if item.revision == row.summary_intent_revision)
    assert narrowed.approved_tier == turn.tier
    assert narrowed.fallback_tiers_json == []
    assert narrowed.approved_tools_json == []
    assert narrowed.minted_by == "policy"
    assert turn.intent_revision == narrowed.revision

    # And restored afterwards, so the step's own turns never run under the summary's envelope.
    restored = next(item for item in intents if item.revision == narrowed.revision + 1)
    first = intents[0]
    assert (restored.approved_tier, restored.approved_tools_json) == (
        first.approved_tier,
        first.approved_tools_json,
    )


def test_the_summary_turn_is_debited(summarizing: LoopHarness) -> None:
    """Lifecycle §7 settles the debit; ADR-0091 rule 1 keeps it."""
    harness = summarizing
    trajectory_id = _long_run(harness, 8)
    harness.claim_and_run(trajectory_id)
    rows = [row for row in _compactions(harness, trajectory_id) if row.summary_turn_id]
    assert rows, "no summarization ran, so this test proves nothing"
    assert rows[0].summary_turn_id in harness.budget.debited_turn_ids(trajectory_id)


def test_the_summary_prompt_record_is_on_the_turn_that_used_it(summarizing: LoopHarness) -> None:
    """Spec §9: PromptCadence's own prompts are recorded on the turn, with version and digest."""
    harness = summarizing
    trajectory_id = _long_run(harness, 8)
    harness.claim_and_run(trajectory_id)
    rows = [row for row in _compactions(harness, trajectory_id) if row.summary_turn_id]
    assert rows, "no summarization ran, so this test proves nothing"
    with harness.database.read() as session:
        asks = list(
            session.execute(
                select(models.Turn)
                .where(models.Turn.prompt_id == "compaction.summarize")
                .order_by(models.Turn.created_at)
            ).scalars()
        )
    assert asks
    assert asks[0].prompt_version == "1.0.0"
    assert (asks[0].prompt_sha256 or "").startswith("sha256:")
    assert asks[0].role == "user"


def test_a_summarization_request_is_always_fulfilled_before_it_is_applied(
    summarizing: LoopHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit condition 5's second half: ``SummaryMissing`` is unreachable from the loop.

    Asserted at the seam rather than by catching the exception. ``apply`` raises ``SummaryMissing``
    when a plan's requests are not covered, so covering them on every call is the property; a test
    that merely never saw the exception would also pass on a build that never summarized.
    """
    seen: list[bool] = []
    original = CompactionExecutor.apply

    def checked(self: Any, transcript: Any, plan: Any, summaries: Any = None) -> Any:
        supplied = set(summaries or {})
        wanted = {request.group_id for request in plan.summarization_requests}
        seen.append(bool(wanted))
        assert wanted <= supplied, f"unfulfilled: {sorted(wanted - supplied)}"
        return original(self, transcript, plan, summaries or {})

    monkeypatch.setattr(CompactionExecutor, "apply", checked)
    trajectory_id = _long_run(summarizing, 8)
    summarizing.claim_and_run(trajectory_id)
    assert any(seen), "no plan carried a summarization request, so this test proves nothing"


# --------------------------------------------------------------------------------------------
# The step's advance budget, and the refusal
# --------------------------------------------------------------------------------------------


def test_a_step_near_its_turn_limit_still_compacts_and_still_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0091 rule 2 at the boundary: housekeeping does not end the step.

    Four answers under ``max_turns_per_step = 4``. If the compaction summary counted against the
    step's advance budget, the fourth answer would never be asked for and the trajectory would
    halt on ``STEP_LIMIT_EXCEEDED`` instead of completing.
    """
    for key, value in {
        "PROMPTCADENCE_EXECUTION__MAX_TURNS_PER_STEP": "4",
        "PROMPTCADENCE_COMPACTION__PROTECTED_RECENT_TURNS": "1",
        **_tiers(220),
    }.items():
        monkeypatch.setenv(key, value)
    with open_harness(load_settings().settings) as harness:
        trajectory_id = _long_run(harness, 4)
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
        assert _compactions(harness, trajectory_id), "nothing compacted; the boundary was not hit"


def test_a_budget_the_untouchable_turns_alone_exceed_halts_with_compaction_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit condition 5: CutCtx's ``BudgetUnsatisfiable``, typed rather than a traceback."""
    for key, value in {
        "PROMPTCADENCE_COMPACTION__PROTECTED_RECENT_TURNS": "8",
        **_tiers(40),
    }.items():
        monkeypatch.setenv(key, value)
    with open_harness(load_settings().settings) as harness:
        trajectory_id = _long_run(harness, 6)
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.HALTED
        with harness.database.read() as session:
            row = session.get(models.Trajectory, trajectory_id)
        assert row is not None
        assert row.error_code == ErrorCode.COMPACTION_FAILED.value
        assert "cannot be compacted" in (row.halted_reason or "")


# --------------------------------------------------------------------------------------------
# The arithmetic, at length
# --------------------------------------------------------------------------------------------


def test_a_hundred_turn_trajectory_completes_inside_its_tier_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The development plan's own test, and the one that proves the trigger does not thrash.

    Every compaction must leave the wire at or under the tier budget, and the trajectory must
    still reach its declared finish. A trigger that compacted to the *threshold* rather than to
    the budget would fire on every turn from here on and this assertion is what would catch it.
    """
    for key, value in {
        "PROMPTCADENCE_EXECUTION__MAX_TURNS_PER_STEP": "120",
        "PROMPTCADENCE_EXECUTION__MAX_STEPS": "120",
        "PROMPTCADENCE_COMPACTION__PROTECTED_RECENT_TURNS": "2",
        **_tiers(400),
    }.items():
        monkeypatch.setenv(key, value)
    with open_harness(load_settings().settings) as harness:
        trajectory_id = _long_run(harness, 100, token_budget=10_000_000)
        assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
        rows = _compactions(harness, trajectory_id)
        assert len(rows) > 1, "one compaction over a hundred turns is not the arithmetic tested"
        assert all(row.tokens_after_estimate <= row.budget_tokens for row in rows)
        assert all(row.tokens_after_estimate < row.tokens_before for row in rows)
