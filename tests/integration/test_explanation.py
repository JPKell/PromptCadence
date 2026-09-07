"""The composed explanation, and the proof that its materialized revision is only a cache.

Spec §11 contract 2 in one file. Two claims are load-bearing and are asserted rather than argued:

* **The document is complete** — every model, tier, tool call, debit and egress verdict a
  trajectory produced is in it (spec §20 #2's explanation clause).
* **The revision is a derived cache** — `materialize(rows) == compose_live(rows)` byte for byte,
  the whole table can be deleted mid-run without changing a single answer, and a rebuild over an
  intact cache writes nothing.

The golden is over a **masked** document: ids are ULIDs and timestamps are instants, so comparing
them verbatim would test the clock. What the golden holds is the shape, the key order and every
value that is not an identifier — which is what a schema golden is for.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from baseaicore import canonical_json
from sqlalchemy import select, update
from tests.fakes.harness import LoopHarness, open_harness, plan_document, step
from tests.fakes.loadcoach_app import ScriptedError, ScriptedGeneration

from promptcadence.config import load_settings
from promptcadence.domain.explanation import CONTENT_REMOVED, SCHEMA_NAME, SCHEMA_VERSION
from promptcadence.infrastructure.db import models
from promptcadence.services.explanation import ExplanationBuilder

_GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "trajectory_explanation.json"
_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_SHA = re.compile(r"^(sha256:)?[0-9a-f]{64}$")

# `duration_ms` is a real wall-clock measurement — `int(round(elapsed))` over ToolYard's monotonic
# source — so it is 0 on a machine that runs a tool call in under a millisecond and 1 on one that
# does not. It is masked by *name* because it is an `int`, where the sibling timings
# (`loadcoach_ms`, `overhead_ms`) are floats and the branch below already catches them by type.
# Pinning it made this golden a benchmark of the runner rather than a check on the document.
_TIMING_KEYS = frozenset({"duration_ms"})


def _call(name: str, arguments: str) -> dict[str, object]:
    return {"call_index": 0, "id": "c0", "name": name, "arguments_fragment": arguments}


def _mask(value: Any) -> Any:
    """Identifiers, instants, digests and timings become placeholders; structure stays."""
    if isinstance(value, dict):
        return {
            key: ("<ms>" if key in _TIMING_KEYS else _mask(item)) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask(item) for item in value]
    if isinstance(value, str):
        if _ULID.match(value):
            return "<id>"
        if value.startswith("compaction:") and _ULID.match(value.removeprefix("compaction:")):
            return "compaction:<id>"
        if _ISO.match(value):
            return "<at>"
        if _SHA.match(value):
            return "<sha>"
        if value.startswith("job-") or value.startswith("01DECISION"):
            return "<ref>"
    if isinstance(value, float):
        return "<ms>"
    return value


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopHarness]:
    monkeypatch.setenv("PROMPTCADENCE_COMPACTION__PROTECTED_RECENT_TURNS", "1")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_FAST__TASK_PROFILE", "tools.agent.local_fast")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_FAST__REMOTE", "false")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_FAST__CONTEXT_BUDGET_TOKENS", "260")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_LARGE__TASK_PROFILE", "tools.agent.local_large")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_LARGE__REMOTE", "false")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_LARGE__CONTEXT_BUDGET_TOKENS", "4096")
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__STEP_RETRIES", "1")
    with open_harness(load_settings().settings) as built:
        yield built


def _builder(harness: LoopHarness) -> ExplanationBuilder:
    return harness.controller().explanations


def _rich(harness: LoopHarness) -> str:
    """A planned trajectory holding as many record types as one journey can produce.

    A drafting attempt and its verdict, a retried step, a tool call, a refused tool (the
    ``undeclared_tool`` deviation), a compaction, debits, egress decisions, and a declared finish.
    """
    harness.script_plan(plan_document(step("s1", tools=["list_dir"])))
    harness.script(
        ScriptedError("PROVIDER_TIMEOUT"),
        ScriptedGeneration(text="Looking." * 40, tool_calls=(_call("list_dir", '{"path": "."}'),)),
        ScriptedGeneration(
            text="Trying harder." * 40, tool_calls=(_call("read_file", '{"path": "x"}'),)
        ),
        ScriptedGeneration(text="Nearly." * 40, tool_calls=(_call("list_dir", '{"path": "."}'),)),
        ScriptedGeneration(text="The directory holds nothing of note."),
    )
    trajectory_id = harness.submit_planned()
    harness.claim_and_run(trajectory_id)
    return trajectory_id


# --------------------------------------------------------------------------------------------
# Completeness
# --------------------------------------------------------------------------------------------


def test_the_document_names_every_model_tier_tool_call_debit_and_egress_verdict(
    harness: LoopHarness,
) -> None:
    """Exit condition 6, and spec §20 #2's explanation clause."""
    trajectory_id = _rich(harness)
    document = _builder(harness).compose_live(trajectory_id)

    assert document["schema"] == SCHEMA_NAME
    assert document["version"] == SCHEMA_VERSION
    assert document["trajectory"]["trajectory_id"] == trajectory_id
    assert document["plan"] is not None, "a planned trajectory's plan is in its explanation"
    assert document["plan"]["attempts"], "every drafting attempt, valid or not"
    assert document["intents"], "every intent revision, superseded ones included"

    turns = [turn for thread in document["threads"] for turn in thread["turns"]]
    answered = [turn for turn in turns if turn["role"] == "assistant"]
    assert answered
    assert all(turn["model"]["canonical_id"] for turn in answered), "every model"
    assert all(turn["tier"] for turn in answered), "every tier"
    assert all(turn["usage"] for turn in answered), "every TokenUsage"
    assert all(turn["loadcoach_job_id"] for turn in answered), "the LoadCoach explanation reference"

    assert document["tool_calls"], "every tool call, refusals included"
    assert document["ledger_entries"], "every debit"
    assert all(entry["ceilings"] for entry in document["ledger_entries"]), "with its balances"
    assert document["egress_decisions"], "every egress verdict"
    assert all(one["verdict"] for one in document["egress_decisions"])
    assert document["events"], "every persisted event"

    # The lease is a runtime fact, not part of the record of what happened.
    assert "lease" not in document["trajectory"]
    # The snapshot travels with the record, so an edited ceiling cannot rewrite history.
    assert document["trajectory"]["tier_snapshot"] is not None


