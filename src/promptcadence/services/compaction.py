"""promptcadence.services.compaction — the wire and CutCtx's transcript, and when to compact.

Pure: values in, values out, no database, no framework, no clock. Every function here could live
in ``domain`` on that count, and does not for one reason — it speaks in
:class:`~promptcadence.infrastructure.loadcoach.Message`, the wire type, and ``domain`` imports no
infrastructure. Purity is not a layer.

The half that needs rows — the superseding intent, the summarization turn, the debit and the
``compactions`` row — is :class:`~promptcadence.services.loop.LoopController`'s, because that is
where the collaborators already are.

**Where compaction acts, and why it is here rather than one layer earlier.** The loop maps recorded
turns onto :class:`~promptcadence.infrastructure.loadcoach.Message` s once, over the *complete*
recorded sequence — complete always, because compaction is a view and never deletes a row — and
that mapping resolves tool-call ids **positionally**: an assistant turn supplies a pending list of
call ids and the ``TOOL`` turns that follow pop from it in order. Drop an assistant turn while
keeping its tool turns and every result answers the wrong call, with ids well-formed enough that
LoadCoach accepts them.

Compaction therefore acts on the mapped messages, after the ids are already on them. By then the
mapping is materialized per message and cannot shift, whatever a policy removes. CutCtx's exchange
invariant — a call and its results travel together — sits on top of that as the second guarantee,
not the only one.

Two conventions carry the mapping back:

* **A message's turn id is the recorded turn's own id.** Not its position: a plan names every turn
  it acted on, and those names go into the ``compactions`` row and the ``context.compacted``
  event, where a positional id would be meaningless the moment anything else was read. The loop
  hands the ids in beside the messages — one message per turn, and turns whose message the wire
  omits are simply not there.
* **An exchange id is the id of the assistant turn that opened it.** Every ``TOOL`` message
  answering that assistant's calls carries the same value, which is what CutCtx's contract 3 needs
  to keep them together — the correlation is per *exchange*, not per call (an assistant issuing
  three calls and the three results it produced are one unit).

What is pinned is the **framing block**: every message before the first assistant message. That is
the caller's task and the step's framing, and it is exactly the part whose loss would leave the
model executing a step it can no longer read. Nothing else is pinned; the protected recent tail is
:class:`~cutctx.CompactionBudget`'s job, not this module's.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final

from baseaicore import ValidationError, canonical_json
from cutctx import (
    SUMMARY_TURN_ID_PREFIX,
    CharRatioEstimator,
    CompactionBudget,
    DropOldestPolicy,
    ObservationMaskingPolicy,
    PolicyChain,
    Role,
    SummarizingPolicy,
    Transcript,
    TranscriptTurn,
)

from promptcadence.domain.errors import CompactionFailedError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from baseaicore import DataClassification
    from cutctx import CompactionPolicy, TokenEstimator

    from promptcadence.config import CompactionSettings
    from promptcadence.domain.tiers import Tier
    from promptcadence.infrastructure.loadcoach import Message, RequestedToolCall

__all__ = [
    "COMPACTION_STEP_PREFIX",
    "COMPACTION_SUMMARIZE_PROMPT_ID",
    "DEFAULT_ESTIMATOR",
    "POLICY_NAMES",
    "budget_for",
    "build_chain",
    "should_compact",
    "summarizing_tier",
    "target_tokens",
    "to_messages",
    "to_transcript",
]

COMPACTION_STEP_PREFIX: Final = "compaction:"
"""What a compaction thread's ``threads.step_id`` starts with (ADR-0091 rule 3).

A prefix rather than a nullable column or a boolean: every reader of ``threads`` already has to
know a step id, the bypass path's synthetic ``loop`` set the precedent (ADR-0056 §1), and the
explanation can tell a compaction thread from a step thread without a join."""

COMPACTION_SUMMARIZE_PROMPT_ID: Final = "compaction.summarize"
"""The versioned record the summarization runs under (ADR-0012). Named here rather than in
``services.prompts`` because :class:`~cutctx.SummarizingPolicy` puts it on every request it emits,
so the policy and the pack must agree and one place should say what they agree on."""

POLICY_NAMES: Final = ("observation_masking", "summarizing", "drop_oldest")
"""The names ``[compaction] policy_chain`` may hold, in CutCtx's own vocabulary."""

DEFAULT_ESTIMATOR: TokenEstimator = CharRatioEstimator()
"""The documented default (CutCtx spec §3): characters over four, rounded up.

PromptCadence ships no tokenizer either, and LoadCoach does not report one it could borrow — the
usage on a response is the provider's count of a request already sent, not a function this side can
call before sending. The ratio rides on every plan, so a reader can tell an estimate from a count
(ADR-0016).
"""


