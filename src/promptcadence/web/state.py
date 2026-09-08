"""promptcadence.web.state — what a route reads off the request that the lifespan put there."""

from __future__ import annotations

from typing import TYPE_CHECKING

from promptcadence.services.runtime import Runtime

if TYPE_CHECKING:
    from fastapi import Request

__all__ = ["request_id_of", "runtime_of"]


def runtime_of(request: Request) -> Runtime:
    """Return the serving runtime the lifespan opened.

    Raises:
        RuntimeError: The application is not serving — only reachable outside the lifespan.
    """
    runtime = request.app.state.runtime
    if not isinstance(runtime, Runtime):  # pragma: no cover — only outside the lifespan
        message = "the application is not serving"
        raise RuntimeError(message)
    return runtime


def request_id_of(request: Request) -> str | None:
    """Return the request ID MirrorWall's middleware assigned, or ``None`` outside it."""
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None
