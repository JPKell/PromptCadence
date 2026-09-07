"""trajectories.content_scrubbed_at: when the retention sweep removed the words

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-06 18:00:00.000000

Row I2, gate C. Spec §14's retention sweep exists from this revision: a terminal trajectory older
than ``[storage] content_retention_hours`` loses its transcript text, its plan document and step
descriptions, its tool arguments and result summaries, its task and its workspace directory —
hashes, usage, decisions and events stay, so it still explains itself. The stamp is the sweep's
own record: it makes a second pass skip what the first already did, and it says when.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from weightsdb import UtcDateTime

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("trajectories") as batch:
        batch.add_column(sa.Column("content_scrubbed_at", UtcDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trajectories") as batch:
        batch.drop_column("content_scrubbed_at")
