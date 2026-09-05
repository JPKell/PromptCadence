"""promptcadence.web.routes.trajectories — ``/trajectories`` (spec §7.1), Phase 3's surface.

``POST``, ``GET`` (list and one), ``/turns``, ``/plan``, ``/intents``, ``/cancel`` and the SSE
``/stream``. Every
handler calls one service method and renders (coding standards §5); the bypass decision, the
tier snapshot, the state machine and the cancel semantics are all
:class:`~promptcadence.services.trajectories.TrajectoryService`'s.

Handlers are ``def`` — Starlette runs them in the worker threadpool — except the stream, which
is ``async def`` and only streams: MirrorWall's ``sse_response`` dispatches every read into the
event store to the threadpool itself (ADR-0003 §6-8).
"""

from __future__ import annotations

from typing import Any, Literal

import anyio
from baseaicore import DataClassification, Money
from fastapi import APIRouter, Query, Request, Response, status
from mirrorwall import clamp_limit, json_response, paginated_response, sse_response
from pydantic import BaseModel, ConfigDict, Field
from setspec import GeneratorInfo
from starlette.responses import StreamingResponse

from promptcadence.__about__ import __version__
from promptcadence.domain.errors import ClassificationInvalidError
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.services.events import TERMINAL_EVENTS
from promptcadence.services.runtime import Runtime
from promptcadence.services.trajectories import TrajectorySubmission

__all__ = ["GENERATOR", "TrajectoryBody", "router"]

router = APIRouter(tags=["trajectories"])

GENERATOR = GeneratorInfo(name="promptcadence", version=__version__)
"""The envelope's generator on every streamed frame: this application, not MirrorWall."""


class MoneyBody(BaseModel):
    """A money amount as ``{currency, nanos}``."""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(default="USD")
    nanos: int = Field(ge=1)


class BudgetBody(BaseModel):
    """The optional per-trajectory ceilings and their partial-pricing rule (spec §7.1).

    ``partial_pricing`` is three-valued: absent means ``[budget] partial_pricing``, which is not
    the same as either value pinned. A request that pinned the current default still pinned it,
    and a later configuration change must not silently move it (ADR-0069).
    """

    model_config = ConfigDict(extra="forbid")

    money: MoneyBody | None = Field(default=None)
    tokens: int | None = Field(default=None, ge=1)
    partial_pricing: Literal["floor", "strict"] | None = Field(default=None)


class TrajectoryBody(BaseModel):
    """``POST /trajectories``'s request body (spec §7.1).

    ``data_classification`` defaults to ``confidential`` — unclassified data is treated as most
    restrictive (ADR-0046 rule 3) — and is a string rather than an enum so a value outside the
    three levels is refused as ``CLASSIFICATION_INVALID`` (spec §13), not as a generic
    validation error.
    """

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1)
    data_classification: str = Field(default="confidential")
    budget: BudgetBody | None = Field(default=None)
    project: str | None = Field(default=None)
    tools: list[str] | None = Field(default=None)
    bypass_planning: bool | None = Field(default=None)
    tier: str | None = Field(default=None)
    max_steps: int | None = Field(default=None, ge=1)
    max_turns: int | None = Field(default=None, ge=1)


def _submission_of(body: TrajectoryBody) -> TrajectorySubmission:
    """Translate the wire body into the service's request — translation, not decision."""
    try:
        classification = DataClassification(body.data_classification)
    except ValueError as exc:
        message = (
            f"data_classification {body.data_classification!r} is not one of "
            f"{[level.value for level in DataClassification]}"
        )
        raise ClassificationInvalidError(
            message, details={"field": "data_classification", "value": body.data_classification}
        ) from exc
    money = (
        Money(currency=body.budget.money.currency, nanos=body.budget.money.nanos)
        if body.budget is not None and body.budget.money is not None
        else None
    )
    return TrajectorySubmission(
        task=body.task,
        classification=classification,
        tools=tuple(body.tools) if body.tools is not None else None,
        bypass_planning=body.bypass_planning,
        tier=body.tier,
        max_turns=body.max_turns,
        max_steps=body.max_steps,
        project=body.project,
        token_budget=body.budget.tokens if body.budget is not None else None,
        money_budget=money,
        partial_pricing=body.budget.partial_pricing if body.budget is not None else None,
    )


def _runtime(request: Request) -> Runtime:
    runtime = request.app.state.runtime
    if not isinstance(runtime, Runtime):  # pragma: no cover — only outside the lifespan
        message = "the application is not serving"
        raise RuntimeError(message)
    return runtime


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


