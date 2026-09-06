"""explanation_revisions: the materialized explanation, which is a cache

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-06 13:00:00.000000

Row I1, gate E. The rows stay authoritative and this table is a derivation of them, so its
defining property is that ``DELETE FROM explanation_revisions`` is a supported operation whose only
visible effect is slower reads until ``promptcadence db rebuild-explanations`` refills it
(ADR-0093).

The body is in the artifact directory under ``document_sha256``, not in a column: a 500-turn
trajectory's document is a large payload, and a text column holding it would be read on every
listing that touched this table.

``plans.raw_document`` becomes nullable in the same migration, and the reason is this row's own
work. The retention sweep must scrub model output wherever it lives, and a plan document is model
output; the column was ``NOT NULL``, so the sweep Phase 9 will write could not have scrubbed it
without a migration of its own — discovered here by the test that scrubs the fixture's rows
directly (ADR-0092 rule 2). Nothing writes ``NULL``: a drafting attempt always records what came
back, and ``document_sha256`` outlives the words either way.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from weightsdb import UtcDateTime

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("plans") as batch:
        batch.alter_column("raw_document", existing_type=sa.Text(), nullable=True)
    op.create_table(
        "explanation_revisions",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("trajectory_id", sa.String(length=26), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("document_sha256", sa.String(length=71), nullable=False),
        sa.Column("artifact_ref", sa.String(length=71), nullable=False),
        sa.Column("cause", sa.String(length=40), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("composed_ms", sa.Float(), nullable=False),
        sa.Column("superseded_at", UtcDateTime(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["trajectory_id"],
            ["trajectories.id"],
            name=op.f("fk_explanation_revisions_trajectory_id_trajectories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_explanation_revisions")),
        sa.UniqueConstraint(
            "trajectory_id", "revision", name="uq_explanation_revisions_trajectory_id_revision"
        ),
    )
    op.create_index(
        "ix_explanation_revisions_trajectory_id_revision",
        "explanation_revisions",
        ["trajectory_id", "revision"],
    )


def downgrade() -> None:
    with op.batch_alter_table("plans") as batch:
        batch.alter_column("raw_document", existing_type=sa.Text(), nullable=False)
    op.drop_index(
        "ix_explanation_revisions_trajectory_id_revision", table_name="explanation_revisions"
    )
    op.drop_table("explanation_revisions")
