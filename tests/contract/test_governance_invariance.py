"""Spec §11 contract 1, roadmap I11: governance invariance, proved by diff (Phase 7, gate D).

One scripted task, run twice against the fake LoadCoach — once planned, once with
``bypass_planning`` — each into its own fresh database. Every table's rows are then reduced to
**shapes** (identifiers, timestamps and digests masked; everything else compared by value) and
diffed. The permitted differences are named here, one by one, each with the document that
permits it; anything else that differs fails, and the failure names the table, the row and the
field that moved.

**The allowance list is the whole point.** Widening it to make the diff pass is the one failure
this test exists to prevent (kickoff §8): a genuine mismatch is a finding for the operator, not a
line here. What it permits, and why:

1. ``plans``, ``plan_steps``, ``plan_approvals`` exist only on the planned path — the rows the
   contract itself names.
2. ``events`` of type ``plan.drafted`` and ``plan.approved`` exist only on the planned path; the
   ``trajectory.claimed`` event names ``planning`` on one and ``executing`` on the other (T2 vs
   T3, lifecycle §8.2).
3. ``turns``: the planned step's thread opens with one extra ``user`` turn — the ``step.execute``
   framing, identified by its ``prompt_id`` (spec §9) — and everything that counts turns
   (``sequence`` on the turn rows and on ``turn.*`` events, ``turn_count`` on ``step.completed``
   and ``trajectory.completed``) is exactly one higher for it. The relation is asserted, not
   masked.
4. ``execution_intents`` (and ``intent.minted``): ``minted_by`` is ``policy`` on one and
   ``bypass_default`` on the other, ``step_id`` is ``s1`` and ``loop``, and the **slice** fields
   differ by construction — the bypass default's slice *is* the trajectory's own ceiling and
   turn cap (ADR-0056 §2: ``policy.default_tier``, the trajectory's own budget,
   ``execution.max_steps`` as ``max_turns``), a planned step's is its estimate × 2 and
   ``max_turns_per_step`` (lifecycle §4.3, §5). Every **governed** field a turn is checked
   against — ``approved_tier``, ``fallback_tiers``, ``permitted_egress_class``,
   ``approved_tools``, ``max_classification``, ``budget_source``, the gate verdict — must be
   identical, and is.
5. ``threads.step_id`` and every ``step_id`` in an event body: ``s1`` versus ``loop``.
6. ``trajectories.bypass_planning`` and the same flag on ``trajectory.created``: the request
   itself.

Everything else — the ledger's entries, balances and verdicts, every egress decision, every
deviation row and event, every tool-call record, the turn rows' provenance and usage, the
terminal transition and its cause — must be identical in shape and value.

``tests/golden/deviation_matrix_bypass.json`` is the baseline F2 named for the deviation rows:
the second scenario here raises the same deviation on both paths and the diff holds.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator, Mapping, Set
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from tests.fakes.harness import LoopHarness, open_harness, plan_document, step
from tests.fakes.loadcoach_app import ScriptedGeneration

from promptcadence.config import load_settings
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db.models import Base

_SRC = Path(__file__).resolve().parents[2] / "src" / "promptcadence"
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DECISION = re.compile(r"^01DECISION[0-9A-Z]{16}$")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")

PLAN_ONLY_TABLES = frozenset({"plans", "plan_steps", "plan_approvals"})
PLAN_ONLY_EVENTS = frozenset({"plan.drafted", "plan.approved"})
STEP_FRAMING_PROMPT = "step.execute"
INTENT_SLICE_FIELDS = frozenset(
    {"token_budget", "money_budget", "money_budget_currency", "money_budget_nanos", "max_turns"}
)
INTENT_IDENTITY_FIELDS = frozenset({"minted_by", "step_id"})
TURN_COUNT_FIELDS = frozenset({"sequence", "turn_count"})
TURN_COUNT_EVENTS = frozenset(
    {"turn.started", "turn.completed", "step.completed", "trajectory.completed"}
)


def _mask(value: Any) -> Any:
    """Identifiers, timestamps and digests become placeholders; structure is kept."""
    if isinstance(value, datetime):
        return "<ts>"
    if isinstance(value, str):
        if _ULID.match(value) or _DECISION.match(value):
            return "<id>"
        if _DIGEST.match(value):
            return "<digest>"
        if _ISO.fullmatch(value):
            return "<ts>"
        if value[:1] in "{[":
            # A JSON text column (the mounted packages store their documents as text): mask
            # inside the structure rather than treating the whole document as one string.
            try:
                return _mask(json.loads(value))
            except ValueError:
                pass
        # A cause or a message that names an id, a digest or an instant inside free text.
        masked = re.sub(r"\b[0-9A-HJKMNP-TV-Z]{26}\b", "<id>", value)
        masked = re.sub(r"sha256:[0-9a-f]{64}", "<digest>", masked)
        return _ISO.sub("<ts>", masked)
    if isinstance(value, Mapping):
        return {key: _mask(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_mask(item) for item in value]
    return value


def _record(harness: LoopHarness) -> dict[str, list[dict[str, Any]]]:
    """Every table's rows, in a deterministic order, masked into shapes."""
    record: dict[str, list[dict[str, Any]]] = {}
    with harness.database.read() as session:
        for table in Base.metadata.sorted_tables:
            rows = [dict(row._mapping) for row in session.execute(select(table)).all()]
            order = [
                column.name
                for column in table.columns
                if column.name in {"sequence", "revision", "attempt", "created_at", "decided_at"}
            ]
            rows.sort(
                key=lambda row: tuple(
                    (0, row[name]) if isinstance(row.get(name), int) else (1, str(row.get(name)))
                    for name in order
                )
            )
            record[table.name] = [{key: _mask(value) for key, value in row.items()} for row in rows]
    return record


