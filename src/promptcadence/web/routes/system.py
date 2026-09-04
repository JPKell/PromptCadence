"""promptcadence.web.routes.system — `/health`, `/version`, `/system/status` and `/tools`.

Health reports two components in Phase 1 (development plan Phase 1): ``database`` and
``loadcoach``. An unreachable LoadCoach makes health *degraded*, never *unavailable* and never a
startup failure (ADR-0045 rule 3, spec §20 AC1) — PromptCadence requires LoadCoach for execution,
and nothing executes yet.

``GET /tools`` reports the registry as assembled, and it reports the tools configuration named that
were **not** registered, with the cause. Listing only what works would leave an operator unable to
tell a tool nobody asked for from one that was asked for and withheld — which is exactly the
question ``http_fetch`` raises before Phase 6.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from mirrorwall import ComponentHealth, ComponentStatus, health_payload, json_response
from starlette.responses import JSONResponse

from promptcadence.__about__ import __version__
from promptcadence.domain.errors import ToolNotFoundError
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.services.tools import isolation_payload

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
    """Report active trajectories, the last recovery pass and configured concurrency (spec §17).

    Approvals and the ledger arrive in later phases; ``pending_approvals`` is the shape spec §17
    commits to, empty until Phase 7 can fill it.
    """
    settings = request.app.state.settings
    runtime = getattr(request.app.state, "runtime", None)
    active: list[dict[str, Any]] = []
    last_recovery: dict[str, Any] | None = None
    if runtime is not None and hasattr(runtime, "trajectories"):
        page, _ = runtime.trajectories.list(state=TrajectoryState.EXECUTING, limit=50)
        active = [
            {
                "trajectory_id": view.trajectory_id,
                "state": view.state.value,
                "lease_owner": view.lease_owner,
                "created_at": view.as_json()["created_at"],
            }
            for view in page
        ]
        summary = runtime.worker.last_recovery
        last_recovery = summary.as_json() if summary is not None else None
    payload: dict[str, Any] = {
        "active_trajectories": active,
        "pending_approvals": [],
        "max_concurrent_trajectories": settings.execution.max_concurrent_trajectories,
        "loadcoach_base_url": settings.loadcoach.base_url,
        "last_recovery": last_recovery,
    }
    return json_response(payload)


@router.get("/tools", summary="List the tool registry, including what was withheld")
def tools(request: Request) -> JSONResponse:
    """Report every tool ``[tools] enabled`` names, registered or not, and the isolation rung.

    Returns:
        ``{"tools": [...], "isolation": {...}}``. Each tool carries its risk class, egress class,
        whether it requires isolation, whether its arguments are redacted, its argument schema, and
        — for one that is not registered — the cause. ``isolation`` is ToolYard's probe result: the
        rung ``run_command`` runs under, the runtime that provides it, and the reason naming every
        rung the probe visited.
    """
    plant = _plant(request)
    if plant is None:
        return json_response({"tools": [], "isolation": None})
    return json_response(
        {
            "tools": [entry.as_payload() for entry in plant.catalog()],
            "isolation": isolation_payload(plant.isolation()),
        }
    )


@router.get("/tools/{name}", summary="Show one tool")
def tool(request: Request, name: str) -> JSONResponse:
    """Report one tool by exact name.

    Args:
        request: The request.
        name: The tool name, matched exactly — the registry's own lookup rule.

    Returns:
        The tool's payload.

    Raises:
        ToolNotFoundError: No configured tool has that name — ``422`` per spec §13's table, which
            assigns one status to the code and the submit path needs it there. A *withheld* tool is
            found, not missing: configuration names it, and saying "no such tool" would hide the
            very thing an operator came to look up.
    """
    plant = _plant(request)
    entry = plant.entry(name) if plant is not None else None
    if entry is None:
        message = f"no tool named {name!r} is configured"
        raise ToolNotFoundError(message, details={"tool_name": name})
    return json_response(entry.as_payload())


def _plant(request: Request) -> Any:
    """The runtime's tool plant, or ``None`` when the application is not serving."""
    runtime = getattr(request.app.state, "runtime", None)
    return getattr(runtime, "tools", None)
