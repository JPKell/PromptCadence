"""E4: the shipped tier defaults, against LoadCoach's committed task profiles.

``loadcoach_task_profiles.toml`` is a byte copy of LoadCoach
``src/loadcoach/config/task_profiles.toml`` (LoadCoach ``5c5aa1f``, the commit that added the five
harness profiles), recorded with its digest below — the idiom I10 uses for the OpenAPI snapshot,
for the same reason: a vendored copy nobody pins is a copy that has already drifted.

**Why this file exists at all.** A PromptCadence tier is configuration over exactly one LoadCoach
task profile (ADR-0047 §1), and PromptCadence performs no routing maths — so a tier naming a
profile LoadCoach does not ship is a trajectory that fails at the first turn, on an operator's
machine, at run time. Before E4 that was the actual state: the shipped defaults named
``tools.agent.local_fast`` and LoadCoach shipped one ``tools.agent``, and D2's live journey could
only run with ``PROMPTCADENCE_TIERS__*`` overrides. The profiles now ship in LoadCoach and this
file makes the pairing a **CI** failure rather than a discovery at ``promptcadence tiers check``.

**What it can and cannot pin.** It pins the pairing: each documented tier's profile exists, and the
three values that are the same fact written in two repositories — the minimum served context and
the tier's context budget, the profile's ``allow_remote_providers`` and the tier's ``remote``. It
cannot pin that the *running* LoadCoach ships this file; that is what ``doctor`` and
``promptcadence tiers check`` are for, against the LoadCoach actually configured.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

import pytest

from promptcadence.config import load_settings

pytestmark = pytest.mark.contract

SNAPSHOT = Path(__file__).resolve().parent / "loadcoach_task_profiles.toml"
SNAPSHOT_SHA256 = "33c3dff72f861b7481a52c9e735fccf7116bd85dec58e0273204cb325581d7a5"
SNAPSHOT_SOURCE = "LoadCoach src/loadcoach/config/task_profiles.toml at 5c5aa1f"


def shipped_profiles() -> dict[str, dict[str, Any]]:
    """Return the vendored profiles, keyed by profile id.

    Also the fake LoadCoach's source of profile shapes, so a test's registry holds the profile
    LoadCoach actually ships rather than one hand-written to match.
    """
    document = tomllib.loads(SNAPSHOT.read_text(encoding="utf-8"))
    profiles: dict[str, dict[str, Any]] = document["task_profiles"]
    return profiles


# Spec §12's four documented tiers, with the two values each one asserts about its profile.
# Written here rather than read from configuration on purpose: only the two local tiers are
# *active* shipped defaults (the remote pair is documented and commented out, because a remote tier
# with no pricing source is refused at startup — `config._default_tiers`), and the remote profiles
# still have to be pinned or they would drift unwatched until LC-E1 turns them on.
_DOCUMENTED_TIERS: dict[str, tuple[str, int, bool]] = {
    "local_fast": ("tools.agent.local_fast", 16_384, False),
    "local_large": ("tools.agent.local_large", 32_768, False),
    "remote_cheap": ("tools.agent.remote_cheap", 128_000, True),
    "remote_frontier": ("tools.agent.remote_frontier", 200_000, True),
}


def test_the_vendored_snapshot_is_the_one_recorded() -> None:
    """A drifted copy is a pairing checked against nothing; the digest names which copy this is."""
    digest = hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest()
    assert digest == SNAPSHOT_SHA256, (
        f"snapshot changed; re-copy LoadCoach's task_profiles.toml, then update SNAPSHOT_SHA256 "
        f"and SNAPSHOT_SOURCE (currently {SNAPSHOT_SOURCE})"
    )


@pytest.mark.parametrize("tier_name", sorted(_DOCUMENTED_TIERS))
def test_every_documented_tiers_task_profile_is_one_loadcoach_ships(tier_name: str) -> None:
    profile_id, _, _ = _DOCUMENTED_TIERS[tier_name]
    assert profile_id in shipped_profiles(), (
        f"tier {tier_name!r} names task profile {profile_id!r}, which LoadCoach does not ship"
    )


def test_the_active_shipped_defaults_are_documented_tiers_with_the_same_values() -> None:
    """The two tiers a zero-configuration install actually gets must match the table above."""
    tiers = load_settings().settings.tiers
    assert set(tiers) <= set(_DOCUMENTED_TIERS)
    for name, tier in tiers.items():
        profile_id, budget, remote = _DOCUMENTED_TIERS[name]
        assert tier.task_profile == profile_id
        assert tier.context_budget_tokens == budget
        assert tier.remote is remote


@pytest.mark.parametrize("profile_id", ["tools.agent.local_fast", "tools.agent.local_large"])
def test_both_agent_profiles_require_tool_use(profile_id: str) -> None:
    """A tier that routes agent turns to a model without tool support is a tier that cannot work."""
    constraints = shipped_profiles()[profile_id]["constraints"]
    assert "tool_use" in constraints["requires_capabilities"]


@pytest.mark.parametrize("tier_name", sorted(_DOCUMENTED_TIERS))
def test_each_profiles_minimum_context_equals_its_tiers_budget(tier_name: str) -> None:
    """The same fact in two repositories: what a tier will compact to, and what it will admit.

    ``context_budget_tokens`` is the compaction trigger's input here; ``min_context_tokens`` is a
    hard constraint there. A model served less than the tier's budget would be compacted *to* a
    size it cannot hold — so LoadCoach must reject it with ``context_too_small`` rather than admit
    it and truncate, and it can only do that if the two numbers are equal.
    """
    profile_id, budget, _ = _DOCUMENTED_TIERS[tier_name]
    assert shipped_profiles()[profile_id]["constraints"]["min_context_tokens"] == budget


@pytest.mark.parametrize("tier_name", sorted(_DOCUMENTED_TIERS))
def test_each_profiles_remote_permission_equals_its_tiers_egress_class(tier_name: str) -> None:
    """A local tier whose profile permitted a remote provider would be egress nobody approved."""
    profile_id, _, remote = _DOCUMENTED_TIERS[tier_name]
    constraints = shipped_profiles()[profile_id]["constraints"]
    assert constraints.get("allow_remote_providers", False) is remote


def test_tools_plan_asks_for_json_and_declares_no_schema() -> None:
    """D-7: the plan document's shape stays PromptCadence-internal.

    LoadCoach validates that the answer *is* JSON; what shape of JSON is a plan is validated here,
    against this application's own model. A schema in LoadCoach would put the plan's contract in
    two repositories and hand LoadCoach's corrective retry a document it does not own.
    """
    profile = shipped_profiles()["tools.plan"]
    assert profile["execution"]["response_format"] == "json"
    assert profile["validation"]["require_valid_json"] is True
    assert not profile["validation"].get("require_schema", False)
    assert "json_schema_ref" not in profile["execution"]