def _run(harness: LoopHarness, *, planned: bool, answers: list[ScriptedGeneration]) -> str:
    if planned:
        harness.script_plan(plan_document(step("s1", tools=["read_file"])))
    harness.script(*answers)
    trajectory_id = (
        harness.submit_planned(tools=("read_file",))
        if planned
        else harness.submit_bypass(tools=("read_file",))
    )
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
    return trajectory_id


@pytest.fixture
def pair(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[LoopHarness, LoopHarness]]:
    settings = load_settings().settings
    with open_harness(settings) as planned, open_harness(settings) as bypassed:
        yield planned, bypassed


_STOP = [ScriptedGeneration(text="The notes describe three meetings.")]
_REFUSED_TOOL_THEN_STOP = [
    ScriptedGeneration(
        text="",
        tool_calls=(
            {
                "call_index": 0,
                "id": "c1",
                "name": "write_file",
                "arguments_fragment": '{"path": "out.txt", "content": "x"}',
            },
        ),
    ),
    ScriptedGeneration(text="I cannot write; here is the summary."),
]


@pytest.mark.parametrize(
    "answers",
    [pytest.param(_STOP, id="one_turn"), pytest.param(_REFUSED_TOOL_THEN_STOP, id="deviation")],
)
def test_planned_and_bypassed_records_are_identical_in_shape_minus_the_named_rows(
    pair: tuple[LoopHarness, LoopHarness], answers: list[ScriptedGeneration]
) -> None:
    planned, bypassed = pair
    _run(planned, planned=True, answers=list(answers))
    _run(bypassed, planned=False, answers=list(answers))
    left, right = _record(planned), _record(bypassed)
    assert set(left) == set(right)
    problems: list[str] = []
    for table in sorted(left):
        problems.extend(_diff_table(table, left[table], right[table]))
    assert problems == [], "governance invariance broke:\n" + "\n".join(problems)


def _diff_table(
    table: str, planned: list[dict[str, Any]], bypassed: list[dict[str, Any]]
) -> list[str]:
    if table in PLAN_ONLY_TABLES:
        return (
            [] if planned and not bypassed else [f"{table}: expected rows only on the planned path"]
        )
    if table == "events":
        return _diff_events(planned, bypassed)
    if table == "turns":
        return _diff_turns(planned, bypassed)
    if table == "execution_intents":
        return _diff_rows(
            table, planned, bypassed, allow=INTENT_SLICE_FIELDS | INTENT_IDENTITY_FIELDS
        )
    if table == "threads":
        return _diff_rows(table, planned, bypassed, allow={"step_id"})
    if table == "trajectories":
        return _diff_rows(table, planned, bypassed, allow={"bypass_planning"})
    return _diff_rows(table, planned, bypassed, allow=set())


def _diff_rows(
    table: str, planned: list[dict[str, Any]], bypassed: list[dict[str, Any]], *, allow: Set[str]
) -> list[str]:
    if len(planned) != len(bypassed):
        return [f"{table}: planned has {len(planned)} row(s), bypassed has {len(bypassed)}"]
    problems: list[str] = []
    for index, (left, right) in enumerate(zip(planned, bypassed, strict=True)):
        for field in sorted(set(left) | set(right)):
            if field in allow:
                continue
            if left.get(field) != right.get(field):
                problems.append(
                    f"{table}[{index}].{field} moved: planned={left.get(field)!r} "
                    f"bypassed={right.get(field)!r}"
                )
    return problems


