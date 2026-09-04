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

Phase 2 adds the governance tables (migration ``0002``): ``tier_snapshots``, ``plans``,
``plan_steps``, ``plan_approvals``, ``approval_requests``, ``execution_intents`` and
``deviations``, plus four columns on ``turns`` and two on ``trajectories``. They arrive **here**
rather than with the loop that first writes them, for the same reason the ``adapter_*`` columns
did: Phase 3's first act mints an intent at claim (T3) and every turn persists
``(intent_id, revision)``, so a migration arriving with the loop would be a retrofit of the turn
row inside the phase that is hardest to review. ``turns`` also gains ``cache_write_tokens`` and
``cache_read_tokens`` now: ADR-0070 decision 7 puts all four token classes on LoadCoach's wire,
and a turn row that cannot hold them throws two away before the ledger at P5 ever sees them.

``execution_intents`` is keyed on ``(intent_id, revision)`` and is append-only in practice — one
row per revision, none ever updated (ADR-0056 §3). ``trajectories.tier_snapshot_id`` carries **no**
foreign key deliberately: a snapshot is content-addressed and shared by every trajectory whose
configuration matched, so a cascade from one trajectory must never reach it.

Phase 4 adds ``tool_call_records`` (migration ``0004``) and one column on ``turns``. The column is
``tool_call_id``: the domain has always refused a ``TOOL`` turn that names no call, and a
non-``TOOL`` turn that names one, and until Phase 4 nothing wrote either, so the row had nowhere to
keep the link. A tool result and the call it answers must survive a compaction that reads only
this field (``domain.threads``), which is why it is a column and not a fact recomputed from the
ordering of rows.

Phase 5 mounts LoadLedger's four tables (migration ``0005``) and is the **first** package mount in
this application, so :data:`LEDGER_TABLES` is the example Commissioner's ``egress_decisions`` mount
at Phase 6 and CutCtx's later one should copy. Three things make it an example rather than a call:

* **It runs at module import**, at the bottom of this module, unconditionally and behind no flag.
  Autogenerate only sees what was mounted before the metadata was inspected, so a host that mounts
  lazily gets a revision that silently *drops* the package's tables. That is the named failure mode
  of ADR-0050's pattern and it is why the mount lives here rather than in a service.
* **The prefix stays the package's default** (``loadledger.sql.DEFAULT_TABLE_PREFIX``, ``ledger_``).
  It is part of the mounted contract: changing it in a host that has already migrated is a table
  rename, not a configuration change.
* **Nothing joins to a mounted table.** They carry no foreign key out of the mounted set and
  ``run_id``/``source_ref`` are opaque strings; the application reads them through the package's
  own ledger class (ADR-0050 decision 2), never through a ``select`` written here.

``egress_decisions`` (Commissioner) is **not** created here yet; it arrives the same way at Phase 6.

SQLAlchemy models never leave the repository layer: a service returns a frozen domain value
object, never one of these.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from loadledger.sql import mount_ledger_tables
from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from weightsdb import PortableJSON, UtcDateTime, ulid_primary_key

__all__ = [
    "LEDGER_TABLES",
    "ApiToken",
    "ApprovalRequest",
    "Base",
    "Deviation",
    "Event",
    "ExecutionIntent",
    "Plan",
    "PlanApproval",
    "PlanStep",
    "Setting",
    "Thread",
    "TierSnapshot",
    "ToolCallRecord",
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

    The state machine is the domain's (``promptcadence.domain.trajectory``); the lease, the
    cancel flag and recovery are Phase 3's service logic over the four columns migration ``0003``
    adds: ``lease_owner``/``lease_expires_at`` (lifecycle §8.1 — a persisted lease is what a
    restart recovers from), ``cancel_requested`` (T14 on an ``executing`` trajectory is honoured
    at the next turn boundary, so the request must outlive the HTTP call that made it) and
    ``error_code`` (the spec §13 code beside the verbatim cause in ``halted_reason``).
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
    # BigInteger, not Integer. Money is whole nanos, so the shipped $5.00 default is
    # 5_000_000_000 -- already past a 4-byte integer's 2_147_483_647. SQLite's dynamic typing
    # stores it regardless, so a SQLite-only suite never sees the DataError PostgreSQL raises;
    # migration 0005 widens both, and the same rule governs every accumulating column here.
    budget_money_nanos: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    budget_token_ceiling: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    budget_partial_pricing: Mapped[str | None] = mapped_column(String(10), nullable=True)
    window_parked_from: Mapped[str | None] = mapped_column(String(30), nullable=True)
    window_next_edge_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    window_days_waited: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tier_snapshot_id: Mapped[str | None] = mapped_column(String(71), nullable=True)
    approval_policy_version: Mapped[str | None] = mapped_column(String(71), nullable=True)
    halted_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        Index("ix_trajectories_status_created_at", "status", "created_at"),
        Index("ix_trajectories_status_lease_expires_at", "status", "lease_expires_at"),
    )


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
    intent_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    intent_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thinking_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loadcoach_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    overhead_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    loadcoach_job_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
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


