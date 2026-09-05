"""promptcadence.web.routes.approvals — ``/approvals`` and ``/trajectories/{id}/approve|deny``.

Spec §7.1. Every handler calls one service method and renders (coding standards §5): what a grant
mints, what a denial records and what "pending" means are
:class:`~promptcadence.services.approvals.ApprovalService`'s. The two resolving routes require the
``approve`` scope (spec §14, ADR-0049 rule 2) — established by :mod:`promptcadence.web.auth` and
handed to the service as the identity every minted intent records.

``def``, not ``async``: synchronous database writes belong in Starlette's worker threadpool
(ADR-0003), and neither route streams.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from baseaicore import Money
from fastapi import APIRouter, Query, Request, Response, status
from mirrorwall import json_response, paginated_response
from pydantic import BaseModel, ConfigDict, Field

from promptcadence.services.approvals import Approver, BudgetRaise, RequestStatus
from promptcadence.services.runtime import Runtime
from promptcadence.web.auth import require_scope

__all__ = ["ApproveBody", "DenyBody", "router"]

router = APIRouter(tags=["approvals"])


class MoneyBody(BaseModel):
    """A money amount as ``{currency, nanos}``."""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(default="USD")
    nanos: int = Field(ge=1)


class RaiseBody(BaseModel):
    """The new per-trajectory ceiling offered with a ``ceiling_raise`` grant."""

    model_config = ConfigDict(extra="forbid")

    tokens: int | None = Field(default=None, ge=1)
    money: MoneyBody | None = Field(default=None)


class ApproveBody(BaseModel):
    """``POST /trajectories/{id}/approve``'s optional body: a budget, for a ceiling raise only."""

    model_config = ConfigDict(extra="forbid")

    budget: RaiseBody | None = Field(default=None)


class DenyBody(BaseModel):
    """``POST /trajectories/{id}/deny``'s optional body: the stated reason."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


def _runtime(request: Request) -> Runtime:
    runtime = request.app.state.runtime
    if not isinstance(runtime, Runtime):  # pragma: no cover — only outside the lifespan
        message = "the application is not serving"
        raise RuntimeError(message)
    return runtime


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def _raise_of(body: ApproveBody | None) -> BudgetRaise | None:
    if body is None or body.budget is None:
        return None
    money = (
        Money(currency=body.budget.money.currency, nanos=body.budget.money.nanos)
        if body.budget.money is not None
        else None
    )
    return BudgetRaise(token_ceiling=body.budget.tokens, money_ceiling=money)


@router.get("/approvals", summary="Pending approval requests")
def list_approvals(
    request: Request,
    trajectory_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
) -> Response:
    """Every pending request with its age, oldest first; ``status=all`` lists resolved ones too.

    ``read`` scope. Each item names the trajectory, the kind of question (a held plan, a gated
    step, the bypass gate, a scoped re-approval, a ceiling raise), the steps it is scoped to, what
    it asks (``detail``), when it expires, and how long it has waited.
    """
    require_scope(request, "read")
    runtime = _runtime(request)
    now = datetime.now(UTC)
    if status_filter == "all" and trajectory_id is not None:
        items = runtime.approvals.requests(trajectory_id)
    elif status_filter in (None, RequestStatus.PENDING.value):
        items = runtime.approvals.pending(trajectory_id=trajectory_id)
    else:
        items = [
            item
            for item in (
                runtime.approvals.requests(trajectory_id)
                if trajectory_id is not None
                else runtime.approvals.pending()
            )
            if status_filter == "all" or item.status.value == status_filter
        ]
    documents: list[dict[str, Any]] = [item.as_json(now=now) for item in items]
    return paginated_response(
        documents, limit=max(len(documents), 1), has_more=False, request_id=_request_id(request)
    )


@router.post(
    "/trajectories/{trajectory_id}/approve",
    status_code=status.HTTP_200_OK,
    summary="Grant the trajectory's pending approval request",
)
def post_approve(request: Request, trajectory_id: str, body: ApproveBody | None = None) -> Response:
    """T8: mint what the pending request asked for, under the caller's identity.

    Requires the ``approve`` scope (``401``/``403`` otherwise). Idempotent per request: granting
    an already-granted request returns ``200`` with ``already_resolved: true`` and changes
    nothing. ``409 APPROVAL_INVALID_STATE`` when nothing is pending or the last request was
    denied or expired; ``400 VALIDATION_ERROR`` when a ceiling raise offers no budget or a budget
    is offered to a request that is not a raise.
    """
    principal = require_scope(request, "approve")
    runtime = _runtime(request)
    outcome = runtime.approvals.grant(
        trajectory_id,
        approver=Approver(token_id=principal.token_id, name=principal.name),
        budget_raise=_raise_of(body),
    )
    runtime.worker.wake()
    document = {
        "trajectory_id": trajectory_id,
        "state": outcome.state.value,
        "already_resolved": outcome.already_resolved,
        "request": outcome.request.as_json(now=datetime.now(UTC)),
        "minted": [
            {"intent_id": intent.intent_id, "revision": intent.revision, "step_id": intent.step_id}
            for intent in outcome.minted
        ],
    }
    return json_response(document, request_id=_request_id(request))


@router.post(
    "/trajectories/{trajectory_id}/deny",
    status_code=status.HTTP_200_OK,
    summary="Deny the trajectory's pending approval request",
)
def post_deny(request: Request, trajectory_id: str, body: DenyBody | None = None) -> Response:
    """T9: halt the trajectory with the denial recorded, under the caller's identity.

    Requires the ``approve`` scope. Idempotent per request. ``409 APPROVAL_INVALID_STATE`` when
    nothing is pending and the last request was not denied.
    """
    principal = require_scope(request, "approve")
    runtime = _runtime(request)
    view = runtime.approvals.deny(
        trajectory_id,
        approver=Approver(token_id=principal.token_id, name=principal.name),
        reason=body.reason if body is not None else None,
    )
    document = {
        "trajectory_id": trajectory_id,
        "request": view.as_json(now=datetime.now(UTC)),
        "state": runtime.trajectories.get(trajectory_id).state.value,
    }
    return json_response(document, request_id=_request_id(request))
