"""promptcadence.domain.compaction — the account one compaction leaves behind.

Compaction is a **view, never a deletion** (lifecycle §7,
[ADR-0052](../../adr/0052-compaction-is-a-view-and-the-package-plans-it-only.md)): every original
turn stays in ``turns``, and what changes is only what goes on the wire. This module holds the
record of that difference — the ``context.compacted`` body — because without it a reader cannot
tell why turn 40 saw less history than turn 39, and a transcript that shrank looks exactly like one
the model was never shown.

The body names **turn ids, counts and estimates, and nothing else**: it is replayed over SSE,
written to logs and rendered in a browser, so a body carrying the summarized text would put a
confidential trajectory's content on all three — and on the trajectory the operator was most
careful about. The summary itself is a turn row like any other, reachable from
``summary_turn_id``, under the retention rules every turn's content follows.

The figures are **estimates, never counts** (ADR-0016). CutCtx ships no tokenizer and neither does
this application; ``estimator_ratio`` is what produced them and rides along so a reader can tell
the difference.

The wire mapping and the trigger live in :mod:`promptcadence.services.compaction` — they speak in
the wire's ``Message``, which is infrastructure, and ``domain`` imports none.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from promptcadence.domain.events import EventType

__all__ = ["ContextCompacted"]


@dataclass(frozen=True, slots=True)
class ContextCompacted:
    """``context.compacted`` — written in the same transaction as the ``compactions`` row.

    Attributes:
        trajectory_id: The trajectory.
        compaction_id: The ``compactions`` row this announces.
        thread_id: The step thread whose wire changed. Compaction is per step thread (lifecycle
            §7), so a parallel sibling's transcript is untouched by this one.
        step_id: That thread's step, or ``loop`` on the bypass path.
        tier: The tier whose ``context_budget_tokens`` the target was taken from.
        budget_tokens: ``threshold × context_budget_tokens`` — the line the transcript crossed,
            and the size it was brought back to. One figure, not two: see
            :func:`~promptcadence.services.compaction.target_tokens` for why the alternatives
            leave either no headroom or nothing to do.
        threshold: The configured fraction the target was taken at.
        policy_name: CutCtx's composed chain name, naming every constituent and its version — a
            report saying only "chain" leaves an auditor unable to tell which policies produced
            the view they are looking at.
        policy_version: The chain's own composition version.
        plan_hash: CutCtx's canonical hash of the plan, so two composes of the same rows name the
            same decision.
        tokens_before: The estimate before.
        tokens_after_estimate: The estimate after.
        turns_before: How many messages the wire held.
        turns_after: How many it holds now.
        budget_unmet: Whether the chain ran out of policies before the budget was met. ``True``
            here is not a failure — the wire is smaller and the request may still fit — but it is
            the thing to look at when the next turn is refused for context.
        masked_turn_ids: The turns whose bodies were replaced by a stub.
        summarized_turn_ids: The turns folded into a summary.
        dropped_turn_ids: The turns the wire no longer carries at all.
        summary_turn_id: The turn the summarization ran as, or ``None`` when the chain fitted the
            budget without one.
        summary_intent_revision: The superseding revision that summary executed under (ADR-0090),
            or ``None`` for the same reason.
    """

    event_type: ClassVar[EventType] = EventType.CONTEXT_COMPACTED
    trajectory_id: str
    compaction_id: str
    thread_id: str
    step_id: str
    tier: str
    budget_tokens: int
    threshold: float
    policy_name: str
    policy_version: str
    plan_hash: str
    tokens_before: int
    tokens_after_estimate: int
    turns_before: int
    turns_after: int
    budget_unmet: bool
    masked_turn_ids: tuple[str, ...]
    summarized_turn_ids: tuple[str, ...]
    dropped_turn_ids: tuple[str, ...]
    summary_turn_id: str | None
    summary_intent_revision: int | None

    def as_canonical(self) -> dict[str, Any]:
        """Return the persisted and streamed mapping form."""
        return {
            "trajectory_id": self.trajectory_id,
            "compaction_id": self.compaction_id,
            "thread_id": self.thread_id,
            "step_id": self.step_id,
            "tier": self.tier,
            "budget_tokens": self.budget_tokens,
            "threshold": self.threshold,
            "policy_name": self.policy_name,
            "policy_version": self.policy_version,
            "plan_hash": self.plan_hash,
            "tokens_before": self.tokens_before,
            "tokens_after_estimate": self.tokens_after_estimate,
            "turns_before": self.turns_before,
            "turns_after": self.turns_after,
            "budget_unmet": self.budget_unmet,
            "masked_turn_ids": list(self.masked_turn_ids),
            "summarized_turn_ids": list(self.summarized_turn_ids),
            "dropped_turn_ids": list(self.dropped_turn_ids),
            "summary_turn_id": self.summary_turn_id,
            "summary_intent_revision": self.summary_intent_revision,
        }
