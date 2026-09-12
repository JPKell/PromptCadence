"""promptcadence.services.explanation — reading the rows, and the cache over them.

Two halves, and the split is the point. :func:`compose_live` reads every table a trajectory
touched and hands plain, ordered values to
:func:`promptcadence.domain.explanation.compose`, which assembles them and does nothing else.
:class:`ExplanationBuilder` adds the cache: materialize once after the terminal transition, serve
the revision when there is one, invalidate and re-materialize when the rows underneath change.

**The cache is a cache.** ``materialize(rows) == compose_live(rows)`` is a byte equality
(ADR-0093), the whole ``explanation_revisions`` table can be dropped at any time, and the only
visible effect is that reads recompose live until ``rebuild`` refills it. Both are asserted
directly rather than argued.

**Determinism is a requirement here, not a style.** Every query in this module has an ``ORDER BY``
whose last term breaks ties on a primary key — ``created_at`` alone is not an order under a
millisecond clock, and an unordered pair is a document that differs between two composes of the
same rows, which is the equality golden failing intermittently. Nothing here reads the clock except
to stamp a revision's ``created_at``, and that value is never part of the document.

**Three traps, all somebody's already-paid-for finding.** A failed attempt has a ``turn.started``
event and no ``turns`` row, so the attempt list is composed from ``step.retried`` and the turn list
from ``turns`` — never one source serving both. An invalid plan attempt has ``validated_json = {}``
*and* ``issues_json`` set, so validity is read from the ``valid`` flag and never from emptiness. And
a bypass gate's ``approval_requests`` row carries ``step_ids = ["loop"]``, which is not a step to
look up in ``plan_steps``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, cast

from baseaicore import canonical_json, new_id, sha256_of
from baseaicore.timeutil import to_rfc3339
from sqlalchemy import delete, select, update

from promptcadence.domain.errors import TrajectoryNotFoundError
from promptcadence.domain.events import EventType
from promptcadence.domain.explanation import (
    SCHEMA_VERSION,
    compose,
    content_or_removed,
)
from promptcadence.infrastructure.db import models
from promptcadence.services.compaction import COMPACTION_STEP_PREFIX
from promptcadence.services.egress import decision_view
from promptcadence.services.intents import intent_document
from promptcadence.services.records import RecordReader
from promptcadence.services.views import approver_of, view_of

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import CursorResult
    from sqlalchemy.orm import Session

    from promptcadence.config import Settings
    from promptcadence.services.budget import BudgetService
    from promptcadence.services.database import Database
    from promptcadence.services.egress import EgressService
    from promptcadence.services.tools import ArtifactStore

__all__ = [
    "CAUSES",
    "ExplanationBuilder",
    "ExplanationRead",
    "RevisionRecord",
    "explanation_store",
]

CAUSES: Final = ("terminal", "retention_scrub", "recosting", "schema_upgrade")
"""Why a revision exists (lifecycle §9.1, ADR-0092).

