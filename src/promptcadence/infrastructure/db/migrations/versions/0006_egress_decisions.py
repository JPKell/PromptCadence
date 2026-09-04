"""Commissioner's mounted egress_decisions table

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-04 00:00:00.000000

Phase 6, and the **second** package-table mount in this application. ``0005`` was the
first-instance example (ADR-0050); this revision is its transcription, so the reasoning is there
and only what differs is written here.

The table is Commissioner's ``egress_decisions`` at the package's default ``egress_`` prefix,
paired with the one unconditional ``mount_egress_tables(Base.metadata)`` call at the bottom of
``promptcadence.infrastructure.db.models``. As with the ledger: the package ships table *shapes*
and never a migration history, so this table appears in PromptCadence's own autogenerate diff,
upgrades with PromptCadence's own history, and is backed up, restored and pruned by whoever owns
this database. The prefix is part of the mounted contract — changing it once a deployment has
migrated is a table rename, not a configuration change — and the DDL below is proved against the
mount by ``check_parity`` rather than trusted because it was transcribed carefully.

**Why this table is append-only in practice and has no delete path here.** A row is the audit
record of an egress verdict, and spec §11 contract 3 makes a denial exactly as durable as an
approval. Nothing in this application updates or deletes one; retention is the operator's, through
the database they own.

``decision_json`` holds the whole decision as SetSpec's ``governance.egress_decision`` 1.0 payload.
The four columns beside it — ``run_id``, ``verdict``, ``target_name``, ``decided_at`` — are
projections of fields inside it, indexed so ``GET /egress-decisions`` can filter without opening
every document. They are Commissioner's choice of projection, not this application's, which is why
none of them is widened or renamed here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "egress_decisions",
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("target_name", sa.String(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("decision_id", name="pk_egress_decisions"),
    )
    op.create_index("ix_egress_decisions_run", "egress_decisions", ["run_id", "decided_at"])
    op.create_index("ix_egress_decisions_decided_at", "egress_decisions", ["decided_at"])


def downgrade() -> None:
    op.drop_index("ix_egress_decisions_decided_at", table_name="egress_decisions")
    op.drop_index("ix_egress_decisions_run", table_name="egress_decisions")
    op.drop_table("egress_decisions")
