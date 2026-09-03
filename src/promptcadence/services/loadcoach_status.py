"""promptcadence.services.loadcoach_status — the one LoadCoach call Phase 1 makes: a health read.

ADR-0045 rule 2 forbids PromptCadence from reaching a model directly, and rule 3 requires that an
unreachable LoadCoach degrade health rather than fail startup or serving. This module is the
minimal ``httpx`` client that fact requires: a single ``GET /api/v1/health`` against the configured
LoadCoach, feeding the ``loadcoach`` health component and (from Phase 8) the telemetry widget.
Nothing else — no ``/generate``, no ``/route``, no job traffic, no error-code mapping. The full
client (``infrastructure/loadcoach.py``) arrives in Phase 3.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import httpx
from mirrorwall import ComponentHealth, ComponentStatus

__all__ = ["loadcoach_health_component"]

_HEALTH_CHECK_TIMEOUT_SECONDS: Final = 3.0


def _resolve_api_key(*, api_key_env: str, api_key_file: str) -> str | None:
    """Read the LoadCoach bearer token from its configured source (ADR-0026 §4).

    Never both — :mod:`promptcadence.config` refuses a configuration naming both a source.
    """
    if api_key_env:
        return os.environ.get(api_key_env)
    if api_key_file:
        try:
            return Path(api_key_file).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return None


def loadcoach_health_component(
    *,
    base_url: str,
    api_key_env: str = "",
    api_key_file: str = "",
) -> ComponentHealth:
    """Report the ``loadcoach`` health component.

    Args:
        base_url: ``settings.loadcoach.base_url``.
        api_key_env: ``settings.loadcoach.api_key_env``.
        api_key_file: ``settings.loadcoach.api_key_file``.

    Returns:
        ``OK`` when LoadCoach answers ``GET /api/v1/health`` with an ``ok`` status; ``DEGRADED``
        for every other outcome — unreachable, a non-2xx response, an unparsable body, or LoadCoach
        itself reporting trouble. **Never** ``UNAVAILABLE``: PromptCadence requires LoadCoach for
        execution, never for startup (ADR-0045 rule 3), so a downstream outage here must never
        drag PromptCadence's own ``/health`` below 200 (spec §20 AC1, development plan Phase 1
        acceptance criterion 1).
    """
    headers = {}
    token = _resolve_api_key(api_key_env=api_key_env, api_key_file=api_key_file)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{base_url.rstrip('/')}/api/v1/health"
    try:
        response = httpx.get(url, headers=headers, timeout=_HEALTH_CHECK_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return ComponentHealth(
            name="loadcoach", status=ComponentStatus.DEGRADED, detail=f"unreachable: {exc}"
        )

    try:
        payload = response.json()
    except ValueError:
        return ComponentHealth(
            name="loadcoach",
            status=ComponentStatus.DEGRADED,
            detail=f"responded {response.status_code} with a non-JSON body",
        )

    reported_status = payload.get("status", "unknown") if isinstance(payload, dict) else "unknown"
    status = ComponentStatus.OK if reported_status == "ok" else ComponentStatus.DEGRADED
    return ComponentHealth(
        name="loadcoach",
        status=status,
        detail=f"loadcoach reports {reported_status}",
        data={"reported_status": reported_status, "base_url": base_url},
    )
