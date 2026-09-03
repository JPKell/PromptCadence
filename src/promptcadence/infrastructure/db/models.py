"""promptcadence.infrastructure.db.models — the declarative base and the Phase 1 tables.

PromptCadence owns this ``MetaData``/``DeclarativeBase`` exclusively (database standards §1):
WeightsDB provides plumbing only and defines no application table, so each application keeps its
own base with no cross-application meaning. **No application ever reads another's database.**

The naming convention is not cosmetic. Alembic's autogenerate diff and SQLite's batch-mode ALTER
both need every constraint and index to have a stable, predictable name; without one, a constraint
recreated by batch mode gets an auto-generated name that differs from the one the model produces,
and the parity check (database standards §5.2) fails forever on a schema that is actually correct.

``trajectories``, ``threads``, ``turns``, ``events``, ``api_tokens`` and ``settings`` are Phase 1's
tables (migration ``0001``). ``turns`` carries the optional LA0 adapter fields from birth
(adapter-roadmap §4.5 — "born, not retrofitted"): ``adapter_name``, ``adapter_digest`` and
``adapter_source_digest``, all nullable, absent unless an adapter served the turn.

``ledger_entries`` (LoadLedger) and ``egress_decisions`` (Commissioner) are **not** created here.
They arrive *mounted* into this same metadata and Alembic history at Phase 5/6
(ADR-0050): each package exports a ``mount_*_tables(metadata, ...)`` function the application
calls at module import, which is why the mount calls belong right here, once those packages are a
dependency — the named failure mode of the pattern is a host that mounts too late and
autogenerates a migration dropping the package's own tables.

SQLAlchemy models never leave the repository layer: a service returns a frozen domain value
object, never one of these.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from weightsdb import PortableJSON, UtcDateTime, ulid_primary_key

__all__ = [
    "ApiToken",
    "Base",
    "Event",
    "Setting",
    "Thread",
    "Trajectory",
    "Turn",
    "utcnow",
]

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """The one declarative base for every PromptCadence-owned table.

    ``metadata`` here is the single source of truth Alembic's autogenerate compares against
    (``MigrationRunner.check_parity``) — a model added without importing it here is invisible to
    that check, not merely untested.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Return the current instant, timezone-aware in UTC.

    Used only as a ``mapped_column`` default for ``created_at``-style columns — an
    infrastructure-layer concern distinct from the ``Clock`` a service takes as a parameter.
    """
    return datetime.now(UTC)


class Trajectory(Base):
    """One persisted, resumable unit of agent work (spec §2, lifecycle §8).

    The state machine, the lease and the recovery path are Phase 3's domain and service logic;
    this table exists in Phase 1 only to prove the schema, its migration and its round trip.
    """

    __tablename__ = "trajectories"

    id: Mapped[str] = ulid_primary_key()
    task: Mapped[str] = mapped_column(Text, nullable=False)
    data_classification: Mapped[str] = mapped_column(
        String(20), nullable=False, default="confidential"
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    project: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tools_json: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False, default=list)
    bypass_planning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tier_override: Mapped[str | None] = mapped_column(String(60), nullable=True)
    max_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_money_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    budget_money_nanos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_token_ceiling: Mapped[int | None] = mapped_column(Integer, nullable=True)
    halted_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (Index("ix_trajectories_status_created_at", "status", "created_at"),)


class Thread(Base):
    """One thread of turns within a trajectory (spec §10: PromptCadence-internal, package-shaped).

    Built without PromptCadence vocabulary leaking into the *shape*, per the recorded ThreadRack
    rejection (ADR-0045 rule 5) — the domain module that will wrap this table arrives in Phase 2.
    """

    __tablename__ = "threads"

    id: Mapped[str] = ulid_primary_key()
    trajectory_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("trajectories.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("ix_threads_trajectory_id", "trajectory_id"),)


class Turn(Base):
    """One model round trip within a thread, with full provenance (spec §9-10).

    LoadCoach time and PromptCadence overhead are recorded separately (spec §15, §17). The three
    ``adapter_*`` columns are the LA0 contract: optional from birth, never retrofitted.
    """

    __tablename__ = "turns"

    id: Mapped[str] = ulid_primary_key()
    thread_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    trajectory_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("trajectories.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    tier: Mapped[str | None] = mapped_column(String(60), nullable=True)
    model_provider_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model_provider_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    model_canonical_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    adapter_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    adapter_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    adapter_source_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thinking_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loadcoach_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    overhead_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    loadcoach_job_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("thread_id", "sequence", name="uq_turns_thread_id_sequence"),
        Index("ix_turns_trajectory_id_sequence", "trajectory_id", "sequence"),
    )


class Event(Base):
    """One persisted event of a trajectory, dense-numbered for SSE replay (spec §17, ADR-0044)."""

    __tablename__ = "events"

    id: Mapped[str] = ulid_primary_key()
    trajectory_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("trajectories.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    data_json: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("trajectory_id", "sequence", name="uq_events_trajectory_id_sequence"),
        Index("ix_events_trajectory_id_sequence", "trajectory_id", "sequence"),
    )


class ApiToken(Base):
    """A bearer token for non-loopback access, stored as a hash and never in the clear.

    ``scopes`` is a comma-separated string over ``read``, ``write``, ``approve`` and ``admin``
    (spec §14) — ``approve`` deliberately separate from ``write``, so the identity that submits
    work cannot approve its own egress.
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = ulid_primary_key()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    scopes: Mapped[str] = mapped_column(String(100), nullable=False, default="read")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("token_sha256", name="uq_api_tokens_token_sha256"),)


class Setting(Base):
    """Runtime-changeable settings, keyed by dotted path."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_json: Mapped[Any] = mapped_column(PortableJSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
