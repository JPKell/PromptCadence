"""promptcadence.web.routes.egress — ``/egress-decisions`` (spec §7.1).

One read over Commissioner's mounted table. The handler calls one service method and renders
(coding standards §5): which policy decided, why, and how a decision is shaped are
:mod:`promptcadence.services.egress`'s and Commissioner's, and nothing here interprets a verdict.

``def``, not ``async`` — a synchronous database read belongs in Starlette's worker threadpool
(ADR-0003), and this read does not stream.

**Denials and approvals come back through the same endpoint, unfiltered by default.** A surface
that showed only refusals would answer "what was blocked" and not "where did this trajectory's data
go", and the second question is the one spec §11 contract 3 exists to make answerable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baseaicore import ValidationError
from commissioner import Verdict
from fastapi import APIRouter, Query, Request, Response
from mirrorwall import clamp_limit, paginated_response

from promptcadence.services.egress import decision_view
from promptcadence.services.runtime import Runtime

if TYPE_CHECKING:
    from commissioner import EgressDecision

__all__ = ["router"]

router = APIRouter(tags=["egress"])


def _runtime(request: Request) -> Runtime:
    runtime = request.app.state.runtime
    if not isinstance(runtime, Runtime):  # pragma: no cover — only outside the lifespan
        message = "the application is not serving"
        raise RuntimeError(message)
    return runtime


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def _verdict(raw: str | None) -> Verdict | None:
    """Parse the ``verdict`` filter, refusing a value the vocabulary does not hold.

    Args:
        raw: The query value, or ``None`` for no filter.

    Returns:
        The verdict, or ``None``.

    Raises:
        ValidationError: If ``raw`` is not a :class:`~commissioner.Verdict`. Refused rather than
            ignored: a caller who asked for ``verdict=blocked`` and got every decision back would
            read an unfiltered list as a filtered one, which on this endpoint means reading
            approvals as denials.
    """
    if raw is None:
        return None
    try:
        return Verdict(raw)
    except ValueError as exc:
        message = (
            f"verdict must be one of {', '.join(sorted(v.value for v in Verdict))}; got {raw!r}"
        )
        raise ValidationError(message, details={"field": "verdict"}) from exc


@router.get("/egress-decisions", summary="Recorded egress decisions")
def get_egress_decisions(
    request: Request,
    trajectory_id: str | None = Query(default=None),
    verdict: str | None = Query(default=None),
    target: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> Response:
    """Recorded egress decisions, oldest-decided first, narrowed by whichever filters are given.

    Every decision this build made is here — approvals, denials and the violations a verification
    step wrote after the fact — each rendered as SetSpec's ``governance.egress_decision`` 1.0.
    ``source_ref`` names the turn or tool invocation the decision gated, which is how a decision is
    matched back to what it governed.

    ``400 VALIDATION_ERROR`` if ``verdict`` names something outside the vocabulary.
    """
    effective = clamp_limit(limit, maximum=200)
    decisions: list[EgressDecision] = list(
        _runtime(request).egress.decisions(
            run_id=trajectory_id, verdict=_verdict(verdict), target=target
        )
    )[:effective]
    return paginated_response(
        [decision_view(decision) for decision in decisions],
        limit=effective,
        has_more=len(decisions) == effective,
        request_id=_request_id(request),
    )
