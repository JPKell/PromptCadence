"""Phase 5: the ceilings bind, the estimates are labelled, and the ledger survives a crash.

Every test here injects a clock. That is not a style preference: a UTC day edge, a
``window_wait_max_days`` expiry and a price's effective window are all decided by an instant, and
a suite that read the machine's clock could only test the boundary it happened to run near.

The fake LoadCoach answers every generation, so nothing here needs a GPU, Ollama or a network
(spec §20 #10). The prices are written into the session's own ``tmp_path`` as the JSON pricing
files a remote tier names.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import Money, is_supported
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import budget_and_estimator
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    ScriptedGeneration,
    Wire,
    build_fake_app,
    shipped_profiles,
    text_profile,
)
from toolyard import TieredSandbox

from promptcadence.config import ConfigurationError, Settings, load_settings
from promptcadence.domain.errors import ErrorCode, ProjectUnknownError
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.infrastructure.db import models
from promptcadence.infrastructure.loadcoach import LoadCoachClient
from promptcadence.services.budget import (
    NOT_PRICED,
    project_tag,
    render_money,
    render_remaining_money,
    tier_tag,
)
from promptcadence.services.database import Database, ensure_ready
from promptcadence.services.events import TrajectoryEventSink
from promptcadence.services.loop import LoopController
from promptcadence.services.pricing import PricingCatalog, load_pricing_records
from promptcadence.services.tools import ToolPlant
from promptcadence.services.trajectories import TrajectoryService, TrajectorySubmission

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_DAY = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
_REMOTE_MODEL = "ollama/qwen3:8b"


class Clock:
    """A clock a test moves by hand. Every instant in this file comes from one of these."""

    def __init__(self, start: datetime = _DAY) -> None:
        self.now = start
        self._tick = 0

    def __call__(self) -> datetime:
        # A millisecond per read, so ULIDs and event sequences still order, while the *day* the
        # test set stays the day every window resolves against.
        self._tick += 1
        return self.now + timedelta(milliseconds=self._tick)

    def advance_to(self, when: datetime) -> None:
        self.now = when
        self._tick = 0


class Harness:
    """One database, one fake LoadCoach, one clock, and the services built over them."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        fake: FakeLoadCoach,
        clock: Clock,
        *,
        pricing: PricingCatalog | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.fake = fake
        self.clock = clock
        self.sink = TrajectoryEventSink(database, clock=clock)
        self.budget, self.estimator = budget_and_estimator(
            database, settings, clock=clock, pricing=pricing
        )
        self.service = TrajectoryService(
            database, self.sink, settings, budget=self.budget, clock=clock
        )
        self.loadcoach = LoadCoachClient(
            TestClient(build_fake_app(fake), base_url="http://loadcoach.test")
        )
        # No isolation rung, deterministically (the D1 seam), so a `list_dir` round trip runs the
        # same on every host. Tools are here only to make a *second* turn happen: a ceiling that
        # binds "mid-trajectory" needs a trajectory with a middle.
        self.tools = ToolPlant(settings, sandbox=TieredSandbox(which=lambda _name: None))

    def controller(self, owner: str = "host:1/0") -> LoopController:
        return LoopController(
            database=self.database,
            sink=self.sink,
            loadcoach=self.loadcoach,
            settings=self.settings,
            owner=owner,
            budget=self.budget,
            estimator=self.estimator,
            clock=self.clock,
            tools=self.tools,
        )

    def submit(self, **overrides: Any) -> str:
        fields: dict[str, Any] = {"task": "summarize ./notes", "bypass_planning": True}
        fields.update(overrides)
        return self.service.submit(TrajectorySubmission(**fields)).trajectory_id

    def run(self, **overrides: Any) -> tuple[str, TrajectoryState]:
        trajectory_id = self.submit(**overrides)
        controller = self.controller()
        assert controller.claim(trajectory_id) is TrajectoryState.EXECUTING
        return trajectory_id, controller.run(trajectory_id)

    def events(self, trajectory_id: str) -> list[str]:
        return [event.event_type for event in self.service.events(trajectory_id)]

    def entries(self, trajectory_id: str) -> list[Any]:
        return list(self.budget.entries(run_id=trajectory_id))