def _arguments_text(call: RequestedToolCall) -> str:
    """One call's arguments as the wire carries them, for estimating what they cost.

    ``RequestedToolCall.arguments`` is the parsed object when the model produced JSON and the raw
    text when it did not — deliberately unvalidated, since a constructor that refused bad model
    input would hand the model a way to end the turn. Both forms cost tokens, so both are rendered.
    """
    if call.arguments_parsed:
        return canonical_json(call.arguments)
    return call.arguments if isinstance(call.arguments, str) else repr(call.arguments)


def to_transcript(
    messages: Sequence[Message],
    turn_ids: Sequence[str],
    *,
    estimator: TokenEstimator = DEFAULT_ESTIMATOR,
) -> Transcript:
    """Map the wire's messages onto a CutCtx transcript, oldest first.

    Args:
        messages: The messages as the loop would send them, in order.
        turn_ids: The recorded turn each message came from, positionally aligned with ``messages``.
        estimator: How a message's token cost is estimated. Injected so a deployment whose provider
            exposes a tokenizer can supply it, and so a test can make the arithmetic exact.

    Returns:
        The transcript. Turn ids are the recorded ids, exchange ids name the assistant turn that
        opened the exchange, and every message before the first assistant message is pinned.

    Raises:
        ValidationError: If the two sequences are not the same length. That can only mean the
            caller built one of them from something other than the other, and a transcript whose
            ids belong to different turns is worse than no compaction at all.

    Estimating a message counts its content **and** the arguments of any tool calls it carries: a
    call with a large argument body costs what it costs on the wire, and an estimate that ignored
    it would let a transcript pass a budget it does not fit.
    """
    if len(messages) != len(turn_ids):
        message = (
            f"to_transcript needs one turn id per message; got {len(turn_ids)} ids for "
            f"{len(messages)} messages"
        )
        raise ValidationError(
            message, details={"messages": len(messages), "turn_ids": len(turn_ids)}
        )
    first_assistant = next(
        (index for index, item in enumerate(messages) if item.role == Role.ASSISTANT.value),
        len(messages),
    )
    turns: list[TranscriptTurn] = []
    exchange: str | None = None
    for index, (turn_id, item) in enumerate(zip(turn_ids, messages, strict=True)):
        text = item.content or ""
        cost = estimator.estimate_tokens(text)
        if item.role == Role.ASSISTANT.value:
            exchange = turn_id if item.tool_calls else None
            for call in item.tool_calls:
                cost += estimator.estimate_tokens(call.name)
                cost += estimator.estimate_tokens(_arguments_text(call))
        turns.append(
            TranscriptTurn(
                turn_id=turn_id,
                role=Role(item.role),
                content=text,
                token_estimate=cost,
                tool_call_id=exchange if item.role in _EXCHANGE_ROLES else None,
                pinned=index < first_assistant,
            )
        )
    return Transcript(tuple(turns))


_EXCHANGE_ROLES = frozenset({Role.ASSISTANT.value, Role.TOOL.value})


def to_messages(
    compacted: Transcript, messages: Sequence[Message], turn_ids: Sequence[str]
) -> tuple[Message, ...]:
    """Map a compacted transcript back onto the wire, by id and never by position.

    Args:
        compacted: The view CutCtx produced.
        messages: The messages :func:`to_transcript` was given, so a kept or masked turn recovers
            its role, its ``tool_call_id`` and its ``tool_calls`` verbatim.
        turn_ids: The same ids that went in, aligned with ``messages``.

    Returns:
        The messages to send, in the compacted transcript's order.

    A **masked** turn keeps everything but its body: the placeholder CutCtx wrote replaces the
    content and nothing else, so a masked tool result is still the same exchange's member and still
    answers the call it answered. A **summary** turn has no original — it is a new assistant message
    carrying the fulfilled text and no tool calls, which is what
    :class:`~cutctx.CompactionExecutor` already decided it is (never ``SYSTEM``, never pinned, or
    summaries would accumulate until they were the only thing a policy could not reduce).

    Raises:
        KeyError: A turn id that is neither a summary nor one of ``turn_ids``. That can only mean
            the transcript and the messages came from different builds, which would produce a wire
            nobody can reason about, so it fails rather than skipping the turn.
    """
    by_id = dict(zip(turn_ids, messages, strict=True))
    out: list[Message] = []
    for turn in compacted.turns:
        if turn.turn_id.startswith(SUMMARY_TURN_ID_PREFIX):
            out.append(
                replace(
                    messages[0],
                    role=Role.ASSISTANT.value,
                    content=turn.content,
                    tool_call_id=None,
                    tool_calls=(),
                )
            )
            continue
        source = by_id[turn.turn_id]
        out.append(
            source
            if turn.content == (source.content or "")
            else replace(source, content=turn.content)
        )
    return tuple(out)


