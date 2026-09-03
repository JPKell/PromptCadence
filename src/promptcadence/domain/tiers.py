"""promptcadence.domain.tiers — tiers, admission, escalation, and the snapshot a trajectory keeps.

A tier is **configuration over LoadCoach, never routing math** (ADR-0047): it names exactly one
LoadCoach task profile, an egress class, a classification ceiling and a context budget, and
*which model within the tier* stays LoadCoach's filter → score → rank → select. Nothing in this
module scores, ranks or selects anything.

Three things live here and nowhere else.

**Admission** is one comparison: ``classification <= tier.max_data_classification``, over
:class:`baseaicore.DataClassification`'s ordering and no vocabulary of PromptCadence's own
(ADR-0046). A local tier has an implicit ceiling of ``CONFIDENTIAL`` — it serves anything, because
nothing leaves the machine. A remote tier must declare one, and
:func:`promptcadence.config.load_settings` already refuses at startup when it does not; this
module does not restate that refusal (ADR-0042 — a second, differently-worded copy of a rule is
how two rules drift apart). What it does instead is make the undeclared case unrepresentable:
:class:`Tier` derives its effective ceiling, and a remote tier constructed without one is refused
here too, as a wiring error rather than a configuration one.

**Availability** is a pure determination over configuration: until LC-E1 registers a second
provider in LoadCoach, every remote tier reports ``TIER_UNAVAILABLE`` with the reason
``loadcoach_has_no_remote_provider`` (lifecycle §3). Nothing calls it until Phase 3, but it needs
no I/O, so it belongs with the rest of the pure tier logic.

**The snapshot** is what makes a trajectory's explanation survive a configuration change. A
trajectory records the tier definitions it ran under, content-addressed, rather than a reference
to today's configuration — otherwise "why did this run remotely?" answers with a tier that has
since been edited.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from baseaicore import DataClassification, ValidationError, sha256_of

from promptcadence.domain.errors import TierNotConfiguredError

__all__ = [
    "LOCAL_TIER_CEILING",
    "EgressClass",
    "Tier",
    "TierAvailability",
    "TierPolicy",
    "TierSnapshot",
    "TierUnavailableReason",
    "most_permissive",
]

LOCAL_TIER_CEILING: Final = DataClassification.CONFIDENTIAL
"""The implicit ceiling of a local tier: it serves anything, because nothing leaves the machine."""


class EgressClass(StrEnum):
    """Whether work on a tier leaves this machine. Ordered, and it defaults closed.

    Two members, ordered ``LOCAL < REMOTE``, with the comparison defined here rather than
    inherited from :class:`str` — alphabetically ``"local" < "remote"`` happens to agree, and
    relying on that coincidence is how the next member breaks the ordering silently.

    This is the same shape ToolYard's egress ceiling has, for the same reason: a caller who has
    not thought about egress must get the answer that cannot leak.
    """

    LOCAL = "local"
    REMOTE = "remote"

    @property
    def rank(self) -> int:
        """Return the position in the ordering: ``0`` for local, ``1`` for remote."""
        return 0 if self is EgressClass.LOCAL else 1

    def _rank_of(self, other: object) -> int:
        """Return ``other``'s rank, refusing anything that is not an :class:`EgressClass`.

        A bare string is **refused rather than compared**. This class subclasses :class:`str`, so
        returning ``NotImplemented`` would let Python fall back to ``str``'s own ordering and
        answer alphabetically — quietly, and one day wrongly. That is the trap
        :class:`baseaicore.DataClassification` names, met here for the same reason.
        """
        if not isinstance(other, EgressClass):
            message = (
                f"an egress class is comparable only to another, not to {type(other).__name__}"
            )
            raise TypeError(message)
        return other.rank

    def __lt__(self, other: object) -> bool:
        """Order by rank, refusing anything that is not an :class:`EgressClass`."""
        return self.rank < self._rank_of(other)

    def __le__(self, other: object) -> bool:
        """Order by rank, refusing anything that is not an :class:`EgressClass`."""
        return self.rank <= self._rank_of(other)

    def __gt__(self, other: object) -> bool:
        """Order by rank, refusing anything that is not an :class:`EgressClass`."""
        return self.rank > self._rank_of(other)

    def __ge__(self, other: object) -> bool:
        """Order by rank, refusing anything that is not an :class:`EgressClass`."""
        return self.rank >= self._rank_of(other)


class TierUnavailableReason(StrEnum):
    """Why a configured tier cannot serve right now. One member; lifecycle §3 names exactly one."""

    LOADCOACH_HAS_NO_REMOTE_PROVIDER = "loadcoach_has_no_remote_provider"


@dataclass(frozen=True, slots=True)
class Tier:
    """One named execution surface, as configured (lifecycle §3).

    A frozen dataclass rather than ``promptcadence.config.Tier`` itself: the domain is testable
    without constructing a ``Settings`` object, and a validation framework's semantics — coercion,
    aliasing, ``model_config`` — stay out of the layer whose outputs are goldens. The adapter that
    builds these from configuration is
    :func:`promptcadence.services.policy_assembly.tier_policy_from_settings`, and it is the only
    place the two shapes meet.

    Attributes:
        name: The tier's configured name, as it appears in a plan, an intent and a turn row.
        task_profile: Exactly one LoadCoach task profile. What model runs is LoadCoach's decision.
        egress_class: Whether work here leaves the machine.
        max_data_classification: The declared ceiling. ``None`` is only legal for a local tier,
            whose effective ceiling is :data:`LOCAL_TIER_CEILING`.
        context_budget_tokens: The compaction trigger input (lifecycle §7).
        pricing_source: The ``ModelPricing`` source, required on a remote tier. Empty otherwise.

    Raises:
        ValidationError: If the name or task profile is empty, ``context_budget_tokens`` is below
            1, or a remote tier declares no ceiling. The last is the wiring-error half of the
            startup refusal ``config.py`` already performs; both exist because a remote tier with
            no ceiling would otherwise be assumed public, which is the fail-open ADR-0046 rejects.
    """

    name: str
    task_profile: str
    egress_class: EgressClass
    max_data_classification: DataClassification | None
    context_budget_tokens: int
    pricing_source: str = ""

    def __post_init__(self) -> None:
        """Refuse an unnamed tier, a non-positive context budget or an unceilinged remote tier."""
        if not self.name.strip():
            message = "a tier must be named"
            raise ValidationError(message, details={"field": "name"})
        if not self.task_profile.strip():
            message = f"tier {self.name} names no task profile (ADR-0047)"
            raise ValidationError(message, details={"field": "task_profile", "tier": self.name})
        if self.context_budget_tokens < 1:
            message = (
                f"tier {self.name} has context_budget_tokens={self.context_budget_tokens}; "
                "a tier must be able to hold at least one token"
            )
            raise ValidationError(
                message, details={"field": "context_budget_tokens", "tier": self.name}
            )
        if self.is_remote and self.max_data_classification is None:
            message = (
                f"remote tier {self.name} declares no max_data_classification. An undeclared "
                "ceiling is never assumed public (ADR-0046 rule 3)."
            )
            raise ValidationError(
                message, details={"field": "max_data_classification", "tier": self.name}
            )

    @property
    def is_remote(self) -> bool:
        """Whether work on this tier leaves the machine."""
        return self.egress_class is EgressClass.REMOTE

    @property
    def effective_max_classification(self) -> DataClassification:
        """The ceiling admission actually compares against.

        A local tier's declared ceiling is meaningless — nothing leaves — so it is
        :data:`LOCAL_TIER_CEILING` regardless of what configuration said. A remote tier's is what
        it declared, which ``__post_init__`` has already required.

        Returns:
            The classification this tier will admit up to, inclusive.
        """
        if not self.is_remote:
            return LOCAL_TIER_CEILING
        assert self.max_data_classification is not None  # noqa: S101 — __post_init__ requires it
        return self.max_data_classification

    def admits(self, classification: DataClassification) -> bool:
        """Whether this tier may serve data of that classification.

        The single admission rule of the whole application: ``classification <= ceiling``, over
        :class:`baseaicore.DataClassification`'s ordering, with no string comparison and no
        parallel vocabulary (ADR-0046).

        Args:
            classification: What the work is classified as.

        Returns:
            ``True`` when the classification is at or below this tier's effective ceiling.
        """
        return classification <= self.effective_max_classification

    def egress_classification(
        self, classification: DataClassification
    ) -> DataClassification | None:
        """What would actually leave the machine if this tier served that classification.

        Args:
            classification: The classification of the work.

        Returns:
            ``None`` for a local tier — nothing leaves, so there is no egress to gate — and the
            lesser of the work's classification and this tier's ceiling for a remote one. The
            ``min`` matters: a ``confidential`` trajectory on a tier ceilinged at ``internal``
            never reaches this function (admission refuses it first), and where admission passes,
            what leaves is bounded by both.
        """
        if not self.is_remote:
            return None
        return min(classification, self.effective_max_classification)

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form used inside a :class:`TierSnapshot`'s content address."""
        return {
            "name": self.name,
            "task_profile": self.task_profile,
            "egress_class": self.egress_class.value,
            "max_data_classification": (
                self.max_data_classification.value
                if self.max_data_classification is not None
                else None
            ),
            "context_budget_tokens": self.context_budget_tokens,
            "pricing_source": self.pricing_source,
        }


