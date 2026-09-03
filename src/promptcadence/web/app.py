"""promptcadence.web.app — the FastAPI application factory.

``create_app`` is a pure function of :class:`~promptcadence.config.Settings`: it opens nothing, so
a test can build an app without touching the filesystem. The database handle is created by the
lifespan, which runs only when the application is actually served.

Host validation, the request-ID middleware and the error envelope come from MirrorWall, not from
this module — three implementations of one security control are three chances to get it subtly
different, and the difference will be in the application nobody audited (ADR-0026 §1). There is
no HTML UI yet (Phase 8), so CSRF and same-origin protection are not wired: the state-changing
routes take JSON bodies and bearer-less loopback callers only, and a browser form cannot reach
them.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final

from baseaicore import SuiteError, new_id
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mirrorwall import HostValidationMiddleware, RequestIdMiddleware, error_body, loopback_allowlist
from starlette.exceptions import HTTPException as StarletteHTTPException

from promptcadence.__about__ import __version__
from promptcadence.config import LOOPBACK_HOSTS, Settings
from promptcadence.web.routes import system as system_routes
from promptcadence.web.routes import trajectories as trajectory_routes

__all__ = ["create_app", "register_exception_handlers"]

logger = logging.getLogger(__name__)

# Only the codes a built phase can actually raise. A spec §13 code with no exception yet to raise
# it (PLAN_REJECTED, BUDGET_EXCEEDED, ...) is added here in the phase that introduces it, so this
# table never claims a status for a code nothing exercises. Statuses follow API standards §4.
_STATUS_BY_CODE: Final[dict[str, int]] = {
    "VALIDATION_ERROR": status.HTTP_400_BAD_REQUEST,
    "CLASSIFICATION_INVALID": status.HTTP_400_BAD_REQUEST,
    "SCHEMA_VERSION_UNSUPPORTED": status.HTTP_400_BAD_REQUEST,
    "TRAJECTORY_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "TRAJECTORY_NOT_CANCELLABLE": status.HTTP_409_CONFLICT,
    "PROJECT_UNKNOWN": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "TOOL_NOT_FOUND": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "TIER_NOT_CONFIGURED": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "TIER_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "LOADCOACH_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "LOADCOACH_ERROR": status.HTTP_502_BAD_GATEWAY,
    "COMPACTION_FAILED": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "CONFIGURATION_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "INSECURE_BINDING": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "MISDIRECTED_REQUEST": 421,
    "DATABASE_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "DATABASE_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "MIGRATION_REQUIRED": status.HTTP_503_SERVICE_UNAVAILABLE,
    "MIGRATION_FAILED": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "SCHEMA_AHEAD": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "STORAGE_BUSY": status.HTTP_503_SERVICE_UNAVAILABLE,
    "STORAGE_FULL": status.HTTP_507_INSUFFICIENT_STORAGE,
    "INTERNAL_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
}

_CODE_BY_HTTP_STATUS: Final[dict[int, str]] = {
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    421: "MISDIRECTED_REQUEST",
}


def _request_id_of(request: Request) -> str:
    """Return this request's ID, generating one if the request-ID middleware did not run."""
    state_id = getattr(request.state, "request_id", None)
    return state_id if isinstance(state_id, str) and state_id else new_id()


def _error_response(
    *,
    request_id: str,
    code: str,
    message: str,
    status_code: int,
    details: Mapping[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_body(code=code, message=message, request_id=request_id, details=details),
        headers={"X-Request-ID": request_id},
    )


def _resolve_allowed_hosts(settings: Settings) -> frozenset[str]:
    """The Host-header allowlist for this bind (ADR-0026 §1)."""
    host = settings.server.host.lower()
    if host in LOOPBACK_HOSTS:
        return loopback_allowlist(host)
    return frozenset(name.lower() for name in settings.server.allowed_hosts) | {host}


def _docs_allowed(settings: Settings) -> bool:
    """Interactive API docs are loopback-only by default (API standards §11)."""
    return settings.server.host in LOOPBACK_HOSTS


def register_exception_handlers(app: FastAPI) -> None:
    """Register the handlers that translate every exception type into the standard envelope."""

    @app.exception_handler(SuiteError)
    async def _suite_error_handler(request: Request, exc: SuiteError) -> JSONResponse:
        status_code = _STATUS_BY_CODE.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        if status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.error("request.failed", extra={"code": exc.code}, exc_info=exc)
        else:
            logger.warning("request.rejected", extra={"code": exc.code})
        return _error_response(
            request_id=_request_id_of(request),
            code=exc.code,
            message=exc.message,
            status_code=status_code,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = [
            {
                "path": ".".join(str(part) for part in error["loc"] if part != "body"),
                "problem": error["msg"],
            }
            for error in exc.errors()
        ]
        return _error_response(
            request_id=_request_id_of(request),
            code="VALIDATION_ERROR",
            message="Request body failed validation.",
            status_code=status.HTTP_400_BAD_REQUEST,
            details={"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _CODE_BY_HTTP_STATUS.get(exc.status_code, "HTTP_ERROR")
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else "Request failed."
        return _error_response(
            request_id=_request_id_of(request),
            code=code,
            message=message,
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("request.unhandled_error", exc_info=exc)
        return _error_response(
            request_id=_request_id_of(request),
            code="INTERNAL_ERROR",
            message="An unexpected error occurred.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the process's runtime handles for as long as the server serves.

    Nothing here may raise because LoadCoach is unreachable (ADR-0045 rule 3); an unopenable
    database does raise, which is deliberate — see :mod:`promptcadence.services.runtime`.
    """
    builder = getattr(app.state, "runtime_builder", None)
    runtime = builder(app.state.settings) if builder is not None else None
    app.state.runtime = runtime
    app.state.health_checkers = runtime.health_checkers if runtime is not None else ()
    if runtime is not None and hasattr(runtime, "start"):
        runtime.start()
    try:
        yield
    finally:
        if runtime is not None:
            runtime.close()
        app.state.runtime = None
        app.state.health_checkers = None


def create_app(settings: Settings, *, runtime_builder: Any | None = None) -> FastAPI:
    """Build the FastAPI application for the given settings.

    Args:
        settings: The validated configuration.
        runtime_builder: Callable taking the settings and returning the object that owns the
            database handle and the health checkers. Injected so a test can build an app with no
            runtime at all, and so the composition root stays the only place that knows how to
            open a database.

    Returns:
        The application. Still a pure function of its arguments — it opens nothing; the runtime is
        created by the lifespan, which runs only when the application is served (or when a test
        enters ``TestClient`` as a context manager).
    """
    app = FastAPI(
        title="PromptCadence",
        version=__version__,
        docs_url="/api/v1/docs" if _docs_allowed(settings) else None,
        openapi_url="/api/v1/openapi.json" if _docs_allowed(settings) else None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.runtime = None
    app.state.runtime_builder = runtime_builder
    app.state.health_checkers = None

    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(HostValidationMiddleware, allowed_hosts=_resolve_allowed_hosts(settings))

    register_exception_handlers(app)

    app.include_router(system_routes.router, prefix="/api/v1")
    app.include_router(trajectory_routes.router, prefix="/api/v1")

    return app
