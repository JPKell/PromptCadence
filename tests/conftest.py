"""Shared fixtures.

Two properties every test in this suite depends on:

* **No network, ever.** The default suite must pass with no LoadCoach reachable and no network
  (spec §18, development plan Phase 1), so the ``no_network`` autouse fixture makes an accidental
  non-loopback socket a loud failure rather than a slow one. A test that genuinely needs one is
  marked ``live``.
* **No ambient state.** Configuration reads ``PROMPTCADENCE_*`` and the XDG variables from the real
  environment, so every test gets its own data directory and a cleared prefix. Without this a
  developer's own ``~/.config/promptcadence/config.toml`` would change the result of a test run.
  A ``live`` test keeps ``PROMPTCADENCE_LOADCOACH__*`` and ``PROMPTCADENCE_TIERS__*`` — the
  operator must be able to point it at their LoadCoach and at the task profiles that LoadCoach
  has — and is isolated in every other respect.

The domain fixtures below are shared because determinism is: the tier snapshot, the approval
policy and ``minted_at`` are fixed values, so a golden derived from them is byte-identical on
every run and on every machine (lifecycle §10).
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from baseaicore import DataClassification, Money

from promptcadence.config import Settings
from promptcadence.domain.policy import ApprovalMode, ApprovalPolicy
from promptcadence.domain.tiers import EgressClass, Tier, TierPolicy, TierSnapshot
from promptcadence.domain.trajectory import TrajectoryDeclaration
from promptcadence.services.budget import BudgetService
from promptcadence.services.database import Database
from promptcadence.services.estimates import StepEstimator
from promptcadence.services.pricing import PricingCatalog

_REAL_SOCKET_CONNECT = socket.socket.connect


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse any outbound socket connection that is not to loopback.

    Tests marked ``live`` are exempt: those are the ones that need a real provider.
    """
    if request.node.get_closest_marker("live"):
        return

    def _guard(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in {"127.0.0.1", "::1", "localhost"}:
            _REAL_SOCKET_CONNECT(self, address)
            return
        message = f"network access refused in a default test: {address!r}"
        raise RuntimeError(message)

    monkeypatch.setattr(socket.socket, "connect", _guard)


_LIVE_KEEPS = ("PROMPTCADENCE_LOADCOACH__", "PROMPTCADENCE_TIERS__")


@pytest.fixture(autouse=True)
def isolated_environment(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point every XDG path at a temporary directory and clear the ``PROMPTCADENCE_`` prefix.

    A test marked ``live`` keeps the LoadCoach address and the tier configuration from the real
    environment (module docstring); everything else is cleared for it too.
    """
    live = request.node.get_closest_marker("live") is not None
    for key in list(os.environ):
        if key.startswith("PROMPTCADENCE_") and not (live and key.startswith(_LIVE_KEEPS)):
            monkeypatch.delenv(key, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("PROMPTCADENCE_DATA_DIR", str(data))
    monkeypatch.chdir(tmp_path)
    data.mkdir(parents=True, exist_ok=True)
    yield data


@pytest.fixture
def tier_snapshot() -> TierSnapshot:
    """Four tiers covering every admission and egress case the domain tests need.

    Ordered by name, as :class:`TierSnapshot` requires for a stable content address. The two
    remote tiers differ in ceiling on purpose: ``remote_cheap`` admits up to ``internal`` and
    ``remote_frontier`` only ``public``, which is the shipped ladder ADR-0046 says three levels
    exist to express.
    """
    return TierSnapshot(
        tiers=(
            Tier(
                name="local_fast",
                task_profile="tools.agent.local_fast",
                egress_class=EgressClass.LOCAL,
                max_data_classification=None,
                context_budget_tokens=16_384,
            ),
            Tier(
                name="local_large",
                task_profile="tools.agent.local_large",
                egress_class=EgressClass.LOCAL,
                max_data_classification=None,
                context_budget_tokens=32_768,
            ),
            Tier(
                name="remote_cheap",
                task_profile="tools.agent.remote_cheap",
                egress_class=EgressClass.REMOTE,
                max_data_classification=DataClassification.INTERNAL,
                context_budget_tokens=64_000,
                pricing_source="pricing/remote_cheap.json",
            ),
            Tier(
                name="remote_frontier",
                task_profile="tools.agent.remote_frontier",
                egress_class=EgressClass.REMOTE,
                max_data_classification=DataClassification.PUBLIC,
                context_budget_tokens=128_000,
                pricing_source="pricing/remote_frontier.json",
            ),
        ),
        default_tier="local_fast",
        escalation_order=("local_fast", "local_large", "remote_cheap", "remote_frontier"),
    )


@pytest.fixture
def tier_policy(tier_snapshot: TierSnapshot) -> TierPolicy:
    """A tier policy with a remote provider registered, as it will be once LC-E1 lands."""
    return TierPolicy(snapshot=tier_snapshot, loadcoach_has_remote_provider=True)


@pytest.fixture
def local_only_policy(tier_snapshot: TierSnapshot) -> TierPolicy:
    """A tier policy as PromptCadence ships today: LoadCoach has no remote provider."""
    return TierPolicy(snapshot=tier_snapshot, loadcoach_has_remote_provider=False)


@pytest.fixture
def approval_policy() -> ApprovalPolicy:
    """The shipped default approval policy: auto, gating egress at internal."""
    return ApprovalPolicy(
        mode=ApprovalMode.AUTO,
        gate_egress_at=DataClassification.INTERNAL,
        gate_step_cost=Money(currency="USD", nanos=1_000_000_000),
    )


@pytest.fixture
def declaration() -> TrajectoryDeclaration:
    """A caller's declaration: internal work, two tools, a token ceiling and eight turns."""
    return TrajectoryDeclaration(
        trajectory_id="01TRAJECTORY0000000000000A",
        classification=DataClassification.INTERNAL,
        tool_allowlist=frozenset({"read_file", "list_dir"}),
        token_budget=100_000,
        money_budget=Money(currency="USD", nanos=5_000_000_000),
        max_turns=8,
    )


@pytest.fixture
def minted_at() -> datetime:
    """A fixed instant, so every minting golden is byte-identical on re-derivation."""
    return datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def budget_for(
    database: Database,
    settings: Settings,
    *,
    clock: Callable[[], datetime],
    pricing: PricingCatalog | None = None,
) -> BudgetService:
    """Build a budget service over one test's database, clock and (usually empty) price list.

    A helper rather than a fixture because most callers need it beside a ``Database`` they built
    themselves, and because the clock is the point: every window, day edge and expiry assertion in
    Phase 5 depends on injecting one.
    """
    return BudgetService(
        database,
        settings,
        pricing if pricing is not None else PricingCatalog(by_tier={}),
        clock=clock,
    )


def budget_and_estimator(
    database: Database,
    settings: Settings,
    *,
    clock: Callable[[], datetime],
    pricing: PricingCatalog | None = None,
) -> tuple[BudgetService, StepEstimator]:
    """The pair every :class:`~promptcadence.services.loop.LoopController` needs."""
    budget = budget_for(database, settings, clock=clock, pricing=pricing)
    return budget, StepEstimator(budget, settings, clock=clock)
