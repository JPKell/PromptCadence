"""promptcadence.services.policy_assembly — the one place configuration becomes domain values.

``promptcadence.config`` parses and validates; the domain decides. The two shapes meet here and
nowhere else, which is what keeps :mod:`promptcadence.domain` free of pydantic's semantics —
coercion, aliasing, ``model_config`` — in the layer whose outputs are goldens, and what lets every
later phase build a :class:`~promptcadence.domain.tiers.TierPolicy` in a test without constructing
a ``Settings``.

The direction is one-way on purpose. Nothing here reads a domain object back into configuration,
and nothing in ``domain`` imports this module.
"""

from __future__ import annotations

from baseaicore import Money

from promptcadence.config import MoneyAmount, Settings
from promptcadence.config import Tier as ConfiguredTier
from promptcadence.domain.policy import ApprovalMode, ApprovalPolicy, ReapprovalScope
from promptcadence.domain.tiers import EgressClass, Tier, TierPolicy, TierSnapshot

__all__ = [
    "approval_policy_from_settings",
    "money_from_amount",
    "tier_from_config",
    "tier_policy_from_settings",
    "tier_snapshot_from_settings",
]


def money_from_amount(amount: MoneyAmount) -> Money:
    """Convert a configured ``{currency, nanos}`` table into a :class:`baseaicore.Money`.

    Args:
        amount: The validated configuration value.

    Returns:
        The money value the domain compares against.
    """
    return Money(currency=amount.currency, nanos=amount.nanos)


def tier_from_config(name: str, configured: ConfiguredTier) -> Tier:
    """Convert one ``[tiers.<name>]`` entry into the domain's frozen tier.

    Args:
        name: The section name, which is the tier's name — configuration keys tiers by name, and
            the domain carries the name on the object so a tier is self-describing once it leaves
            the mapping.
        configured: The validated configuration entry.

    Returns:
        The domain tier.

    Raises:
        ValidationError: If the entry would produce an invalid tier — a remote tier with no
            ceiling, for instance. ``config.load_settings`` already refuses that at startup; this
            is the same refusal reached by a different door, and both exist because an unceilinged
            remote tier must never be assumed public (ADR-0046 rule 3).
    """
    return Tier(
        name=name,
        task_profile=configured.task_profile,
        egress_class=EgressClass.REMOTE if configured.remote else EgressClass.LOCAL,
        max_data_classification=configured.max_data_classification,
        context_budget_tokens=configured.context_budget_tokens,
        pricing_source=configured.pricing_file,
    )


def tier_snapshot_from_settings(settings: Settings) -> TierSnapshot:
    """Build the tier snapshot a trajectory records at creation.

    Taken once, at creation, and stored with the trajectory: a trajectory's explanation must stay
    readable after an operator edits a ceiling, so it carries the definitions it ran under rather
    than a reference to today's configuration.

    Args:
        settings: The validated configuration.

    Returns:
        The snapshot, with tiers ordered by name so its content address is stable.
    """
    tiers = tuple(
        tier_from_config(name, configured) for name, configured in sorted(settings.tiers.items())
    )
    return TierSnapshot(
        tiers=tiers,
        default_tier=settings.policy.default_tier,
        escalation_order=tuple(settings.policy.escalation_order),
    )


def tier_policy_from_settings(
    settings: Settings, *, loadcoach_has_remote_provider: bool = False
) -> TierPolicy:
    """Build the tier policy a trajectory is governed by.

    Args:
        settings: The validated configuration.
        loadcoach_has_remote_provider: Whether LoadCoach has a remote provider registered. False
            until LC-E1 lands (lifecycle §3); Phase 3 supplies it from LoadCoach's
            ``/system/status``, which is why it is a parameter rather than a configuration value.

    Returns:
        The policy, wrapping a snapshot taken from these settings.
    """
    return TierPolicy(
        snapshot=tier_snapshot_from_settings(settings),
        loadcoach_has_remote_provider=loadcoach_has_remote_provider,
    )


def approval_policy_from_settings(settings: Settings) -> ApprovalPolicy:
    """Build the approval policy whose version every trajectory records.

    ``reapproval_scope`` comes from ``[planning]`` rather than ``[approval]`` because that is where
    spec §12 puts it; it is an approval-policy input all the same, and having it here means a
    change to it changes :attr:`~promptcadence.domain.policy.ApprovalPolicy.version` like any
    other.

    Args:
        settings: The validated configuration.

    Returns:
        The approval policy.
    """
    return ApprovalPolicy(
        mode=ApprovalMode(settings.approval.mode),
        gate_egress_at=settings.approval.gate_egress_at,
        gate_step_cost=money_from_amount(settings.approval.gate_step_cost),
        request_timeout_hours=settings.approval.request_timeout_hours,
        reapproval_scope=ReapprovalScope(settings.planning.reapproval_scope),
    )
