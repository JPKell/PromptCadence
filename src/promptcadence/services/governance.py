"""promptcadence.services.governance — the recorded envelope a trajectory is governed under.

The loop, the approval service and the reads all need the same four things about a trajectory
before they can decide anything: its view, the declaration rebuilt from it, the tier snapshot it
was submitted under (wrapped with today's availability) and the approval policy as configured.
Building them in one place keeps the three callers from disagreeing about which snapshot governs
— it is always the **recorded** one (lifecycle §3), never today's configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from baseaicore import ValidationError

from promptcadence.domain.tiers import TierPolicy
from promptcadence.infrastructure.db import models
from promptcadence.services.policy_assembly import (
    approval_policy_from_settings,
    tier_snapshot_from_document,
)
from promptcadence.services.views import TrajectoryView, declaration_of

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from promptcadence.config import Settings
    from promptcadence.domain.policy import ApprovalPolicy
    from promptcadence.domain.trajectory import TrajectoryDeclaration

__all__ = ["GovernanceContext", "load_context", "tier_policy_of"]


@dataclass(frozen=True, slots=True)
class GovernanceContext:
    """What every decision about one trajectory is made against."""

    view: TrajectoryView
    declaration: TrajectoryDeclaration
    tier_policy: TierPolicy
    approval_policy: ApprovalPolicy


def tier_policy_of(
    session: Session, view: TrajectoryView, *, loadcoach_has_remote_provider: bool = False
) -> TierPolicy:
    """The trajectory's own recorded snapshot, wrapped with today's availability.

    Args:
        session: An open session.
        view: The trajectory.
        loadcoach_has_remote_provider: Whether LoadCoach has a remote provider registered.
            ``False`` until LC-E1 lands (lifecycle §3); a parameter rather than a constant so
            the hybrid egress gate on the planned path can be exercised against a fake that
            plays one, and so the fact arrives from LoadCoach rather than from configuration
            when it does.

    Raises:
        ValidationError: The trajectory records no snapshot, or the recorded document does not
            match its content address.
    """
    row = (
        session.get(models.TierSnapshot, view.tier_snapshot_id)
        if view.tier_snapshot_id is not None
        else None
    )
    if row is None:
        message = f"trajectory {view.trajectory_id} records no tier snapshot"
        raise ValidationError(message, details={"field": "tier_snapshot_id"})
    snapshot = tier_snapshot_from_document(row.document_json)
    if snapshot.snapshot_id != view.tier_snapshot_id:  # pragma: no cover — a corrupt row
        message = "the recorded tier snapshot does not match its content address"
        raise ValidationError(message, details={"field": "tier_snapshot_id"})
    return TierPolicy(
        snapshot=snapshot, loadcoach_has_remote_provider=loadcoach_has_remote_provider
    )


def load_context(
    session: Session,
    view: TrajectoryView,
    settings: Settings,
    *,
    loadcoach_has_remote_provider: bool = False,
) -> GovernanceContext:
    """Build the governance context for one trajectory from its rows and the configuration.

    Args:
        session: An open session.
        view: The trajectory.
        settings: The validated configuration — the approval policy's source, and the bypass
            loop's default ``max_turns``.
        loadcoach_has_remote_provider: See :func:`tier_policy_of`.

    Returns:
        The context.

    Raises:
        ValidationError: The rows are incomplete (see :func:`tier_policy_of`).
    """
    return GovernanceContext(
        view=view,
        declaration=declaration_of(view, default_max_turns=settings.execution.max_steps),
        tier_policy=tier_policy_of(
            session, view, loadcoach_has_remote_provider=loadcoach_has_remote_provider
        ),
        approval_policy=approval_policy_from_settings(settings),
    )
