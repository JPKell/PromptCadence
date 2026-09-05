"""promptcadence.services.tiers — does every configured tier's profile exist in LoadCoach?

``promptcadence tiers check``, ``doctor``'s ``tiers`` component and ``GET /health`` all ask the
same question (spec §12, §17): for each ``[tiers.<name>]``, does the running LoadCoach serve the
task profile it names — and does it serve ``tools.plan``, which no tier names and every planned
trajectory calls. One function answers it, so the three surfaces cannot disagree.

Like the ``loadcoach`` component, this is never ``UNAVAILABLE``: PromptCadence requires LoadCoach
for execution, never for startup (ADR-0045 rule 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mirrorwall import ComponentHealth, ComponentStatus

from promptcadence.domain.errors import LoadCoachError, LoadCoachUnavailableError
from promptcadence.services.planner import PLANNER_TASK_PROFILE

if TYPE_CHECKING:
    from promptcadence.config import Settings
    from promptcadence.infrastructure.loadcoach import LoadCoachClient

__all__ = ["ProfileCheck", "TierCheck", "check_tiers", "tiers_health_component"]


@dataclass(frozen=True, slots=True)
class ProfileCheck:
    """One profile PromptCadence needs, and whether LoadCoach has it."""

    tier: str | None
    task_profile: str
    found: bool
    enabled: bool | None
    detail: str

    def as_json(self) -> dict[str, Any]:
        """The API and CLI mapping form."""
        return {
            "tier": self.tier,
            "task_profile": self.task_profile,
            "found": self.found,
            "enabled": self.enabled,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class TierCheck:
    """The whole check: every configured tier plus the planner profile."""

    reachable: bool
    checks: tuple[ProfileCheck, ...]
    detail: str

    @property
    def ok(self) -> bool:
        """Whether LoadCoach answered and every profile resolved and is enabled."""
        return self.reachable and all(check.found and check.enabled for check in self.checks)

    def as_json(self) -> dict[str, Any]:
        """The API and CLI mapping form."""
        return {
            "reachable": self.reachable,
            "ok": self.ok,
            "detail": self.detail,
            "checks": [check.as_json() for check in self.checks],
        }


def check_tiers(settings: Settings, loadcoach: LoadCoachClient) -> TierCheck:
    """Ask LoadCoach for each configured tier's profile, and for ``tools.plan``.

    Args:
        settings: The validated configuration.
        loadcoach: The client.

    Returns:
        The check. An unreachable LoadCoach is reported as such with every profile unfound, never
        raised: this is a diagnostic, and a diagnostic that crashes on the condition it explains
        has failed at its one job.
    """
    wanted: list[tuple[str | None, str]] = [
        (name, tier.task_profile) for name, tier in sorted(settings.tiers.items())
    ]
    wanted.append((None, PLANNER_TASK_PROFILE))
    checks: list[ProfileCheck] = []
    try:
        for tier, profile_id in wanted:
            info = loadcoach.task_profile(profile_id)
            if info is None:
                checks.append(
                    ProfileCheck(
                        tier=tier,
                        task_profile=profile_id,
                        found=False,
                        enabled=None,
                        detail="LoadCoach has no such task profile (TASK_PROFILE_NOT_FOUND)",
                    )
                )
            else:
                checks.append(
                    ProfileCheck(
                        tier=tier,
                        task_profile=profile_id,
                        found=True,
                        enabled=info.enabled,
                        detail=(f"version {info.version}" + ("" if info.enabled else ", disabled")),
                    )
                )
    except LoadCoachUnavailableError as exc:
        return TierCheck(
            reachable=False,
            checks=tuple(
                ProfileCheck(
                    tier=tier,
                    task_profile=profile_id,
                    found=False,
                    enabled=None,
                    detail="unreachable",
                )
                for tier, profile_id in wanted
            ),
            detail=f"LoadCoach unreachable: {exc.message}",
        )
    except LoadCoachError as exc:
        return TierCheck(
            reachable=True,
            checks=tuple(checks),
            detail=f"LoadCoach failed the profile read: {exc.message}",
        )
    missing = [check.task_profile for check in checks if not (check.found and check.enabled)]
    detail = (
        f"{len(checks)} profile(s) resolve in LoadCoach, tools.plan included"
        if not missing
        else f"missing or disabled in LoadCoach: {', '.join(missing)}"
    )
    return TierCheck(reachable=True, checks=tuple(checks), detail=detail)


def tiers_health_component(settings: Settings, loadcoach: LoadCoachClient) -> ComponentHealth:
    """Report the ``tiers`` health component (spec §17): each tier's profile resolvable."""
    result = check_tiers(settings, loadcoach)
    return ComponentHealth(
        name="tiers",
        status=ComponentStatus.OK if result.ok else ComponentStatus.DEGRADED,
        detail=result.detail,
        data=result.as_json(),
    )
