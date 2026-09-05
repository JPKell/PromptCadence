"""promptcadence.web.auth — who is calling, and whether they hold the scope a route needs.

Spec §14 and ADR-0049 rule 2: ``approve`` is its own scope, distinct from ``write``, so the
identity that submits work cannot approve its own egress. This module establishes the caller from
the request and refuses when the scope is missing; the vocabulary and the containment rule are
:mod:`promptcadence.services.tokens`'s, so the route and the CLI cannot disagree about them.

Where a principal comes from, in order (the LoadCoach precedent, ADR-0026 §5):

1. ``Authorization: Bearer <token>`` — scripts, the CLI in client mode.
2. **Loopback with no tokens is open** (spec §20 AC1): the principal is ``loopback`` holding
   every scope, and the OS user boundary is the security boundary. Its grants are recorded as
   ``approver:loopback``, so the record still says who — the open install, by name.

Once any token exists, or the bind is not loopback, every scoped endpoint needs a bearer token; a
token is stored only as its SHA-256 and compared constant-time; a revoked row is not a token.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar, Literal

from baseaicore import SuiteError
from fastapi import Request
from sqlalchemy import select

from promptcadence.config import LOOPBACK_HOSTS
from promptcadence.infrastructure.db.models import ApiToken
from promptcadence.services.runtime import Runtime
from promptcadence.services.tokens import SCOPES, grants, parse_scopes, token_sha256

if TYPE_CHECKING:
    from promptcadence.services.database import Database

__all__ = ["Forbidden", "Principal", "Unauthorized", "require_scope", "resolve_principal"]

LOOPBACK_PRINCIPAL_NAME = "loopback"


class Unauthorized(SuiteError):
    """A scoped endpoint was called with no usable bearer token."""

    code: ClassVar[str] = "UNAUTHORIZED"


class Forbidden(SuiteError):
    """The caller is known and its scopes do not contain the one this endpoint requires."""

    code: ClassVar[str] = "FORBIDDEN"


@dataclass(frozen=True, slots=True)
class Principal:
    """The caller of one request.

    Attributes:
        token_id: The token row's id, or ``"loopback"`` for the open install. This is what a
            grant records as the approver identity (``minted_by = approver:<token_id>``).
        name: The token's name, or ``"loopback"``.
        scopes: The scopes held.
        source: How the principal was established.
    """

    token_id: str
    name: str
    scopes: frozenset[str]
    source: Literal["token", "loopback"]

    def grants(self, required: str) -> bool:
        """Whether this principal holds ``required``."""
        return grants(self.scopes, required)


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def resolve_principal(
    database: Database, *, authorization: str | None, bind_host: str, now: datetime
) -> Principal:
    """Establish who is calling, or raise.

    Args:
        database: The application's database handle.
        authorization: The ``Authorization`` header, or ``None``.
        bind_host: ``server.host``.
        now: The instant, recorded as the token's ``last_used_at`` on a match.

    Returns:
        The principal: the token's identity and scopes, or the open loopback install.

    Raises:
        Unauthorized: Tokens exist (or the bind is not loopback) and no usable one was presented.
    """
    presented = _bearer(authorization)
    with database.write() as session:
        rows = list(
            session.execute(
                select(ApiToken).where(ApiToken.active.is_(True), ApiToken.revoked_at.is_(None))
            ).scalars()
        )
        if not rows:
            if bind_host in LOOPBACK_HOSTS:
                return Principal(
                    token_id=LOOPBACK_PRINCIPAL_NAME,
                    name=LOOPBACK_PRINCIPAL_NAME,
                    scopes=frozenset(SCOPES),
                    source="loopback",
                )
            raise Unauthorized(
                "This endpoint requires a bearer token, and no API token exists. A non-loopback "
                "bind must have tokens: `promptcadence token create` (ADR-0026).",
                details={},
            )
        if presented is None:
            raise Unauthorized("This endpoint requires a bearer token.", details={})
        digest = token_sha256(presented)
        for row in rows:
            if hmac.compare_digest(row.token_sha256, digest):
                row.last_used_at = now
                row.use_count = int(row.use_count or 0) + 1
                return Principal(
                    token_id=row.id,
                    name=row.name,
                    scopes=parse_scopes(row.scopes),
                    source="token",
                )
    raise Unauthorized("The bearer token presented is not a known, active API token.", details={})


def require_scope(request: Request, required: str) -> Principal:
    """Resolve the request's principal and refuse unless it holds ``required``.

    Args:
        request: The request.
        required: One of :data:`~promptcadence.services.tokens.SCOPES`.

    Returns:
        The principal, for the handler to hand to the service as the acting identity.

    Raises:
        Unauthorized: No usable credential where one was required.
        Forbidden: The credential does not carry ``required``.
    """
    runtime = request.app.state.runtime
    if not isinstance(runtime, Runtime):  # pragma: no cover — only outside the lifespan
        message = "the application is not serving"
        raise RuntimeError(message)
    principal = resolve_principal(
        runtime.database,
        authorization=request.headers.get("authorization"),
        bind_host=request.app.state.settings.server.host,
        now=datetime.now(UTC),
    )
    if not principal.grants(required):
        raise Forbidden(
            f"This endpoint requires the {required!r} scope; token {principal.name!r} holds "
            f"{sorted(principal.scopes)}.",
            details={"required": required, "held": sorted(principal.scopes)},
        )
    return principal
