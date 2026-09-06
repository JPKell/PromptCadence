"""promptcadence.web.routes.console — the operator console (dev plan P8, ADR-0020).

Server-rendered HTML with progressive enhancement: every read-only page works with JavaScript
disabled, and the one place JavaScript adds anything is the timeline's live updates, which come
from the SSE stream the API already serves. Each handler calls one
:mod:`promptcadence.services.console` report and renders (coding standards §5).

**Authentication is the API's** (ADR-0094). An open loopback install browses and approves as
``loopback``; once a token exists, or the bind is not loopback, every page answers ``401`` with a
readable page naming ``promptcadence token create``. There is no session cookie, deliberately:
login, rotation, fixation defence, logout and idle expiry are a security surface with its own
threat model, and inventing one at the end of a long phase is where security surfaces go wrong.

**The two buttons in the inbox are the first forms this application has ever served.** They carry
MirrorWall's double-submit CSRF token, and ``CsrfMiddleware`` refuses a post without it. They also
POST to the same service methods ``/approve`` and ``/deny`` call, under the same ``approve`` scope
— a ``read``-scoped principal is not shown the buttons and is refused by their handler.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette import status

from promptcadence.services.approvals import Approver
from promptcadence.services.console import (
    approvals_report,
    dashboard_report,
    egress_report,
    ledger_report,
    system_report,
    tiers_report,
    timeline_report,
    tools_report,
    trajectories_report,
)
from promptcadence.services.runtime import Runtime
from promptcadence.web.auth import require_scope
from promptcadence.web.csrf import render_form_page
from promptcadence.web.rendering import render

__all__ = ["ui_router"]

ui_router = APIRouter(tags=["ui"], include_in_schema=False)


def _runtime(request: Request) -> Runtime:
    runtime = request.app.state.runtime
    if not isinstance(runtime, Runtime):  # pragma: no cover — only outside the lifespan
        message = "the application is not serving"
        raise RuntimeError(message)
    return runtime


def _now() -> datetime:
    return datetime.now(UTC)


async def _form(request: Request) -> dict[str, str]:
    """The posted form, parsed here rather than through ``request.form()``.

    The console posts ``application/x-www-form-urlencoded`` only — the LoadCoach precedent — and
    that needs no ``python-multipart`` dependency. A dependency acquired to read two fields is a
    dependency in every install for the sake of one page.
    """
    body = (await request.body()).decode("utf-8", "replace")
    return {key: values[-1] for key, values in parse_qs(body).items()}


def _page(template: str, **context: Any) -> HTMLResponse:
    return HTMLResponse(render(template, **context))


@ui_router.get("/", summary="Dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> HTMLResponse:
    """What is running, what is waiting for a person, and today's spend."""
    require_scope(request, "read")
    report = dashboard_report(_runtime(request), now=_now())
    return _page("dashboard/index.html", page="dashboard", report=report)


