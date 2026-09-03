"""Tests for promptcadence.domain.tiers: admission, escalation, availability and the snapshot."""

from __future__ import annotations

import dataclasses

import pytest
from baseaicore import DataClassification, ValidationError

from promptcadence.domain.errors import TierNotConfiguredError
from promptcadence.domain.tiers import (
    LOCAL_TIER_CEILING,
    EgressClass,
    Tier,
    TierAvailability,
    TierPolicy,
    TierSnapshot,
    TierUnavailableReason,
    most_permissive,
)

_LEVELS = tuple(DataClassification)


def _tier(name: str, **kwargs: object) -> Tier:
    """A tier with sensible defaults, so a test names only what it is about."""
    defaults: dict[str, object] = {
        "task_profile": "tools.agent",
        "egress_class": EgressClass.LOCAL,
        "max_data_classification": None,
        "context_budget_tokens": 8_192,
    }
    return Tier(name=name, **{**defaults, **kwargs})  # type: ignore[arg-type]  # defaults are typed above


@pytest.mark.parametrize("classification", _LEVELS)
def test_a_local_tier_admits_every_classification(classification: DataClassification) -> None:
    """Nothing leaves the machine, so the implicit ceiling is the top of the ordering."""
    tier = _tier("local_fast")
    assert tier.admits(classification)
    assert tier.effective_max_classification is LOCAL_TIER_CEILING


@pytest.mark.parametrize(
    ("ceiling", "classification", "admitted"),
    [
        (ceiling, classification, classification <= ceiling)
        for ceiling in _LEVELS
        for classification in _LEVELS
    ],
)
def test_remote_admission_is_the_ordering_and_nothing_else(
    ceiling: DataClassification, classification: DataClassification, admitted: bool
) -> None:
    """The full 3x3 matrix: admission is ``classification <= ceiling`` over ADR-0046's ordering."""
    tier = _tier(
        "remote",
        egress_class=EgressClass.REMOTE,
        max_data_classification=ceiling,
        pricing_source="p.json",
    )
    assert tier.admits(classification) is admitted


def test_a_remote_tier_without_a_ceiling_is_refused_rather_than_assumed_public() -> None:
    """ADR-0046 rule 3: absence is not a level; it is a reason to assume the worst."""
    with pytest.raises(ValidationError, match="max_data_classification"):
        _tier("remote", egress_class=EgressClass.REMOTE, max_data_classification=None)


def test_a_tier_refuses_an_empty_name_or_task_profile_or_a_zero_context_budget() -> None:
    with pytest.raises(ValidationError, match="named"):
        _tier("  ")
    with pytest.raises(ValidationError, match="task profile"):
        _tier("t", task_profile="")
    with pytest.raises(ValidationError, match="context_budget_tokens"):
        _tier("t", context_budget_tokens=0)


def test_egress_classification_is_none_locally_and_bounded_by_both_ceilings() -> None:
    assert _tier("local").egress_classification(DataClassification.CONFIDENTIAL) is None
    remote = _tier(
        "remote",
        egress_class=EgressClass.REMOTE,
        max_data_classification=DataClassification.INTERNAL,
        pricing_source="p.json",
    )
    assert remote.egress_classification(DataClassification.PUBLIC) is DataClassification.PUBLIC
    assert remote.egress_classification(DataClassification.INTERNAL) is DataClassification.INTERNAL


def test_egress_class_orders_by_rank_and_refuses_a_foreign_comparison() -> None:
    assert EgressClass.LOCAL < EgressClass.REMOTE
    assert EgressClass.REMOTE >= EgressClass.LOCAL
    assert not EgressClass.REMOTE <= EgressClass.LOCAL
    assert not EgressClass.LOCAL > EgressClass.REMOTE
    with pytest.raises(TypeError):
        _ = EgressClass.LOCAL < "remote"


def test_a_snapshot_is_content_addressed_and_identical_configurations_share_one_id(
    tier_snapshot: TierSnapshot,
) -> None:
    """Deduplication for free, and the reason a trajectory can carry the id on every row."""
    twin = dataclasses.replace(tier_snapshot)
    assert twin.snapshot_id == tier_snapshot.snapshot_id
    assert tier_snapshot.snapshot_id.startswith("sha256:")


def test_editing_one_ceiling_produces_a_different_snapshot_id(tier_snapshot: TierSnapshot) -> None:
    """The explanation must stay readable after configuration changes; a new id is how."""
    edited = dataclasses.replace(
        tier_snapshot,
        tiers=tuple(
            dataclasses.replace(tier, max_data_classification=DataClassification.INTERNAL)
            if tier.name == "remote_frontier"
            else tier
            for tier in tier_snapshot.tiers
        ),
    )
    assert edited.snapshot_id != tier_snapshot.snapshot_id


def test_a_snapshot_refuses_unordered_duplicate_or_dangling_tiers(
    tier_snapshot: TierSnapshot,
) -> None:
    with pytest.raises(ValidationError, match="ordered by name"):
        dataclasses.replace(tier_snapshot, tiers=tuple(reversed(tier_snapshot.tiers)))
    with pytest.raises(ValidationError, match="same name"):
        TierSnapshot(tiers=(_tier("a"), _tier("a")), default_tier="a", escalation_order=("a",))
    with pytest.raises(TierNotConfiguredError, match="default tier"):
        dataclasses.replace(tier_snapshot, default_tier="nowhere")
    with pytest.raises(TierNotConfiguredError, match="escalation order"):
        dataclasses.replace(tier_snapshot, escalation_order=("local_fast", "nowhere"))