@dataclass(frozen=True, slots=True)
class TierAvailability:
    """Whether a tier can serve right now, and why not when it cannot.

    Attributes:
        tier_name: The tier this is about.
        available: Whether it can serve.
        reason: Why not. ``None`` exactly when ``available`` is ``True``.
    """

    tier_name: str
    available: bool
    reason: TierUnavailableReason | None = None

    def __post_init__(self) -> None:
        """Refuse an available tier carrying a reason, or an unavailable one carrying none."""
        if self.available and self.reason is not None:
            message = f"tier {self.tier_name} is available but carries reason {self.reason}"
            raise ValidationError(message, details={"field": "reason", "tier": self.tier_name})
        if not self.available and self.reason is None:
            message = (
                f"tier {self.tier_name} is unavailable with no reason. Every refusal names its "
                "cause (spec §13)."
            )
            raise ValidationError(message, details={"field": "reason", "tier": self.tier_name})


@dataclass(frozen=True, slots=True)
class TierSnapshot:
    """The tier definitions one trajectory ran under, content-addressed.

    A trajectory's explanation must stay readable after the configuration changes, so it records
    the definitions rather than a reference to them. The identity is a hash of the content, which
    makes the store naturally deduplicating: a deployment whose configuration is stable writes one
    row and every trajectory points at it, and the day an operator edits a ceiling a second row
    appears with no migration and no coordination.

    ``approval_policy_version`` is the *other* half of pinning a decision and lives on the
    trajectory, not here: this snapshot fixes what the tiers were, and that version fixes what the
    approval rules were.

    Attributes:
        tiers: Every configured tier, ordered by name so the content address is stable.
        default_tier: ``policy.default_tier`` — where an unplanned or bypass turn starts.
        escalation_order: ``policy.escalation_order``. Explicit, never a silent climb.

    Raises:
        ValidationError: If two tiers share a name, the default tier is not among them, or the
            escalation order names a tier that is not configured.
    """

    tiers: tuple[Tier, ...]
    default_tier: str
    escalation_order: tuple[str, ...]

    def __post_init__(self) -> None:
        """Refuse duplicate, unordered or dangling tier references."""
        names = [tier.name for tier in self.tiers]
        if len(names) != len(set(names)):
            message = "a tier snapshot cannot hold two tiers with the same name"
            raise ValidationError(message, details={"field": "tiers"})
        if list(names) != sorted(names):
            message = (
                "tier snapshot tiers must be ordered by name; the content address depends on it"
            )
            raise ValidationError(message, details={"field": "tiers"})
        known = set(names)
        if self.default_tier not in known:
            message = f"default tier {self.default_tier!r} is not configured"
            raise TierNotConfiguredError(
                message, details={"field": "default_tier", "tier": self.default_tier}
            )
        for name in self.escalation_order:
            if name not in known:
                message = f"escalation order names unconfigured tier {name!r}"
                raise TierNotConfiguredError(
                    message, details={"field": "escalation_order", "tier": name}
                )

    @property
    def by_name(self) -> Mapping[str, Tier]:
        """The tiers keyed by name, for the lookups approval and minting perform."""
        return {tier.name: tier for tier in self.tiers}

    @property
    def snapshot_id(self) -> str:
        """The content address: ``sha256:`` followed by the digest of the canonical form.

        Two identical configurations produce one identity, so this is safe to use as a primary
        key and safe to write on every trajectory.
        """
        return "sha256:" + sha256_of(self.as_canonical())

    def as_canonical(self) -> dict[str, Any]:
        """Return the canonical mapping the content address is computed over."""
        return {
            "tiers": [tier.as_canonical() for tier in self.tiers],
            "default_tier": self.default_tier,
            "escalation_order": list(self.escalation_order),
        }

    def require(self, name: str) -> Tier:
        """Return the named tier.

        Args:
            name: The tier name, as a plan step or an intent declared it.

        Returns:
            The tier definition this trajectory ran under.

        Raises:
            TierNotConfiguredError: If the snapshot does not define it. A plan naming a tier the
                snapshot never had is a plan validated against a different configuration.
        """
        tier = self.by_name.get(name)
        if tier is None:
            message = f"tier {name!r} is not configured in this trajectory's tier snapshot"
            raise TierNotConfiguredError(message, details={"tier": name})
        return tier


