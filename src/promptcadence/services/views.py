"""promptcadence.services.views — the trajectory and turn views, and the row-to-value mapping.

Shared by the trajectory service (which reads and cancels) and the loop (which claims and runs),
so neither imports the other. A view is the row as a frozen value object: the ORM object never
leaves the repository layer (coding standards §4), and every API document and CLI line is rendered
from one of these.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from baseaicore import DataClassification, Money, ValidationError
from baseaicore.timeutil import to_rfc3339

from promptcadence.domain.threads import Turn
from promptcadence.domain.trajectory import (
    TrajectoryDeclaration,
    TrajectoryState,
    WindowWait,
)

if TYPE_CHECKING:
    from promptcadence.domain.intent import TurnProvenance
    from promptcadence.infrastructure.db import models

__all__ = ["TrajectoryView", "TurnView", "declaration_of", "view_of"]


@dataclass(frozen=True, slots=True)
class TrajectoryView:
    """One trajectory as the API and CLI show it: the row, never the ORM object."""

    trajectory_id: str
    task: str
    state: TrajectoryState
    classification: DataClassification
    project: str | None
    tools: tuple[str, ...]
    bypass_planning: bool
    tier_override: str | None
    max_turns: int | None
    max_steps: int | None
    token_budget: int | None
    money_budget: Money | None
    partial_pricing: str | None
    """The request's ``partial_pricing`` override, or ``None`` for ``[budget] partial_pricing``.

    Three-valued on purpose (ADR-0069): ``None`` is "whatever the operator configured", which is
    not the same as either ``"floor"`` or ``"strict"`` written down. A request that pinned the
    default still pinned it, and a later configuration change must not silently move it.
    """
    window: WindowWait | None
    """The persisted ``awaiting_window`` clock, or ``None`` when the trajectory is not parked."""
    tier_snapshot_id: str | None
    approval_policy_version: str | None
    halted_reason: str | None
    error_code: str | None
    cancel_requested: bool
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition can happen."""
        return self.state.is_terminal

    def as_json(self) -> dict[str, Any]:
        """Return the API document (spec §7.1). ``cause`` is the verbatim halt/failure reason."""
        return {
            "trajectory_id": self.trajectory_id,
            "task": self.task,
            "state": self.state.value,
            "data_classification": self.classification.value,
            "project": self.project,
            "tools": list(self.tools),
            "bypass_planning": self.bypass_planning,
            "tier": self.tier_override,
            "max_turns": self.max_turns,
            "max_steps": self.max_steps,
            "budget": {
                "tokens": self.token_budget,
                "money": self.money_budget.as_canonical() if self.money_budget else None,
                "partial_pricing": self.partial_pricing,
            },
            "window_wait": (
                {
                    "parked_from": self.window.parked_from.value,
                    "next_edge_at": to_rfc3339(self.window.next_edge_at),
                    "days_waited": self.window.days_waited,
                }
                if self.window is not None
                else None
            ),
            "tier_snapshot_id": self.tier_snapshot_id,
            "approval_policy_version": self.approval_policy_version,
            "cause": self.halted_reason,
            "error_code": self.error_code,
            "cancel_requested": self.cancel_requested,
            "lease": {
                "owner": self.lease_owner,
                "expires_at": to_rfc3339(self.lease_expires_at) if self.lease_expires_at else None,
            },
            "created_at": to_rfc3339(self.created_at),
            "updated_at": to_rfc3339(self.updated_at),
            "completed_at": to_rfc3339(self.completed_at) if self.completed_at else None,
        }


@dataclass(frozen=True, slots=True)
class TurnView:
    """One turn as ``GET /trajectories/{id}/turns`` shows it."""

    turn: Turn[TurnProvenance]
    loadcoach_job_id: str | None
    loadcoach_ms: float | None
    overhead_ms: float | None
    created_at: datetime

    def as_json(self) -> dict[str, Any]:
        """Return the API document: the transcript row with its provenance and timings."""
        turn = self.turn
        usage = turn.usage
        return {
            "turn_id": turn.turn_id,
            "sequence": turn.sequence,
            "role": turn.role.value,
            "content": turn.content,
            "content_sha256": turn.content_sha256,
            "tier": turn.provenance.tier,
            "intent_id": turn.provenance.intent_id,
            "intent_revision": turn.provenance.intent_revision,
            "model_canonical_id": turn.model_canonical_id,
            "finish_reason": turn.finish_reason.value if turn.finish_reason else None,
            "usage": None
            if usage is None
            else {
                "input_tokens": _count_json(usage.input_tokens),
                "output_tokens": _count_json(usage.output_tokens),
                "cache_write_tokens": _count_json(usage.cache_write_tokens),
                "cache_read_tokens": _count_json(usage.cache_read_tokens),
            },
            "loadcoach_job_id": self.loadcoach_job_id,
            "loadcoach_ms": self.loadcoach_ms,
            "overhead_ms": self.overhead_ms,
            "created_at": to_rfc3339(self.created_at),
        }


def _count_json(value: object) -> int | str:
    """Render a token count the way LoadCoach does: a number, or ``"unsupported"``."""
    return value if isinstance(value, int) else "unsupported"


def view_of(row: models.Trajectory) -> TrajectoryView:
    """Map a trajectory row onto its view. The ORM object never leaves the repository layer."""
    money = (
        Money(currency=row.budget_money_currency, nanos=row.budget_money_nanos)
        if row.budget_money_currency and row.budget_money_nanos
        else None
    )
    return TrajectoryView(
        trajectory_id=row.id,
        task=row.task,
        state=TrajectoryState(row.status),
        classification=DataClassification(row.data_classification),
        project=row.project,
        tools=tuple(str(tool) for tool in row.tools_json),
        bypass_planning=bool(row.bypass_planning),
        tier_override=row.tier_override,
        max_turns=row.max_turns,
        max_steps=row.max_steps,
        token_budget=row.budget_token_ceiling,
        money_budget=money,
        partial_pricing=row.budget_partial_pricing,
        window=(
            WindowWait(
                parked_from=TrajectoryState(row.window_parked_from),
                next_edge_at=row.window_next_edge_at,
                days_waited=row.window_days_waited,
            )
            if row.window_parked_from is not None and row.window_next_edge_at is not None
            else None
        ),
        tier_snapshot_id=row.tier_snapshot_id,
        approval_policy_version=row.approval_policy_version,
        halted_reason=row.halted_reason,
        error_code=row.error_code,
        cancel_requested=bool(row.cancel_requested),
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def declaration_of(view: TrajectoryView, *, default_max_turns: int) -> TrajectoryDeclaration:
    """Build the caller's declaration — the outer envelope every intent is bounded by.

    Args:
        view: The trajectory.
        default_max_turns: ``execution.max_steps``, used when the request set no cap.

    Returns:
        The declaration.

    Raises:
        ValidationError: If the row carries no token budget — every trajectory is queued with
            one, so this is a row written by something other than :meth:`TrajectoryService.submit`.
    """
    if view.token_budget is None:
        message = f"trajectory {view.trajectory_id} carries no token budget"
        raise ValidationError(message, details={"field": "token_budget"})
    return TrajectoryDeclaration(
        trajectory_id=view.trajectory_id,
        classification=view.classification,
        tool_allowlist=frozenset(view.tools),
        token_budget=view.token_budget,
        money_budget=view.money_budget,
        max_turns=view.max_turns if view.max_turns is not None else default_max_turns,
        project=view.project,
    )