def test_a_snapshot_refuses_a_tier_it_does_not_define(tier_snapshot: TierSnapshot) -> None:
    with pytest.raises(TierNotConfiguredError, match="not configured in this trajectory"):
        tier_snapshot.require("gpt_9")


def test_remote_tiers_are_unavailable_until_loadcoach_registers_a_remote_provider(
    local_only_policy: TierPolicy, tier_policy: TierPolicy
) -> None:
    """Lifecycle §3: pure, and it needs no call to LoadCoach to determine."""
    unavailable = local_only_policy.availability("remote_cheap")
    assert unavailable.available is False
    assert unavailable.reason is TierUnavailableReason.LOADCOACH_HAS_NO_REMOTE_PROVIDER
    assert local_only_policy.availability("local_fast").available is True
    assert tier_policy.availability("remote_cheap").available is True


def test_an_availability_verdict_cannot_be_unavailable_without_a_reason() -> None:
    with pytest.raises(ValidationError, match="no reason"):
        TierAvailability(tier_name="t", available=False)
    with pytest.raises(ValidationError, match="carries reason"):
        TierAvailability(
            tier_name="t",
            available=True,
            reason=TierUnavailableReason.LOADCOACH_HAS_NO_REMOTE_PROVIDER,
        )


def test_admitting_tiers_follow_the_configured_escalation_order(tier_policy: TierPolicy) -> None:
    """Explicit, never a ranking this module invents (lifecycle §3)."""
    assert [tier.name for tier in tier_policy.admitting_tiers(DataClassification.PUBLIC)] == [
        "local_fast",
        "local_large",
        "remote_cheap",
        "remote_frontier",
    ]
    assert [tier.name for tier in tier_policy.admitting_tiers(DataClassification.INTERNAL)] == [
        "local_fast",
        "local_large",
        "remote_cheap",
    ]
    assert [tier.name for tier in tier_policy.admitting_tiers(DataClassification.CONFIDENTIAL)] == [
        "local_fast",
        "local_large",
    ]


def test_escalation_never_wraps_and_stops_at_the_end(tier_policy: TierPolicy) -> None:
    """``None`` is the honest answer that halts; there is no fallback to "the biggest one"."""
    following = tier_policy.next_escalation("local_fast", DataClassification.CONFIDENTIAL)
    assert following is not None
    assert following.name == "local_large"
    assert tier_policy.next_escalation("local_large", DataClassification.CONFIDENTIAL) is None
    assert tier_policy.next_escalation("remote_frontier", DataClassification.PUBLIC) is None


def test_the_default_tier_resolves_to_a_definition(tier_policy: TierPolicy) -> None:
    assert tier_policy.default_tier.name == "local_fast"


def test_most_permissive_prefers_a_remote_fallback_over_a_local_primary(
    tier_snapshot: TierSnapshot,
) -> None:
    """ADR-0056 rule 4's whole point: a pre-approved fallback cannot smuggle egress past a gate."""
    local = tier_snapshot.require("local_fast")
    remote = tier_snapshot.require("remote_cheap")
    chosen = most_permissive((local, remote), classification=DataClassification.INTERNAL)
    assert chosen.name == "remote_cheap"


def test_most_permissive_prefers_the_higher_egress_ceiling_among_remote_tiers(
    tier_snapshot: TierSnapshot,
) -> None:
    cheap = tier_snapshot.require("remote_cheap")
    frontier = tier_snapshot.require("remote_frontier")
    chosen = most_permissive((frontier, cheap), classification=DataClassification.INTERNAL)
    assert chosen.name == "remote_cheap"


def test_most_permissive_breaks_ties_by_declaration_order(tier_snapshot: TierSnapshot) -> None:
    """Determinism: a golden over an intent's gate must not depend on dict or set ordering."""
    fast = tier_snapshot.require("local_fast")
    large = tier_snapshot.require("local_large")
    assert most_permissive((fast, large), classification=DataClassification.PUBLIC).name == (
        "local_fast"
    )
    assert most_permissive((large, fast), classification=DataClassification.PUBLIC).name == (
        "local_large"
    )


def test_most_permissive_refuses_an_empty_set() -> None:
    """An intent permitting no tier could never execute; a default here would invent an approval."""
    with pytest.raises(ValidationError, match="empty tier set"):
        most_permissive((), classification=DataClassification.PUBLIC)


def test_a_tier_outside_the_escalation_order_has_no_next(tier_snapshot: TierSnapshot) -> None:
    """A tier an operator left out of the order is one they did not want escalated into."""
    narrowed = TierPolicy(
        snapshot=dataclasses.replace(tier_snapshot, escalation_order=("local_fast",)),
        loadcoach_has_remote_provider=True,
    )
    assert narrowed.next_escalation("local_large", DataClassification.PUBLIC) is None
