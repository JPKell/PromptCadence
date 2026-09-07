"""promptcadence.web.limits — request body size, and same-origin for JSON writes (Security §14).

**Oversize body rejected before buffering.** A declared ``Content-Length`` over
``[server] max_body_bytes`` is ``413 PAYLOAD_TOO_LARGE`` before a byte of the body is read; a
chunked body is counted as it streams and cut off at the same cap.

**A cross-origin JSON post is rejected.** ADR-0026 §2 exempts the JSON API from the CSRF token
because a cross-origin form cannot produce ``application/json`` without a CORS preflight, and
CORS is disabled. This middleware is the server-side statement of that same fact: a JSON write
that arrives with an ``Origin`` header naming a host other than the one it was sent to is
``403 CSRF_FAILED``. A script sends no ``Origin``; a browser on the same origin sends its own; only
a page elsewhere sends another host's.

LoadCoach's ``web/limits.py``, transcribed: one control, one shape, in every application that
serves HTTP (ADR-0026 §1).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from baseaicore import new_id
from mirrorwall import error_body
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["BodySizeLimitMiddleware", "SameOriginMiddleware"]

logger = logging.getLogger(__name__)

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def _reject(
    scope: Scope, receive: Receive, send: Send, *, status: int, code: str, message: str
) -> None:
    request_id = scope.get("state", {}).get("request_id") or new_id()
    response = JSONResponse(
        status_code=status,
        content=error_body(code=code, message=message, request_id=request_id),
        headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
    )
    await response(scope, receive, send)


class BodySizeLimitMiddleware:
    """Refuse a body over ``max_bytes`` — by its declared length first, else while it arrives.

    A body with no usable ``Content-Length`` is read up to the cap and one byte more; past it the
    application is never called. Within it, the body is replayed to the application as it
    arrived, so a route sees exactly what a client sent.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        """Wrap ``app``."""
        self.app = app
        self._max = max(int(max_bytes), 1)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """413 before buffering when the body is, or declares itself, too large."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max:
            logger.warning("request.body_too_large", extra={"declared": int(declared)})
            await _reject(
                scope,
                receive,
                send,
                status=413,
                code="PAYLOAD_TOO_LARGE",
                message=f"The request body is {declared} bytes; the limit is {self._max}.",
            )
            return
        chunks: list[bytes] = []
        seen = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            seen += len(body)
            if seen > self._max:
                logger.warning("request.body_too_large", extra={"streamed": seen})
                await _reject(
                    scope,
                    receive,
                    send,
                    status=413,
                    code="PAYLOAD_TOO_LARGE",
                    message=f"The request body exceeded {self._max} bytes.",
                )
                return
            chunks.append(body)
            more = bool(message.get("more_body", False))
        buffered = b"".join(chunks)
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": buffered, "more_body": False}

        await self.app(scope, replay, send)


class SameOriginMiddleware:
    """Refuse an unsafe request whose ``Origin`` names another host (ADR-0026 §2)."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """403 ``CSRF_FAILED`` for a cross-origin write; everything else passes through."""
        if scope["type"] != "http" or scope["method"] not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is None:
            await self.app(scope, receive, send)
            return
        host = headers.get("host", "").split(":")[0].lower()
        origin_host = (urlsplit(origin).hostname or "").lower() if origin != "null" else ""
        if origin_host and origin_host == host:
            await self.app(scope, receive, send)
            return
        logger.warning("request.cross_origin_refused")
        await _reject(
            scope,
            receive,
            send,
            status=403,
            code="CSRF_FAILED",
            message="Cross-origin writes are not accepted; CORS is disabled (ADR-0026 §2).",
        )
