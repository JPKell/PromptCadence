"""A loop harness over one migrated SQLite database and one fake LoadCoach — both paths.

Phase 7's integration tests need what ``tests/integration/test_bypass_loop.py``'s ``Harness``
provides plus a planned submission, a scriptable plan document, and a way to run a trajectory to
its next rest — so this is that harness, shared. The injected clock ticks a millisecond per read;
approval-timeout tests move it by hand through :meth:`LoopHarness.advance`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from tests.conftest import budget_and_estimator, egress_for
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    ScriptedGeneration,
    build_fake_app,
    shipped_profiles,
)
from toolyard import TieredSandbox
from weightsdb import MigrationRunner
from weightsdb.testing import temporary_sqlite

from promptcadence.config import Settings
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.loadcoach import LoadCoachClient
from promptcadence.services.approvals import ApprovalService
from promptcadence.services.database import MIGRATIONS_LOCATION, Database
from promptcadence.services.events import TrajectoryEventSink
from promptcadence.services.loop import LoopController, RunSignals
from promptcadence.services.pricing import PricingCatalog
from promptcadence.services.tools import ToolPlant
from promptcadence.services.trajectories import TrajectoryService, TrajectorySubmission

__all__ = ["LoopHarness", "open_harness", "plan_document", "remote_tier_env", "step"]

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

PLANNER_PROFILES = (
    "tools.agent.local_fast",
    "tools.agent.local_large",
    "tools.agent.remote_cheap",
    "tools.plan",
)


def step(step_id: str = "s1", **overrides: Any) -> dict[str, Any]:
    """One well-formed plan step, with named overrides."""
    return {
        "step_id": step_id,
        "description": f"do {step_id}",
        "depends_on": [],
        "tools": ["read_file"],
        "tier": "local_fast",
        "data_classification": "confidential",
        "expected_turns": 1,
        **overrides,
    }


def plan_document(*steps: dict[str, Any]) -> str:
    """A plan document as the planner returns it."""
    return json.dumps({"steps": list(steps) if steps else [step()]})


class Clock:
    """A clock a test can move: a millisecond per read, plus explicit advances."""

    def __init__(self, start: datetime = _NOW) -> None:
        self.now = start
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return self.now + timedelta(milliseconds=self._tick)

    def advance(self, delta: timedelta) -> None:
        """Move the clock forward."""
        self.now = self.now + delta


class LoopHarness:
    """Everything one Phase 7 test needs, over one database and one fake."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        fake: FakeLoadCoach,
        *,
        pricing: PricingCatalog | None = None,
        remote_provider: bool = False,
    ) -> None:
        self.settings = settings
        self.database = database
        self.fake = fake
        self.remote_provider = remote_provider
        self.clock = Clock()
        self.sink = TrajectoryEventSink(database, clock=self.clock)
        self.budget, self.estimator = budget_and_estimator(
            database, settings, clock=self.clock, pricing=pricing
        )
        self.egress = egress_for(database, clock=self.clock)
        self.service = TrajectoryService(
            database, self.sink, settings, budget=self.budget, clock=self.clock
        )
        self.loadcoach = LoadCoachClient(
            TestClient(build_fake_app(fake), base_url="http://loadcoach.test")
        )
        self.tools = ToolPlant(settings, sandbox=TieredSandbox(which=lambda _name: None))
        self.approvals = ApprovalService(
            database,
            self.sink,
            settings,
            estimator=self.estimator,
            budget=self.budget,
            clock=self.clock,
            loadcoach_has_remote_provider=remote_provider,
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
            approvals=self.approvals,
            loadcoach_has_remote_provider=self.remote_provider,
        )

    def submit(self, **overrides: object) -> str:
        fields: dict[str, object] = {"task": "summarize ./notes"}
        fields.update(overrides)
        submission = TrajectorySubmission(**fields)  # type: ignore[arg-type]
        return self.service.submit(submission).trajectory_id

    def submit_bypass(self, **overrides: object) -> str:
        return self.submit(bypass_planning=True, **overrides)

    def submit_planned(self, **overrides: object) -> str:
        return self.submit(bypass_planning=None, **overrides)

    def script(self, *items: Any) -> None:
        self.fake.script(*items)

    def script_plan(self, document: str) -> None:
        """Queue the planner's answer."""
        self.fake.script(ScriptedGeneration(text=document))

    def claim_and_run(
        self, trajectory_id: str, *, owner: str = "host:1/0", signals: RunSignals | None = None
    ) -> TrajectoryState:
        """Claim a queued trajectory and run it to its next rest."""
        controller = self.controller(owner)
        claimed = controller.claim(trajectory_id)
        assert claimed is not None
        if claimed is TrajectoryState.AWAITING_APPROVAL:
            return claimed
        return controller.run(trajectory_id, signals=signals)

    def resume(self, trajectory_id: str, *, owner: str = "host:1/0") -> TrajectoryState:
        """Take a released ``executing`` trajectory (after a grant) and run it on."""
        controller = self.controller(owner)
        assert controller.claim_released(trajectory_id), "the trajectory was not released"
        return controller.run(trajectory_id)

    def events(self, trajectory_id: str) -> list[str]:
        return [event.event_type for event in self.service.events(trajectory_id)]

    def event_data(self, trajectory_id: str, event_type: str) -> list[dict[str, Any]]:
        return [
            dict(event.data)
            for event in self.service.events(trajectory_id)
            if event.event_type == event_type
        ]


