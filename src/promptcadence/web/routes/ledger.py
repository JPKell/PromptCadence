"""promptcadence.web.routes.ledger — ``/ledger`` and ``/ledger/entries`` (spec §7.1).

Two reads over the mounted LoadLedger tables. Each handler calls one service method and renders
(coding standards §5): which ceilings are active, what they say and how a floor is written down are
:class:`~promptcadence.services.budget.BudgetService`'s, and nothing here adds up a token.

Handlers are ``def`` — Starlette runs them in the worker threadpool, which is where every
synchronous database read in this application belongs (ADR-0003). Neither read streams, so neither
is ``async``.

**Every money figure crosses this boundary twice**: once as ``{currency, nanos}`` for a caller that
computes, and once as a rendered string for a caller that displays. The second is not a
convenience — it is how "at least 0.004 USD" and "—" reach a UI that would otherwise print a floor
as a total or an unpriced amount as ``$0.00`` (ADR-0069, ADR-0016).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Query, Request, Response
from mirrorwall import clamp_limit, json_response, paginated_response

from promptcadence.services.runtime import Runtime

if TYPE_CHECKING:
    from promptcadence.services.views import TrajectoryView

__all__ = ["router"]

router = APIRouter(tags=["ledger"])


def _runtime(request: Request) -> Runtime:
    runtime = request.app.state.runtime
    if not isinstance(runtime, Runtime):  # pragma: no cover — only outside the lifespan
        message = "the application is not serving"
        raise RuntimeError(message)
    return runtime


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


@router.get("/ledger", summary="Today's ledger position")
def get_ledger(request: Request, trajectory_id: str | None = Query(default=None)) -> Response:
    """Today's position against the per-day ceiling and against each configured project's.

    ``trajectory_id`` adds that trajectory's own per-run position. ``404 TRAJECTORY_NOT_FOUND``
    if it names one that does not exist.
    """
    runtime = _runtime(request)
    trajectory: TrajectoryView | None = (
        runtime.trajectories.get(trajectory_id) if trajectory_id is not None else None
    )
    view = runtime.budget.ledger_view(
        reference_run=trajectory.trajectory_id
        if trajectory is not None
        else runtime.trajectories.most_recent_id(),
        trajectory=trajectory,
    )
    return json_response(view.as_json(), request_id=_request_id(request))


@router.get("/ledger/entries", summary="Recorded debits")
def get_ledger_entries(
    request: Request,
    trajectory_id: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> Response:
    """Recorded debits, newest first, optionally narrowed to one trajectory or one tag.

    Each entry carries its four token counts, its ``pricing_hash`` and every ceiling's verdict as
    of that debit — never a money figure as a fact of its own (ADR-0030 rule 1).
    """
    effective = clamp_limit(limit, maximum=200)
    entries = _runtime(request).budget.entry_views(
        trajectory_id=trajectory_id, tag=tag, limit=effective
    )
    return paginated_response(
        [entry.as_json() for entry in entries],
        limit=effective,
        has_more=len(entries) == effective,
        request_id=_request_id(request),
    )
