"""promptcadence.services.console — the reports the operator console's pages render.

One function per page, each returning a plain mapping the template walks. Route handlers call one
of these and render; no page assembles its own figures (coding standards §5), and nothing here
knows about Jinja, HTML or FastAPI.

**Every figure is read from the surface that owns it**, never re-derived here. The ledger position
comes from :class:`~promptcadence.services.budget.BudgetService`, so a tier's *spend* is not
rendered as headroom and an unpriced amount is an em dash rather than ``$0.00`` (ADR-0016,
ADR-0030). Egress decisions come from Commissioner's payload. The timeline is the composed
``promptcadence.trajectory_explanation`` document — the same bytes ``GET
/trajectories/{id}/explanation`` returns — so the page and the API cannot drift, and every record
type the document holds is a record type the timeline can render.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from baseaicore.timeutil import to_rfc3339
from mirrorwall import ComponentHealth

from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.services.compaction import COMPACTION_STEP_PREFIX
from promptcadence.services.egress import decision_view
from promptcadence.services.policy_assembly import tier_snapshot_from_settings
from promptcadence.services.tools import isolation_payload

if TYPE_CHECKING:
    from datetime import datetime

    from promptcadence.config import Settings
    from promptcadence.services.runtime import Runtime

__all__ = [
    "approvals_report",
    "dashboard_report",
    "egress_report",
    "ledger_report",
    "system_report",
    "tiers_report",
    "timeline_report",
    "tools_report",
    "trajectories_report",
]

_ACTIVE_STATES = (TrajectoryState.QUEUED, TrajectoryState.PLANNING, TrajectoryState.EXECUTING)
_WAITING_STATES = (TrajectoryState.AWAITING_APPROVAL, TrajectoryState.AWAITING_WINDOW)
_TERMINAL_STATES = (
    TrajectoryState.COMPLETED,
    TrajectoryState.HALTED,
    TrajectoryState.FAILED,
    TrajectoryState.CANCELLED,
    TrajectoryState.REJECTED,
)

PAGE_SIZE = 50
"""How many rows a listing page shows. A cap rather than a setting: the console is an operator's
window onto a database, and a page that offered to render fifty thousand rows would be offering to
hang the browser."""


def dashboard_report(runtime: Runtime, *, now: datetime) -> dict[str, Any]:
    """What is running, what is waiting for a person, and today's spend.

    Every headline figure links to the page that owns it, so nothing here is more than two
    interactions from its raw record (UI standards §5).
    """
    counts = {
        state.value: _count(runtime, state)
        for state in (*_ACTIVE_STATES, *_WAITING_STATES, *_TERMINAL_STATES)
    }
    pending = [item.as_json(now=now) for item in runtime.approvals.pending()]
    recent, _ = runtime.trajectories.list(limit=10)
    return {
        "counts": counts,
        "active": sum(counts[state.value] for state in _ACTIVE_STATES),
        "waiting": sum(counts[state.value] for state in _WAITING_STATES),
        "pending_approvals": pending,
        "oldest_approval_age_seconds": max((one["age_seconds"] for one in pending), default=None),
        "recent": [view.as_json() for view in recent],
        "ledger": runtime.budget.ledger_view(trajectory=None).as_json(),
        "loadcoach_base_url": runtime.settings.loadcoach.base_url,
        "max_concurrent": runtime.settings.execution.max_concurrent_trajectories,
    }


def _count(runtime: Runtime, state: TrajectoryState) -> int:
    """How many trajectories are in that state, capped at :data:`PAGE_SIZE`.

    A capped count, and the dashboard says so: the alternative is a ``COUNT(*)`` per state on every
    page view, and what the number is for is "is anything stuck", which a cap answers.
    """
    page, more = runtime.trajectories.list(state=state, limit=PAGE_SIZE)
    return len(page) if more is None else PAGE_SIZE


def trajectories_report(
    runtime: Runtime, *, state: str | None = None, cursor: str | None = None
) -> dict[str, Any]:
    """The trajectory list, newest first, optionally narrowed to one state."""
    parsed = TrajectoryState(state) if state else None
    page, next_cursor = runtime.trajectories.list(state=parsed, limit=PAGE_SIZE, cursor=cursor)
    return {
        "state": state or "",
        "states": [one.value for one in TrajectoryState],
        "rows": [view.as_json() for view in page],
        "next_cursor": next_cursor,
        "row_count": len(page),
    }


def timeline_report(runtime: Runtime, trajectory_id: str) -> dict[str, Any]:
    """One trajectory's timeline — the composed explanation, plus where it came from.

    The page renders the **document**, not a second reading of the rows: the timeline and
    ``GET /trajectories/{id}/explanation`` are the same bytes, so a record type that appears in one
    appears in the other. ``source`` says whether a materialized revision or a live composition
    answered, which is the honest thing to show beside a figure whose cost differs by two orders of
    magnitude (spec §15).

    Raises:
        TrajectoryNotFoundError: No such trajectory.
    """
    read = runtime.explanations.read(trajectory_id)
    document = read.document
    revision = read.revision
    threads = document["threads"]
    return {
        "document": document,
        "trajectory": document["trajectory"],
        "source": read.source,
        "composed_ms": round(read.composed_ms, 3),
        "revision": revision.revision if revision is not None else None,
        "revision_cause": revision.cause if revision is not None else None,
        "step_threads": [one for one in threads if one["kind"] == "step"],
        "compaction_threads": [one for one in threads if one["kind"] == "compaction"],
        "turn_count": sum(len(one["turns"]) for one in threads),
        "compaction_prefix": COMPACTION_STEP_PREFIX,
    }


def approvals_report(
    runtime: Runtime, *, now: datetime, include_resolved: bool = False
) -> dict[str, Any]:
    """The inbox: every pending request, oldest first, with what a grant would widen.

    ``include_resolved`` adds the history, which is what an operator asking "what did I approve
    last week" needs and what a review needs to see denials in.
    """
    pending = [item.as_json(now=now) for item in runtime.approvals.pending()]
    resolved: list[dict[str, Any]] = []
    if include_resolved:
        seen = {one["request_id"] for one in pending}
        for view in runtime.trajectories.list(limit=PAGE_SIZE)[0]:
            resolved.extend(
                item.as_json(now=now)
                for item in runtime.approvals.requests(view.trajectory_id)
                if item.request_id not in seen
            )
        resolved.sort(key=lambda one: one["created_at"], reverse=True)
    return {
        "pending": pending,
        "resolved": resolved,
        "include_resolved": include_resolved,
        "row_count": len(pending),
    }


def tiers_report(
    settings: Settings,
    *,
    loadcoach_has_remote_provider: bool,
    unpriced: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Every configured tier, its ceiling and whether it can serve right now.

    Availability is a fact about the deployment rather than about the configuration: a remote tier
    is unavailable until LoadCoach has a remote provider registered (lifecycle §3) and its price
    list holds a record claiming now (spec §11 contract 5) — one recorded reason, in that order
    (ADR-0098 rule 3) — and saying so beside the tier is the difference between "misconfigured"
    and "not wired up yet".
    """
    snapshot = tier_snapshot_from_settings(settings)
    return {
        "snapshot_id": snapshot.snapshot_id,
        "default_tier": snapshot.default_tier,
        "escalation_order": list(snapshot.escalation_order),
        "remote_provider": loadcoach_has_remote_provider,
        "rows": [
            {
                **tier.as_canonical(),
                "is_remote": tier.is_remote,
                "effective_max_classification": tier.effective_max_classification.value,
                "available": (not tier.is_remote)
                or (loadcoach_has_remote_provider and tier.name not in unpriced),
                "unavailable_reason": (
                    None
                    if not tier.is_remote
                    else "loadcoach_has_no_remote_provider"
                    if not loadcoach_has_remote_provider
                    else "unpriced"
                    if tier.name in unpriced
                    else None
                ),
            }
            for tier in snapshot.tiers
        ],
        "row_count": len(snapshot.tiers),
    }