def _two_turns(fake: FakeLoadCoach, **usage: Any) -> None:
    """Script a tool call and then a declared stop, so one trajectory runs two turns.

    A tool call is not a declared finish, so the loop continues — which is the only way a bypass
    trajectory gets a second turn, and therefore the only way a ceiling can bind *mid*-trajectory
    rather than before the first turn or after the last.
    """
    fake.script(
        ScriptedGeneration(
            text="",
            tool_calls=(
                {
                    "call_index": 0,
                    "id": "c1",
                    "name": "list_dir",
                    "arguments_fragment": '{"path": "."}',
                },
            ),
            **usage,
        ),
        ScriptedGeneration(text="The workspace is empty.", **usage),
    )


def _settings(**budget: Any) -> Settings:
    """Load the shipped settings with a ``[budget]`` override and cheap per-step estimates.

    The shipped 4096+1024 default estimate is right for an installation and far too large for a
    test that wants a ceiling crossed on the second turn rather than refused before the first, so
    every tier here estimates 100+50.
    """
    loaded = load_settings().settings
    tiers = {
        name: tier.model_copy(
            update={"default_step_input_tokens": 100, "default_step_output_tokens": 50}
        )
        for name, tier in loaded.tiers.items()
    }
    return loaded.model_copy(
        update={"tiers": tiers, "budget": loaded.budget.model_copy(update=budget)}
    )


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    handle = Database.from_url(f"sqlite:///{tmp_path / 'budget.sqlite3'}")
    ensure_ready(handle, auto_migrate=True)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def fake() -> FakeLoadCoach:
    served = FakeLoadCoach(wire=Wire.POST_MODELRACK_070)
    served.register_profile(
        *shipped_profiles("tools.agent.local_fast", "tools.agent.local_large"),
        text_profile("tools.agent.remote_cheap"),
    )
    served.set_default(ScriptedGeneration(input_tokens=800, output_tokens=200))
    return served


@pytest.fixture
def harness(database: Database, fake: FakeLoadCoach) -> Harness:
    return Harness(_settings(), database, fake, Clock())


# ---------------------------------------------------------------------------------------------
# declare_run, and the pre-flight that must never meet UnknownRun (§0.2 of the kickoff)
# ---------------------------------------------------------------------------------------------


def test_a_trajectorys_first_action_may_be_a_preflight_without_meeting_unknown_run(
    harness: Harness,
) -> None:
    """``declare_run`` fires at creation, so a budget question precedes any spend safely.

    The cheap test that catches a later refactor moving the declaration to the first turn: a
    trajectory that has done nothing at all is asked what its budget allows, and answers.
    """
    trajectory_id = harness.submit()
    view = harness.service.get(trajectory_id)
    position = harness.budget.position(view)  # would raise UnknownRun if undeclared
    assert [one.scope for one in position.headroom] == ["trajectory", "day"]
    assert position.binding is None
    assert position.headroom[0].tokens_remaining == harness.settings.budget.default_token_ceiling


def test_the_declaration_is_in_the_same_write_as_the_trajectory_row(harness: Harness) -> None:
    """One write, so a crash can never leave a trajectory the ledger has never heard of."""
    trajectory_id = harness.submit()
    with harness.database.read() as session:
        declared = session.execute(
            select(models.LEDGER_TABLES.runs.c.run_id).where(
                models.LEDGER_TABLES.runs.c.run_id == trajectory_id
            )
        ).scalar_one()
    assert declared == trajectory_id


# ---------------------------------------------------------------------------------------------
# The debit itself
# ---------------------------------------------------------------------------------------------


