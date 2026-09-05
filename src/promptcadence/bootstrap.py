"""promptcadence.bootstrap — the composition root: settings, logging and the ASGI app, wired once.

This module sits outside the ``web``/``cli``/``services``/``domain`` ordering that
``.importlinter`` enforces, precisely so it can import both configuration and the web layer.
``promptcadence.cli`` never imports it directly — the ``web-cli-independence`` contract forbids any
import chain from ``cli`` into ``web``, and this module imports ``web``. The CLI's ``serve``
command hands uvicorn the dotted string ``"promptcadence.bootstrap:create_app_from_environment"``
and lets uvicorn perform that import itself; a string literal is invisible to import-linter's
static analysis, so the two surfaces stay decoupled at the source level while running one
application in one process.

The two database-backed halves of the startup refusal set live here, once the database is ready:
at least one active API token before a non-loopback bind (ADR-0026), and at least one
``approve``-scoped active token before ``approval.mode = "manual"`` (ADR-0049 rule 2) — a mode
nobody can satisfy is a configuration error, not a runtime surprise. The config-only halves
(``server.allowed_hosts``, tier and project-budget rules) already ran inside
:func:`promptcadence.config.load_settings`.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy import select

from promptcadence.config import LOOPBACK_HOSTS, InsecureBindingError, LoadedSettings, load_settings
from promptcadence.infrastructure.db.models import ApiToken
from promptcadence.observability.logging import configure_logging
from promptcadence.services.database import Database, ensure_ready
from promptcadence.services.runtime import build_runtime
from promptcadence.services.tokens import grants
from promptcadence.web.app import create_app

__all__ = ["Application", "bootstrap", "create_app_from_environment"]


@dataclass(frozen=True, slots=True)
class Application:
    """A fully wired application: the settings it was built from and its ASGI app."""

    loaded_settings: LoadedSettings
    app: FastAPI


def _has_active_token(database: Database, *, scope: str | None = None) -> bool:
    """Return whether an active, unrevoked token exists, optionally carrying ``scope``.

    Args:
        database: An open handle.
        scope: When given, only a token whose comma-separated ``scopes`` column contains this
            exact scope counts. PromptCadence stores tokens in the ``api_tokens`` table (created
            by ``promptcadence token create``) rather than in ``config.toml`` — a token an
            operator can issue and revoke without editing a file. ``admin`` contains every
            scope (:func:`promptcadence.services.tokens.grants`).
    """
    with database.read() as session:
        rows = session.execute(
            select(ApiToken.scopes).where(ApiToken.active.is_(True), ApiToken.revoked_at.is_(None))
        ).scalars()
        if scope is None:
            return any(True for _ in rows)
        return any(grants(row.split(","), scope) for row in rows)


def bootstrap() -> Application:
    """Load configuration, configure logging, ready the database, build the app.

    Reads configuration through the standard precedence chain (defaults, file, environment) with
    no CLI-argument layer of its own: a caller that needs CLI overrides applies them as
    environment variables before calling this function, which is what
    ``promptcadence.cli.commands.system.serve`` does.

    The two database-backed refusals run here, in the composition root, and deliberately not
    inside :func:`~promptcadence.web.app.create_app` — that function is documented as a pure
    function of :class:`~promptcadence.config.Settings` precisely so tests can build an app without
    touching the filesystem, and opening a database is neither pure nor free.

    Returns:
        The wired :class:`Application`.

    Raises:
        ConfigurationError: Configuration is invalid, or an unsafe bind combination is configured.
        InsecureBindingError: ``server.host`` is not loopback and no active API token exists, or
            ``approval.mode`` is ``"manual"`` and no active ``approve``-scoped token exists.
        MigrationRequired: The database is behind head and ``storage.auto_migrate`` is false.
        SchemaAhead: The database was written by a newer application version.
        DatabaseUnavailable: The configured database could not be reached at all.
    """
    loaded = load_settings()
    configure_logging(
        level=loaded.settings.logging.level, log_format=loaded.settings.logging.format
    )
    database_url = loaded.settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        message = "no database_url configured"
        raise RuntimeError(message)
    with Database.from_url(database_url) as database:
        ensure_ready(database, auto_migrate=loaded.settings.storage.auto_migrate)
        if loaded.settings.server.host not in LOOPBACK_HOSTS and not _has_active_token(database):
            raise InsecureBindingError(
                "server.host is not loopback but no active API token exists. A non-loopback bind "
                "must have at least one token created first: `promptcadence token create`.",
                details={"field": "server.host", "host": loaded.settings.server.host},
            )
        if loaded.settings.approval.mode == "manual" and not _has_active_token(
            database, scope="approve"
        ):
            raise InsecureBindingError(
                "approval.mode is 'manual' but no active, approve-scoped API token exists. A mode "
                "nobody can satisfy is a configuration error: create one with "
                "`promptcadence token create --scope approve` first (ADR-0049 rule 2).",
                details={"field": "approval.mode"},
            )
    return Application(
        loaded_settings=loaded, app=create_app(loaded.settings, runtime_builder=build_runtime)
    )


def create_app_from_environment() -> FastAPI:
    """Zero-argument ASGI factory: the target uvicorn imports by dotted name.

    See the module docstring for why this is referenced by string rather than imported directly by
    ``promptcadence.cli``.
    """
    return bootstrap().app
