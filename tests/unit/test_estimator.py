"""Phase 5's estimator: the ladder, the threshold, and the input it must never have.

Two claims, asserted separately because they fail differently. The **ladder** is arithmetic —
which rung answers, at exactly which sample count, and what label it wears. The **input rule** is
structural: D-3 / ADR-0047 says a model-generated number is never an estimator input, and a test
that only checked today's behaviour would pass on the day someone adds the parameter that breaks
it. So the second half reads the module's own imports and signature.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from baseaicore import TokenUsage, new_id
from tests.conftest import budget_for

from promptcadence.config import Settings, load_settings
from promptcadence.domain.policy import EstimateSource
from promptcadence.services.budget import BudgetService, PricedUsage
from promptcadence.services.database import Database, ensure_ready
from promptcadence.services.estimates import StepEstimator, p80
from promptcadence.services.events import TrajectoryEventSink
from promptcadence.services.trajectories import TrajectoryService, TrajectorySubmission

if TYPE_CHECKING:
    from collections.abc import Iterator

_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
_ESTIMATES = Path(__file__).resolve().parents[2] / "src/promptcadence/services/estimates.py"


class Clock:
    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return _NOW + timedelta(milliseconds=self._tick)


@pytest.fixture
def settings() -> Settings:
    loaded = load_settings().settings
    tiers = {
        name: tier.model_copy(
            update={"default_step_input_tokens": 700, "default_step_output_tokens": 300}
        )
        for name, tier in loaded.tiers.items()
    }
    return loaded.model_copy(update={"tiers": tiers})


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    handle = Database.from_url(f"sqlite:///{tmp_path / 'estimates.sqlite3'}")
    ensure_ready(handle, auto_migrate=True)
    try:
        yield handle
    finally:
        handle.close()


def _seed(budget: BudgetService, database: Database, settings: Settings, *, samples: int) -> None:
    """Record ``samples`` debits on ``local_fast``, one per turn, with a spread of usages.

    Real debits through the real ledger, not fabricated rows: the estimator reads ``entries()``,
    and a test that wrote the rows itself would be testing its own fixture.
    """
    clock = Clock()
    sink = TrajectoryEventSink(database, clock=clock)
    service = TrajectoryService(database, sink, settings, budget=budget, clock=clock)
    view = service.submit(TrajectorySubmission(task="seed", bypass_planning=True))
    for index in range(samples):
        usage = TokenUsage(
            input_tokens=100 * (index + 1),
            output_tokens=10 * (index + 1),
            cache_write_tokens=0,
            cache_read_tokens=0,
        )
        with database.write() as session:
            budget.debit(
                session,
                view=view,
                turn_id=new_id(),
                tier="local_fast",
                priced=PricedUsage(usage=usage, cost=None),
                at=clock(),
            )


@pytest.mark.parametrize(
    ("samples", "source", "expected_tokens"),
    [
        (19, EstimateSource.CONFIGURED_DEFAULT, 1_000),
        (20, EstimateSource.HISTORICAL, 1_760),
    ],
)
def test_the_source_flips_exactly_at_the_sample_threshold(
    database: Database,
    settings: Settings,
    samples: int,
    source: EstimateSource,
    expected_tokens: int,
) -> None:
    """Both sides of ``estimate_min_samples``, which defaults to 20.

    At 19 observations the configured 700+300 default answers; at 20 the p80 of each class does —
    rank ``ceil(0.8 x 20) = 16``, so the 16th smallest input (1 600) and output (160), 1 760
    together. The threshold is ``>=``, and testing only one side of it would pass on ``>``.
    """
    clock = Clock()
    budget = budget_for(database, settings, clock=clock)
    _seed(budget, database, settings, samples=samples)
    assert settings.budget.estimate_min_samples == 20
    estimate, priced = StepEstimator(budget, settings, clock=clock).estimate(tier="local_fast")
    assert estimate.source is source
    assert estimate.token_estimate == expected_tokens
    assert estimate.sample_count == (samples if source is EstimateSource.HISTORICAL else 0)
    assert priced.cost is None, "a local tier prices nothing, so the estimate carries no money"


def test_a_historical_estimate_always_names_the_samples_behind_it(
    database: Database, settings: Settings
) -> None:
    """A "historical" estimate with nothing behind it is a configured default wearing a label."""
    clock = Clock()
    budget = budget_for(database, settings, clock=clock)
    _seed(budget, database, settings, samples=25)
    estimate, _ = StepEstimator(budget, settings, clock=clock).estimate(tier="local_fast")
    assert estimate.source is EstimateSource.HISTORICAL
    assert estimate.sample_count == 25


def test_a_tier_with_no_history_of_its_own_uses_its_own_configured_default(
    database: Database, settings: Settings
) -> None:
    """The estimator's key is the tier tag, so one tier's history never sizes another's step."""
    clock = Clock()
    budget = budget_for(database, settings, clock=clock)
    _seed(budget, database, settings, samples=25)
    estimator = StepEstimator(budget, settings, clock=clock)
    other, _ = estimator.estimate(tier="local_large")
    assert other.source is EstimateSource.CONFIGURED_DEFAULT
    assert other.token_estimate == 1_000


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        ([5], 5),
        ([1, 2, 3, 4, 5], 4),
        (list(range(1, 11)), 8),
        (list(range(1, 21)), 16),
        ([7, 7, 7], 7),
    ],
)
def test_p80_is_the_nearest_rank_percentile_in_whole_integers(
    samples: list[int], expected: int
) -> None:
    """``ceil(0.8 x n)``, one-based, computed without a single division into a float."""
    assert p80(samples) == expected


def test_p80_refuses_a_negative_sample() -> None:
    """A negative token count is not a small estimate; it is a corrupt one."""
    with pytest.raises(ValueError, match="must not be negative"):
        p80([10, -1])


# ---------------------------------------------------------------------------------------------
# D-3 / ADR-0047: a model-generated number is never an estimator input
# ---------------------------------------------------------------------------------------------


def test_the_estimator_takes_only_a_tier_name(database: Database, settings: Settings) -> None:
    """There is no parameter through which a model's output could reach the estimate.

    The signature *is* the guarantee. A ``response``, ``text`` or ``estimated_cost`` argument here
    would be the defect ADR-0047 names — the thing being constrained supplying the constraint —
    and it would arrive as an innocuous-looking parameter, not as an obviously wrong one.
    """
    signature = inspect.signature(StepEstimator.estimate)
    assert list(signature.parameters) == ["self", "tier"]
    assert signature.parameters["tier"].annotation == "str"


def test_the_estimator_module_cannot_see_a_model_response_at_all() -> None:
    """Structural, not behavioural: the module does not import the client that holds one.

    ``promptcadence.infrastructure.loadcoach`` is where a ``GenerationResponse`` — the only object
    in this application carrying a number a model produced — lives. The estimator importing it
    would not be a bug yet, but it is the step before one, and this is the cheapest place to
    refuse it.
    """
    tree = ast.parse(_ESTIMATES.read_text(encoding="utf-8"), filename=str(_ESTIMATES))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("loadcoach" in name for name in imported), sorted(imported)
    assert not any(name.endswith(".threads") or name.endswith(".turns") for name in imported)