def test_a_turn_debits_all_four_token_classes_and_stores_usage_not_money(harness: Harness) -> None:
    """ADR-0070 and ADR-0030 together: four classes in, usage plus a hash out, no money stored."""
    harness.fake.set_default(
        ScriptedGeneration(
            input_tokens=800, output_tokens=200, cache_write_tokens=64, cache_read_tokens=32
        )
    )
    trajectory_id, state = harness.run()
    assert state is TrajectoryState.COMPLETED
    entry = harness.entries(trajectory_id)[0]
    assert entry.debit.usage.as_counts() == {
        "input": 800,
        "output": 200,
        "cache_write": 64,
        "cache_read": 32,
    }
    assert entry.debit.source_ref == harness.service.turns(trajectory_id)[1].turn.turn_id
    assert entry.debit.cost is None, "a local tier prices nothing; UNSUPPORTED is not $0.00"
    body = next(
        event.data
        for event in harness.service.events(trajectory_id)
        if event.event_type == "budget.debited"
    )
    assert "money" not in body and "cost" not in body
    assert body["usage"]["cache_read"] == 32
    assert body["unpriced"] is True


def test_an_unreported_token_class_stays_unsupported_and_is_never_counted_as_zero(
    harness: Harness,
) -> None:
    """ADR-0016 rule 4: a class the provider never reported is excluded, not zeroed."""
    harness.fake.set_default(
        ScriptedGeneration(input_tokens=800, output_tokens=None, cache_read_tokens=0)
    )
    trajectory_id, _ = harness.run()
    entry = harness.entries(trajectory_id)[0]
    assert not is_supported(entry.debit.usage.output_tokens)
    assert entry.debit.usage.cache_read_tokens == 0, "a reported zero is a zero"
    verdict = next(one for one in entry.verdicts if one.ceiling.tokens is not None)
    assert verdict.tokens_spent == 800 + 0 + 0, "the unreported class contributed nothing"
    assert verdict.unmetered_debit_count == 1, "and the balance says it is a floor"


def test_every_debit_carries_its_tier_tag_and_its_project_tag(harness: Harness) -> None:
    settings = _settings()
    settings = settings.model_copy(
        update={
            "budget": settings.budget.model_copy(
                update={"projects": {"research": _project(tokens=1_000_000)}}
            )
        }
    )
    harness = Harness(settings, harness.database, harness.fake, harness.clock)
    trajectory_id, _ = harness.run(project="research")
    entry = harness.entries(trajectory_id)[0]
    assert set(entry.debit.tags) == {tier_tag("local_fast"), project_tag("research")}


# ---------------------------------------------------------------------------------------------
# Ceilings crossing mid-trajectory (spec §20 #6)
# ---------------------------------------------------------------------------------------------


def test_crossing_the_token_ceiling_mid_trajectory_halts_with_every_debit_on_the_ledger(
    database: Database, fake: FakeLoadCoach
) -> None:
    """Spec §20 #6, the token half.

    The first turn fits under a 1 100-token ceiling and spends 1 000; the second turn's pre-flight
    adds the 150-token estimate to that — 1 150, past the cap — and refuses, before the call. The
    trajectory halts with every debit on the ledger and the balance that crossed on the verdict.
    """
    _two_turns(fake, input_tokens=800, output_tokens=200)
    harness = Harness(_settings(on_exhausted="halt"), database, fake, Clock())
    trajectory_id, state = harness.run(token_budget=1_100)
    assert state is TrajectoryState.HALTED
    view = harness.service.get(trajectory_id)
    assert view.error_code == ErrorCode.TOKEN_BUDGET_EXCEEDED.value
    assert "trajectory budget refuses the next step" in (view.halted_reason or "")
    assert "the tokens cap cannot admit it" in (view.halted_reason or "")
    assert "over by 50" in (view.halted_reason or ""), (
        "the cause is about the pre-flight -- 1 000 spent plus a 150 estimate against a 1 100 cap"
    )
    entries = harness.entries(trajectory_id)
    assert len(entries) == 1, "the turn that ran is on the ledger; the refused one never ran"
    verdict = next(one for one in entries[0].verdicts if one.ceiling.tokens == 1_100)
    assert (verdict.tokens_spent, verdict.tokens_remaining) == (1_000, 100)


