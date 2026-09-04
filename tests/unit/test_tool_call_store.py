"""The collecting store and the row mapping: what a ToolYard record becomes in this schema."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from toolyard import EgressClass, RiskClass, ToolCallRecord, ToolStatus

from promptcadence.infrastructure.tool_calls import (
    CollectingToolCallStore,
    ToolCallLinks,
    tool_call_row,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def record(
    *,
    invocation_id: str = "01INV",
    tool_name: str = "read_file",
    args_json: str | None = '{"path": "a.md"}',
    status: ToolStatus = ToolStatus.OK,
    reason: str | None = None,
    reason_detail: str | None = None,
) -> ToolCallRecord:
    """One record shaped the way the executor produces them, with the fields tests vary."""
    return ToolCallRecord(
        invocation_id=invocation_id,
        tool_name=tool_name,
        args_json=args_json,
        args_sha256="a" * 64,
        status=status,
        result_summary="a.md",
        result_sha256="b" * 64,
        duration_ms=4,
        risk_class=RiskClass.READ_ONLY,
        egress=EgressClass.NONE,
        started_at=datetime(2026, 9, 4, tzinfo=UTC),
        reason=reason,
        reason_detail=reason_detail,
    )


def test_every_field_toolyard_produces_reaches_the_row() -> None:
    row = tool_call_row(
        record(),
        row_id="01ROW",
        trajectory_id="01TRJ",
        turn_id="01TRN",
        links=ToolCallLinks(tool_turn_id="01TOOL", artifact_ref="b" * 64, isolation_tier="bwrap"),
    )
    assert row.invocation_id == "01INV"
    assert row.tool_name == "read_file"
    assert row.args_json == '{"path": "a.md"}'
    assert row.status == "ok"
    assert row.risk_class == "read_only"
    assert row.egress == "none"
    assert row.tool_turn_id == "01TOOL"
    assert row.isolation_tier == "bwrap"


def test_a_redacted_record_keeps_the_digest_and_drops_the_plaintext() -> None:
    """The row still proves what was asked even when it no longer says it."""
    row = tool_call_row(
        record(args_json=None),
        row_id="01ROW",
        trajectory_id="01TRJ",
        turn_id="01TRN",
        links=ToolCallLinks(),
    )
    assert row.args_json is None
    assert row.args_sha256 == "a" * 64


def test_a_refusal_reaches_the_row_with_the_name_that_does_not_exist() -> None:
    """A refusal that does not say what was asked for cannot be diagnosed."""
    row = tool_call_row(
        record(
            tool_name="teleport",
            status=ToolStatus.REFUSED,
            reason="unknown_tool",
            reason_detail="no tool of that name is registered",
        ),
        row_id="01ROW",
        trajectory_id="01TRJ",
        turn_id="01TRN",
        links=ToolCallLinks(),
    )
    assert row.tool_name == "teleport"
    assert row.status == "refused"
    assert row.reason == "unknown_tool"


def test_the_store_collects_in_order_and_snapshots() -> None:
    store = CollectingToolCallStore()
    assert len(store.records) == 0
    store.append(record(invocation_id="01A"))
    seen = store.records
    store.append(record(invocation_id="01B"))
    assert [entry.invocation_id for entry in store.records] == ["01A", "01B"]
    assert [entry.invocation_id for entry in seen] == ["01A"]


def test_flush_refuses_a_mismatched_number_of_row_ids() -> None:
    """A caller bug that would otherwise write a row with a duplicate or missing primary key."""
    store = CollectingToolCallStore()
    store.append(record())
    with pytest.raises(ValueError, match="one row id per collected record"):
        # The session is never touched: the count is checked before any row is built.
        store.flush(
            cast("Session", None),
            trajectory_id="01TRJ",
            turn_id="01TRN",
            links=ToolCallLinks(),
            row_ids=[],
        )
