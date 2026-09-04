"""tool call records, and the link from a tool turn to the call it answers

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-04 00:00:00.000000

Phase 4's one table and one column. `tool_call_records` holds a row per model-directed tool call,
refusals and failures included, because the audit question is "what did this trajectory try". Its
columns are ToolYard's `ToolCallRecord` (spec §10 there: the package owns the shape, the
application owns the table, the retention and this migration) plus `trajectory_id`, `turn_id` and
`tool_turn_id`, which are what put a call back into a transcript.

`turns.tool_call_id` closes a gap the domain has carried since Phase 2: `Turn` refuses a `TOOL`
turn that names no call, and until now nothing wrote one, so the row had no column for the link.
Nullable, because every non-`TOOL` turn must leave it unset — the domain refuses the reverse too.

`invocation_id` is unique. The executor's context id is minted per call by this application and
never by a model, so a duplicate is a bug in the loop rather than something a model could force;
the constraint is what turns that bug into a failed write instead of two rows claiming to be the
same call. There is deliberately no cost column: a call's cost is derived at Phase 5 from usage and
a pricing hash (ADR-0030), and a column here would be a second place for it to live.
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("turns", sa.Column("tool_call_id", sa.String(length=26), nullable=True))
    op.create_table(
        "tool_call_records",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("trajectory_id", sa.String(length=26), nullable=False),
        sa.Column("turn_id", sa.String(length=26), nullable=False),
        sa.Column("tool_turn_id", sa.String(length=26), nullable=True),
        sa.Column("invocation_id", sa.String(length=26), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("args_json", sa.Text(), nullable=True),
        sa.Column("args_sha256", sa.String(length=71), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=True),
        sa.Column("reason_detail", sa.Text(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("result_sha256", sa.String(length=71), nullable=False),
        sa.Column("artifact_ref", sa.String(length=71), nullable=True),
        sa.Column("output_truncated", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("risk_class", sa.String(length=20), nullable=False),
        sa.Column("egress", sa.String(length=20), nullable=False),
        sa.Column("isolation_tier", sa.String(length=20), nullable=True),
        sa.Column("started_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tool_call_records"),
        sa.ForeignKeyConstraint(
            ["trajectory_id"],
            ["trajectories.id"],
            name="fk_tool_call_records_trajectory_id_trajectories",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["turns.id"],
            name="fk_tool_call_records_turn_id_turns",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("invocation_id", name="uq_tool_call_records_invocation_id"),
    )
    op.create_index("ix_tool_call_records_trajectory_id", "tool_call_records", ["trajectory_id"])
    op.create_index("ix_tool_call_records_turn_id", "tool_call_records", ["turn_id"])


def downgrade() -> None:
    op.drop_index("ix_tool_call_records_turn_id", table_name="tool_call_records")
    op.drop_index("ix_tool_call_records_trajectory_id", table_name="tool_call_records")
    op.drop_table("tool_call_records")
    op.drop_column("turns", "tool_call_id")
