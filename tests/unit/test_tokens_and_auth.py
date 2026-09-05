"""API tokens and the ``approve`` scope (spec §14, ADR-0026, ADR-0049 rule 2)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from baseaicore import ValidationError
from weightsdb import MigrationRunner
from weightsdb.testing import temporary_sqlite

from promptcadence.services.database import MIGRATIONS_LOCATION, Database
from promptcadence.services.tokens import (
    SCOPES,
    TokenNotFoundError,
    create_token,
    grants,
    list_tokens,
    revoke_token,
    token_sha256,
)
from promptcadence.web.auth import Unauthorized, resolve_principal

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def database() -> Iterator[Database]:
    with temporary_sqlite() as engine:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        yield Database(engine)


def test_scopes_are_a_set_and_only_admin_contains_the_others() -> None:
    assert SCOPES == ("read", "write", "approve", "admin")
    assert grants({"approve"}, "approve") and not grants({"write"}, "approve")
    assert not grants({"approve"}, "write"), "approve is deliberately not write"
    assert all(grants({"admin"}, scope) for scope in SCOPES)


def test_a_token_is_issued_once_stored_as_a_hash_and_listed_without_its_secret(
    database: Database,
) -> None:
    issued = create_token(database, name="ops", scopes=["approve", "read"], now=_NOW)
    assert issued.record.scopes == frozenset({"approve", "read"})
    (record,) = list_tokens(database)
    assert record.name == "ops" and record.active
    assert not hasattr(record, "token")
    with database.read() as session:
        from sqlalchemy import select

        from promptcadence.infrastructure.db.models import ApiToken

        row = session.execute(select(ApiToken)).scalar_one()
    assert row.token_sha256 == token_sha256(issued.token)
    assert issued.token not in row.token_sha256


def test_names_are_unique_among_active_tokens_and_scopes_are_validated(
    database: Database,
) -> None:
    create_token(database, name="ops", scopes=["read"], now=_NOW)
    with pytest.raises(ValidationError):
        create_token(database, name="ops", scopes=["read"], now=_NOW)
    with pytest.raises(ValidationError):
        create_token(database, name="x", scopes=["root"], now=_NOW)
    with pytest.raises(ValidationError):
        create_token(database, name="  ", scopes=["read"], now=_NOW)
    revoke_token(database, name="ops", now=_NOW)
    create_token(database, name="ops", scopes=["read"], now=_NOW), "a revoked name is free again"
    with pytest.raises(TokenNotFoundError):
        revoke_token(database, name="nobody", now=_NOW)


def test_loopback_with_no_tokens_is_open_and_names_itself(database: Database) -> None:
    principal = resolve_principal(database, authorization=None, bind_host="127.0.0.1", now=_NOW)
    assert principal.token_id == "loopback" and principal.source == "loopback"  # noqa: S105
    assert principal.grants("approve")


def test_a_non_loopback_bind_with_no_tokens_is_refused(database: Database) -> None:
    with pytest.raises(Unauthorized):
        resolve_principal(database, authorization=None, bind_host="0.0.0.0", now=_NOW)  # noqa: S104


def test_once_a_token_exists_a_bearer_is_required_and_checked(database: Database) -> None:
    issued = create_token(database, name="ops", scopes=["approve"], now=_NOW)
    with pytest.raises(Unauthorized):
        resolve_principal(database, authorization=None, bind_host="127.0.0.1", now=_NOW)
    with pytest.raises(Unauthorized):
        resolve_principal(database, authorization="Bearer nope", bind_host="127.0.0.1", now=_NOW)
    principal = resolve_principal(
        database, authorization=f"Bearer {issued.token}", bind_host="127.0.0.1", now=_NOW
    )
    assert principal.token_id == issued.record.token_id  # noqa: S105
    assert principal.source == "token"
    assert principal.grants("approve") and not principal.grants("write")
    (record,) = list_tokens(database)
    assert record.use_count == 1 and record.last_used_at == _NOW
    revoke_token(database, name="ops", now=_NOW)
    # With no active token left the loopback install is open again (the LoadCoach precedent);
    # the revoked secret buys nothing — the principal is the open install, not the token.
    reopened = resolve_principal(
        database, authorization=f"Bearer {issued.token}", bind_host="127.0.0.1", now=_NOW
    )
    assert reopened.source == "loopback" and reopened.token_id != issued.record.token_id
