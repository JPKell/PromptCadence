"""Egress governance end to end: the decision, the refusal, and the violation (dev plan Phase 6).

Integration by nature — a migrated SQLite database and the in-process fake — and in the default
suite (spec §18): no LoadCoach, no GPU, no network. The fetch tests reach ``http_fetch`` through an
injected httpx transport, so registering a network tool did not cost the suite that property
(spec §20 #10).

Two things here are asserted the hard way on purpose. **"Refused before any request leaves" is
checked against the client, not inferred from the outcome** — a refusal that happened after the
call would produce exactly the same halted trajectory, and only the request count tells the two
apart. And **absence is asserted as a violation**, not as an error, because a fail-closed contract
whose failure mode is "read absence as success" is one no happy-path test can reach.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from baseaicore import DataClassification
from commissioner import Verdict
from fastapi.testclient import TestClient
from tests.conftest import budget_and_estimator, egress_for
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    FakeModel,
    ScriptedGeneration,
    build_fake_app,
    shipped_profiles,
)
from toolyard import TieredSandbox
from weightsdb import MigrationRunner
from weightsdb.testing import temporary_sqlite

from promptcadence.config import Settings, load_settings
from promptcadence.domain.errors import ErrorCode
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.loadcoach import LoadCoachClient
from promptcadence.services.database import MIGRATIONS_LOCATION, Database
from promptcadence.services.events import TrajectoryEventSink
from promptcadence.services.loop import LoopController
from promptcadence.services.pricing import PricingCatalog
from promptcadence.services.tools import ToolPlant
from promptcadence.services.trajectories import TrajectoryService, TrajectorySubmission

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class CountingClient(TestClient):
    """A ``TestClient`` that counts the requests it was actually asked to make.

    The whole point of spec §20 #4 is *when* a refusal happens, and a refusal after the call looks
    identical to a refusal before it from the trajectory's row alone. This counter is the only
    witness that separates them, so it counts at the transport boundary — every request the client
    sends, whatever the path above it believed it was doing.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sent: list[str] = []

    def request(self, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        """Record the request, then make it."""
        self.sent.append(f"{method} {url}")
        response: httpx.Response = super().request(method, url, **kwargs)
        return response

    @property
    def generate_calls(self) -> list[str]:
        """Only the generation requests — the ones that would have sent data to a model."""
        return [sent for sent in self.sent if "/generate" in sent]


class Harness:
    """One database, one fake, one counting client."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        fake: FakeLoadCoach,
        *,
        pricing: PricingCatalog | None = None,
        fetch_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.fake = fake
        ticks = iter(range(100_000))
        self.clock = lambda: _NOW + timedelta(milliseconds=next(ticks))
        self.sink = TrajectoryEventSink(database, clock=self.clock)
        self.budget, self.estimator = budget_and_estimator(
            database, settings, clock=self.clock, pricing=pricing
        )
        self.egress = egress_for(database, clock=self.clock)
        self.service = TrajectoryService(
            database, self.sink, settings, budget=self.budget, clock=self.clock
        )
        self.client = CountingClient(build_fake_app(fake), base_url="http://loadcoach.test")
        self.loadcoach = LoadCoachClient(self.client)
        self.tools = ToolPlant(
            settings,
            sandbox=TieredSandbox(which=lambda _name: None),
            resolver=lambda host: ["203.0.113.7"],
            fetch_transport=fetch_transport,
        )

    def controller(self, owner: str = "host:1/0") -> LoopController:
        return LoopController(
            budget=self.budget,
            estimator=self.estimator,
            egress=self.egress,
            database=self.database,
            sink=self.sink,
            loadcoach=self.loadcoach,
            settings=self.settings,
            owner=owner,
            clock=self.clock,
            tools=self.tools,
        )

    def submit(self, **overrides: object) -> str:
        fields: dict[str, object] = {"task": "summarize ./notes", "bypass_planning": True}
        fields.update(overrides)
        submission = TrajectorySubmission(**fields)  # type: ignore[arg-type]
        return self.service.submit(submission).trajectory_id

    def run(self, **overrides: object) -> tuple[str, TrajectoryState | None]:
        """Submit, claim and run one trajectory to whatever state it reaches."""
        trajectory_id = self.submit(**overrides)
        controller = self.controller()
        assert controller.claim(trajectory_id) is TrajectoryState.EXECUTING
        state = controller.run(trajectory_id)
        return trajectory_id, state

    def decisions(self, trajectory_id: str) -> list[Any]:
        return list(self.egress.decisions(run_id=trajectory_id))


def _fake(model: FakeModel | None = None) -> FakeLoadCoach:
    fake = FakeLoadCoach(model=model) if model is not None else FakeLoadCoach()
    fake.register_profile(
        *shipped_profiles(
            "tools.agent.local_fast", "tools.agent.local_large", "tools.agent.remote_cheap"
        )
    )
    return fake


def _harness(settings: Settings, fake: FakeLoadCoach, **kwargs: Any) -> Iterator[Harness]:
    with temporary_sqlite() as engine:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        yield Harness(settings, Database(engine), fake, **kwargs)


@pytest.fixture
def harness() -> Iterator[Harness]:
    yield from _harness(load_settings().settings, _fake())


@pytest.fixture
def remote_harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    """A configured, priced remote tier — the thing Phase 6 makes configurable."""
    monkeypatch.setenv(
        "PROMPTCADENCE_TIERS__REMOTE_CHEAP__TASK_PROFILE", "tools.agent.remote_cheap"
    )
    monkeypatch.setenv("PROMPTCADENCE_TIERS__REMOTE_CHEAP__REMOTE", "true")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__REMOTE_CHEAP__MAX_DATA_CLASSIFICATION", "internal")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__REMOTE_CHEAP__PRICING_FILE", "pricing.json")
    monkeypatch.setenv("PROMPTCADENCE_POLICY__DEFAULT_TIER", "remote_cheap")
    # Setting any `TIERS__<name>__*` replaces the shipped default map rather than adding to it,
    # so the escalation order has to name what is actually configured here.
    monkeypatch.setenv("PROMPTCADENCE_POLICY__ESCALATION_ORDER", "remote_cheap")
    yield from _harness(load_settings().settings, _fake())


@pytest.fixture
def unpriced_remote_harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    """The same remote tier with no pricing source — spec §20 #5's subject."""
    monkeypatch.setenv(
        "PROMPTCADENCE_TIERS__REMOTE_CHEAP__TASK_PROFILE", "tools.agent.remote_cheap"
    )
    monkeypatch.setenv("PROMPTCADENCE_TIERS__REMOTE_CHEAP__REMOTE", "true")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__REMOTE_CHEAP__MAX_DATA_CLASSIFICATION", "public")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__REMOTE_CHEAP__PRICING_FILE", "prices.json")
    monkeypatch.setenv("PROMPTCADENCE_POLICY__DEFAULT_TIER", "remote_cheap")
    # Setting any `TIERS__<name>__*` replaces the shipped default map rather than adding to it,
    # so the escalation order has to name what is actually configured here.
    monkeypatch.setenv("PROMPTCADENCE_POLICY__ESCALATION_ORDER", "remote_cheap")
    # A tier that names a price list holding no record claiming now. Startup already refuses a
    # remote tier that names no file at all (B4's decision), so this - an expired or empty list -
    # is the only shape spec §20 #5's refusal can actually reach at runtime.
    yield from _harness(
        load_settings().settings, _fake(), pricing=PricingCatalog(by_tier={"remote_cheap": ()})
    )