def test_a_token_ceiling_binds_a_local_tier_where_a_money_ceiling_cannot(harness: Harness) -> None:
    """The ADR-0030 case. A local model's cost is UNSUPPORTED, so only tokens can brake it."""
    _two_turns(harness.fake, input_tokens=800, output_tokens=200)
    harness = Harness(_settings(on_exhausted="halt"), harness.database, harness.fake, harness.clock)
    trajectory_id, state = harness.run(
        token_budget=1_050, money_budget=Money.from_decimal("USD", "1000.00")
    )
    assert state is TrajectoryState.HALTED
    entries = harness.entries(trajectory_id)
    assert entries, "the local turn spent tokens"
    money = next(one for one in entries[0].verdicts if one.ceiling.money is not None)
    assert money.money_spent is None, "nothing was priced, and None is not zero"
    assert money.exceeded is False, "an enormous money cap cannot be crossed by unpriced work"
    tokens = next(one for one in entries[0].verdicts if one.ceiling.tokens == 1_050)
    assert tokens.tokens_spent == 1_000, "and the token cap is what the next pre-flight refused on"


def test_the_most_restrictive_of_three_active_ceilings_binds(harness: Harness) -> None:
    """Three ceilings, one answer, and every entry records its balance against each of them."""
    settings = _settings(on_exhausted="halt")
    settings = settings.model_copy(
        update={
            "budget": settings.budget.model_copy(update={"projects": {"tight": _project(1_100)}})
        }
    )
    harness = Harness(settings, harness.database, harness.fake, harness.clock)
    spender, _ = harness.run(project="tight", token_budget=2_000_000)
    entry = harness.entries(spender)[0]
    scopes = [_label(verdict) for verdict in entry.verdicts]
    assert scopes == ["trajectory", "day", "project:tight"], "a balance against each, in order"
    assert [verdict.tokens_spent for verdict in entry.verdicts] == [1_000, 1_000, 1_000]

    refused, state = harness.run(project="tight", token_budget=2_000_000)
    assert state is TrajectoryState.HALTED
    # The trajectory's own 2 000 000-token ceiling and the per-day money ceiling both admit; the
    # project's 1 100 does not, and the most restrictive is the one the cause names.
    assert "project:tight budget refuses" in (harness.service.get(refused).halted_reason or "")
    position = harness.budget.position(harness.service.get(refused))
    assert [one.scope for one in position.headroom] == ["trajectory", "day", "project:tight"]


# ---------------------------------------------------------------------------------------------
# The project label
# ---------------------------------------------------------------------------------------------


def test_an_unknown_project_is_refused_before_anything_is_persisted(harness: Harness) -> None:
    with pytest.raises(ProjectUnknownError) as raised:
        harness.submit(project="nowhere")
    assert raised.value.code == ErrorCode.PROJECT_UNKNOWN
    with harness.database.read() as session:
        assert session.execute(select(models.Trajectory)).all() == []
        assert session.execute(select(models.LEDGER_TABLES.runs)).all() == []


def test_a_project_ceiling_binds_across_two_trajectories_that_share_the_label(
    harness: Harness,
) -> None:
    """A project cap is a lifetime cap over a tag, so the second trajectory inherits the first's
    spend — which is the whole difference between a project budget and a per-run one."""
    settings = _settings(on_exhausted="halt")
    settings = settings.model_copy(
        update={
            "budget": settings.budget.model_copy(update={"projects": {"shared": _project(1_100)}})
        }
    )
    harness = Harness(settings, harness.database, harness.fake, harness.clock)
    first, first_state = harness.run(project="shared")
    assert first_state is TrajectoryState.COMPLETED
    second, second_state = harness.run(project="shared")
    assert second_state is TrajectoryState.HALTED, "the first trajectory's spend binds the second"
    assert harness.entries(second) == [], "and it never ran a turn of its own"
    assert "project:shared" in (harness.service.get(second).halted_reason or "")
    assert first != second


