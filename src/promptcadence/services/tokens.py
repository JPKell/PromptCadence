"""promptcadence.services.tokens — API tokens and the four scopes (spec §14, ADR-0026, ADR-0049).

The token itself is 256 bits of URL-safe randomness, returned to the caller exactly once; the row
stores its SHA-256 and nothing else. Names are unique among active tokens so ``revoke`` is
unambiguous. Scopes are a **set**, not a ladder: ``approve`` is deliberately separate from
``write`` so the identity that submits work cannot approve its own egress (ADR-0049 rule 2), and
the only scope that contains others is ``admin``, which is what an operator issues themselves on a
single-operator machine.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final

from baseaicore import SuiteError, ValidationError, new_id
from baseaicore.timeutil import to_rfc3339
from sqlalchemy import select

from promptcadence.infrastructure.db.models import ApiToken

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from promptcadence.services.database import Database

__all__ = [
    "SCOPES",
    "IssuedToken",
    "TokenNotFoundError",
    "TokenRecord",
    "create_token",
    "grants",
    "list_tokens",
    "revoke_token",
    "token_sha256",
]

SCOPES: Final[tuple[str, ...]] = ("read", "write", "approve", "admin")
"""Spec §14's four scopes. ``admin`` contains the other three; nothing else contains anything."""


class TokenNotFoundError(SuiteError):
    """No active token with that name."""

    code: ClassVar[str] = "TOKEN_NOT_FOUND"


def token_sha256(token: str) -> str:
    """Return the stored form of a bearer token: its lowercase hex SHA-256, prefixed."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def grants(scopes: Iterable[str], required: str) -> bool:
    """Whether a scope set contains ``required``.

    Args:
        scopes: The token's scopes.
        required: The scope an operation needs.

    Returns:
        ``True`` when ``required`` is in the set, or the set holds ``admin``.
    """
    held = set(scopes)
    return required in held or "admin" in held


def parse_scopes(raw: str) -> frozenset[str]:
    """Parse the comma-separated ``scopes`` column, refusing a name outside the vocabulary.

    Raises:
        ValidationError: A scope is not one of :data:`SCOPES`, or the set is empty.
    """
    parsed = frozenset(part.strip() for part in raw.split(",") if part.strip())
    unknown = sorted(parsed - set(SCOPES))
    if unknown or not parsed:
        message = f"scopes must be a non-empty subset of {', '.join(SCOPES)}; got {raw!r}"
        raise ValidationError(message, details={"field": "scopes", "unknown": unknown})
    return parsed


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """One ``api_tokens`` row, without the secret."""

    token_id: str
    name: str
    scopes: frozenset[str]
    active: bool
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    use_count: int

    def as_json(self) -> dict[str, Any]:
        """The record as ``promptcadence token list --json`` prints it."""
        return {
            "token_id": self.token_id,
            "name": self.name,
            "scopes": sorted(self.scopes),
            "active": self.active,
            "created_at": to_rfc3339(self.created_at),
            "revoked_at": to_rfc3339(self.revoked_at) if self.revoked_at else None,
            "last_used_at": to_rfc3339(self.last_used_at) if self.last_used_at else None,
            "use_count": self.use_count,
        }


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A freshly created token: the secret, shown once, and its record."""

    token: str
    record: TokenRecord


def _record_of(row: ApiToken) -> TokenRecord:
    return TokenRecord(
        token_id=row.id,
        name=row.name,
        scopes=parse_scopes(row.scopes),
        active=bool(row.active) and row.revoked_at is None,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
        use_count=row.use_count,
    )


def create_token(
    database: Database, *, name: str, scopes: Iterable[str], now: datetime
) -> IssuedToken:
    """Issue a token.

    Args:
        database: An open handle.
        name: The token's name, unique among active tokens.
        scopes: A non-empty subset of :data:`SCOPES`.
        now: The creation instant.

    Returns:
        The secret and its record. The secret is never stored and never shown again.

    Raises:
        ValidationError: The name is empty, an active token already carries it, or a scope is
            outside the vocabulary.
    """
    cleaned = name.strip()
    if not cleaned:
        message = "a token needs a name"
        raise ValidationError(message, details={"field": "name"})
    held = parse_scopes(",".join(scopes))
    secret = secrets.token_urlsafe(32)
    with database.write() as session:
        clash = session.execute(
            select(ApiToken.id).where(
                ApiToken.name == cleaned, ApiToken.active.is_(True), ApiToken.revoked_at.is_(None)
            )
        ).scalar_one_or_none()
        if clash is not None:
            message = f"an active token named {cleaned!r} already exists"
            raise ValidationError(message, details={"field": "name", "name": cleaned})
        row = ApiToken(
            id=new_id(),
            name=cleaned,
            token_sha256=token_sha256(secret),
            scopes=",".join(sorted(held)),
            active=True,
            created_at=now,
        )
        session.add(row)
        session.flush()
        record = _record_of(row)
    return IssuedToken(token=secret, record=record)


def list_tokens(database: Database) -> tuple[TokenRecord, ...]:
    """Every token, revoked ones included, oldest first. Never the secret."""
    with database.read() as session:
        rows = session.execute(select(ApiToken).order_by(ApiToken.created_at)).scalars()
        return tuple(_record_of(row) for row in rows)


def revoke_token(database: Database, *, name: str, now: datetime) -> TokenRecord:
    """Revoke the active token with that name. Idempotent per name: a second call is refused.

    Raises:
        TokenNotFoundError: No active token has that name.
    """
    with database.write() as session:
        row = session.execute(
            select(ApiToken).where(
                ApiToken.name == name, ApiToken.active.is_(True), ApiToken.revoked_at.is_(None)
            )
        ).scalar_one_or_none()
        if row is None:
            raise TokenNotFoundError(f"No active token named {name!r}.", details={"name": name})
        row.active = False
        row.revoked_at = now
        session.flush()
        return _record_of(row)
