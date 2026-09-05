"""promptcadence.services.records — the plan and intent reads behind the record surfaces.

Spec §11 contract 2 says the record is retrievable for the lifetime of the trajectory, and
Phase 7 adds two shapes to it that the turn and event surfaces do not show: the plan (every
drafting attempt, the validated steps with their execution state, and the verdict) and every
intent revision. These are read straight from the rows and rendered as documents; the composed
``promptcadence.trajectory_explanation`` is Phase 8's, and nothing here is a schema it commits to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from baseaicore.timeutil import to_rfc3339
from sqlalchemy import select

from promptcadence.domain.errors import TrajectoryNotFoundError
from promptcadence.infrastructure.db import models
from promptcadence.infrastructure.db.models import ExecutionIntent as ExecutionIntentRow
from promptcadence.services.intents import intent_document

if TYPE_CHECKING:
    from promptcadence.services.database import Database

__all__ = ["RecordReader"]


class RecordReader:
    """Reads over the plan and intent tables. One per process."""

    __slots__ = ("_database",)

    def __init__(self, database: Database) -> None:
        """Bind to the application's database handle."""
        self._database = database

    def plan(self, trajectory_id: str) -> dict[str, Any] | None:
        """The trajectory's plan record, or ``None`` when nothing was drafted.

        Returns:
            ``attempts`` (every drafting attempt, oldest first, with its validity, issues, the
            planning call's subject and token classes, and the prompt it was rendered from),
            ``steps`` (the validated plan's steps with their execution state), and ``approval``
            (the recorded verdict), or ``None``.

        Raises:
            TrajectoryNotFoundError: No such trajectory.
        """
        with self._database.read() as session:
            if session.get(models.Trajectory, trajectory_id) is None:
                raise TrajectoryNotFoundError(
                    f"No trajectory {trajectory_id!r}.", details={"trajectory_id": trajectory_id}
                )
            attempts = list(
                session.execute(
                    select(models.Plan)
                    .where(models.Plan.trajectory_id == trajectory_id)
                    .order_by(models.Plan.attempt)
                ).scalars()
            )
            if not attempts:
                return None
            valid = next((row for row in reversed(attempts) if row.valid), None)
            steps: list[dict[str, Any]] = []
            approval: dict[str, Any] | None = None
            if valid is not None:
                steps = [
                    {
                        "step_id": row.step_id,
                        "sequence": row.sequence,
                        "description": row.description,
                        "depends_on": list(row.depends_on_json),
                        "tools": list(row.tools_json),
                        "tier": row.tier,
                        "data_classification": row.data_classification,
                        "expected_turns": row.expected_turns,
                        "status": row.status,
                        "started_at": to_rfc3339(row.started_at) if row.started_at else None,
                        "completed_at": (
                            to_rfc3339(row.completed_at) if row.completed_at else None
                        ),
                    }
                    for row in session.execute(
                        select(models.PlanStep)
                        .where(models.PlanStep.plan_id == valid.id)
                        .order_by(models.PlanStep.sequence)
                    ).scalars()
                ]
                verdict = session.execute(
                    select(models.PlanApproval)
                    .where(models.PlanApproval.plan_id == valid.id)
                    .order_by(models.PlanApproval.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if verdict is not None:
                    approval = {
                        "approval_id": verdict.id,
                        "outcome": verdict.outcome,
                        "approval_policy_version": verdict.approval_policy_version,
                        "verdict": dict(verdict.verdict_json),
                        "created_at": to_rfc3339(verdict.created_at),
                    }
            return {
                "trajectory_id": trajectory_id,
                "plan_id": valid.id if valid is not None else None,
                "attempts": [_attempt_json(row) for row in attempts],
                "steps": steps,
                "approval": approval,
            }

    def intents(self, trajectory_id: str) -> list[dict[str, Any]]:
        """Every intent revision the trajectory minted, in ``(intent_id, revision)`` order.

        Raises:
            TrajectoryNotFoundError: No such trajectory.
        """
        with self._database.read() as session:
            if session.get(models.Trajectory, trajectory_id) is None:
                raise TrajectoryNotFoundError(
                    f"No trajectory {trajectory_id!r}.", details={"trajectory_id": trajectory_id}
                )
            rows = session.execute(
                select(ExecutionIntentRow)
                .where(ExecutionIntentRow.trajectory_id == trajectory_id)
                .order_by(
                    ExecutionIntentRow.step_id,
                    ExecutionIntentRow.intent_id,
                    ExecutionIntentRow.revision,
                )
            ).scalars()
            return [intent_document(row) for row in rows]


def _attempt_json(row: models.Plan) -> dict[str, Any]:
    return {
        "plan_id": row.id,
        "attempt": row.attempt,
        "valid": bool(row.valid),
        "document_sha256": row.document_sha256,
        "raw_document": row.raw_document,
        "issues": list(row.issues_json) if row.issues_json is not None else [],
        "loadcoach_job_id": row.loadcoach_job_id,
        "model_canonical_id": row.model_canonical_id,
        "usage": {
            "input_tokens": _count(row.input_tokens),
            "output_tokens": _count(row.output_tokens),
            "cache_write_tokens": _count(row.cache_write_tokens),
            "cache_read_tokens": _count(row.cache_read_tokens),
        },
        "loadcoach_ms": row.loadcoach_ms,
        "prompt": {
            "prompt_id": row.prompt_id,
            "version": row.prompt_version,
            "sha256": row.prompt_sha256,
        },
        "created_at": to_rfc3339(row.created_at),
    }


def _count(value: int | None) -> int | str:
    return value if value is not None else "unsupported"