class TierSnapshot(Base):
    """The tier definitions one or more trajectories ran under, content-addressed.

    The primary key **is** the content address, so identical configurations share one row and an
    edited ceiling produces a new one with no migration. A trajectory records the id; the
    explanation reads the document, and stays readable after the configuration changes
    (lifecycle §3).
    """

    __tablename__ = "tier_snapshots"

    id: Mapped[str] = mapped_column(String(71), primary_key=True)
    document_json: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class Plan(Base):
    """One drafted plan, kept verbatim beside its validated form (lifecycle §4.1).

    ``raw_document`` is what the planner returned, byte for byte; ``validated_json`` is what
    PromptCadence made of it. Both survive because they answer different questions — what the model
    proposed, and what execution was held to.
    """

    __tablename__ = "plans"

    id: Mapped[str] = ulid_primary_key()
    trajectory_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("trajectories.id", ondelete="CASCADE"), nullable=False
    )
    document_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    raw_document: Mapped[str] = mapped_column(Text, nullable=False)
    validated_json: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("ix_plans_trajectory_id", "trajectory_id"),)


class PlanStep(Base):
    """One step of a validated plan, denormalized so the DAG is queryable without parsing JSON."""

    __tablename__ = "plan_steps"

    id: Mapped[str] = ulid_primary_key()
    plan_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    depends_on_json: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False, default=list)
    tools_json: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False, default=list)
    tier: Mapped[str] = mapped_column(String(60), nullable=False)
    data_classification: Mapped[str] = mapped_column(String(20), nullable=False)
    expected_turns: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (UniqueConstraint("plan_id", "step_id", name="uq_plan_steps_plan_id_step_id"),)


class PlanApproval(Base):
    """One approval verdict over a plan, with the policy version and headroom behind it.

    ``PLAN_REJECTED`` always lists every step's verdict and the ceiling that rejected it (spec
    §13), so ``verdict_json`` holds the whole :class:`~promptcadence.domain.policy.PlanVerdict`
    rather than a summary.
    """

    __tablename__ = "plan_approvals"

    id: Mapped[str] = ulid_primary_key()
    trajectory_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("trajectories.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("plans.id", ondelete="CASCADE"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    approval_policy_version: Mapped[str] = mapped_column(String(71), nullable=False)
    verdict_json: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("ix_plan_approvals_trajectory_id", "trajectory_id"),)


class ApprovalRequest(Base):
    """One pending question for a person, with its timeout as a persisted value (ADR-0049).

    ``expires_at`` is a stored instant rather than process state, so the clock survives a restart;
    a timeout is never a grant, and never an indefinite wait. A trajectory parks on exactly one
    pending request at a time.
    """

    __tablename__ = "approval_requests"

    id: Mapped[str] = ulid_primary_key()
    trajectory_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("trajectories.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    step_ids_json: Mapped[list[Any]] = mapped_column(PortableJSON, nullable=False, default=list)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    approver_token_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    __table_args__ = (
        Index("ix_approval_requests_trajectory_id_status", "trajectory_id", "status"),
    )


class ExecutionIntent(Base):
    """One immutable envelope revision (ADR-0056). Keyed on ``(intent_id, revision)``.

    Append-only in practice: a re-approval writes revision *n+1* with ``supersedes`` pointing at
    *n*, and *n* is retained, so "under whose grant did turn 7 run?" is answerable after the fact
    — which an edited row cannot do.
    """

    __tablename__ = "execution_intents"

    intent_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    trajectory_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("trajectories.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_tier: Mapped[str] = mapped_column(String(60), nullable=False)
    fallback_tiers_json: Mapped[list[Any]] = mapped_column(
        PortableJSON, nullable=False, default=list
    )
    permitted_egress_class: Mapped[str] = mapped_column(String(10), nullable=False)
    approved_tools_json: Mapped[list[Any]] = mapped_column(
        PortableJSON, nullable=False, default=list
    )
    max_classification: Mapped[str] = mapped_column(String(20), nullable=False)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    money_budget_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    money_budget_nanos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_source: Mapped[str] = mapped_column(String(30), nullable=False)
    budget_sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_turns: Mapped[int] = mapped_column(Integer, nullable=False)
    minted_by: Mapped[str] = mapped_column(String(60), nullable=False)
    minted_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    approval_request_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    gate_json: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_execution_intents_trajectory_id_step_id", "trajectory_id", "step_id"),
    )


