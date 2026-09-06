"""promptcadence.domain.explanation — the composed record, assembled and nothing else.

Spec §11 contract 2: every trajectory yields a complete record — the plan when planned, the
approvals, every intent revision, every turn with its intent reference and LoadCoach explanation
reference, every tool call, every ledger entry, every egress decision, every deviation, every
compaction — in turn order, retrievable for the lifetime of the trajectory. This module is the
**assembler**: it takes sections already read from the rows and returns the document. It reads
nothing, opens nothing, and never asks the clock.

That split is what makes the equality golden meaningful. `materialize(rows) == compose_live(rows)`
is a **byte** equality (ADR-0093), so every source of variation has to be somewhere it can be
seen:

* **Key order is this module's**, fixed by the literal that builds each mapping.
* **Timestamps are rendered from stored values**, by the caller, before they arrive here. Nothing
  in this file can reach for ``now``, because nothing in this file imports a clock.
* **No ``set`` reaches a serialized position.** Where a section holds an unordered collection, the
  caller sorts it; a set serialized as a list is the classic intermittently-failing golden.
* **Sections arrive in order** and are copied in order. Ordering is the caller's, because the
  caller is the one with the ``ORDER BY``.

**Content and its absence.** Model output — turn text, plan documents, tool arguments — is subject
to the retention sweep, and an explanation that survived a scrub by keeping its own copy would
have defeated the scrub. So the document carries whatever the rows carry: text when the row still
has it, and a labelled stub with the digest when it does not. :func:`content_or_removed` is that
one rule, in one place, so no section can quietly implement it differently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "CONTENT_REMOVED",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "compose",
    "content_or_removed",
]

SCHEMA_NAME: Final = "promptcadence.trajectory_explanation"
"""The application-owned document's name (ADR-0035). Not a SetSpec payload: no other application
reads a PromptCadence trajectory record in v1 (roadmap §2, D-7). The egress decisions *inside* it
are SetSpec ``governance.egress_decision`` payloads, because IdeaPress's badge is a named second
consumer of that shape."""

SCHEMA_VERSION: Final = "1.0"
"""``MAJOR.MINOR``. A minor bump invalidates every materialized revision and re-materializes it
(lifecycle §9.1); a major bump is a new document."""

CONTENT_REMOVED: Final = "content removed by retention"
"""What stands where text used to be. A sentence rather than ``None``, because a reader must be
able to tell "this turn said nothing" from "this turn's words are gone"."""


def content_or_removed(text: str | None, digest: str | None) -> dict[str, Any]:
    """Render one piece of retained-or-scrubbed content.

    Args:
        text: The stored text, or ``None`` after a retention sweep.
        digest: The digest kept alongside it, which outlives the text.

    Returns:
        ``{"text": …, "removed": False, "sha256": …}`` while the row still holds the words, and
        ``{"text": "content removed by retention", "removed": True, "sha256": …}`` once it does
        not — the digest either way, so a scrubbed trajectory still proves what it contained.

    An **empty string is content**, not an absence: a turn that genuinely said nothing is a fact
    about the model, and folding it into the scrub stub would report a scrub that never happened.
    """
    if text is None:
        return {"text": CONTENT_REMOVED, "removed": True, "sha256": digest}
    return {"text": text, "removed": False, "sha256": digest}


def compose(
    *,
    trajectory: Mapping[str, Any],
    plan: Mapping[str, Any] | None,
    intents: Sequence[Mapping[str, Any]],
    approvals: Sequence[Mapping[str, Any]],
    threads: Sequence[Mapping[str, Any]],
    tool_calls: Sequence[Mapping[str, Any]],
    compactions: Sequence[Mapping[str, Any]],
    ledger_entries: Sequence[Mapping[str, Any]],
    egress_decisions: Sequence[Mapping[str, Any]],
    deviations: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Assemble the ``promptcadence.trajectory_explanation`` document.

    Args:
        trajectory: The request and its outcome — the task, classification, budgets, state, the
            tier snapshot it ran under, and the terminal cause when there is one.
        plan: The plan record (every drafting attempt, the validated steps with their execution
            state, and the verdict), or ``None`` for a bypassed trajectory. ``None`` and not an
            empty mapping: contract 1 says the two paths differ in exactly this, so the document
            says so in the same shape.
        intents: Every ``ExecutionIntent`` revision, superseded ones included, in
            ``(step_id, intent_id, revision)`` order.
        approvals: Every approval request with its resolution, oldest first.
        threads: One entry per thread, oldest first, each carrying its turns in sequence order. A
            compaction's summary thread is a thread like any other and appears here with its
            ``kind`` naming it.
        tool_calls: Every recorded call — including refusals and failures — oldest first.
        compactions: Every compaction, oldest first.
        ledger_entries: Every debit with the ceiling verdicts as of that debit, oldest first.
        egress_decisions: Every ``governance.egress_decision`` payload, oldest first.
        deviations: Every deviation, oldest first.
        events: Every persisted event in sequence order.

    Returns:
        The document. Top-level keys in the order written here, and every section a list or a
        mapping — never a set, never an iterator.

    Refuses nothing. Every section is already the rows as they are; a trajectory that halted before
    it planned has an empty plan and empty everything else, and that is a complete record of a
    trajectory that did nothing.
    """
    return {
        "schema": SCHEMA_NAME,
        "version": SCHEMA_VERSION,
        "trajectory": dict(trajectory),
        "plan": dict(plan) if plan is not None else None,
        "intents": [dict(item) for item in intents],
        "approvals": [dict(item) for item in approvals],
        "threads": [dict(item) for item in threads],
        "tool_calls": [dict(item) for item in tool_calls],
        "compactions": [dict(item) for item in compactions],
        "ledger_entries": [dict(item) for item in ledger_entries],
        "egress_decisions": [dict(item) for item in egress_decisions],
        "deviations": [dict(item) for item in deviations],
        "events": [dict(item) for item in events],
    }