# --------------------------------------------------------------------------------------------
# Spec §20 #4 — a confidential trajectory can never reach a remote tier
# --------------------------------------------------------------------------------------------


def test_a_confidential_trajectory_never_reaches_a_remote_tier(remote_harness: Harness) -> None:
    """Spec §20 #4, verbatim, including the half that is about *when*.

    The tier's ceiling is ``internal`` and the trajectory declares ``confidential``, so the
    classification exceeds it. The assertion that matters most is the request count: a refusal
    that arrived after the call would leave an identical trajectory row.
    """
    trajectory_id, state = remote_harness.run(classification=DataClassification.CONFIDENTIAL)

    assert state is TrajectoryState.HALTED
    assert remote_harness.client.generate_calls == [], "a request left before the refusal"

    view = remote_harness.service.get(trajectory_id)
    assert view.error_code == ErrorCode.EGRESS_DENIED.value
    assert "classification_exceeds_ceiling" in (view.halted_reason or "")


def test_the_refusal_is_a_queryable_egress_decision(remote_harness: Harness) -> None:
    """ "The refusal is a queryable EgressDecision" — the audit half of §20 #4.

    A refusal nobody can query afterwards is a refusal that only existed in a log line, so this
    reads it back through the ledger the way ``GET /egress-decisions`` will.
    """
    trajectory_id, _ = remote_harness.run(classification=DataClassification.CONFIDENTIAL)

    (decision,) = remote_harness.decisions(trajectory_id)
    assert decision.verdict is Verdict.DENIED
    assert decision.reason == "classification_exceeds_ceiling"
    assert decision.request.data_classification is DataClassification.CONFIDENTIAL
    assert decision.request.target.name == "remote_cheap"
    assert decision.request.target.remote is True
    assert decision.policy_name == "OrderedClassificationPolicy"

    denied = remote_harness.egress.decisions(run_id=trajectory_id, verdict=Verdict.DENIED)
    assert [d.decision_id for d in denied] == [decision.decision_id]


def test_an_internal_trajectory_within_the_ceiling_is_approved_and_recorded(
    remote_harness: Harness,
) -> None:
    """The same tier, a classification it admits: approved, and recorded exactly as fully.

    Contract 3's "a declined call is as auditable as an approved one" is only a real property if
    the approval carries the same fields, so this asserts them rather than asserting a verdict.
    """
    trajectory_id, _ = remote_harness.run(classification=DataClassification.INTERNAL)

    approvals = [
        decision
        for decision in remote_harness.decisions(trajectory_id)
        if decision.verdict is Verdict.APPROVED
    ]
    assert approvals, "an approved egress must be recorded, not merely allowed"
    first = approvals[0]
    assert first.reason == "within_ceiling"
    assert first.request.target.name == "remote_cheap"
    assert first.request.data_classification is DataClassification.INTERNAL
    assert first.policy_name == "OrderedClassificationPolicy"
    assert first.decided_at.tzinfo is not None