@dataclass(frozen=True, slots=True)
class TierPolicy:
    """The tier configuration a trajectory is governed by, plus the environment's limits.

    Split from :class:`TierSnapshot` because the snapshot is *recorded* while availability is a
    fact about right now: LC-E1 landing makes every remote tier available without changing a
    single stored snapshot.

    Attributes:
        snapshot: The tier definitions.
        loadcoach_has_remote_provider: Whether LoadCoach has a remote provider registered. False
            until LC-E1 (lifecycle §3, roadmap §5).
    """

    snapshot: TierSnapshot
    loadcoach_has_remote_provider: bool = False

    @property
    def default_tier(self) -> Tier:
        """The tier an unplanned or bypass turn starts on (``policy.default_tier``)."""
        return self.snapshot.require(self.snapshot.default_tier)

    def availability(self, name: str) -> TierAvailability:
        """Report whether the named tier can serve, and why not when it cannot.

        Pure: a tier's availability is a function of configuration and of what LoadCoach has
        registered, and needs no call to LoadCoach to determine.

        Args:
            name: The tier name.

        Returns:
            An available verdict for every local tier, and for a remote tier once LoadCoach has a
            remote provider. Otherwise unavailable with
            ``loadcoach_has_no_remote_provider`` — lifecycle §3's single reason.

        Raises:
            TierNotConfiguredError: If the snapshot does not define the tier.
        """
        tier = self.snapshot.require(name)
        if tier.is_remote and not self.loadcoach_has_remote_provider:
            return TierAvailability(
                tier_name=name,
                available=False,
                reason=TierUnavailableReason.LOADCOACH_HAS_NO_REMOTE_PROVIDER,
            )
        return TierAvailability(tier_name=name, available=True)

    def admitting_tiers(self, classification: DataClassification) -> tuple[Tier, ...]:
        """Return the tiers that may serve that classification, in ``escalation_order``.

        Escalation is explicit and ordered: a step that fails on one tier does not silently climb
        to a more capable one, and the order here is the configured order, never a ranking this
        module invents (lifecycle §3).

        Args:
            classification: What the work is classified as.

        Returns:
            The admitting tiers, in ``escalation_order``. Tiers outside that order are omitted:
            a tier an operator did not put in the escalation order is one they did not want
            escalated into.
        """
        return tuple(
            tier
            for tier in (self.snapshot.require(name) for name in self.snapshot.escalation_order)
            if tier.admits(classification)
        )

    def next_escalation(self, current: str, classification: DataClassification) -> Tier | None:
        """Return the next tier after ``current`` in the escalation order that admits the work.

        Args:
            current: The tier that could not serve.
            classification: What the work is classified as.

        Returns:
            The next admitting tier, or ``None`` when the order is exhausted. ``None`` is the
            honest answer that halts; there is no wrap-around and no fallback to "the biggest one".

        Raises:
            TierNotConfiguredError: If ``current`` is not configured.
        """
        self.snapshot.require(current)
        order = list(self.snapshot.escalation_order)
        if current not in order:
            return None
        for name in order[order.index(current) + 1 :]:
            tier = self.snapshot.require(name)
            if tier.admits(classification):
                return tier
        return None


