"""promptcadence.services.retention — content retention for finished trajectories (spec §14).

Transcript and tool-output text follows LoadCoach's retention model: a terminal trajectory keeps
its words for ``[storage] content_retention_hours`` and then loses them, while hashes, usage,
decisions and events stay, so the trajectory remains explicable. A trajectory still in flight is
never touched, whatever its age — the sweep is about finished work.

**What one sweep removes** from a trajectory past the cutoff, all model or caller text:

* ``turns.content_text`` and ``turns.tool_calls_json`` — the transcript and what an assistant
  turn asked for. The summary thread's turns are turns, so a compaction summary goes with them.
* ``plans.raw_document`` and every step description, in ``plan_steps.description`` and inside
  ``plans.validated_json`` — the plan document is model output (I1 made the column nullable for
  exactly this), and a description is free text quoted verbatim into the executing context.
* ``tool_call_records.args_json``, ``result_summary`` and ``reason_detail`` — the arguments, what
  the model saw of the result, and a refusal's detail, which quotes the candidate path or host the
  model chose. ``reason`` stays: it is the decision.
* ``trajectories.task`` — the caller's words, replaced by the marker rather than nulled because the
  column is what every listing shows.
* **The trajectory's workspace directory** — content on disk, swept with the content it produced.

**What survives**, and does: every digest (``content_hash``, ``args_sha256``, ``result_sha256``,
``document_sha256``), every model identity, tier, usage figure, ceiling verdict, egress decision,
deviation, approval and event. The explanation's ``content.removed`` flags say the words are gone;
``trajectories.content_scrubbed_at`` says when, and is what makes a second pass skip the first's
work.

**The explanation is invalidated afterwards, in the same call**, because a materialized revision
composed before the scrub holds the words the scrub removed (ADR-0092, I1 §7.3). The rows are
written and committed first, then ``invalidate(cause="retention_scrub")`` composes from them as
they then stand. A trajectory with no revision is left to the live path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from promptcadence.domain.explanation import CONTENT_REMOVED
from promptcadence.infrastructure.db import models

if TYPE_CHECKING:
    from datetime import datetime

    from promptcadence.services.database import Database
    from promptcadence.services.explanation import ExplanationBuilder
    from promptcadence.services.tools import ToolPlant

__all__ = ["TERMINAL_STATES", "RetentionOutcome", "scrub_content"]

logger = logging.getLogger(__name__)

TERMINAL_STATES: tuple[str, ...] = ("completed", "halted", "failed", "cancelled", "rejected")


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    """What one sweep did.

    Attributes:
        scrubbed: The trajectories whose text was removed in this pass, in ``completed_at`` order.
        workspaces_removed: How many of them had a workspace directory to remove.
        revisions_bumped: How many had a materialized explanation, now superseded by one composed
            from the scrubbed rows.
        cutoff: The instant a trajectory had to have finished before to be selected.
    """

    scrubbed: tuple[str, ...]
    workspaces_removed: int
    revisions_bumped: int
    cutoff: datetime


def _scrubbed_steps(document: Any) -> Any:
    """``plans.validated_json`` with every step's ``description`` replaced by the marker."""
    if not isinstance(document, dict):
        return document
    steps = document.get("steps")
    if not isinstance(steps, list):
        return document
    cleaned = dict(document)
    cleaned["steps"] = [
        {**step, "description": CONTENT_REMOVED} if isinstance(step, dict) else step
        for step in steps
    ]
    return cleaned


def scrub_content(
    database: Database,
    *,
    now: datetime,
    retention_hours: int,
    tools: ToolPlant,
    explanations: ExplanationBuilder,
    batch_size: int = 200,
) -> RetentionOutcome:
    """Remove the words from trajectories finished before ``now - retention_hours``.

    Idempotent: a trajectory carrying ``content_scrubbed_at`` is never selected again. A
    trajectory that is not terminal is never touched, whatever its age — its workspace is the
    files an operator reads while diagnosing the run that is still producing them, and its rows
    are still being written.

    Args:
        database: The application's database handle.
        now: The sweep instant, stamped as ``content_scrubbed_at``.
        retention_hours: How long finished text is kept.
        tools: The process's tool plant, which owns the workspace root; its ``sweep_workspace``
            removes a trajectory's directory.
        explanations: The explanation builder whose ``invalidate`` supersedes a revision composed
            before the scrub.
        batch_size: The most trajectories one sweep scrubs, so a first sweep over a large history
            is bounded; the next sweep continues.

    Returns:
        The :class:`RetentionOutcome`. A sweep that found nothing past the cutoff returns one with
        no trajectories and is not an error.

    Refuses nothing and raises nothing of its own: a workspace that cannot be removed is logged
    and the rows are scrubbed regardless, because a directory that outlives its text is a smaller
    failure than text that outlives its retention.
    """
    cutoff = now - timedelta(hours=retention_hours)
    with database.read() as session:
        targets = list(
            session.execute(
                select(models.Trajectory.id)
                .where(
                    models.Trajectory.status.in_(TERMINAL_STATES),
                    models.Trajectory.completed_at.is_not(None),
                    models.Trajectory.completed_at <= cutoff,
                    models.Trajectory.content_scrubbed_at.is_(None),
                )
                .order_by(models.Trajectory.completed_at, models.Trajectory.id)
                .limit(batch_size)
            ).scalars()
        )
    workspaces = 0
    bumped = 0
    for trajectory_id in targets:
        with database.write() as session:
            session.execute(
                update(models.Turn)
                .where(models.Turn.trajectory_id == trajectory_id)
                .values(content_text=None, tool_calls_json=None)
            )
            for plan in session.execute(
                select(models.Plan).where(models.Plan.trajectory_id == trajectory_id)
            ).scalars():
                plan.raw_document = None
                plan.validated_json = _scrubbed_steps(plan.validated_json)
                session.execute(
                    update(models.PlanStep)
                    .where(models.PlanStep.plan_id == plan.id)
                    .values(description=CONTENT_REMOVED)
                )
            session.execute(
                update(models.ToolCallRecord)
                .where(models.ToolCallRecord.trajectory_id == trajectory_id)
                .values(args_json=None, result_summary=None, reason_detail=None)
            )
            session.execute(
                update(models.Trajectory)
                .where(models.Trajectory.id == trajectory_id)
                .values(task=CONTENT_REMOVED, content_scrubbed_at=now)
            )
        try:
            if tools.sweep_workspace(trajectory_id):
                workspaces += 1
        except OSError:
            logger.warning(
                "retention.workspace_not_removed", extra={"trajectory_id": trajectory_id}
            )
        if explanations.current_revision(trajectory_id) is not None:
            explanations.invalidate(trajectory_id, cause="retention_scrub", now=now)
            bumped += 1
    if targets:
        logger.info(
            "retention.scrubbed",
            extra={"trajectories": len(targets), "workspaces": workspaces, "revisions": bumped},
        )
    return RetentionOutcome(
        scrubbed=tuple(targets),
        workspaces_removed=workspaces,
        revisions_bumped=bumped,
        cutoff=cutoff,
    )