# ---------------------------------------------------------------------------------------------
# The daily window: T15, T16, T17 (lifecycle §8, all on an injected clock)
# ---------------------------------------------------------------------------------------------


def _daily(**budget: Any) -> Settings:
    """Settings whose per-day money ceiling admits exactly one priced turn.

    One turn of 800 input + 200 output at the test's rates costs 4 000 000 nanos ($0.004); the cap
    is $0.0045, so the first trajectory fits and the second's pre-flight — 4 000 000 already spent
    plus a 750 000-nano estimate — does not.
    """
    return _settings(
        daily_money_ceiling=_amount("0.0045"),
        on_daily_exhausted="window",
        **budget,
    )


def _priced_catalog(pricing_file: Path) -> PricingCatalog:
    """A catalogue that prices the **local** tier the fake actually serves.

    A deliberate test fiction, and the reason it is built here rather than through
    :meth:`PricingCatalog.from_settings`: that classmethod refuses to price a local tier, because a
    local model's cost is ``UNSUPPORTED`` and never ``$0.00`` (ADR-0016), and
    ``test_from_settings_never_prices_a_local_tier`` holds it to that. But a *remote* tier cannot
    run at all in this build — every remote tier reports ``loadcoach_has_no_remote_provider`` until
    LC-E1 registers one (lifecycle §3, ADR-0047 §2) — so a money ceiling could not otherwise be
    reached through the loop by any test. Constructing the catalogue directly says "pretend this
    tier's model has a price list" in one visible place, and exercises every money path against the
    real loop instead of deferring all of them to a mock.
    """
    return PricingCatalog(by_tier={"local_fast": load_pricing_records(pricing_file)})


def test_a_trajectory_parks_on_the_per_day_ceiling_and_resumes_on_the_utc_day_edge(
    database: Database, fake: FakeLoadCoach, pricing_file: Path
) -> None:
    """T15 then T16: the day is spent, it parks holding no lease, and the edge releases it."""
    clock = Clock()
    harness = Harness(_daily(), database, fake, clock, pricing=_priced_catalog(pricing_file))
    spender, spent = harness.run()
    assert spent is TrajectoryState.COMPLETED, harness.service.get(spender).halted_reason
    assert harness.entries(spender), "the first trajectory spent the day's money"

    parked, state = harness.run()
    assert state is TrajectoryState.AWAITING_WINDOW
    view = harness.service.get(parked)
    assert view.lease_owner is None, "awaiting_window holds no lease (lifecycle §8.1)"
    assert view.window is not None
    assert view.window.next_edge_at == datetime(2026, 9, 4, tzinfo=UTC)
    assert view.window.parked_from is TrajectoryState.EXECUTING
    assert "budget.window_wait" in harness.events(parked)

    clock.advance_to(datetime(2026, 9, 4, 0, 30, tzinfo=UTC))
    assert harness.controller().release_window(parked) is TrajectoryState.EXECUTING
    resumed = harness.service.get(parked)
    assert resumed.state is TrajectoryState.EXECUTING
    assert resumed.window is None
    assert "trajectory.resumed" in harness.events(parked)


def test_a_parked_trajectory_stays_parked_when_another_has_already_spent_the_new_day(
    database: Database, fake: FakeLoadCoach, pricing_file: Path
) -> None:
    """The middle answer: a day edge is not an entitlement to run."""
    clock = Clock()
    harness = Harness(_daily(), database, fake, clock, pricing=_priced_catalog(pricing_file))
    harness.run()
    parked, state = harness.run()
    assert state is TrajectoryState.AWAITING_WINDOW

    clock.advance_to(datetime(2026, 9, 4, 0, 30, tzinfo=UTC))
    harness.run()  # a third trajectory spends the new day first
    assert harness.controller().release_window(parked) is TrajectoryState.AWAITING_WINDOW
    view = harness.service.get(parked)
    assert view.window is not None
    assert view.window.days_waited == 1, "one edge counted"
    assert view.window.next_edge_at == datetime(2026, 9, 5, tzinfo=UTC), "waiting for the next"