class open_harness:  # noqa: N801 — a context manager, used as one
    """``with open_harness(settings) as harness:`` over a fresh database and fake."""

    def __init__(
        self,
        settings: Settings,
        *,
        profiles: tuple[str, ...] = PLANNER_PROFILES,
        pricing: PricingCatalog | None = None,
        remote_provider: bool = False,
    ) -> None:
        self._settings = settings
        self._profiles = profiles
        self._pricing = pricing
        self._remote_provider = remote_provider
        self._engine: Any = None

    def __enter__(self) -> LoopHarness:
        fake = FakeLoadCoach()
        fake.register_profile(*shipped_profiles(*self._profiles))
        self._engine = temporary_sqlite()
        engine = self._engine.__enter__()
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        return LoopHarness(
            self._settings,
            Database(engine),
            fake,
            pricing=self._pricing,
            remote_provider=self._remote_provider,
        )

    def __exit__(self, *exc: object) -> None:
        self._engine.__exit__(*exc)


def remote_tier_env(pricing_file: str, *, name: str = "remote_cheap") -> dict[str, str]:
    """Environment overrides configuring one remote tier, for ``monkeypatch.setenv``."""
    # A ``tiers`` table given through the environment replaces the shipped defaults rather than
    # extending them, so the two local tiers are restated beside the remote one.
    prefix = f"PROMPTCADENCE_TIERS__{name.upper()}__"
    return {
        "PROMPTCADENCE_TIERS__LOCAL_FAST__TASK_PROFILE": "tools.agent.local_fast",
        "PROMPTCADENCE_TIERS__LOCAL_FAST__REMOTE": "false",
        "PROMPTCADENCE_TIERS__LOCAL_FAST__CONTEXT_BUDGET_TOKENS": "16384",
        "PROMPTCADENCE_TIERS__LOCAL_LARGE__TASK_PROFILE": "tools.agent.local_large",
        "PROMPTCADENCE_TIERS__LOCAL_LARGE__REMOTE": "false",
        "PROMPTCADENCE_TIERS__LOCAL_LARGE__CONTEXT_BUDGET_TOKENS": "32768",
        f"{prefix}TASK_PROFILE": f"tools.agent.{name}",
        f"{prefix}REMOTE": "true",
        f"{prefix}MAX_DATA_CLASSIFICATION": "internal",
        f"{prefix}CONTEXT_BUDGET_TOKENS": "128000",
        f"{prefix}PRICING_FILE": pricing_file,
    }


PRICING_DOCUMENT = """
{
  "records": [
    {
      "provider_kind": "ollama",
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
