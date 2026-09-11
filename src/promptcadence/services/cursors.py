"""promptcadence.services.cursors — the opaque cursor two listings page by (API standards §6).

A cursor is base64 of a stable, total sort key — an instant and the row's id, which breaks ties —
so a page boundary between two rows written in the same millisecond neither skips nor repeats one.
Clients never construct a cursor; one this build did not mint is refused by name rather than read
as "start from the top", which would hand a paging loop the first page forever.
"""

from __future__ import annotations

import base64
from datetime import datetime

from baseaicore import ValidationError
from baseaicore.timeutil import to_rfc3339

__all__ = ["decode_cursor", "encode_cursor"]


def encode_cursor(at: datetime, identity: str) -> str:
    """The cursor naming the row at ``(at, identity)``.

    Args:
        at: The row's sort instant; timezone-aware.
        identity: The row's id, the tie-breaker.

    Returns:
        URL-safe base64 text.
    """
    return base64.urlsafe_b64encode(f"{to_rfc3339(at)}|{identity}".encode()).decode("ascii")


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    """The ``(instant, id)`` a cursor names, or ``None`` when no cursor was given.

    Raises:
        ValidationError: The text is not a cursor :func:`encode_cursor` minted (``field: cursor``).
    """
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        stamp, separator, identity = raw.partition("|")
        at = datetime.fromisoformat(stamp)
    except (ValueError, UnicodeDecodeError) as exc:
        message = "cursor is not one this API issued; pass page.next_cursor unchanged"
        raise ValidationError(message, details={"field": "cursor"}) from exc
    if not separator or not identity or at.tzinfo is None:
        message = "cursor is not one this API issued; pass page.next_cursor unchanged"
        raise ValidationError(message, details={"field": "cursor"})
    return at, identity