def test_a_parked_trajectory_halts_after_window_wait_max_days(
    database: Database, fake: FakeLoadCoach, pricing_file: Path
) -> None:
    """T17: it never waits forever, and the cause names the configured limit."""
    clock = Clock()
    harness = Harness(
        _daily(window_wait_max_days=2), database, fake, clock, pricing=_priced_catalog(pricing_file)
    )
    harness.run()
    parked, _ = harness.run()
    controller = harness.controller()
    for day in (4, 5):
        clock.advance_to(datetime(2026, 9, day, 0, 30, tzinfo=UTC))
        harness.run()  # every new day is spent before it is reached
        state = controller.release_window(parked)
    assert state is TrajectoryState.HALTED
    view = harness.service.get(parked)
    assert view.error_code == ErrorCode.BUDGET_EXCEEDED.value
    assert "window_wait_max_days (2)" in (view.halted_reason or "")


def test_the_per_day_ceiling_binds_money_only_so_local_work_never_parks(
    database: Database, fake: FakeLoadCoach
) -> None:
    """Spec §11.5: the per-day ceiling is what lets any amount of *work* run.

    Local work is unpriced and never counts against it, which is why ``[budget]`` has no
    ``daily_token_ceiling`` and this build does not invent one.
    """
    harness = Harness(_daily(), database, fake, Clock())
    for _ in range(3):
        _, state = harness.run()
        assert state is TrajectoryState.COMPLETED, "unpriced local work cannot spend a money cap"


# ---------------------------------------------------------------------------------------------
# on_exhausted = approval
# ---------------------------------------------------------------------------------------------


def test_exhaustion_under_the_approval_policy_parks_on_one_pending_request(
    harness: Harness,
) -> None:
    """T10. The request exists before the state moves, because a trajectory parked with no
    request is one nobody can release (ADR-0049 rule 6). Granting the raise is P7's."""
    _two_turns(harness.fake, input_tokens=800, output_tokens=200)
    trajectory_id, state = harness.run(token_budget=1_050)
    assert state is TrajectoryState.AWAITING_APPROVAL
    with harness.database.read() as session:
        request = session.execute(select(models.ApprovalRequest)).scalar_one()
    assert request.trajectory_id == trajectory_id
    assert request.status == "pending"
    assert request.reason == "budget_exceeded"
    assert "approval.requested" in harness.events(trajectory_id)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _amount(decimal: str) -> Any:
    from promptcadence.config import MoneyAmount

    money = Money.from_decimal("USD", decimal)
    return MoneyAmount(currency=money.currency, nanos=money.nanos)


def _project(tokens: int | None = None, money: str | None = None) -> Any:
    from promptcadence.config import ProjectBudget

    return ProjectBudget(
        token_ceiling=tokens, money_ceiling=_amount(money) if money is not None else None
    )


def _label(verdict: Any) -> str:
    if verdict.ceiling.scope.value == "per_run":
        return "trajectory"
    if verdict.ceiling.scope.value == "per_day":
        return "day"
    return str(verdict.ceiling.tag)


@pytest.fixture
def pricing_file(tmp_path: Path) -> Path:
    """A JSON pricing file for the model the fake serves, as a tier's ``pricing_file``.

    The rates are decimal *strings* and all four classes are stated, so an ordinary turn prices
    completely; the tests that need a floor withhold a rate deliberately.
    """
    path = tmp_path / "tier.pricing.json"
    path.write_text(
        _PRICING_DOCUMENT.replace("__CACHE_READ__", '"0.25"'),
        encoding="utf-8",
    )
    return path


_PRICING_DOCUMENT = """
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
        "cache_read_per_million_tokens": __CACHE_READ__
      }
    }
  ]
}
"""


