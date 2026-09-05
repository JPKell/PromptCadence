"""a step's attempts: the counter beside the step's status

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-05 12:00:00.000000

Row G3. ``plan_steps.attempt`` — which attempt of this step is running or last ran, ``1`` for a
step that never had to repeat. A retryable LoadCoach service failure repeats the step's turn under
the same intent revision (`ADR-0076`), and this column is the planned path's queryable summary of
how many times that happened.

It is a **summary, not the history**. Every attempt's tier, cause and error code lives in the
``step.retried`` events written in the same write that starts it, because those are the only half
the bypass path also has — its synthetic ``loop`` step has no ``plan_steps`` row at all, and an
explanation that read attempts from this column would have a history for one mode and nothing for
the other.

A server default fills the ``NOT NULL`` addition on rows that already exist and is then dropped,
so the model's Python-side default and the schema stay in parity — the ``0007`` idiom.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("plan_steps") as batch:
        batch.add_column(
            sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1"))
        )
    with op.batch_alter_table("plan_steps") as batch:
        batch.alter_column("attempt", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("plan_steps") as batch:
        batch.drop_column("attempt")
