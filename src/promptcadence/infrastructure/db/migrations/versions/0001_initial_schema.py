"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-02 00:00:00.000000

Phase 1's six tables: `trajectories`, `threads`, `turns`, `events`, `api_tokens` and `settings`.
Portable across SQLite and PostgreSQL — `weightsdb.PortableJSON` and `weightsdb.UtcDateTime` are
what make one migration correct on both, and the PostgreSQL job runs this same file against a real
server rather than trusting that it would.
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "trajectories",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("data_classification", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("project", sa.String(length=100), nullable=True),
        sa.Column("tools_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("bypass_planning", sa.Boolean(), nullable=False),
        sa.Column("tier_override", sa.String(length=60), nullable=True),
        sa.Column("max_steps", sa.Integer(), nullable=True),
        sa.Column("max_turns", sa.Integer(), nullable=True),
        sa.Column("budget_money_currency", sa.String(length=3), nullable=True),
        sa.Column("budget_money_nanos", sa.Integer(), nullable=True),
        sa.Column("budget_token_ceiling", sa.Integer(), nullable=True),
        sa.Column("halted_reason", sa.Text(), nullable=True),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("updated_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("completed_at", weightsdb.UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trajectories")),
    )
    op.create_index("ix_trajectories_status_created_at", "trajectories", ["status", "created_at"])

    op.create_table(
        "threads",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("trajectory_id", sa.String(length=26), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["trajectory_id"],
            ["trajectories.id"],
            name=op.f("fk_threads_trajectory_id_trajectories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_threads")),
    )
    op.create_index("ix_threads_trajectory_id", "threads", ["trajectory_id"])

    op.create_table(
        "turns",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("thread_id", sa.String(length=26), nullable=False),
        sa.Column("trajectory_id", sa.String(length=26), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("tier", sa.String(length=60), nullable=True),
        sa.Column("model_provider_kind", sa.String(length=40), nullable=True),
        sa.Column("model_provider_name", sa.String(length=200), nullable=True),
        sa.Column("model_digest", sa.String(length=71), nullable=True),
        sa.Column("model_canonical_id", sa.String(length=300), nullable=True),
        sa.Column("adapter_name", sa.String(length=200), nullable=True),
        sa.Column("adapter_digest", sa.String(length=71), nullable=True),
        sa.Column("adapter_source_digest", sa.String(length=71), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=71), nullable=True),
        sa.Column("finish_reason", sa.String(length=20), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("thinking_tokens", sa.Integer(), nullable=True),
        sa.Column("loadcoach_ms", sa.Float(), nullable=True),
        sa.Column("overhead_ms", sa.Float(), nullable=True),
        sa.Column("loadcoach_job_id", sa.String(length=60), nullable=True),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["threads.id"],
            name=op.f("fk_turns_thread_id_threads"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trajectory_id"],
            ["trajectories.id"],
            name=op.f("fk_turns_trajectory_id_trajectories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_turns")),
        sa.UniqueConstraint("thread_id", "sequence", name="uq_turns_thread_id_sequence"),
    )
    op.create_index("ix_turns_trajectory_id_sequence", "turns", ["trajectory_id", "sequence"])

    op.create_table(
        "events",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("trajectory_id", sa.String(length=26), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("timestamp", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("data_json", weightsdb.PortableJSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["trajectory_id"],
            ["trajectories.id"],
            name=op.f("fk_events_trajectory_id_trajectories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("trajectory_id", "sequence", name="uq_events_trajectory_id_sequence"),
    )
    op.create_index("ix_events_trajectory_id_sequence", "events", ["trajectory_id", "sequence"])

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_sha256", sa.String(length=71), nullable=False),
        sa.Column("scopes", sa.String(length=100), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("last_used_at", weightsdb.UtcDateTime(), nullable=True),
        sa.Column("revoked_at", weightsdb.UtcDateTime(), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_sha256", name="uq_api_tokens_token_sha256"),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("updated_at", weightsdb.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("api_tokens")
    op.drop_index("ix_events_trajectory_id_sequence", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_turns_trajectory_id_sequence", table_name="turns")
    op.drop_table("turns")
    op.drop_index("ix_threads_trajectory_id", table_name="threads")
    op.drop_table("threads")
    op.drop_index("ix_trajectories_status_created_at", table_name="trajectories")
    op.drop_table("trajectories")