def test_from_settings_never_prices_a_local_tier(tmp_path: Path, pricing_file: Path) -> None:
    """ADR-0016 through the catalogue: a local tier holds no price, whatever it names.

    The invariant the window tests' hand-built catalogue deliberately steps around, asserted here
    so stepping around it in a test can never become the shipped behaviour.
    """
    loaded = load_settings().settings
    tiers = dict(loaded.tiers)
    tiers["local_fast"] = tiers["local_fast"].model_copy(update={"pricing_file": str(pricing_file)})
    catalog = PricingCatalog.from_settings(loaded.model_copy(update={"tiers": tiers}))
    assert catalog.by_tier["local_fast"] == ()
    assert catalog.for_model(tier="local_fast", canonical_id=_REMOTE_MODEL, at=_DAY) is None


def test_an_unreadable_pricing_file_refuses_at_startup_rather_than_mid_trajectory(
    tmp_path: Path,
) -> None:
    """A price list discovered to be broken halfway through a run leaves spend nobody can cost."""
    broken = tmp_path / "broken.pricing.json"
    broken.write_text('{"records": [{"provider_kind": "ollama"}]}', encoding="utf-8")
    with pytest.raises(ConfigurationError) as raised:
        load_pricing_records(broken)
    assert "provider_model_name" in str(raised.value)

    missing = tmp_path / "absent.pricing.json"
    with pytest.raises(ConfigurationError):
        load_pricing_records(missing)