def test_a_retried_step_reports_its_attempts_from_the_events_not_the_turns(
    harness: LoopHarness,
) -> None:
    """A failed attempt has a ``turn.started`` event and no ``turns`` row (G3 §7)."""
    trajectory_id = _rich(harness)
    document = _builder(harness).compose_live(trajectory_id)
    attempts = [one for thread in document["threads"] for one in thread["failed_attempts"]]
    assert attempts, "the fixture's first answer is a retryable failure"
    assert all(one["failed_turn_id"] for one in attempts), "joined by id, never by adjacency"
    assert all(one["cause"] for one in attempts)
    recorded = {turn["turn_id"] for thread in document["threads"] for turn in thread["turns"]}
    assert not {one["failed_turn_id"] for one in attempts} & recorded


def test_the_compaction_and_its_summary_thread_are_both_in_the_document(
    harness: LoopHarness,
) -> None:
    """Otherwise a reader cannot tell why one turn saw less history than the turn before it."""
    trajectory_id = _rich(harness)
    document = _builder(harness).compose_live(trajectory_id)
    assert document["compactions"], "the fixture's tier is small enough to compact"
    kinds = {thread["kind"] for thread in document["threads"]}
    summarized = any(one["summary_turn_id"] for one in document["compactions"])
    assert ("compaction" in kinds) == summarized


# --------------------------------------------------------------------------------------------
# The golden
# --------------------------------------------------------------------------------------------


def test_the_trajectory_explanation_golden(harness: LoopHarness) -> None:
    """The document's shape and key order, masked of everything that is an identifier."""
    trajectory_id = _rich(harness)
    produced = canonical_json(_mask(_builder(harness).compose_live(trajectory_id))) + "\n"
    if not _GOLDEN.exists():  # pragma: no cover — first run writes the golden
        _GOLDEN.write_text(produced, encoding="utf-8")
    assert produced == _GOLDEN.read_text(encoding="utf-8")


