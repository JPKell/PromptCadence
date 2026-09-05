"""planning and approval: per-step threads, prompt provenance, drafting attempts, request kinds

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-04 12:00:00.000000

Phase 7. Nothing here is a new table: the governance tables arrived at ``0002`` so that Phase 3's
first minted intent had somewhere to go, and this revision gives them the columns the planned path
writes.

* ``threads.step_id`` — one thread per step. Existing rows are the bypass loop's and get ``loop``,
  the synthetic step id that path's intent already carries (ADR-0056 §1).
* ``turns.prompt_id/prompt_version/prompt_sha256`` — spec §9: PromptCadence's own prompt records
  are recorded on the turn that used them. Nullable; set only on a planned step's framing turn.
* ``turns.tool_calls_json`` — the calls an assistant turn requested, verbatim, so a step resumed
  after a scoped re-approval or a crash runs the calls the model asked for rather than losing
  them.
* ``plans`` — every drafting attempt is a row (``valid``, ``issues_json``), with the planning
  call's job, subject, token classes and prompt provenance beside the verbatim document. Token
  classes, never money (ADR-0030).
* ``plan_steps.status/started_at/completed_at`` — the ready set is computed from this.
* ``approval_requests.kind/detail_json/resolution_reason`` — what a grant produces, and why a
  denial was given.
* ``deviations.turn_id`` loses its foreign key to ``turns``. A ``tier_escalation`` deviation
  describes a turn that was announced and never answered — no permitted tier could serve it — so
  there is no turn row to reference, and a row invented to satisfy the constraint would be a turn
  that did not happen. The column and its meaning stay; only the constraint goes.

Server defaults fill the three ``NOT NULL`` additions on rows that already exist and are then
dropped, so the model's Python-side defaults and the schema stay in parity.
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("threads") as batch:
        batch.add_column(
            sa.Column(
                "step_id", sa.String(length=64), nullable=False, server_default=sa.text("'loop'")
            )
        )
    with op.batch_alter_table("threads") as batch:
        batch.alter_column("step_id", server_default=None)

    with op.batch_alter_table("turns") as batch:
        batch.add_column(sa.Column("prompt_id", sa.String(length=80), nullable=True))
        batch.add_column(sa.Column("prompt_version", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("prompt_sha256", sa.String(length=71), nullable=True))
        batch.add_column(sa.Column("tool_calls_json", weightsdb.PortableJSON(), nullable=True))

    with op.batch_alter_table("plans") as batch:
        batch.add_column(sa.Column("valid", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("issues_json", weightsdb.PortableJSON(), nullable=True))
        batch.add_column(sa.Column("idempotency_key", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("loadcoach_job_id", sa.String(length=60), nullable=True))
        batch.add_column(sa.Column("model_canonical_id", sa.String(length=300), nullable=True))
        batch.add_column(sa.Column("input_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("output_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("cache_write_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("cache_read_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("loadcoach_ms", sa.Float(), nullable=True))
        batch.add_column(sa.Column("prompt_id", sa.String(length=80), nullable=True))
        batch.add_column(sa.Column("prompt_version", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("prompt_sha256", sa.String(length=71), nullable=True))
    with op.batch_alter_table("plans") as batch:
        batch.alter_column("valid", server_default=None)

    with op.batch_alter_table("plan_steps") as batch:
        batch.add_column(
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default=sa.text("'pending'"),
            )
        )
        batch.add_column(sa.Column("started_at", weightsdb.UtcDateTime(), nullable=True))
        batch.add_column(sa.Column("completed_at", weightsdb.UtcDateTime(), nullable=True))
    with op.batch_alter_table("plan_steps") as batch:
        batch.alter_column("status", server_default=None)

    with op.batch_alter_table("approval_requests") as batch:
        batch.add_column(
            sa.Column(
                "kind", sa.String(length=30), nullable=False, server_default=sa.text("'plan'")
            )
        )
        batch.add_column(sa.Column("detail_json", weightsdb.PortableJSON(), nullable=True))
        batch.add_column(sa.Column("resolution_reason", sa.Text(), nullable=True))
    with op.batch_alter_table("approval_requests") as batch:
        batch.alter_column("kind", server_default=None)

    with op.batch_alter_table("deviations") as batch:
        batch.drop_constraint("fk_deviations_turn_id_turns", type_="foreignkey")


def downgrade() -> None:
    with op.batch_alter_table("deviations") as batch:
        batch.create_foreign_key(
            "fk_deviations_turn_id_turns", "turns", ["turn_id"], ["id"], ondelete="CASCADE"
        )
    with op.batch_alter_table("approval_requests") as batch:
        batch.drop_column("resolution_reason")
        batch.drop_column("detail_json")
        batch.drop_column("kind")
    with op.batch_alter_table("plan_steps") as batch:
        batch.drop_column("completed_at")
        batch.drop_column("started_at")
        batch.drop_column("status")
    with op.batch_alter_table("plans") as batch:
        for name in (
            "prompt_sha256",
            "prompt_version",
            "prompt_id",
            "loadcoach_ms",
            "cache_read_tokens",
            "cache_write_tokens",
            "output_tokens",
            "input_tokens",
            "model_canonical_id",
            "loadcoach_job_id",
            "idempotency_key",
            "issues_json",
            "valid",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("turns") as batch:
        batch.drop_column("tool_calls_json")
        batch.drop_column("prompt_sha256")
        batch.drop_column("prompt_version")
        batch.drop_column("prompt_id")
    with op.batch_alter_table("threads") as batch:
        batch.drop_column("step_id")