def test_a_rate_stated_as_a_json_number_is_refused(tmp_path: Path) -> None:
    """A float has already lost the value the suite's integer money arithmetic protects."""
    path = tmp_path / "float.pricing.json"
    path.write_text(
        _PRICING_DOCUMENT.replace("__CACHE_READ__", "0.25").replace('"2.50"', "2.5"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError) as raised:
        load_pricing_records(path)
    assert "decimal *string*" in str(raised.value)


# ---------------------------------------------------------------------------------------------
# Gate C — a partial price is a floor, and a money ceiling chooses how it binds (ADR-0069)
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def partial_pricing_file(tmp_path: Path) -> Path:
    """A price list that states no cache-read rate — the ordinary adapter case.

    Omitted, not zero. A call that read from cache therefore cannot be fully priced: the priced
    components accumulate as a **floor** and the estimate does not total, which is the exact
    condition ``partial_pricing`` decides the meaning of.
    """
    path = tmp_path / "partial.pricing.json"
    path.write_text(_PRICING_DOCUMENT.replace("__CACHE_READ__", "null"), encoding="utf-8")
    return path


def _partial(database: Database, fake: FakeLoadCoach, path: Path, **budget: Any) -> Harness:
    """A harness whose (fictionally priced) local tier cannot fully price a cache-reading turn."""
    fake.set_default(ScriptedGeneration(input_tokens=800, output_tokens=200, cache_read_tokens=500))
    return Harness(
        _settings(**budget),
        database,
        fake,
        Clock(),
        pricing=PricingCatalog(by_tier={"local_fast": load_pricing_records(path)}),
    )


def test_under_floor_the_trajectory_continues_and_the_balance_reads_at_least(
    database: Database, fake: FakeLoadCoach, partial_pricing_file: Path
) -> None:
    """The default. The brake may fire late by the unreported portion, and never early."""
    harness = _partial(database, fake, partial_pricing_file, partial_pricing="floor")
    trajectory_id, state = harness.run()
    assert state is TrajectoryState.COMPLETED, "a floor does not stop the work"
    entry = harness.entries(trajectory_id)[0]
    verdict = next(one for one in entry.verdicts if one.ceiling.money is not None)
    assert verdict.exceeded is False
    assert verdict.untotalled_debit_count == 1, "the cache-read class could not be priced"
    assert verdict.unpriced_debit_count == 1
    assert verdict.money_spent == Money(currency="USD", nanos=4_000_000)
    position = harness.budget.position(harness.service.get(trajectory_id))
    trajectory = position.headroom[0]
    assert trajectory.money_is_floor is True, "render it 'at least', never as a bare figure"
    left = render_remaining_money(trajectory.money_remaining, is_floor=trajectory.money_is_floor)
    assert left.startswith("at most "), (
        "what is *left* is an upper bound when the spend it was derived from is a floor -- "
        "rendering it 'at least' would reassure in exactly the case where less may remain"
    )
    assert render_money(verdict.money_spent, is_floor=True).startswith("at least ")


def test_under_strict_the_next_step_is_refused_at_preflight_not_detected_afterwards(
    database: Database, fake: FakeLoadCoach, partial_pricing_file: Path
) -> None:
    """A hard budget is never crossed: an amount that cannot be shown to be under the cap is over.

    The refusal is at pre-flight, *before* the second call — not a verdict recorded after it.
    """
    harness = _partial(
        database, fake, partial_pricing_file, partial_pricing="strict", on_exhausted="halt"
    )
    _two_turns(harness.fake, input_tokens=800, output_tokens=200, cache_read_tokens=500)
    trajectory_id, state = harness.run()
    assert state is TrajectoryState.HALTED
    view = harness.service.get(trajectory_id)
    assert view.error_code == ErrorCode.BUDGET_EXCEEDED.value
    assert "partial_pricing is strict" in (view.halted_reason or "")
    assert len(harness.entries(trajectory_id)) == 1, "the refused step was never called"
    assert len(harness.fake.jobs) == 1, "and LoadCoach was never asked a second time"


def test_a_per_request_partial_pricing_override_beats_the_configured_default(
    database: Database, fake: FakeLoadCoach, partial_pricing_file: Path
) -> None:
    """Strictness is a property of the piece of work, so the request may pin it either way."""
    harness = _partial(database, fake, partial_pricing_file, partial_pricing="floor")
    _two_turns(harness.fake, input_tokens=800, output_tokens=200, cache_read_tokens=500)
    trajectory_id, state = harness.run(partial_pricing="strict")
    assert state is TrajectoryState.AWAITING_APPROVAL, "the request's rule bound, not the config's"
    assert harness.service.get(trajectory_id).partial_pricing == "strict"

    ceilings = harness.budget.ceilings_for(harness.service.get(trajectory_id))
    assert all(
        ceiling.partial_pricing.value == "strict"
        for ceiling in ceilings
        if ceiling.money is not None
    ), "the rule rides on every money ceiling, so would_exceed applies it at pre-flight"


def test_a_local_step_trips_neither_floor_nor_strict(
    database: Database, fake: FakeLoadCoach
) -> None:
    """A debit that carried no estimate at all is unpriced and **not** untotalled.

    That distinction is what keeps a mixed trajectory running under ``strict``: the rule is about
    an estimate that could not be totalled, never about work that was never priced (ADR-0069).
    """
    harness = Harness(_settings(partial_pricing="strict"), database, fake, Clock())
    _two_turns(harness.fake, input_tokens=800, output_tokens=200)
    trajectory_id, state = harness.run()
    assert state is TrajectoryState.COMPLETED
    verdict = next(
        one for one in harness.entries(trajectory_id)[0].verdicts if one.ceiling.money is not None
    )
    assert verdict.unpriced_debit_count == 1
    assert verdict.untotalled_debit_count == 0, "no estimate is not a failed estimate"
    assert verdict.exceeded is False


def test_unpriced_local_usage_renders_an_em_dash_and_never_a_zero(harness: Harness) -> None:
    """Spec §20 acceptance criterion 1, at the renderer every surface goes through.

    ``$0.00`` would say the work was free. It was not free; its cost is simply unknowable, which
    is a different thing and is what the em dash says (ADR-0016).
    """
    trajectory_id, state = harness.run()
    assert state is TrajectoryState.COMPLETED
    position = harness.budget.position(harness.service.get(trajectory_id))
    trajectory = position.headroom[0]
    money = next(
        one for one in harness.entries(trajectory_id)[0].verdicts if one.ceiling.money is not None
    )
    assert money.money_spent is None, "nothing was priced, and None is not zero"
    assert render_money(money.money_spent, is_floor=money.unpriced_debit_count > 0) == NOT_PRICED
    assert "0.00" not in render_money(None, is_floor=False)
    assert trajectory.money_is_floor is True, "the money figure is a lower bound, not a total"