``terminal`` is revision 1. The other three are invalidations: the retention sweep (Phase 9's, and
this build has the entry point and not the sweep), a ledger re-costing under a corrected price
record, and a document-schema minor bump on upgrade — the only one with a caller here.
"""

_LEASE_KEYS: Final = frozenset({"lease"})
"""Dropped from the trajectory section. Who holds the work right now is a runtime fact, not part of
the record of what happened, and it is one more thing that could differ between two composes."""


def explanation_store(settings: Settings) -> ArtifactStore:
    """The directory composed explanation documents are written to.

    A **sibling** of the tool artifact directory, never the same store. Both are content-addressed
    and could not collide — the name is the bytes — but they answer different questions: one holds
    what a model was not shown, the other holds a composed record. A tool-output store that also
    contained explanations would make "no artifact was filed for this call" unanswerable, which is
    an assertion the tool tests actually make.
    """
    from promptcadence.services.tools import ArtifactStore, resolved_root

    return ArtifactStore(resolved_root(settings).parent / "explanations")


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    """One ``explanation_revisions`` row, as a value."""

    revision: int
    schema_version: str
    document_sha256: str
    cause: str
    turn_count: int
    composed_ms: float
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExplanationRead:
    """One answered explanation, and which path answered it.

    ``revision`` is ``None`` when the document was composed live — an in-flight trajectory, or a
    terminal one whose revision has not been written yet or was dropped. That is an ordinary state
    and not an error (ADR-0093 rule 3), and the surface says which path answered because the two
    have different §15 budgets.
    """

    document: dict[str, Any]
    revision: RevisionRecord | None
    composed_ms: float

    @property
    def source(self) -> str:
        """``"materialized"`` or ``"live"``."""
        return "materialized" if self.revision is not None else "live"


class ExplanationBuilder:
    """Composes a trajectory's explanation, and owns its materialized revisions."""

    __slots__ = ("_artifacts", "_budget", "_database", "_egress", "_records")

    def __init__(
        self,
        database: Database,
        *,
        budget: BudgetService,
        egress: EgressService,
        artifacts: ArtifactStore,
    ) -> None:
        """Bind the reads and the artifact store the document body is written to."""
        self._database = database
        self._budget = budget
        self._egress = egress
        self._artifacts = artifacts
        self._records = RecordReader(database)

    # ---- composition ------------------------------------------------------------------------

    def compose_live(self, trajectory_id: str) -> dict[str, Any]:
        """Compose the document from the rows as they stand now.

        Args:
            trajectory_id: The trajectory.

        Returns:
            The ``promptcadence.trajectory_explanation`` document.

        Raises:
            TrajectoryNotFoundError: No such trajectory.
        """
        with self._database.read() as session:
            row = session.get(models.Trajectory, trajectory_id)
            if row is None:
                raise TrajectoryNotFoundError(
                    f"No trajectory {trajectory_id!r}.", details={"trajectory_id": trajectory_id}
                )
            # `approver` needs the session `view_of` does not take: the row keeps the approving
            # token's id and the field is its name (row W10). Composed here so the document and
            # `GET /trajectories/{id}` answer the same thing (row WPF3).
            view = replace(view_of(row), approver=approver_of(session, trajectory_id))
            trajectory = {
                key: value for key, value in view.as_json().items() if key not in _LEASE_KEYS
            }
            trajectory["tier_snapshot"] = _tier_snapshot(session, row.tier_snapshot_id)
            return compose(
                trajectory=trajectory,
                plan=self._records.plan(trajectory_id),
                intents=self._intents(session, trajectory_id),
                approvals=self._approvals(session, trajectory_id),
                threads=self._threads(session, trajectory_id),
                tool_calls=self._tool_calls(session, trajectory_id),
                compactions=self._compactions(session, trajectory_id),
                ledger_entries=self._ledger_entries(trajectory_id),
                egress_decisions=self._egress_decisions(trajectory_id),
                deviations=self._deviations(session, trajectory_id),
                events=self._events(session, trajectory_id),
            )

    # ---- the cache --------------------------------------------------------------------------

    def read(self, trajectory_id: str) -> ExplanationRead:
        """Answer the explanation, from the current revision when there is one.

        Returns:
            The document with the revision that served it, or with ``revision=None`` when it was
            composed live. A terminal trajectory with no revision is served live and is not an
            error: the rows are authoritative and the revision is a cache (ADR-0093 rule 3).

        Raises:
            TrajectoryNotFoundError: No such trajectory.
        """
        started = time.perf_counter()
        current = self.current_revision(trajectory_id)
        if current is not None and current.schema_version == SCHEMA_VERSION:
            body = self._artifacts.path_for(current.document_sha256)
            if body.is_file():
                document = json.loads(body.read_text(encoding="utf-8"))
                return ExplanationRead(document, current, (time.perf_counter() - started) * 1000.0)
        document = self.compose_live(trajectory_id)
        return ExplanationRead(document, None, (time.perf_counter() - started) * 1000.0)

    def current_revision(self, trajectory_id: str) -> RevisionRecord | None:
        """The highest revision that has not been superseded, or ``None``."""
        with self._database.read() as session:
            row = session.execute(
                select(models.ExplanationRevision)
                .where(
                    models.ExplanationRevision.trajectory_id == trajectory_id,
                    models.ExplanationRevision.superseded_at.is_(None),
                )
                .order_by(models.ExplanationRevision.revision.desc())
                .limit(1)
            ).scalar_one_or_none()
            return _revision_record(row) if row is not None else None

    def materialize(
        self, trajectory_id: str, *, now: datetime, cause: str = "terminal"
    ) -> RevisionRecord:
        """Compose the document once and persist it as the next revision.

        Args:
            trajectory_id: The trajectory, which must exist.
            now: The instant to stamp. Injected, like every clock in this application.
            cause: One of :data:`CAUSES`.

        Returns:
            The revision written, or the current one unchanged when it already describes these
            rows under this schema version — the call is idempotent, so the terminal transition's
            follow-up write and a later ``rebuild`` do not produce two identical revisions.

        Raises:
            TrajectoryNotFoundError: No such trajectory.
            ValueError: ``cause`` is not one of :data:`CAUSES`. A revision whose reason is a typo
                is a revision nobody can explain.
        """
        if cause not in CAUSES:
            message = f"unknown revision cause {cause!r}; expected one of {list(CAUSES)}"
            raise ValueError(message)
        started = time.perf_counter()
        document = self.compose_live(trajectory_id)
        body = canonical_json(document)
        digest = sha256_of(body)
        composed_ms = (time.perf_counter() - started) * 1000.0
        turn_count = sum(len(thread["turns"]) for thread in document["threads"])
        current = self.current_revision(trajectory_id)
        if (
            current is not None
            and current.document_sha256 == digest
            and current.schema_version == SCHEMA_VERSION
        ):
            return current
        self._artifacts.put(body, digest=digest)
        with self._database.write() as session:
            session.execute(
                update(models.ExplanationRevision)
                .where(
                    models.ExplanationRevision.trajectory_id == trajectory_id,
                    models.ExplanationRevision.superseded_at.is_(None),
                )
                .values(superseded_at=now)
            )
            highest = session.execute(
                select(models.ExplanationRevision.revision)
                .where(models.ExplanationRevision.trajectory_id == trajectory_id)
                .order_by(models.ExplanationRevision.revision.desc())
                .limit(1)
            ).scalar_one_or_none()
            row = models.ExplanationRevision(
                id=new_id(),
                trajectory_id=trajectory_id,
                revision=(highest or 0) + 1,
                schema_version=SCHEMA_VERSION,
                document_sha256=digest,
                artifact_ref=digest,
                cause=cause,
                turn_count=turn_count,
                composed_ms=composed_ms,
                superseded_at=None,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return _revision_record(row)

    def invalidate(self, trajectory_id: str, *, cause: str, now: datetime) -> RevisionRecord:
        """Supersede the current revision and materialize the next from the rows as they stand.

        The entry point Phase 9's retention sweep and a ledger re-costing call (ADR-0092). It does
        **not** scrub anything and does not decide retention policy: it is the reaction, and the
        caller is the change.

        Args:
            trajectory_id: The trajectory whose rows moved.
            cause: ``retention_scrub``, ``recosting`` or ``schema_upgrade``.
            now: The instant to stamp.

        Returns:
            The new revision. When the rows compose to the same bytes — an invalidation that
            changed nothing the document reports — the current revision is returned unchanged
            rather than a duplicate written, so a sweep that touched a trajectory it did not
            actually alter leaves no trace it did.

        Raises:
            ValueError: ``cause`` is ``terminal`` or unknown. Revision 1 is not an invalidation.
        """
        if cause == "terminal":
            message = "invalidate needs a reason the rows changed; 'terminal' is revision 1's"
            raise ValueError(message)
        return self.materialize(trajectory_id, now=now, cause=cause)

    def rebuild(self, *, now: datetime, trajectory_id: str | None = None) -> tuple[int, int]:
        """Recompose every terminal trajectory's revision — ``promptcadence db``'s maintenance arm.

        Args:
            now: The instant to stamp.
            trajectory_id: One trajectory, or ``None`` for every terminal one.

        Returns:
            ``(considered, written)``. ``written`` counts only the revisions that actually changed
            bytes, so a rebuild over an intact cache reports ``(n, 0)`` — which is the assertion
            that the cache was correct, not a rebuild that did nothing.
        """
        with self._database.read() as session:
            query = select(models.Trajectory.id).where(
                models.Trajectory.status.in_(_TERMINAL_STATES)
            )
            if trajectory_id is not None:
                query = query.where(models.Trajectory.id == trajectory_id)
            targets = list(session.execute(query.order_by(models.Trajectory.id)).scalars())
        written = 0
        for target in targets:
            before = self.current_revision(target)
            cause = "terminal" if before is None else "schema_upgrade"
            after = self.materialize(target, now=now, cause=cause)
            if before is None or after.revision != before.revision:
                written += 1
        return len(targets), written

    def drop_revisions(self, trajectory_id: str | None = None) -> int:
        """Delete materialized revisions — the operation that proves this table is a cache.

        Args:
            trajectory_id: One trajectory, or ``None`` for all of them.

        Returns:
            How many rows were deleted. The artifacts are **not** removed: a superseded revision
            keeps its artifact until an operator prunes it (lifecycle §9.1), and the same reasoning
            covers a dropped one.
        """
        statement = delete(models.ExplanationRevision)
        if trajectory_id is not None:
            statement = statement.where(models.ExplanationRevision.trajectory_id == trajectory_id)
        with self._database.write() as session:
            result = cast("CursorResult[Any]", session.execute(statement))
            return int(result.rowcount)

    # ---- the sections -----------------------------------------------------------------------

    def _intents(self, session: Session, trajectory_id: str) -> list[dict[str, Any]]:
        rows = session.execute(
            select(models.ExecutionIntent)
            .where(models.ExecutionIntent.trajectory_id == trajectory_id)
            .order_by(
                models.ExecutionIntent.step_id,
                models.ExecutionIntent.intent_id,
                models.ExecutionIntent.revision,
            )
        ).scalars()
        return [intent_document(row) for row in rows]

    def _approvals(self, session: Session, trajectory_id: str) -> list[dict[str, Any]]:
        """Every approval request with its resolution — never an age, which would read the clock."""
        rows = session.execute(
            select(models.ApprovalRequest)
            .where(models.ApprovalRequest.trajectory_id == trajectory_id)
            .order_by(models.ApprovalRequest.created_at, models.ApprovalRequest.id)
        ).scalars()
        return [
            {
                "request_id": row.id,
                "kind": row.kind,
                "status": row.status,
                "reason": row.reason,
                # A bypass gate's row carries ["loop"], which is not a plan step (G1 §15a).
                "step_ids": list(row.step_ids_json),
                "detail": dict(row.detail_json) if row.detail_json is not None else None,
                "created_at": to_rfc3339(row.created_at),
                "expires_at": to_rfc3339(row.expires_at),
                "resolved_at": to_rfc3339(row.resolved_at) if row.resolved_at else None,
                "approver_token_id": row.approver_token_id,
                "resolution_reason": row.resolution_reason,
            }
            for row in rows
        ]

    def _threads(self, session: Session, trajectory_id: str) -> list[dict[str, Any]]:
        """Every thread with its turns, and the attempts that produced no turn.

        Threads order by ``created_at`` **then id**: ties are real under a millisecond clock, and
        an unordered pair is a document that differs between two composes of the same rows.
        """
        threads = list(
            session.execute(
                select(models.Thread)
                .where(models.Thread.trajectory_id == trajectory_id)
                .order_by(models.Thread.created_at, models.Thread.id)
            ).scalars()
        )
        turns_by_thread: dict[str, list[dict[str, Any]]] = {thread.id: [] for thread in threads}
        rows = session.execute(
            select(models.Turn)
            .where(models.Turn.trajectory_id == trajectory_id)
            .order_by(models.Turn.thread_id, models.Turn.sequence)
        ).scalars()
        for row in rows:
            turns_by_thread.setdefault(row.thread_id, []).append(_turn_document(row))
        attempts = _attempt_documents(session, trajectory_id)
        return [
            {
                "thread_id": thread.id,
                "step_id": thread.step_id,
                "kind": (
                    "compaction" if thread.step_id.startswith(COMPACTION_STEP_PREFIX) else "step"
                ),
                "created_at": to_rfc3339(thread.created_at),
                "turns": turns_by_thread.get(thread.id, []),
                # From ``step.retried``, never from the turns: a failed attempt has a
                # ``turn.started`` event and no ``turns`` row (G3 §7), so one source cannot serve
                # both lists. Joined by ``failed_turn_id``, never by adjacency.
                "failed_attempts": attempts.get(thread.step_id, []),
            }
            for thread in threads
        ]

    def _tool_calls(self, session: Session, trajectory_id: str) -> list[dict[str, Any]]:
        rows = session.execute(
            select(models.ToolCallRecord)
            .where(models.ToolCallRecord.trajectory_id == trajectory_id)
            .order_by(models.ToolCallRecord.started_at, models.ToolCallRecord.id)
        ).scalars()
        return [
            {
                "record_id": row.id,
                "invocation_id": row.invocation_id,
                "turn_id": row.turn_id,
                "tool_turn_id": row.tool_turn_id,
                "tool_name": row.tool_name,
                # Model-authored, and scrubbed exactly as transcript text is — an explanation that
                # kept its own copy would have defeated the sweep.
                "arguments": content_or_removed(row.args_json, row.args_sha256),
                "status": row.status,
                "reason": row.reason,
                "reason_detail": row.reason_detail,
                "result": content_or_removed(row.result_summary, row.result_sha256),
                "artifact_ref": row.artifact_ref,
                "output_truncated": bool(row.output_truncated),
                "duration_ms": row.duration_ms,
                "risk_class": row.risk_class,
                "egress": row.egress,
                "isolation_tier": row.isolation_tier,
                "started_at": to_rfc3339(row.started_at),
            }
            for row in rows
        ]

    def _compactions(self, session: Session, trajectory_id: str) -> list[dict[str, Any]]:
        rows = session.execute(
            select(models.Compaction)
            .where(models.Compaction.trajectory_id == trajectory_id)
            .order_by(models.Compaction.created_at, models.Compaction.id)
        ).scalars()
        return [
            {
                "compaction_id": row.id,
                "thread_id": row.thread_id,
                "step_id": row.step_id,
                "tier": row.tier,
                "budget_tokens": row.budget_tokens,
                "threshold": row.threshold,
                "policy_name": row.policy_name,
                "policy_version": row.policy_version,
                "plan_hash": row.plan_hash,
                "tokens_before": row.tokens_before,
                "tokens_after_estimate": row.tokens_after_estimate,
                "turns_before": row.turns_before,
                "turns_after": row.turns_after,
                "budget_unmet": bool(row.budget_unmet),
                "masked_turn_ids": list(row.masked_turn_ids),
                "summarized_turn_ids": list(row.summarized_turn_ids),
                "dropped_turn_ids": list(row.dropped_turn_ids),
                "summary_turn_id": row.summary_turn_id,
                "summary_intent_id": row.summary_intent_id,
                "summary_intent_revision": row.summary_intent_revision,
                "created_at": to_rfc3339(row.created_at),
            }
            for row in rows
        ]

    def _deviations(self, session: Session, trajectory_id: str) -> list[dict[str, Any]]:
        rows = session.execute(
            select(models.Deviation)
            .where(models.Deviation.trajectory_id == trajectory_id)
            .order_by(models.Deviation.created_at, models.Deviation.id)
        ).scalars()
        return [
            {
                "deviation_id": row.id,
                "turn_id": row.turn_id,
                "intent_id": row.intent_id,
                "intent_revision": row.intent_revision,
                "category": row.category,
                "severity": row.severity,
                "disposition": row.disposition,
                "reapprovable": bool(row.reapprovable),
                "detail": dict(row.detail_json),
                "created_at": to_rfc3339(row.created_at),
            }
            for row in rows
        ]

    def _events(self, session: Session, trajectory_id: str) -> list[dict[str, Any]]:
        rows = session.execute(
            select(models.Event)
            .where(models.Event.trajectory_id == trajectory_id)
            .order_by(models.Event.sequence)
        ).scalars()
        return [
            {
                "sequence": row.sequence,
                "event_type": row.event_type,
                "timestamp": to_rfc3339(row.timestamp),
                "data": dict(row.data_json),
            }
            for row in rows
        ]

    def _ledger_entries(self, trajectory_id: str) -> list[dict[str, Any]]:
        """Every debit, oldest first, with the ceiling verdicts as of that debit.

        No position and no ``as_of``: a balance taken now is a fact about now, and a document that
        carried one would differ between two composes of the same rows. What each entry carries is
        what the ceilings **said** when the debit was recorded, which is a stored fact (ADR-0030).
        """
        views = self._budget.entry_views(trajectory_id=trajectory_id, limit=_ALL)
        return [view.as_json() for view in reversed(views)]

    def _egress_decisions(self, trajectory_id: str) -> list[dict[str, Any]]:
        return [
            decision_view(decision) for decision in self._egress.decisions(run_id=trajectory_id)
        ]


_ALL: Final = 1_000_000
"""``entry_views`` counts from the newest and needs a bound; the explanation wants every entry."""

_TERMINAL_STATES: Final = ("completed", "halted", "failed", "cancelled", "rejected")


def _revision_record(row: models.ExplanationRevision) -> RevisionRecord:
    return RevisionRecord(
        revision=row.revision,
        schema_version=row.schema_version,
        document_sha256=row.document_sha256,
        cause=row.cause,
        turn_count=row.turn_count,
        composed_ms=row.composed_ms,
        created_at=row.created_at,
    )


def _tier_snapshot(session: Session, snapshot_id: str | None) -> dict[str, Any] | None:
    """The tier definitions the trajectory ran under.

    Recorded rather than referenced, so the explanation stays readable after an operator edits a
    ceiling — which is the whole reason ``tier_snapshots`` is content-addressed.
    """
    if not snapshot_id:
        return None
    row = session.get(models.TierSnapshot, snapshot_id)
    if row is None:
        return None
    return {"tier_snapshot_id": row.id, **dict(row.document_json)}


def _turn_document(row: models.Turn) -> dict[str, Any]:
    """One turn with everything spec §9 names, and its content under the retention rule."""
    return {
        "turn_id": row.id,
        "sequence": row.sequence,
        "role": row.role,
        "content": content_or_removed(row.content_text, row.content_hash),
        "tier": row.tier,
        "intent_id": row.intent_id,
        "intent_revision": row.intent_revision,
        "model": {
            "canonical_id": row.model_canonical_id,
            "provider_kind": row.model_provider_kind,
            "provider_name": row.model_provider_name,
            "digest": row.model_digest,
        },
        "adapter": (
            None
            if row.adapter_name is None
            else {
                "name": row.adapter_name,
                "digest": row.adapter_digest,
                "source_digest": row.adapter_source_digest,
            }
        ),
        "finish_reason": row.finish_reason,
        "usage": {
            "input_tokens": _count(row.input_tokens),
            "output_tokens": _count(row.output_tokens),
            "thinking_tokens": _count(row.thinking_tokens),
            "cache_write_tokens": _count(row.cache_write_tokens),
            "cache_read_tokens": _count(row.cache_read_tokens),
        },
        "loadcoach_job_id": row.loadcoach_job_id,
        "loadcoach_ms": row.loadcoach_ms,
        "overhead_ms": row.overhead_ms,
        "tool_call_id": row.tool_call_id,
        "prompt": {
            "prompt_id": row.prompt_id,
            "version": row.prompt_version,
            "sha256": row.prompt_sha256,
        },
        # Model output, so it follows the scrub exactly as transcript text does.
        "tool_calls": row.tool_calls_json if row.tool_calls_json is not None else [],
        "created_at": to_rfc3339(row.created_at),
    }


def _attempt_documents(session: Session, trajectory_id: str) -> dict[str, list[dict[str, Any]]]:
    """The failed attempts per step, read from ``step.retried`` and keyed by ``failed_turn_id``.

    The turn a retry replaced was announced and never answered, so it has an event and no row; a
    composition that read attempts from ``turns`` would report a step that failed twice as a step
    that never failed. Adjacency is not a join here either — the event names the turn it is about.
    """
    rows = session.execute(
        select(models.Event)
        .where(
            models.Event.trajectory_id == trajectory_id,
            models.Event.event_type == EventType.STEP_RETRIED.value,
        )
        .order_by(models.Event.sequence)
    ).scalars()
    attempts: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        data = dict(row.data_json)
        step_id = str(data.get("step_id", ""))
        attempts.setdefault(step_id, []).append(
            {
                "attempt": data.get("attempt"),
                "failed_turn_id": data.get("failed_turn_id"),
                "failed_tier": data.get("failed_tier"),
                "cause": data.get("cause"),
                "error_code": data.get("error_code"),
                "at": to_rfc3339(row.timestamp),
            }
        )
    return attempts


def _count(value: int | None) -> int | str:
    """A number, or ``"unsupported"`` — never a zero standing in for an unreported class."""
    return value if value is not None else "unsupported"
