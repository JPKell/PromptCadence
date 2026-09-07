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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mirrorwall import ComponentHealth, ComponentStatus

from promptcadence.domain.errors import LoadCoachError, LoadCoachUnavailableError
from promptcadence.services.loadcoach_surface import remote_provider_registered
from promptcadence.services.planner import PLANNER_TASK_PROFILE

if TYPE_CHECKING:
    from promptcadence.config import Settings
    from promptcadence.infrastructure.loadcoach import LoadCoachClient
    from promptcadence.services.pricing import PricingCatalog

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
class RemoteTierCheck:
    """Whether one remote tier can serve right now, and the one recorded reason when it cannot.

    ADR-0098 rule 3: exactly two reasons, in this order — ``loadcoach_has_no_remote_provider``
    (LoadCoach has no registration declaring ``remote = true``) or ``unpriced`` (the tier's price
    list holds no record claiming now). A remote tier with both is available; ADR-0073's
    pre-flight order still runs on every turn.
    """

    tier: str
    available: bool
    reason: str | None

    def as_json(self) -> dict[str, Any]:
        """The API and CLI mapping form."""
        return {"tier": self.tier, "available": self.available, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class TierCheck:
    """The whole check: every configured tier plus the planner profile, and the remote tiers."""

    reachable: bool
    checks: tuple[ProfileCheck, ...]
    detail: str
    remote: tuple[RemoteTierCheck, ...] = ()

    @property
    def ok(self) -> bool:
        """LoadCoach answered, every profile resolves and is enabled, every remote tier serves."""
        return (
            self.reachable
            and all(check.found and check.enabled for check in self.checks)
            and all(check.available for check in self.remote)
        )

    def as_json(self) -> dict[str, Any]:
        """The API and CLI mapping form."""
        return {
            "reachable": self.reachable,
            "ok": self.ok,
            "detail": self.detail,
            "checks": [check.as_json() for check in self.checks],
            "remote": [check.as_json() for check in self.remote],
        }


def unpriced_remote_tiers(
    settings: Settings, pricing: PricingCatalog | None, *, at: datetime
) -> frozenset[str]:
    """The remote tiers whose price list holds no record claiming ``at`` (spec §11 contract 5).

    Args:
        settings: The validated configuration.
        pricing: The loaded catalogue, or ``None`` when the caller could not load one — then no
            tier is reported unpriced here, because the answer is unknown rather than negative.
        at: The instant a record must claim.
    """
    if pricing is None:
        return frozenset()
    return frozenset(
        name
        for name, tier in settings.tiers.items()
        if tier.remote and not pricing.claiming(tier=name, at=at)
    )


def remote_tier_checks(
    settings: Settings, *, remote_provider: bool, unpriced: frozenset[str]
) -> tuple[RemoteTierCheck, ...]:
    """One :class:`RemoteTierCheck` per configured remote tier, in name order."""
    checks = []
    for name, tier in sorted(settings.tiers.items()):
        if not tier.remote:
            continue
        reason = (
            "loadcoach_has_no_remote_provider"
            if not remote_provider
            else "unpriced"
            if name in unpriced
            else None
        )
        checks.append(RemoteTierCheck(tier=name, available=reason is None, reason=reason))
    return tuple(checks)


def check_tiers(
    settings: Settings,
    loadcoach: LoadCoachClient,
    *,
    pricing: PricingCatalog | None = None,
    now: datetime | None = None,
) -> TierCheck:
    """Ask LoadCoach for each tier's profile, for ``tools.plan``, and for the remote fact.

    Args:
        settings: The validated configuration.
        loadcoach: The client.
        pricing: The loaded price catalogue, for the ``unpriced`` half of a remote tier's
            availability; ``None`` leaves that half unknown and unreported.
        now: The instant a pricing record must claim; the wall clock when omitted.

    Returns:
        The check. An unreachable LoadCoach is reported as such with every profile unfound, never
        raised: this is a diagnostic, and a diagnostic that crashes on the condition it explains
        has failed at its one job. Every configured remote tier is reported available or
        unavailable with its one reason (ADR-0098 rule 3).
    """
    at = now if now is not None else datetime.now(UTC)
    remote = remote_tier_checks(
        settings,
        remote_provider=remote_provider_registered(loadcoach),
        unpriced=unpriced_remote_tiers(settings, pricing, at=at),
    )
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
            remote=remote,
        )
    except LoadCoachError as exc:
        return TierCheck(
            reachable=True,
            checks=tuple(checks),
            detail=f"LoadCoach failed the profile read: {exc.message}",
            remote=remote,
        )
    missing = [check.task_profile for check in checks if not (check.found and check.enabled)]
    detail = (
        f"{len(checks)} profile(s) resolve in LoadCoach, tools.plan included"
        if not missing
        else f"missing or disabled in LoadCoach: {', '.join(missing)}"
    )
    unavailable = [f"{check.tier} ({check.reason})" for check in remote if not check.available]
    if unavailable:
        detail += f"; remote tier(s) unavailable: {', '.join(unavailable)}"
    return TierCheck(reachable=True, checks=tuple(checks), detail=detail, remote=remote)


def tiers_health_component(
    settings: Settings,
    loadcoach: LoadCoachClient,
    *,
    pricing: PricingCatalog | None = None,
    now: datetime | None = None,
) -> ComponentHealth:
    """Report the ``tiers`` health component (spec §17).

    Each tier's profile must resolve in LoadCoach, and each remote tier reports whether it can
    serve and, when it cannot, which of its two preconditions is unmet (ADR-0098 rule 3). A
    missing remote provider **degrades** the component with the reason; it is never a failure to
    serve.
    """
    result = check_tiers(settings, loadcoach, pricing=pricing, now=now)
    return ComponentHealth(
        name="tiers",
        status=ComponentStatus.OK if result.ok else ComponentStatus.DEGRADED,
        detail=result.detail,
        data=result.as_json(),
    )
