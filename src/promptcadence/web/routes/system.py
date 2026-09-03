"""promptcadence.web.routes.system — `/health`, `/version` and `/system/status`.

Health reports two components in Phase 1 (development plan Phase 1): ``database`` and
``loadcoach``. An unreachable LoadCoach makes health *degraded*, never *unavailable* and never a
startup failure (ADR-0045 rule 3, spec §20 AC1) — PromptCadence requires LoadCoach for execution,
and nothing executes yet.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from mirrorwall import ComponentHealth, ComponentStatus, health_payload, json_response
from starlette.responses import JSONResponse

from promptcadence.__about__ import __version__

__all__ = ["API_VERSION", "SCHEMA_VERSION", "router"]

router = APIRouter(tags=["system"])

API_VERSION = "v1"
SCHEMA_VERSION = "1"


def _components(request: Request) -> list[ComponentHealth]:
    """Build the health components from whatever the lifespan actually opened."""
    checkers = getattr(request.app.state, "health_checkers", None)
    if not checkers:
        return [
            ComponentHealth(
                name="database",
                status=ComponentStatus.NOT_CONFIGURED,
                detail="The application is not serving; no handles are open.",
            )
        ]
    return [check() for check in checkers]


@router.get("/health")
def health(request: Request) -> JSONResponse:
    """Report component health.

    Returns:
        MirrorWall's standard health payload. ``200`` when every component is ``ok`` or
        ``degraded``, ``503`` when any is ``unavailable`` — an unreachable LoadCoach never reaches
        ``unavailable`` (see :mod:`promptcadence.services.loadcoach_status`), so only an unopenable
        database can bring this endpoint below 200.
    """
    components = _components(request)
    payload = health_payload(
        application="promptcadence", version=__version__, components=components
    )
    unavailable = any(c.status is ComponentStatus.UNAVAILABLE for c in components)
    return json_response(payload, status=503 if unavailable else 200)


@router.get("/version")
def version() -> JSONResponse:
    """Report application, API and schema versions.

    Never authenticated (ADR-0026 §5): a client has to be able to discover what it is talking to
    before it can present a credential.
    """
    payload: dict[str, Any] = {
        "application": "promptcadence",
        "version": __version__,
        "api_version": API_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    return json_response(payload)


@router.get("/system/status")
def system_status(request: Request) -> JSONResponse:
    """Report active trajectories and configured concurrency (spec §17).

    Trajectories, approvals and the ledger arrive in later phases; this reports the shape spec §17
    commits to, with the pieces this phase has: nothing queued yet, and the concurrency ceiling
    that will bound it.
    """
    settings = request.app.state.settings
    payload: dict[str, Any] = {
        "active_trajectories": [],
        "pending_approvals": [],
        "max_concurrent_trajectories": settings.execution.max_concurrent_trajectories,
        "loadcoach_base_url": settings.loadcoach.base_url,
    }
    return json_response(payload)
