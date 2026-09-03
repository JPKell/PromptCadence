"""governance tables: intents, plans, approvals, deviations and the tier snapshot

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-02 00:00:00.000000

Phase 2's seven tables — `tier_snapshots`, `plans`, `plan_steps`, `plan_approvals`,
`approval_requests`, `execution_intents` and `deviations` — plus four columns on `turns` and two
on `trajectories`.

They arrive with the domain types that define them rather than with the loop that first writes
them. Phase 3's first act mints an intent at claim (T3) and every turn persists
`(intent_id, revision)`, so a migration arriving with the loop would retrofit the turn row inside
the phase that is hardest to review — the same argument that put the `adapter_*` columns on
`turns` at Phase 1. `cache_write_tokens` and `cache_read_tokens` ride along for the same reason
(ADR-0070 decision 7): a turn row that cannot hold all four token classes throws two away before
LoadLedger at P5 ever sees them.

`spec.md` §10 lists neither `tier_snapshots` nor `deviations`; both are proposed amendments
recorded in `C4_HANDOFF.md`. `ledger_entries` and `egress_decisions` are still not created here —
they arrive *mounted* at Phase 5/6 (ADR-0050).
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "tier_snapshots",
        sa.Column("id", sa.String(length=71), nullable=False),
        sa.Column("document_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tier_snapshots")),
    )

    op.create_table(
        "plans",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("trajectory_id", sa.String(length=26), nullable=False),
        sa.Column("document_sha256", sa.String(length=71), nullable=False),
        sa.Column("raw_document", sa.Text(), nullable=False),
        sa.Column("validated_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["trajectory_id"],
            ["trajectories.id"],
            name=op.f("fk_plans_trajectory_id_trajectories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plans")),
    )
    op.create_index("ix_plans_trajectory_id", "plans", ["trajectory_id"])

    op.create_table(
        "plan_steps",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("plan_id", sa.String(length=26), nullable=False),
        sa.Column("step_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("depends_on_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("tools_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("tier", sa.String(length=60), nullable=False),
        sa.Column("data_classification", sa.String(length=20), nullable=False),
        sa.Column("expected_turns", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plans.id"],
            name=op.f("fk_plan_steps_plan_id_plans"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_steps")),
        sa.UniqueConstraint("plan_id", "step_id", name="uq_plan_steps_plan_id_step_id"),
    )

    op.create_table(
        "plan_approvals",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("trajectory_id", sa.String(length=26), nullable=False),
        sa.Column("plan_id", sa.String(length=26), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("approval_policy_version", sa.String(length=71), nullable=False),
        sa.Column("verdict_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plans.id"],
            name=op.f("fk_plan_approvals_plan_id_plans"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trajectory_id"],
            ["trajectories.id"],
            name=op.f("fk_plan_approvals_trajectory_id_trajectories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_approvals")),
    )
    op.create_index("ix_plan_approvals_trajectory_id", "plan_approvals", ["trajectory_id"])

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("trajectory_id", sa.String(length=26), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("step_ids_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("expires_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("resolved_at", weightsdb.UtcDateTime(), nullable=True),
        sa.Column("approver_token_id", sa.String(length=26), nullable=True),
        sa.ForeignKeyConstraint(
            ["trajectory_id"],
            ["trajectories.id"],
            name=op.f("fk_approval_requests_trajectory_id_trajectories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_requests")),
    )
    op.create_index(
        "ix_approval_requests_trajectory_id_status",
        "approval_requests",
        ["trajectory_id", "status"],
    )

    op.create_table(
        "execution_intents",
        sa.Column("intent_id", sa.String(length=26), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("trajectory_id", sa.String(length=26), nullable=False),
        sa.Column("step_id", sa.String(length=64), nullable=False),
        sa.Column("supersedes", sa.Integer(), nullable=True),
        sa.Column("approved_tier", sa.String(length=60), nullable=False),
        sa.Column("fallback_tiers_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("permitted_egress_class", sa.String(length=10), nullable=False),
        sa.Column("approved_tools_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("max_classification", sa.String(length=20), nullable=False),
        sa.Column("token_budget", sa.Integer(), nullable=False),
        sa.Column("money_budget_currency", sa.String(length=3), nullable=True),
        sa.Column("money_budget_nanos", sa.Integer(), nullable=True),
        sa.Column("budget_source", sa.String(length=30), nullable=False),
        sa.Column("budget_sample_count", sa.Integer(), nullable=False),
        sa.Column("max_turns", sa.Integer(), nullable=False),
        sa.Column("minted_by", sa.String(length=60), nullable=False),
        sa.Column("minted_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("approval_request_id", sa.String(length=26), nullable=True),
        sa.Column("gate_json", weightsdb.PortableJSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["trajectory_id"],
            ["trajectories.id"],
            name=op.f("fk_execution_intents_trajectory_id_trajectories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("intent_id", "revision", name=op.f("pk_execution_intents")),
    )
    op.create_index(
        "ix_execution_intents_trajectory_id_step_id",
        "execution_intents",
        ["trajectory_id", "step_id"],
    )

    op.create_table(
        "deviations",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("trajectory_id", sa.String(length=26), nullable=False),
        sa.Column("turn_id", sa.String(length=26), nullable=False),
        sa.Column("intent_id", sa.String(length=26), nullable=False),
        sa.Column("intent_revision", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("disposition", sa.String(length=30), nullable=False),
        sa.Column("reapprovable", sa.Boolean(), nullable=False),
        sa.Column("detail_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["trajectory_id"],
            ["trajectories.id"],
            name=op.f("fk_deviations_trajectory_id_trajectories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["turns.id"],
            name=op.f("fk_deviations_turn_id_turns"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deviations")),
    )
    op.create_index("ix_deviations_trajectory_id", "deviations", ["trajectory_id"])

    op.add_column(
        "trajectories", sa.Column("tier_snapshot_id", sa.String(length=71), nullable=True)
    )
    op.add_column(
        "trajectories", sa.Column("approval_policy_version", sa.String(length=71), nullable=True)
    )
    op.add_column("turns", sa.Column("intent_id", sa.String(length=26), nullable=True))
    op.add_column("turns", sa.Column("intent_revision", sa.Integer(), nullable=True))
    op.add_column("turns", sa.Column("cache_write_tokens", sa.Integer(), nullable=True))
    op.add_column("turns", sa.Column("cache_read_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("turns", "cache_read_tokens")
    op.drop_column("turns", "cache_write_tokens")
    op.drop_column("turns", "intent_revision")
    op.drop_column("turns", "intent_id")
    op.drop_column("trajectories", "approval_policy_version")
    op.drop_column("trajectories", "tier_snapshot_id")
    op.drop_index("ix_deviations_trajectory_id", table_name="deviations")
    op.drop_table("deviations")
    op.drop_index("ix_execution_intents_trajectory_id_step_id", table_name="execution_intents")
    op.drop_table("execution_intents")
    op.drop_index("ix_approval_requests_trajectory_id_status", table_name="approval_requests")
    op.drop_table("approval_requests")
    op.drop_index("ix_plan_approvals_trajectory_id", table_name="plan_approvals")
    op.drop_table("plan_approvals")
    op.drop_table("plan_steps")
    op.drop_index("ix_plans_trajectory_id", table_name="plans")
    op.drop_table("plans")
    op.drop_table("tier_snapshots")
