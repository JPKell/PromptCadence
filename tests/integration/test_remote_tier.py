"""Roadmap I13, the recorded-transport half (ADR-0098 rule 4).

With one local and one remote registration in LoadCoach's registry, a ``remote_cheap`` step is
served by the remote registration — the turn's egress decision approved with a remote target, its
spend priced and debited, the explanation naming the provider — and a local tier never is. The
fact that LoadCoach has a remote registration is **read from LoadCoach**, never configured.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from baseaicore import DataClassification
from commissioner import Verdict
from tests.fakes.harness import LoopHarness, open_harness, remote_tier_env
from tests.fakes.loadcoach_app import FakeModel, ScriptedGeneration, Wire

from promptcadence.config import load_settings
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.services.pricing import PricingCatalog

REMOTE = FakeModel(
    canonical_id="openai_compatible/qwen3:8b@sha256:" + "e" * 64,
    provider_kind="openai_compatible",
    provider_name="openrouter",
    is_remote=True,
)

# An ADR-0072 record for the remote registration's model, so the remote turn can be priced.
PRICING = """
{
  "records": [
    {
      "provider_kind": "openai_compatible",
      "provider_model_name": "qwen3:8b",
      "source": "provider_published",
      "observed_at": "2026-09-01T00:00:00Z",
      "price_tier": "standard",
      "rates": {
        "currency": "USD",
        "input_per_million_tokens": "2.50",
        "output_per_million_tokens": "10.00",
        "cache_write_per_million_tokens": "3.125",
        "cache_read_per_million_tokens": "0.25"
      }
    }
  ]
}
"""


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[LoopHarness]:
    pricing = tmp_path / "pricing.json"
    pricing.write_text(PRICING)
    for key, value in remote_tier_env(str(pricing)).items():
        monkeypatch.setenv(key, value)
    settings = load_settings().settings
    # ``remote_provider`` is left ``None``: the harness reads the fact from the fake's registry,
    # as the served process reads it from LoadCoach.
    # LoadCoach 1.1 speaks the post-modelrack-0.7.0 usage wire, so a priced turn totals rather
    # than floors — which is what lets ``unpriced is False`` be the assertion below.
    with open_harness(
        settings,
        pricing=PricingCatalog.from_settings(settings),
        remote_model=REMOTE,
        wire=Wire.POST_MODELRACK_070,
    ) as built:
        yield built


def test_a_remote_tier_step_is_served_by_the_remote_registration_and_recorded_as_egress(
    harness: LoopHarness,
) -> None:
    harness.script(ScriptedGeneration(text="Summarized, remotely."))
    trajectory_id = harness.submit_bypass(
        tier="remote_cheap", classification=DataClassification.INTERNAL
    )
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED

    request = harness.fake.requests[-1]["body"]
    assert request["task"] == "tools.agent.remote_cheap"
    (answer,) = [
        t for t in harness.service.turns(trajectory_id) if t.turn.role.value == "assistant"
    ]
    assert answer.turn.model_canonical_id == REMOTE.canonical_id
    assert answer.turn.provenance.tier == "remote_cheap"

    approved = [
        d
        for d in harness.egress.decisions(run_id=trajectory_id, verdict=Verdict.APPROVED)
        if d.request.target.remote
    ]
    assert len(approved) == 1 and approved[0].request.target.name == "remote_cheap"
    (entry,) = harness.budget.entry_views(trajectory_id=trajectory_id, limit=10)
    assert entry.unpriced is False and entry.pricing_hash, "remote spend is priced and debited"
    assert "tier:remote_cheap" in entry.tags

    document = harness.controller().explanations.read(trajectory_id).document
    turns = [t for th in document["threads"] for t in th["turns"] if t["role"] == "assistant"]
    assert turns[0]["model"]["provider_name"] == "openrouter"
    assert turns[0]["model"]["canonical_id"] == REMOTE.canonical_id
    assert any(d["request"]["target"]["remote"] for d in document["egress_decisions"])


def test_a_local_tier_step_is_served_locally_with_no_remote_egress(harness: LoopHarness) -> None:
    harness.script(ScriptedGeneration(text="Summarized, locally."))
    trajectory_id = harness.submit_bypass(
        tier="local_fast", classification=DataClassification.INTERNAL
    )
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.COMPLETED
    assert harness.fake.requests[-1]["body"]["task"] == "tools.agent.local_fast"
    (answer,) = [
        t for t in harness.service.turns(trajectory_id) if t.turn.role.value == "assistant"
    ]
    assert answer.turn.model_canonical_id == harness.fake.model.canonical_id
    decisions = harness.egress.decisions(run_id=trajectory_id)
    assert decisions and all(not d.request.target.remote for d in decisions)
    assert all(d.reason == "target_not_remote" for d in decisions)
    (entry,) = harness.budget.entry_views(trajectory_id=trajectory_id, limit=10)
    assert entry.unpriced is True, "local work is unpriced, never $0.00"


def test_a_confidential_trajectory_still_never_reaches_the_remote_registration(
    harness: LoopHarness,
) -> None:
    """The fact that a remote provider exists changes availability, never the egress verdict."""
    trajectory_id = harness.submit_bypass(
        tier="remote_cheap", classification=DataClassification.CONFIDENTIAL
    )
    assert harness.claim_and_run(trajectory_id) is TrajectoryState.HALTED
    assert all(r["body"]["task"] != "tools.agent.remote_cheap" for r in harness.fake.requests)
    denied = harness.egress.decisions(run_id=trajectory_id, verdict=Verdict.DENIED)
    assert denied and denied[0].reason == "classification_exceeds_ceiling"