# --------------------------------------------------------------------------------------------
# Spec §20 #5 — an unpriced remote tier
# --------------------------------------------------------------------------------------------


def test_an_unpriced_remote_tier_refuses_before_any_call(
    unpriced_remote_harness: Harness,
) -> None:
    """Spec §20 #5. Unpriced egress is refused, not free (ADR-0016/ADR-0030).

    The classification here is *within* the tier's ceiling, so the egress decision approves and
    the refusal is unambiguously the pricing one — a test whose trajectory failed the
    classification check first would prove nothing about pricing.
    """
    trajectory_id, state = unpriced_remote_harness.run(classification=DataClassification.PUBLIC)

    assert state is TrajectoryState.HALTED
    assert unpriced_remote_harness.client.generate_calls == []

    view = unpriced_remote_harness.service.get(trajectory_id)
    assert view.error_code == ErrorCode.UNPRICED_EGRESS_REFUSED.value
    assert "no ModelPricing record" in (view.halted_reason or "")

    (decision,) = unpriced_remote_harness.decisions(trajectory_id)
    assert decision.verdict is Verdict.APPROVED, "the egress question was answered before pricing"


# --------------------------------------------------------------------------------------------
# Contract 4 — verification, and the two ways it fails closed
# --------------------------------------------------------------------------------------------


def test_a_remote_provider_answering_a_local_tier_is_a_violation_and_halts() -> None:
    """The plan's named test. The registry says ``ollama``; the answer says otherwise.

    This is the shape contract 4 exists for: the tier promised local, something else answered, and
    the only way to notice is to check the response's subject against the configured provider
    rather than to trust the tier.
    """
    remote_answer = FakeModel(
        canonical_id="openai_compatible/gpt-4o@sha256:" + "b" * 64, provider_kind="ollama"
    )
    for harness in _harness(load_settings().settings, _fake(remote_answer)):
        trajectory_id, state = harness.run()

        assert state is TrajectoryState.HALTED
        view = harness.service.get(trajectory_id)
        assert view.error_code == ErrorCode.DEVIATION_HALTED.value

        violations = harness.egress.decisions(run_id=trajectory_id, verdict=Verdict.VIOLATION)
        assert len(violations) == 1
        assert violations[0].reason.startswith("tier_violation:served_remote")
        assert violations[0].policy_name == "promptcadence.verification"


def test_a_response_with_no_execution_subject_is_a_violation_not_a_pass() -> None:
    """The known risk the plan names, and the one a happy-path suite never reaches.

    A response that claims work was done and declines to say by what is out of LoadCoach's own
    contract. Reading that as "probably the tier we asked for" would turn a verified constraint
    back into an assumed one, so it is a violation — recorded, and halting.
    """
    for harness in _harness(load_settings().settings, _fake()):
        harness.fake.script(ScriptedGeneration(omit_subject=True))
        trajectory_id, state = harness.run()

        assert state is TrajectoryState.HALTED
        view = harness.service.get(trajectory_id)
        assert view.error_code == ErrorCode.DEVIATION_HALTED.value
        assert "could not be verified" in (view.halted_reason or "")

        violations = harness.egress.decisions(run_id=trajectory_id, verdict=Verdict.VIOLATION)
        assert len(violations) == 1
        assert violations[0].reason == "execution_subject_unverified"


# --------------------------------------------------------------------------------------------
# Acceptance criterion 1 — every turn carries an egress decision
# --------------------------------------------------------------------------------------------


def test_every_turn_of_an_ordinary_local_journey_carries_an_egress_decision(
    harness: Harness,
) -> None:
    """Acceptance criterion 1, over the journey Phase 4 and Phase 5 already ran.

    A local tier is approved with ``target_not_remote`` rather than skipped. Exempting local turns
    would make "every turn carries a decision" uncheckable by counting, which is the property the
    contract-1 invariance diff at Phase 7 is going to rest on.
    """
    trajectory_id, state = harness.run()
    assert state is TrajectoryState.COMPLETED

    turns = [record for record in harness.service.turns(trajectory_id)]
    assistant_turns = [record for record in turns if record.turn.role.value == "assistant"]
    assert assistant_turns

    decisions = harness.decisions(trajectory_id)
    assert len(decisions) == len(assistant_turns)
    assert {decision.verdict for decision in decisions} == {Verdict.APPROVED}
    assert {decision.reason for decision in decisions} == {"target_not_remote"}

    gated = {decision.request.source_ref for decision in decisions}
    assert gated == {record.turn.turn_id for record in assistant_turns}, (
        "each decision must name the turn it gated"
    )