@ui_router.get("/trajectories", summary="Trajectories", response_class=HTMLResponse)
def trajectories_page(
    request: Request,
    state: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """The trajectory list, newest first, filterable by state."""
    require_scope(request, "read")
    report = trajectories_report(_runtime(request), state=state or None, cursor=cursor)
    return _page("trajectories/index.html", page="trajectories", report=report)


@ui_router.get(
    "/trajectories/{trajectory_id}", summary="Trajectory timeline", response_class=HTMLResponse
)
def timeline_page(request: Request, trajectory_id: str) -> HTMLResponse:
    """One trajectory's whole record, rendered as a readable timeline.

    The page renders the composed explanation document — the same bytes the API returns — so every
    record type the document holds is a record type this page shows, and the two cannot drift.
    """
    require_scope(request, "read")
    report = timeline_report(_runtime(request), trajectory_id)
    return _page("trajectories/detail.html", page="trajectories", report=report)


@ui_router.get("/approvals", summary="Approvals inbox", response_class=HTMLResponse)
def approvals_page(request: Request, resolved: Annotated[bool, Query()] = False) -> HTMLResponse:
    """The inbox. Its two buttons are the reason this application has CSRF (ADR-0094)."""
    principal = require_scope(request, "read")
    report = approvals_report(_runtime(request), now=_now(), include_resolved=resolved)
    return render_form_page(
        request,
        "approvals/index.html",
        page="approvals",
        report=report,
        may_approve=principal.grants("approve"),
    )


@ui_router.post("/approvals/{trajectory_id}/grant", summary="Grant from the inbox")
async def grant_from_inbox(request: Request, trajectory_id: str) -> RedirectResponse:
    """Resolve a pending request as a grant, under the ``approve`` scope.

    The CSRF token is consumed by :class:`mirrorwall.CsrfMiddleware` before this handler runs, so
    a post that reached here already matched the cookie.
    """
    principal = require_scope(request, "approve")
    runtime = _runtime(request)
    runtime.approvals.grant(
        trajectory_id, approver=Approver(token_id=principal.token_id, name=principal.name)
    )
    runtime.worker.wake()
    return RedirectResponse("/approvals", status_code=status.HTTP_303_SEE_OTHER)


@ui_router.post("/approvals/{trajectory_id}/deny", summary="Deny from the inbox")
async def deny_from_inbox(request: Request, trajectory_id: str) -> RedirectResponse:
    """Resolve a pending request as a denial, with the operator's stated reason on the record."""
    principal = require_scope(request, "approve")
    reason = (await _form(request)).get("reason", "").strip()
    _runtime(request).approvals.deny(
        trajectory_id,
        approver=Approver(token_id=principal.token_id, name=principal.name),
        reason=reason or None,
    )
    return RedirectResponse("/approvals", status_code=status.HTTP_303_SEE_OTHER)


@ui_router.get("/tiers", summary="Tiers", response_class=HTMLResponse)
def tiers_page(request: Request) -> HTMLResponse:
    """Every configured tier, its ceiling, and whether it can serve right now."""
    require_scope(request, "read")
    runtime = _runtime(request)
    # ``False`` until LC-E1 registers a remote provider with LoadCoach (lifecycle §3). Read from
    # the same place every other reader reads it, so the page cannot disagree with the router
    # about whether a remote tier can serve.
    report = tiers_report(runtime.settings, loadcoach_has_remote_provider=False)
    return _page("tiers/index.html", page="tiers", report=report)


@ui_router.get("/tools", summary="Tools", response_class=HTMLResponse)
def tools_page(request: Request) -> HTMLResponse:
    """The registry, including what configuration named and could not be registered."""
    require_scope(request, "read")
    return _page("tools/index.html", page="tools", report=tools_report(_runtime(request)))


@ui_router.get("/ledger", summary="Ledger", response_class=HTMLResponse)
def ledger_page(
    request: Request, trajectory_id: Annotated[str | None, Query()] = None
) -> HTMLResponse:
    """Today's position and the recorded debits behind it."""
    require_scope(request, "read")
    report = ledger_report(_runtime(request), trajectory_id=trajectory_id or None)
    return _page("ledger/index.html", page="ledger", report=report)


@ui_router.get("/egress", summary="Egress decisions", response_class=HTMLResponse)
def egress_page(
    request: Request,
    verdict: Annotated[str | None, Query()] = None,
    trajectory_id: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Every recorded egress decision. A refusal is as auditable as an approval."""
    require_scope(request, "read")
    report = egress_report(
        _runtime(request), verdict=verdict or None, trajectory_id=trajectory_id or None
    )
    return _page("egress/index.html", page="egress", report=report)


@ui_router.get("/system", summary="System", response_class=HTMLResponse)
def system_page(request: Request) -> HTMLResponse:
    """Health, the last recovery pass, and how this install authenticates."""
    require_scope(request, "read")
    return _page(
        "system/index.html", page="system", report=system_report(_runtime(request), now=_now())
    )
