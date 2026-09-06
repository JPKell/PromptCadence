"""promptcadence.web.csrf — the page side of MirrorWall's double-submit CSRF (ADR-0026 §2).

MirrorWall's ``CsrfMiddleware`` rejects a form post whose ``csrf_token`` field does not equal the
``__Host-mw-csrf`` cookie. This module is the other half: a page carrying a form is rendered with
a token in a hidden field and the same token set as the cookie. The token is reused when the
browser already holds one, so two tabs do not invalidate each other's forms.

A transcription of LoadCoach's ``web/csrf.py`` rather than a second design (ADR-0094 rule 3). The
``__Host-`` prefix requires ``Secure``; browsers treat ``http://localhost`` and
``http://127.0.0.1`` as secure contexts, which is exactly the loopback bind PromptCadence's
console is reachable on. A LAN deployment terminates TLS in front of it (ADR-0026 §1), so the
prefix holds there too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi.responses import HTMLResponse
from mirrorwall import CSRF_COOKIE_NAME, CSRF_FIELD_NAME, issue_csrf_token

from promptcadence.web.rendering import render

if TYPE_CHECKING:
    from fastapi import Request

__all__ = ["csrf_token_for", "render_form_page"]


def csrf_token_for(request: Request) -> tuple[str, bool]:
    """The request's CSRF token, and whether it was minted now (so the cookie must be set)."""
    existing = request.cookies.get(CSRF_COOKIE_NAME, "")
    if existing:
        return existing, False
    return issue_csrf_token(), True


def render_form_page(request: Request, template_name: str, /, **context: Any) -> HTMLResponse:
    """Render a page that carries a form, with the token in the context and in the cookie.

    The template receives ``csrf_token`` and ``csrf_field_name``; a form includes
    ``<input type="hidden" name="{{ csrf_field_name }}" value="{{ csrf_token }}">``.
    """
    token, fresh = csrf_token_for(request)
    response = HTMLResponse(
        render(template_name, csrf_token=token, csrf_field_name=CSRF_FIELD_NAME, **context)
    )
    if fresh:
        response.set_cookie(
            CSRF_COOKIE_NAME, token, path="/", secure=True, httponly=True, samesite="strict"
        )
    return response
