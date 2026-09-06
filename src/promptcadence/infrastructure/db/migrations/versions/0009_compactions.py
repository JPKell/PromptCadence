"""compactions: the record that the wire differed from the rows

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-06 12:00:00.000000

Row I1, gate B. Compaction is a view and never a deletion (lifecycle §7, ADR-0052): every original
turn stays in ``turns``, and this table is what lets a reader tell why one turn saw less history
than the turn before it.

No foreign key on ``summary_turn_id``. The summary turn lives in its own thread, is written in the
same transaction as this row, and a ``CASCADE`` from it would delete the account of a compaction
because the turn it produced was swept — which is exactly backwards: the account must outlive the
content, like ``tool_call_records.args_json`` already does.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from weightsdb import PortableJSON, UtcDateTime

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "compactions",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("trajectory_id", sa.String(length=26), nullable=False),
        sa.Column("thread_id", sa.String(length=26), nullable=False),
        sa.Column("step_id", sa.String(length=64), nullable=False),
        sa.Column("tier", sa.String(length=60), nullable=False),
        sa.Column("budget_tokens", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("policy_name", sa.String(length=300), nullable=False),
        sa.Column("policy_version", sa.String(length=20), nullable=False),
        sa.Column("plan_hash", sa.String(length=71), nullable=False),
        sa.Column("tokens_before", sa.Integer(), nullable=False),
        sa.Column("tokens_after_estimate", sa.Integer(), nullable=False),
        sa.Column("turns_before", sa.Integer(), nullable=False),
        sa.Column("turns_after", sa.Integer(), nullable=False),
        sa.Column("budget_unmet", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("masked_turn_ids", PortableJSON(), nullable=False),
        sa.Column("summarized_turn_ids", PortableJSON(), nullable=False),
        sa.Column("dropped_turn_ids", PortableJSON(), nullable=False),
        sa.Column("summary_turn_id", sa.String(length=26), nullable=True),
        sa.Column("summary_intent_id", sa.String(length=26), nullable=True),
        sa.Column("summary_intent_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["trajectory_id"],
            ["trajectories.id"],
            name=op.f("fk_compactions_trajectory_id_trajectories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["threads.id"],
            name=op.f("fk_compactions_thread_id_threads"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compactions")),
    )
    op.create_index(
        "ix_compactions_trajectory_id_created_at", "compactions", ["trajectory_id", "created_at"]
    )
    op.create_index("ix_compactions_thread_id", "compactions", ["thread_id"])


def downgrade() -> None:
    op.drop_index("ix_compactions_thread_id", table_name="compactions")
    op.drop_index("ix_compactions_trajectory_id_created_at", table_name="compactions")
    op.drop_table("compactions")