@router.post("/trajectories", status_code=status.HTTP_202_ACCEPTED, summary="Submit a trajectory")
def post_trajectory(request: Request, body: TrajectoryBody) -> Response:
    """T1: validate, decide the bypass, snapshot the tiers, queue; ``202`` with the trajectory.

    Errors: ``VALIDATION_ERROR``, ``CLASSIFICATION_INVALID``, ``PROJECT_UNKNOWN``,
    ``TOOL_NOT_FOUND``, ``TIER_NOT_CONFIGURED``.
    """
    runtime = _runtime(request)
    view = runtime.trajectories.submit(_submission_of(body))
    runtime.worker.wake()
    return json_response(
        view.as_json(), status=status.HTTP_202_ACCEPTED, request_id=_request_id(request)
    )


@router.get("/trajectories", summary="List trajectories")
def list_trajectories(
    request: Request,
    state: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> Response:
    """Trajectories newest first, filtered by state, cursor-paginated (API standards §6)."""
    runtime = _runtime(request)
    effective = clamp_limit(limit, maximum=200)
    filter_state = TrajectoryState(state) if state else None
    page, next_cursor = runtime.trajectories.list(
        state=filter_state, limit=effective, cursor=cursor
    )
    return paginated_response(
        [view.as_json() for view in page],
        limit=effective,
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
        request_id=_request_id(request),
    )


@router.get("/trajectories/{trajectory_id}", summary="One trajectory")
def get_trajectory(request: Request, trajectory_id: str) -> Response:
    """The trajectory with its state, cause and lease. ``404 TRAJECTORY_NOT_FOUND``."""
    view = _runtime(request).trajectories.get(trajectory_id)
    return json_response(view.as_json(), request_id=_request_id(request))


@router.get("/trajectories/{trajectory_id}/turns", summary="The transcript")
def get_turns(request: Request, trajectory_id: str) -> Response:
    """Every turn in order, each with its ``(intent_id, revision)`` and LoadCoach job."""
    turns = _runtime(request).trajectories.turns(trajectory_id)
    documents: list[dict[str, Any]] = [turn.as_json() for turn in turns]
    return paginated_response(
        documents, limit=max(len(documents), 1), has_more=False, request_id=_request_id(request)
    )


@router.get("/trajectories/{trajectory_id}/plan", summary="The plan record")
def get_plan(request: Request, trajectory_id: str) -> Response:
    """Every drafting attempt, the validated steps with their state, and the verdict.

    ``null`` for a bypassed trajectory or one not yet drafted; ``404`` for an unknown one. The
    composed explanation document is Phase 8's — this is the plan rows, rendered.
    """
    document = _runtime(request).records.plan(trajectory_id)
    return json_response(document, request_id=_request_id(request))


@router.get("/trajectories/{trajectory_id}/intents", summary="Every intent revision")
def get_intents(request: Request, trajectory_id: str) -> Response:
    """Every ``ExecutionIntent`` revision the trajectory minted, superseded ones included."""
    documents: list[dict[str, Any]] = _runtime(request).records.intents(trajectory_id)
    return paginated_response(
        documents, limit=max(len(documents), 1), has_more=False, request_id=_request_id(request)
    )


@router.post(
    "/trajectories/{trajectory_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel a trajectory",
)
def post_cancel(request: Request, trajectory_id: str) -> Response:
    """T14: at once for an unleased trajectory, at the next turn boundary for a leased one.

    ``409 TRAJECTORY_NOT_CANCELLABLE`` for a terminal trajectory.
    """
    view = _runtime(request).trajectories.cancel(trajectory_id)
    return json_response(
        view.as_json(), status=status.HTTP_202_ACCEPTED, request_id=_request_id(request)
    )


@router.get("/trajectories/{trajectory_id}/stream", summary="The trajectory's event stream")
async def stream_trajectory(request: Request, trajectory_id: str) -> StreamingResponse:
    """SSE with replay from ``Last-Event-ID`` (spec §7.1, API standards §8).

    Every frame is a persisted event; the stream closes on the terminal one. ``404`` before any
    frame for an unknown trajectory.
    """
    runtime = _runtime(request)
    await anyio.to_thread.run_sync(runtime.trajectories.get, trajectory_id)
    return sse_response(
        runtime.sink.source(trajectory_id),
        stream_id=trajectory_id,
        last_event_id=request.headers.get("last-event-id"),
        generator=GENERATOR,
        heartbeat_seconds=15.0,
        poll_interval_seconds=0.02,
        terminal_events=TERMINAL_EVENTS,
    )
