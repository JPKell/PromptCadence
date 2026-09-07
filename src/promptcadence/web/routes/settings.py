"""promptcadence.web.routes.settings — ``GET``/``PUT /settings`` (spec §7.1) and the page.

Runtime-changeable keys only. A security-relevant key is ``403 FORBIDDEN`` naming the key; an
unknown one is ``400 VALIDATION_ERROR`` naming it and listing what can be changed. Both come from
:mod:`promptcadence.services.settings`, so the API, the page, the CLI and the generated reference
enforce one registry.

The scopes are LoadCoach's rather than a literal reading of spec §14's "``admin`` (settings,
tokens)": ``GET`` is ``read``, so an operator who may look at the console may see what the process
is running on, and only ``PUT`` — the change — is ``admin`` (ADR-0100).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, Request, Response
from mirrorwall import json_response

from promptcadence.services.runtime import Runtime
from promptcadence.services.settings import (
    runtime_settings_document,
    write_runtime_settings,
)
from promptcadence.web.auth import require_scope

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["router"]

router = APIRouter(tags=["settings"])


def _runtime(request: Request) -> Runtime:
    runtime = request.app.state.runtime
    if not isinstance(runtime, Runtime):  # pragma: no cover — only outside the lifespan
        message = "the application is not serving"
        raise RuntimeError(message)
    return runtime


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def _document(runtime: Runtime) -> Mapping[str, Any]:
    """The document both verbs answer with, over the **configured** settings (spec §12)."""
    return runtime_settings_document(runtime.database, settings=runtime.settings)


@router.get("/settings", summary="Runtime-changeable settings")
def get_settings(request: Request) -> Response:
    """Every runtime-changeable key's effective value, its definition, and what is config-only.

    Each definition names the configured value, the stored row (or ``null``), which of the two is
    effective, and — when the environment pins the key — what shadows the row: a stored value that
    does nothing is visible as such rather than silently applied or silently dropped
    (configuration standards §7). ``read`` scope.
    """
    require_scope(request, "read")
    return json_response(dict(_document(_runtime(request))), request_id=_request_id(request))


@router.put("/settings", summary="Change runtime settings")
def put_settings(request: Request, body: Annotated[dict[str, Any], Body()]) -> Response:
    """Set one or more runtime-changeable keys, and answer with the whole document.

    ``403 FORBIDDEN`` names a security-relevant key and writes nothing — a request naming one is
    refused whole. ``400 VALIDATION_ERROR`` names an unknown key and lists the runtime-changeable
    set, or names a value of the wrong type or outside its bounds. Applied by the running worker
    within one lease-reap cadence. ``admin`` scope.
    """
    require_scope(request, "admin")
    runtime = _runtime(request)
    write_runtime_settings(runtime.database, body, settings=runtime.settings, now=datetime.now(UTC))
    return json_response(dict(_document(runtime)), request_id=_request_id(request))
