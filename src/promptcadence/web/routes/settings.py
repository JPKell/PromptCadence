"""promptcadence.web.routes.settings — ``GET``/``PUT /settings`` (spec §7.1) and the page.

Runtime-changeable keys only. A security-relevant key is ``403 FORBIDDEN`` naming the key; an
unknown one is ``400 VALIDATION_ERROR`` naming it and listing what can be changed. Both come from
:mod:`promptcadence.services.settings`, so the API, the page, the CLI and the generated reference
enforce one registry.

The scopes are LoadCoach's rather than a literal reading of spec §14's "``admin`` (settings,
tokens)": ``GET`` is ``read``, so an operator who may look at the console may see what the process
is running on, and only ``PUT`` — the change — is ``admin`` (ADR-0100). The page follows
[ADR-0094](../../../docs/adr/0094-the-console-authenticates-as-the-api-does.md): it renders for
``read`` **without the form**, and its POST refuses anything below ``admin``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import parse_qs

from baseaicore import ValidationError
from fastapi import APIRouter, Body, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from mirrorwall import json_response

from promptcadence.services.runtime import Runtime
from promptcadence.services.settings import (
    RUNTIME_SETTINGS,
    runtime_settings_document,
    write_runtime_settings,
)
from promptcadence.web.auth import require_scope
from promptcadence.web.csrf import render_form_page

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["settings"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)


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


async def _changes_of(request: Request) -> dict[str, Any]:
    """Parse a Settings form post into the changes :func:`write_runtime_settings` takes.

    Numbers arrive as text and an empty field is not a change — a form that treated a cleared box
    as a zero would set a retention of nothing the first time an operator tabbed through it. Only
    registry keys are read, so a field naming anything else is ignored rather than carried into a
    refusal the operator did not ask for.

    Parsed here rather than through ``request.form()``: the page posts
    ``application/x-www-form-urlencoded`` only, and that needs no extra dependency (the console's
    own precedent).

    Raises:
        ValidationError: A number field holds something that is not a number, named by key.
    """
    posted = {
        key: values[-1]
        for key, values in parse_qs((await request.body()).decode("utf-8", "replace")).items()
    }
    changes: dict[str, Any] = {}
    for key, setting in RUNTIME_SETTINGS.items():
        raw = posted.get(key)
        if setting.kind is bool:  # pragma: no cover — the set holds no boolean today
            if raw is not None:
                changes[key] = raw in ("on", "true", "1")
            continue
        if raw is None or not raw.strip():
            continue
        try:
            changes[key] = int(raw.strip()) if setting.kind is int else float(raw.strip())
        except ValueError as exc:
            raise ValidationError(
                f"{key} must be a number.",
                details={"fields": [{"path": key, "problem": "expected a number"}]},
            ) from exc
    return changes


@ui_router.get("/settings", summary="Settings page", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    """Render what may change while the server runs, and what may not.

    Both halves, deliberately: a page that listed only the changeable keys would leave an
    operator guessing whether the bind, the tool roots or the ceilings are missing or hidden.
    ``read`` renders the page; only ``admin`` is shown the form (ADR-0094).
    """
    principal = require_scope(request, "read")
    runtime = _runtime(request)
    return render_form_page(
        request,
        "settings/index.html",
        page="settings",
        document=_document(runtime),
        keys=list(RUNTIME_SETTINGS),
        may_change=principal.grants("admin"),
        saved=request.query_params.get("saved") == "1",
    )


@ui_router.post("/settings", summary="Save from the Settings page")
async def settings_form(request: Request) -> RedirectResponse:
    """Store what the form named, then redirect to the page with the change applied.

    The CSRF token is consumed by :class:`mirrorwall.CsrfMiddleware` before this handler runs, so
    a post that reached here already matched the cookie. ``admin`` scope.
    """
    require_scope(request, "admin")
    runtime = _runtime(request)
    write_runtime_settings(
        runtime.database,
        await _changes_of(request),
        settings=runtime.settings,
        now=datetime.now(UTC),
    )
    return RedirectResponse("/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER)