def test_two_composes_of_the_same_rows_are_byte_identical(harness: LoopHarness) -> None:
    """Determinism is the deliverable: the equality golden is only as strong as this."""
    trajectory_id = _rich(harness)
    builder = _builder(harness)
    first = canonical_json(builder.compose_live(trajectory_id))
    second = canonical_json(builder.compose_live(trajectory_id))
    assert first == second


# --------------------------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------------------------


def test_materialize_equals_compose_live(harness: LoopHarness) -> None:
    """Exit condition 7, at its simplest: the cache holds exactly what the rows compose to."""
    trajectory_id = _rich(harness)
    builder = _builder(harness)
    read = builder.read(trajectory_id)
    assert read.source == "materialized", "the terminal transition materialized it"
    assert canonical_json(read.document) == canonical_json(builder.compose_live(trajectory_id))


def test_dropping_every_revision_changes_no_answer(harness: LoopHarness) -> None:
    """Exit condition 8, and ADR-0093 rule 4: a cache whose deletion changes an answer is none."""
    trajectory_id = _rich(harness)
    builder = _builder(harness)
    before = canonical_json(builder.read(trajectory_id).document)

    assert builder.drop_revisions() >= 1
    after = builder.read(trajectory_id)
    assert after.source == "live", "with no revision, the live path answers"
    assert canonical_json(after.document) == before

    considered, written = builder.rebuild(now=_NOW)
    assert (considered, written) == (1, 1)
    rebuilt = builder.read(trajectory_id)
    assert rebuilt.source == "materialized"
    assert canonical_json(rebuilt.document) == before


def test_a_rebuild_over_an_intact_cache_writes_nothing(harness: LoopHarness) -> None:
    """Which is the assertion that the cache was correct, not that the rebuild did nothing."""
    trajectory_id = _rich(harness)
    builder = _builder(harness)
    assert builder.read(trajectory_id).source == "materialized"
    assert builder.rebuild(now=_NOW) == (1, 0)


def test_materialize_is_idempotent(harness: LoopHarness) -> None:
    """The terminal transition's follow-up write and a later rebuild are not two revisions."""
    trajectory_id = _rich(harness)
    builder = _builder(harness)
    first = builder.materialize(trajectory_id, now=_NOW)
    second = builder.materialize(trajectory_id, now=_NOW)
    assert (first.revision, first.document_sha256) == (second.revision, second.document_sha256)


# --------------------------------------------------------------------------------------------
# Invalidation (ADR-0092: the entry point, not the sweep)
# --------------------------------------------------------------------------------------------


def _scrub(harness: LoopHarness, trajectory_id: str) -> None:
    """What Phase 9's sweep will do to the rows, done directly to the fixture.

    Transcript text, the plan document and the tool arguments together: ``plans.raw_document`` and
    ``turns.tool_calls_json`` are model output too, and an explanation that survived a scrub by
    keeping a copy of them would have defeated the scrub.
    """
    with harness.database.write() as session:
        session.execute(
            update(models.Turn)
            .where(models.Turn.trajectory_id == trajectory_id)
            .values(content_text=None, tool_calls_json=None)
        )
        session.execute(
            update(models.Plan)
            .where(models.Plan.trajectory_id == trajectory_id)
            .values(raw_document=None)
        )
        session.execute(
            update(models.ToolCallRecord)
            .where(models.ToolCallRecord.trajectory_id == trajectory_id)
            .values(args_json=None, result_summary=None)
        )