def _diff_turns(planned: list[dict[str, Any]], bypassed: list[dict[str, Any]]) -> list[str]:
    framing = [row for row in planned if row.get("prompt_id") == STEP_FRAMING_PROMPT]
    if len(framing) != 1:
        return [
            f"turns: expected exactly one {STEP_FRAMING_PROMPT} framing turn, found {len(framing)}"
        ]
    rest = [row for row in planned if row.get("prompt_id") != STEP_FRAMING_PROMPT]
    problems = _diff_rows("turns", rest, bypassed, allow={"sequence"})
    for index, (left, right) in enumerate(zip(rest, bypassed, strict=False)):
        if left["role"] == "user" and left["sequence"] != right["sequence"]:
            problems.append(f"turns[{index}]: the task turn must be turn 1 on both paths")
        if left["role"] != "user" and left["sequence"] != right["sequence"] + 1:
            problems.append(
                f"turns[{index}].sequence: planned={left['sequence']} is not bypassed+1 "
                f"({right['sequence']})"
            )
    return problems


def _diff_events(planned: list[dict[str, Any]], bypassed: list[dict[str, Any]]) -> list[str]:
    left = [row for row in planned if row["event_type"] not in PLAN_ONLY_EVENTS]
    if len(left) == len(planned):
        return ["events: no plan.* event on the planned path"]
    if [row["event_type"] for row in left] != [row["event_type"] for row in bypassed]:
        return [
            "events: the sequence of event types differs once plan.* events are removed:\n"
            f"  planned : {[row['event_type'] for row in left]}\n"
            f"  bypassed: {[row['event_type'] for row in bypassed]}"
        ]
    problems: list[str] = []
    for index, (one, other) in enumerate(zip(left, bypassed, strict=True)):
        kind = one["event_type"]
        allow: set[str] = {"step_id", "minted_by"}
        if kind == "trajectory.created":
            allow |= {"bypass_planning"}  # allowance 6: the request itself
        if kind == "trajectory.claimed":
            allow |= {"state"}
        if kind == "intent.minted":
            allow |= INTENT_SLICE_FIELDS
        for field in sorted(set(one["data_json"]) | set(other["data_json"])):
            a, b = one["data_json"].get(field), other["data_json"].get(field)
            if field in allow or a == b:
                continue
            if kind in TURN_COUNT_EVENTS and field in TURN_COUNT_FIELDS:
                if isinstance(a, int) and isinstance(b, int) and a == b + 1:
                    continue
                problems.append(
                    f"events[{index}] {kind}.{field}: planned={a} is not bypassed+1 ({b})"
                )
                continue
            problems.append(f"events[{index}] {kind}.{field} moved: planned={a!r} bypassed={b!r}")
    return problems


# --------------------------------------------------------------------------------------------
# The structural half: there is no code path that executes a turn without an intent.
# --------------------------------------------------------------------------------------------


def _generate_call_sites() -> set[tuple[str, str]]:
    """Every ``.generate(...)`` call in the package, as (module, enclosing function)."""
    sites: set[tuple[str, str]] = set()
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "generate"
                ):
                    sites.add((str(path.relative_to(_SRC)), node.name))
    return sites


def test_the_only_generate_call_sites_are_the_governed_turn_and_the_planner() -> None:
    """The diff shows two runs agreed; this shows they had to.

    ``LoadCoachClient.generate`` is reached from exactly two places: the loop's ``_call``, which
    takes a ``_StepRun`` — an intent and the thread its turns go in — and the planner's ``draft``,
    which produces no turn and runs under ``tools.plan``. There is no third site, so there is no
    turn a model could answer outside an envelope. The other half of the proof is
    ``TurnProvenance``'s ``InitVar``: a turn row cannot be built without an intent object
    (``tests/unit/test_domain_intent.py``).
    """
    sites = _generate_call_sites()
    assert sites == {
        ("services/loop.py", "_call"),
        ("services/planner.py", "draft"),
    }, f"an ungoverned call site appeared: {sites}"
    loop = ast.parse((_SRC / "services" / "loop.py").read_text(encoding="utf-8"))
    call = next(
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.FunctionDef) and node.name == "_call"
    )
    parameters = {
        argument.arg: ast.unparse(argument.annotation)
        for argument in call.args.args
        if argument.annotation
    }
    assert parameters.get("run") == "_StepRun", (
        "the governed call takes the step run, not a bare intent"
    )
