"""promptcadence.services.diagnostics — the one-shot health report ``health`` and ``doctor`` share.

``GET /api/v1/health`` reads from a :class:`~promptcadence.services.runtime.Runtime` already open
in the served process. The CLI has no such process — :func:`health_report` opens (and closes) its
own database handle for the single check, and tolerates every failure a served process would
instead refuse to start over: unlike :class:`~promptcadence.services.runtime.Runtime`, this
function never raises. A diagnostic that itself crashes on the exact condition it exists to
explain has failed at its one job.
"""

from __future__ import annotations

from typing import Any

from mirrorwall import ComponentHealth, ComponentStatus, health_payload

from promptcadence.__about__ import __version__
from promptcadence.config import ConfigurationError, load_settings
from promptcadence.services.database import Database, database_health_component
from promptcadence.services.loadcoach_status import loadcoach_health_component
from promptcadence.services.tools import ToolPlant, tools_health_component

__all__ = ["health_report"]


def _components() -> list[ComponentHealth]:
    """Build the three components, tolerating a broken configuration.

    ``tools`` joins ``database`` and ``loadcoach`` at Phase 4, because the question an operator
    brings to ``doctor`` after a refused command — which isolation rung does this host have, and
    why that one — is answerable only by probing the host, and this is the command that probes it.
    """
    try:
        loaded = load_settings()
    except ConfigurationError as exc:
        detail = f"configuration: {exc.message}"
        return [
            ComponentHealth(name="database", status=ComponentStatus.DEGRADED, detail=detail),
            ComponentHealth(name="loadcoach", status=ComponentStatus.DEGRADED, detail=detail),
            ComponentHealth(name="tools", status=ComponentStatus.DEGRADED, detail=detail),
        ]

    settings = loaded.settings
    database_url = settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        database_component = ComponentHealth(
            name="database", status=ComponentStatus.DEGRADED, detail="no database_url configured"
        )
    else:
        with Database.from_url(database_url) as database:
            database_component = database_health_component(database)

    loadcoach_component = loadcoach_health_component(
        base_url=settings.loadcoach.base_url,
        api_key_env=settings.loadcoach.api_key_env,
        api_key_file=settings.loadcoach.api_key_file,
    )
    try:
        tools_component = tools_health_component(ToolPlant(settings))
    except ConfigurationError as exc:
        # A misconfigured [tools] root must not crash the diagnostic that exists to explain it.
        tools_component = ComponentHealth(
            name="tools", status=ComponentStatus.DEGRADED, detail=f"configuration: {exc.message}"
        )
    return [database_component, loadcoach_component, tools_component]


def health_report() -> dict[str, Any]:
    """Build the same payload ``GET /api/v1/health`` returns, for a one-shot CLI call.

    Returns:
        MirrorWall's standard health payload (``status``, ``application``, ``version``,
        ``checked_at``, ``components``) — the shape ``promptcadence health --json`` prints
        verbatim, so the CLI and the API report identical status by construction, never by review.
    """
    return health_payload(
        application="promptcadence", version=__version__, components=_components()
    )
