"""trajectory leases, the cancel flag and the error code

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-03 00:00:00.000000

Phase 3's four columns on `trajectories`. Lifecycle §8.1 says `planning` and `executing` hold a
lease, and §8.3 says recovery is exercised "at startup and on lease expiry" — so the lease must
be a persisted value, not process state, or a restart has nothing to recover from. `lease_owner`
names the worker (process and thread) and `lease_expires_at` is the instant after which any other
worker may take it over (ADR-0036).

`cancel_requested` is the flag T14 sets on an `executing` trajectory: the transition itself is
honoured at the next turn boundary, by the worker, in one write with its event (ADR-0044), so the
request has to survive until then somewhere the worker reads. `error_code` carries the spec §13
code behind a halt or failure, beside the verbatim cause in `halted_reason`.
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("trajectories", sa.Column("lease_owner", sa.String(length=100), nullable=True))
    op.add_column(
        "trajectories", sa.Column("lease_expires_at", weightsdb.UtcDateTime(), nullable=True)
    )
    op.add_column(
        "trajectories",
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("trajectories", sa.Column("error_code", sa.String(length=40), nullable=True))
    op.create_index(
        "ix_trajectories_status_lease_expires_at",
        "trajectories",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_trajectories_status_lease_expires_at", table_name="trajectories")
    op.drop_column("trajectories", "error_code")
    op.drop_column("trajectories", "cancel_requested")
    op.drop_column("trajectories", "lease_expires_at")
    op.drop_column("trajectories", "lease_owner")