def test_a_scrubbed_trajectory_still_explains_itself_and_bumps_a_revision(
    harness: LoopHarness,
) -> None:
    """Exit conditions 7 and 8's second halves."""
    trajectory_id = _rich(harness)
    builder = _builder(harness)
    first = builder.read(trajectory_id).revision
    assert first is not None

    _scrub(harness, trajectory_id)
    revision = builder.invalidate(trajectory_id, cause="retention_scrub", now=_NOW)
    assert revision.revision == first.revision + 1
    assert revision.cause == "retention_scrub"

    read = builder.read(trajectory_id)
    assert read.source == "materialized"
    assert canonical_json(read.document) == canonical_json(builder.compose_live(trajectory_id))

    document = read.document
    turns = [turn for thread in document["threads"] for turn in thread["turns"]]
    assert all(turn["content"]["removed"] for turn in turns)
    assert all(turn["content"]["text"] == CONTENT_REMOVED for turn in turns)
    assert all(turn["content"]["sha256"] for turn in turns), "the digest outlives the words"
    assert all(turn["tool_calls"] == [] for turn in turns)
    assert document["plan"]["attempts"][0]["raw_document"] is None
    assert all(one["arguments"]["removed"] for one in document["tool_calls"])
    # And it still explains itself: the decisions, the numbers and the identities are all there.
    answered = [turn for turn in turns if turn["role"] == "assistant"]
    assert all(turn["model"]["canonical_id"] for turn in answered)
    assert document["ledger_entries"] and document["egress_decisions"]

    superseded = _revisions(harness, trajectory_id)
    assert [one.revision for one in superseded] == [1, 2]
    assert superseded[0].superseded_at is not None, "a revision is never edited"


def test_a_recosting_bumps_a_revision_and_the_equality_still_holds(
    harness: LoopHarness,
) -> None:
    """The second invalidator, tested the same way: change the rows, then call the entry point."""
    trajectory_id = _rich(harness)
    builder = _builder(harness)
    first = builder.read(trajectory_id).revision
    assert first is not None
    with harness.database.write() as session:
        session.execute(
            update(models.Turn)
            .where(models.Turn.trajectory_id == trajectory_id)
            .values(output_tokens=4321)
        )
    revision = builder.invalidate(trajectory_id, cause="recosting", now=_NOW)
    assert revision.revision == first.revision + 1
    read = builder.read(trajectory_id)
    assert canonical_json(read.document) == canonical_json(builder.compose_live(trajectory_id))


def test_an_invalidation_that_changed_nothing_leaves_no_trace(harness: LoopHarness) -> None:
    """A sweep that touched a trajectory it did not alter should not look like one that did."""
    trajectory_id = _rich(harness)
    builder = _builder(harness)
    before = builder.read(trajectory_id).revision
    assert before is not None
    after = builder.invalidate(trajectory_id, cause="retention_scrub", now=_NOW)
    assert after.revision == before.revision


def test_revision_one_is_not_an_invalidation(harness: LoopHarness) -> None:
    trajectory_id = _rich(harness)
    with pytest.raises(ValueError, match="revision 1"):
        _builder(harness).invalidate(trajectory_id, cause="terminal", now=_NOW)


def test_an_unknown_cause_is_refused(harness: LoopHarness) -> None:
    """A revision whose reason is a typo is a revision nobody can explain."""
    trajectory_id = _rich(harness)
    with pytest.raises(ValueError, match="unknown revision cause"):
        _builder(harness).materialize(trajectory_id, now=_NOW, cause="because")


def _revisions(harness: LoopHarness, trajectory_id: str) -> list[models.ExplanationRevision]:
    with harness.database.read() as session:
        return list(
            session.execute(
                select(models.ExplanationRevision)
                .where(models.ExplanationRevision.trajectory_id == trajectory_id)
                .order_by(models.ExplanationRevision.revision)
            ).scalars()
        )


def test_a_missing_artifact_falls_back_to_the_live_path(harness: LoopHarness) -> None:
    """The body is a file, and a file can be gone. That is a slow read, not a broken one."""
    trajectory_id = _rich(harness)
    builder = _builder(harness)
    revision = builder.read(trajectory_id).revision
    assert revision is not None
    for path in Path(harness.settings.tools.artifact_root or "").parent.rglob("*"):
        if path.is_file() and revision.document_sha256.endswith(path.name):
            path.unlink()
    read = builder.read(trajectory_id)
    assert read.source == "live"
    assert json.loads(canonical_json(read.document))["schema"] == SCHEMA_NAME