def tools_report(runtime: Runtime) -> dict[str, Any]:
    """The registry, including what configuration named and could not be registered.

    A list of only the working tools cannot tell a tool that was never asked for from one that was
    asked for and withheld, which is the question an operator is usually here to answer.
    """
    plant = runtime.tools
    return {
        "rows": [entry.as_payload() for entry in plant.catalog()],
        "isolation": isolation_payload(plant.isolation()),
        "row_count": len(plant.catalog()),
    }


def ledger_report(runtime: Runtime, *, trajectory_id: str | None = None) -> dict[str, Any]:
    """Today's position and the recorded debits behind it.

    Rendered from the budget service's own views, so ``—`` stays ``—`` and a floor stays "at
    least": a console that formatted money itself would be the second place the suite decided what
    an unpriced amount looks like.
    """
    trajectory = runtime.trajectories.get(trajectory_id) if trajectory_id else None
    entries = runtime.budget.entry_views(trajectory_id=trajectory_id, limit=PAGE_SIZE)
    return {
        "position": runtime.budget.ledger_view(trajectory=trajectory).as_json(),
        "entries": [entry.as_json() for entry in entries],
        "trajectory_id": trajectory_id or "",
        "row_count": len(entries),
    }


def egress_report(
    runtime: Runtime, *, verdict: str | None = None, trajectory_id: str | None = None
) -> dict[str, Any]:
    """Every recorded egress decision, oldest-decided first, narrowable by verdict.

    A refusal is as auditable as an approval (spec §11 contract 3), so denials and violations are
    on the same page as approvals rather than behind a filter that has to be discovered.
    """
    from commissioner import Verdict

    decisions = runtime.egress.decisions(
        run_id=trajectory_id or None,
        verdict=Verdict(verdict) if verdict else None,
    )
    rows = [decision_view(one) for one in decisions][-PAGE_SIZE:]
    return {
        "rows": list(reversed(rows)),
        "verdict": verdict or "",
        "verdicts": [one.value for one in Verdict],
        "trajectory_id": trajectory_id or "",
        "row_count": len(rows),
    }


def _component_json(component: ComponentHealth) -> dict[str, Any]:
    """One health component as the page walks it — the same fields ``GET /health`` reports."""
    return {
        "name": component.name,
        "status": component.status.value,
        "detail": component.detail,
        "checked_at": to_rfc3339(component.checked_at) if component.checked_at else None,
        "data": dict(component.data or {}),
    }


def system_report(runtime: Runtime, *, now: datetime) -> dict[str, Any]:
    """Health components, the recovery pass, and how this install authenticates.

    The last of those is on the page deliberately (ADR-0094 rule 2): an operator who cannot see
    that the console is loopback-first, and what it would take to change that, will discover it
    from a browser on another machine getting a 401.
    """
    components = [_component_json(component()) for component in runtime.health_checkers]
    summary = runtime.worker.last_recovery
    return {
        "components": components,
        "degraded": [one for one in components if one["status"] != "ok"],
        "last_recovery": summary.as_json() if summary is not None else None,
        "bind_host": runtime.settings.server.host,
        "bind_port": runtime.settings.server.port,
        "loadcoach_base_url": runtime.settings.loadcoach.base_url,
        "max_concurrent": runtime.settings.execution.max_concurrent_trajectories,
        "checked_at": to_rfc3339(now),
    }