class Deviation(Base):
    """One category-typed deviation of a turn from its intent (lifecycle §5).

    Every deviation is an event *and* a row, including a drift the policy silently continued past
    — a record holding only the deviations that stopped something answers the wrong question.
    """

    __tablename__ = "deviations"

    id: Mapped[str] = ulid_primary_key()
    trajectory_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("trajectories.id", ondelete="CASCADE"), nullable=False
    )
    turn_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False
    )
    intent_id: Mapped[str] = mapped_column(String(26), nullable=False)
    intent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    disposition: Mapped[str] = mapped_column(String(30), nullable=False)
    reapprovable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detail_json: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("ix_deviations_trajectory_id", "trajectory_id"),)


class ToolCallRecord(Base):
    """One model-directed tool call, whatever became of it (spec §10, ADR-0053 decision 7).

    A row per call, **including refusals and failures** — the table answers "what did this
    trajectory try", not "what did it manage", and the second question alone is useless to a
    security review. The columns are ToolYard's ``ToolCallRecord`` plus the three links this
    application needs to put a call back in its trajectory: the trajectory, the turn whose
    ``tool_calls`` it answers, and the ``TOOL`` turn that carried its result back to the model.

    Two columns exist because a record must outlive its content. ``args_json`` is ``NULL`` when
    the tool is named in ``[tools] redact_args`` and after a retention sweep; ``args_sha256`` is
    written in every case, so the row still proves what was asked even when it no longer says it.
    ``result_sha256`` is the digest of the handler's **whole** output, never of the capped text the
    model saw — which is what lets ``artifact_ref`` point at a spilled body and be checkable.

    There is no cost column and no ledger link. A tool call's cost is LoadLedger's at Phase 5,
    derived from usage and a pricing hash rather than stored as money (ADR-0030), and a column
    here would be the second place it lived.
    """

    __tablename__ = "tool_call_records"

    id: Mapped[str] = ulid_primary_key()
    trajectory_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("trajectories.id", ondelete="CASCADE"), nullable=False
    )
    turn_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False
    )
    tool_turn_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    invocation_id: Mapped[str] = mapped_column(String(26), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    args_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    args_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    artifact_ref: Mapped[str | None] = mapped_column(String(71), nullable=True)
    output_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_class: Mapped[str] = mapped_column(String(20), nullable=False)
    egress: Mapped[str] = mapped_column(String(20), nullable=False)
    isolation_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("invocation_id", name="uq_tool_call_records_invocation_id"),
        Index("ix_tool_call_records_trajectory_id", "trajectory_id"),
        Index("ix_tool_call_records_turn_id", "turn_id"),
    )


LEDGER_TABLES: Final = mount_ledger_tables(Base.metadata)
"""LoadLedger's four tables, mounted into this application's metadata (ADR-0050, migration 0005).

**Mounted here, at module import, on purpose.** ``Base.metadata`` is what Alembic's ``env.py``
names as ``target_metadata`` and what ``MigrationRunner.check_parity`` compares the live schema
against; both read the metadata *as it is when they inspect it*. A mount performed later — inside a
service, a request handler or behind a configuration flag — is a mount autogenerate never sees, and
the revision it then generates does not merely omit these tables, it **drops** them. So this line
has no condition on it and never will.

The prefix is left at ``loadledger.sql.DEFAULT_TABLE_PREFIX`` (``ledger_``), giving
``ledger_entries``, ``ledger_balances``, ``ledger_balance_money`` and ``ledger_runs``. Changing it
once a deployment has migrated is a table rename, not a setting, so it is not configurable here.

The handle is kept rather than dropped so tests can assert the prefix and the shapes; no query in
this application is written against these :class:`~sqlalchemy.Table` objects. Reads and writes go
through :class:`loadledger.sql.SqlLedger` (ADR-0050 decision 2) — a join from an application entity
to a mounted table would freeze a shape the package is free to change under an upgrade note.
"""