def build_chain(settings: CompactionSettings) -> PolicyChain:
    """Build the policy chain ``[compaction] policy_chain`` names, in the configured order.

    Args:
        settings: The ``[compaction]`` section.

    Returns:
        The chain. Order is the operator's, not this function's — CutCtx's ``default_chain``
        encodes an opinion about cost (mask, then summarize, then drop) and the configuration
        defaults to exactly that order, but a deployment that reorders it gets what it asked for.

    Raises:
        ValidationError: If a name is not one of :data:`POLICY_NAMES`. Refused at construction so
            a typo is a startup failure rather than a compaction that silently skipped a policy.
    """
    members: list[CompactionPolicy] = []
    for name in settings.policy_chain:
        if name == "observation_masking":
            members.append(ObservationMaskingPolicy())
        elif name == "summarizing":
            members.append(SummarizingPolicy(prompt_id=COMPACTION_SUMMARIZE_PROMPT_ID))
        elif name == "drop_oldest":
            members.append(DropOldestPolicy())
        else:  # pragma: no cover — config validation refuses it first
            message = f"unknown compaction policy {name!r}; expected one of {POLICY_NAMES}"
            raise ValidationError(message, details={"policy": name, "known": list(POLICY_NAMES)})
    return PolicyChain(members)


def target_tokens(tier: Tier, settings: CompactionSettings) -> int:
    """``threshold × context_budget_tokens``, floored — the one figure compaction uses twice.

    It is both the line a transcript crosses to trigger a compaction and the size the compaction
    brings it back to, and it is **one** number on purpose. The two obvious alternatives are both
    worse, and the difference is headroom:

    * Trigger at the threshold and compact to the **whole tier budget**, and a transcript sitting
      between the two triggers a compaction that has nothing to do — writing a ``compactions`` row
      per turn saying nothing changed — and then, once it does exceed the budget, is brought back
      to exactly the budget with no slack, so every following turn compacts again.
    * Trigger and compact at the whole tier budget, and there is no threshold at all; the first
      request over the line is the one that gets refused.

    Compacting to the threshold leaves the gap between it and the tier budget as the margin, and
    the reduction itself as the hysteresis: a drop or a summary removes whole turns, so the next
    compaction is several turns away rather than on the next one.
    """
    return int(settings.threshold * tier.context_budget_tokens)


def budget_for(tier: Tier, settings: CompactionSettings) -> CompactionBudget:
    """The window a compacted transcript must fit, and the tail no policy may touch."""
    return CompactionBudget(
        max_tokens=target_tokens(tier, settings),
        protected_recent_turns=settings.protected_recent_turns,
    )


def should_compact(transcript: Transcript, tier: Tier, settings: CompactionSettings) -> bool:
    """Whether the transcript has outgrown :func:`target_tokens` (lifecycle §7).

    Strictly greater than, matching the spec's "compact when estimate > 0.8 × tier context
    budget": a transcript exactly at the target has not crossed it, and is therefore also exactly
    what a compaction would leave behind.
    """
    return transcript.token_estimate() > target_tokens(tier, settings)


def summarizing_tier(
    tiers: Sequence[Tier], *, ceiling: DataClassification, step_tier: Tier
) -> Tier:
    """The cheapest admissible **local** tier a compaction summary may run on (ADR-0090).

    Args:
        tiers: The trajectory's configured tiers, from its recorded snapshot.
        ceiling: The step intent's ``max_classification``. The summary inherits it, so a tier that
            could not serve the step's own classification cannot serve its summary either.
        step_tier: The tier the step itself runs on, used only to name it in the refusal.

    Returns:
        The admissible local tier with the smallest ``context_budget_tokens``, ties broken by
        name. "Cheapest" is proxied by context window because PromptCadence performs no routing
        math of its own (ADR-0047) — which model serves a tier is LoadCoach's filter/score/rank,
        and the only cost signal this side holds is how much window the operator gave the tier.

    Raises:
        CompactionFailedError: If no local tier admits the ceiling. There is deliberately no fall
            back to a remote tier: a summary of confidential turns must not itself become egress
            (lifecycle §7), and the whole point of choosing a tier here is that the choice is not
            "whatever was already running".
    """
    admissible = sorted(
        (tier for tier in tiers if not tier.is_remote and tier.admits(ceiling)),
        key=lambda tier: (tier.context_budget_tokens, tier.name),
    )
    if not admissible:
        message = (
            f"no local tier admits {ceiling.value} data, so the transcript on tier "
            f"{step_tier.name} cannot be summarized without turning a summary of confidential "
            "turns into egress (ADR-0090)"
        )
        raise CompactionFailedError(
            message,
            details={
                "reason": "no_admissible_local_tier",
                "classification": ceiling.value,
                "step_tier": step_tier.name,
                "considered": [tier.name for tier in tiers],
            },
        )
    return admissible[0]
