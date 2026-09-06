"""The wire ↔ CutCtx mapping, and the trigger. Pure, so unit tests.

The load-bearing assertion here is the one at the ``Message`` level: a compaction that drops an
assistant turn must not leave a tool result answering somebody else's call. The trap it guards
against is real and silent — LoadCoach accepts a well-formed id whatever it means — so it is
asserted on the messages that would go on the wire, not on the plan that produced them.
"""

from __future__ import annotations

import pytest
from baseaicore import DataClassification, ValidationError
from cutctx import (
    CompactionBudget,
    CompactionExecutor,
    DropOldestPolicy,
    ObservationMaskingPolicy,
)

from promptcadence.config import CompactionSettings
from promptcadence.domain.errors import CompactionFailedError
from promptcadence.domain.tiers import EgressClass, Tier
from promptcadence.infrastructure.loadcoach import Message, RequestedToolCall
from promptcadence.services.compaction import (
    budget_for,
    build_chain,
    should_compact,
    summarizing_tier,
    target_tokens,
    to_messages,
    to_transcript,
)


def _tier(name: str, *, budget: int, remote: bool = False, ceiling: str | None = None) -> Tier:
    return Tier(
        name=name,
        task_profile=f"tools.agent.{name}",
        egress_class=EgressClass.REMOTE if remote else EgressClass.LOCAL,
        max_data_classification=DataClassification(ceiling) if ceiling else None,
        context_budget_tokens=budget,
    )


def _exchange(index: int) -> list[Message]:
    """One assistant turn asking for two tools, and the two results answering it."""
    return [
        Message(
            role="assistant",
            content="",
            tool_calls=(
                RequestedToolCall(f"c{index}a", "read_file", {}, True),
                RequestedToolCall(f"c{index}b", "read_file", {}, True),
            ),
        ),
        Message(role="tool", content="A" * 400, tool_call_id=f"c{index}a"),
        Message(role="tool", content="B" * 400, tool_call_id=f"c{index}b"),
    ]


def _wire() -> tuple[list[Message], list[str]]:
    messages = [Message(role="user", content="the task"), Message(role="user", content="step s1")]
    for index in range(4):
        messages.extend(_exchange(index))
    messages.append(Message(role="assistant", content="nearly there"))
    return messages, [f"turn{index:02d}" for index in range(len(messages))]


# --------------------------------------------------------------------------------------------
# The mapping
# --------------------------------------------------------------------------------------------


def test_the_transcript_carries_the_recorded_turn_ids_not_positions() -> None:
    """A ``compactions`` row naming ``m3`` would be unreadable the moment anything else was."""
    messages, ids = _wire()
    assert to_transcript(messages, ids).turn_ids() == tuple(ids)


def test_the_framing_block_is_pinned_and_nothing_else_is() -> None:
    messages, ids = _wire()
    transcript = to_transcript(messages, ids)
    assert [turn.pinned for turn in transcript.turns[:2]] == [True, True]
    assert not any(turn.pinned for turn in transcript.turns[2:])


def test_an_exchange_is_one_correlation_id_over_the_call_and_its_results() -> None:
    messages, ids = _wire()
    transcript = to_transcript(messages, ids)
    # turns 2..4 are the first exchange: the assistant and the two results it produced.
    assert {turn.tool_call_id for turn in transcript.turns[2:5]} == {ids[2]}


def test_a_tool_call_argument_is_counted_against_the_budget() -> None:
    """A call with a large argument body costs what it costs on the wire."""
    bare = Message(role="assistant", content="x")
    heavy = Message(
        role="assistant",
        content="x",
        tool_calls=(RequestedToolCall("c", "read_file", {"path": "y" * 800}, True),),
    )
    assert (
        to_transcript([heavy], ["t"]).token_estimate()
        > to_transcript([bare], ["t"]).token_estimate()
    )


def test_the_two_sequences_must_be_the_same_length() -> None:
    with pytest.raises(ValidationError):
        to_transcript([Message(role="user", content="x")], [])