def most_permissive(tiers: Iterable[Tier], *, classification: DataClassification) -> Tier:
    """Return the tier in the set that permits the most, for gate evaluation at minting.

    ADR-0056 rule 4: approval gates are evaluated against the most permissive tier an intent
    permits, not against its first choice, "so a pre-approved fallback cannot smuggle egress past
    a hybrid gate". This function is that rule, and the ordering it uses is stated rather than
    implied, because "most permissive" is ambiguous until someone writes it down:

    1. **Egress first.** A remote tier outranks every local tier. A local tier sends nothing off
       the machine at all, so no ceiling it carries can make it more permissive than one that does.
    2. **Then how much may leave.** Among remote tiers, the one whose
       :meth:`Tier.egress_classification` for this work is higher outranks the other. A tier
       ceilinged at ``internal`` lets more leave than one ceilinged at ``public``.
    3. **Then declaration order.** Ties go to the tier that appeared first in the intent's set, so
       the result is deterministic and a golden stays a golden.

    Args:
        tiers: The tiers the intent permits — the approved tier followed by its fallbacks, in
            order.
        classification: The work's classification, which bounds what could actually leave.

    Returns:
        The most permissive tier by the ordering above.

    Raises:
        ValidationError: If the set is empty. An intent permitting no tier could never execute,
            and returning some default here would invent an approval nobody gave.
    """
    ordered: Sequence[Tier] = tuple(tiers)
    if not ordered:
        message = "cannot evaluate gates against an empty tier set"
        raise ValidationError(message, details={"field": "tiers"})

    def key(indexed: tuple[int, Tier]) -> tuple[int, int, int]:
        index, tier = indexed
        leaving = tier.egress_classification(classification)
        return (tier.egress_class.rank, -1 if leaving is None else leaving.rank, -index)

    return max(enumerate(ordered), key=key)[1]
