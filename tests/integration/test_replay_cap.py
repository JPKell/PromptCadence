"""ADR-0096: a replayed tool call's arguments are capped at ToolYard's record bound.

G2 moved G1's hazard rather than removing it — model-chosen arguments went back onto the wire as
structured JSON instead of prose, uncapped. The cap lives where the persisted calls become wire
messages; the record and the wire carry the same size-and-digest object at the same bound.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from tests.fakes.harness import LoopHarness, open_harness
from tests.fakes.loadcoach_app import ScriptedGeneration
from toolyard import DEFAULT_MAX_ARGS_JSON_BYTES

from promptcadence.config import load_settings
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models


@pytest.fixture
def harness() -> Iterator[LoopHarness]:
    with open_harness(load_settings().settings) as built:
        yield built


def _write(content: str) -> dict[str, object]:
    return {
        "call_index": 0,
        "id": "c0",
        "name": "write_file",
        "arguments_fragment": json.dumps({"path": "out.txt", "content": content}),
    }


def test_an_oversize_argument_never_rides_back_onto_the_wire_uncapped(
    harness: LoopHarness,
) -> None:
    big = "x" * (DEFAULT_MAX_ARGS_JSON_BYTES + 512)
    harness.script(
        ScriptedGeneration(text="", tool_calls=(_write(big),)),
        ScriptedGeneration(text="Written."),
    )
    trajectory_id = harness.submit_bypass()
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
    # The call ran with its full arguments: the bound is on the replay, not on the work.
    assert (harness.tools.workspace_root / trajectory_id / "out.txt").read_text() == big

    replayed = harness.fake.requests[-1]["body"]["messages"]
    assistant = next(m for m in replayed if m["role"] == "assistant")
    (call,) = assistant["tool_calls"]
    assert call["id"] == "c0" and call["name"] == "write_file"
    assert call["arguments"]["__toolyard_args_omitted__"] == "oversize"
    assert call["arguments"]["bytes"] > DEFAULT_MAX_ARGS_JSON_BYTES
    assert big not in json.dumps(replayed), "the argument body is not on the wire"
    # The TOOL turn still answers the call by its id.
    assert next(m for m in replayed if m["role"] == "tool")["tool_call_id"] == "c0"

    # The record says the same thing, with the same digest — one bound, one shape.
    with harness.database.read() as session:
        record = session.execute(
            select(models.ToolCallRecord).where(
                models.ToolCallRecord.trajectory_id == trajectory_id
            )
        ).scalar_one()
    assert record.args_json is not None
    recorded = json.loads(record.args_json)
    assert recorded["__toolyard_args_omitted__"] == "oversize"
    assert call["arguments"]["sha256"] == recorded["sha256"] == record.args_sha256
    assert call["arguments"]["bytes"] == recorded["bytes"]


def test_an_argument_within_the_bound_is_replayed_verbatim(harness: LoopHarness) -> None:
    small = "y" * 1024
    harness.script(
        ScriptedGeneration(text="", tool_calls=(_write(small),)),
        ScriptedGeneration(text="Written."),
    )
    trajectory_id = harness.submit_bypass()
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
    replayed = harness.fake.requests[-1]["body"]["messages"]
    assistant = next(m for m in replayed if m["role"] == "assistant")
    assert assistant["tool_calls"] == [
        {"id": "c0", "name": "write_file", "arguments": {"path": "out.txt", "content": small}}
    ]