def test_a_masked_turn_keeps_its_role_and_its_call_id() -> None:
    messages, ids = _wire()
    transcript = to_transcript(messages, ids)
    plan = ObservationMaskingPolicy(keep_recent_results=0).decide(
        transcript, CompactionBudget(max_tokens=200, protected_recent_turns=1)
    )
    view = CompactionExecutor().apply(transcript, plan)
    out = to_messages(view.transcript, messages, ids)
    masked = [message for message in out if message.role == "tool"]
    assert masked, "the masking policy masked nothing, so this test proves nothing"
    assert all(message.tool_call_id is not None for message in masked)
    assert all("A" * 400 != message.content for message in masked)


# --------------------------------------------------------------------------------------------
# The trap: a call and its result are never separated, asserted on the wire
# --------------------------------------------------------------------------------------------


def test_a_dropped_assistant_turn_takes_its_tool_results_with_it() -> None:
    """Exit condition 4, at the ``Message`` level.

    Every ``tool_call_id`` on a surviving ``TOOL`` message must be named by a surviving assistant
    message's ``tool_calls``. If a compaction dropped an assistant turn and kept its results, the
    ids would still be well-formed and LoadCoach would still accept them — every result answering
    a call that is no longer there, or worse, the wrong one.
    """
    messages, ids = _wire()
    transcript = to_transcript(messages, ids)
    plan = DropOldestPolicy().decide(
        transcript, CompactionBudget(max_tokens=500, protected_recent_turns=2)
    )
    view = CompactionExecutor().apply(transcript, plan)
    out = to_messages(view.transcript, messages, ids)

    dropped = set(view.report.dropped_turn_ids)
    assert dropped, "nothing was dropped, so this test proves nothing"
    assert any(messages[ids.index(turn_id)].role == "assistant" for turn_id in dropped), (
        "no assistant turn was dropped, so the mapping was never put under pressure"
    )

    offered = {call.call_id for message in out for call in message.tool_calls if message.tool_calls}
    answered = {
        message.tool_call_id for message in out if message.role == "tool" and message.tool_call_id
    }
    assert answered <= offered, f"orphaned tool results: {sorted(answered - offered)}"


# --------------------------------------------------------------------------------------------
# The trigger, the budget and the tier
# --------------------------------------------------------------------------------------------


def test_the_trigger_is_strictly_above_the_threshold() -> None:
    tier = _tier("local_fast", budget=100)
    settings = CompactionSettings(threshold=0.8)
    exactly = to_transcript([Message(role="user", content="c" * 320)], ["t"])
    assert exactly.token_estimate() == 80
    assert not should_compact(exactly, tier, settings)
    assert should_compact(
        to_transcript([Message(role="user", content="c" * 324)], ["t"]), tier, settings
    )


def test_the_trigger_line_and_the_compaction_target_are_one_figure() -> None:
    """Headroom: compacting to the whole tier budget leaves none, and triggers on every turn."""
    tier = _tier("local_fast", budget=16384)
    settings = CompactionSettings()
    assert target_tokens(tier, settings) == 13107
    assert budget_for(tier, settings).max_tokens == 13107


def test_the_chain_is_built_in_the_configured_order() -> None:
    chain = build_chain(CompactionSettings(policy_chain=("drop_oldest", "observation_masking")))
    assert [policy.name for policy in chain.policies] == ["drop_oldest", "observation_masking"]


def test_the_summarizing_tier_is_the_cheapest_admissible_local_one() -> None:
    tiers = (
        _tier("local_large", budget=32768),
        _tier("local_fast", budget=16384),
        _tier("remote", budget=128000, remote=True, ceiling="internal"),
    )
    chosen = summarizing_tier(tiers, ceiling=DataClassification.CONFIDENTIAL, step_tier=tiers[0])
    assert chosen.name == "local_fast"


def test_no_local_tier_means_a_typed_refusal_and_never_a_remote_summary() -> None:
    """A summary of confidential turns must not itself become egress (lifecycle §7)."""
    remote_only = (_tier("remote", budget=128000, remote=True, ceiling="internal"),)
    with pytest.raises(CompactionFailedError) as caught:
        summarizing_tier(remote_only, ceiling=DataClassification.INTERNAL, step_tier=remote_only[0])
    assert caught.value.details["reason"] == "no_admissible_local_tier"
