"""promptcadence.services.estimates — what a step is expected to cost, and where that came from.

The layered estimator of [lifecycle §6](../../docs/apps/promptcadence/lifecycle.md), whose whole
point is that **its source is always recorded**:

.. code-block:: text

    estimate = p80 of observed usage for (tier, task_profile)   when >= estimate_min_samples exist
                                                                -> source "historical"
               the tier's configured per-step defaults           otherwise
                                                                -> source "configured_default"

**A model-generated number is never an input** (D-3 / ADR-0047). This module reads exactly two
things — recorded ledger entries and configuration — and there is no parameter, field or code path
through which a response, a completion, a tool result or any other model output can reach it. A
number the model invented must not size the budget that constrains the model, and
``tests/unit/test_estimator_takes_no_model_output.py`` asserts that the module cannot even see one.

**Observed *usage*, not observed cost.** LoadLedger stores a debit's token counts and its pricing
hash and re-derives money from them (ADR-0030 rule 1), so an entry read back carries usage and no
money figure. That is not a gap to work around: the historical estimate is a usage estimate, and
the money it implies is *derived* by pricing it — the same operation, against the same price
record, that costs a real turn. An estimator that had stored money would be the second place a
money figure lived, and a price correction would leave the two disagreeing.

**p80 per class, in integers.** The percentile is taken over each token class separately rather
than over a single total, because the classes are priced at different rates and a total split by a
fixed ratio would be a magic number standing between an operator's history and the cap that binds
them. The index is ``ceil(0.8 x n) - 1`` over the sorted samples — computed with integer division,
because a float in a budget is how a total stops equalling the sum of its own rows. A class no
sample reported is estimated as ``0``: nothing observed it being used, and an ``UNSUPPORTED``
estimate would make every estimate untotalled and halt every ``strict`` trajectory at its first
pre-flight.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from baseaicore import TokenUsage, is_supported

from promptcadence.domain.policy import EstimateSource, StepEstimate
from promptcadence.services.budget import tier_tag

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from loadledger import LedgerEntry

    from promptcadence.config import Settings
    from promptcadence.services.budget import BudgetService, PricedUsage

__all__ = ["StepEstimator", "p80"]

_CLASSES: Final = ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")


def p80(samples: Sequence[int]) -> int:
    """Return the 80th percentile of ``samples`` by the nearest-rank method.

    Args:
        samples: Observed counts. Order does not matter; the function sorts.

    Returns:
        The sample at rank ``ceil(0.8 x n)``, one-based — the smallest observation at least 80 % of
        the observations are at or below. ``0`` for an empty sequence, which is the honest answer
        when nothing was observed and is never reached from
        :meth:`StepEstimator.estimate`, whose historical rung requires ``estimate_min_samples``
        observations first.

    Raises:
        ValueError: If any sample is negative. A negative token count is not a small estimate, it
            is a corrupt one.
    """
    if any(sample < 0 for sample in samples):
        message = f"token samples must not be negative; got {sorted(samples)[:5]}"
        raise ValueError(message)
    if not samples:
        return 0
    ordered = sorted(samples)
    # ceil(0.8 * n) with integer arithmetic only: -(-a // b) is the ceiling of a / b.
    rank = -(-8 * len(ordered) // 10)
    return ordered[max(rank, 1) - 1]


class StepEstimator:
    """The layered estimator, over recorded entries and configuration and nothing else."""

    __slots__ = ("_budget", "_clock", "_settings")

    def __init__(
        self, budget: BudgetService, settings: Settings, *, clock: Callable[[], datetime]
    ) -> None:
        """Bind the estimator to the ledger it reads and the configuration it falls back to.

        Args:
            budget: The budget service, used only to read ``entries()``. The estimator writes
                nothing and holds no balance of its own.
            settings: The validated configuration: ``[budget] estimate_min_samples`` and each
                tier's per-step defaults.
            clock: The instant source. An estimate is priced at "now", because it is a statement
                about a call that has not happened yet.
        """
        self._budget = budget
        self._settings = settings
        self._clock = clock

    def estimate(self, *, tier: str) -> tuple[StepEstimate, PricedUsage]:
        """Estimate the next step on ``tier``, and say which rung of the ladder answered.

        Args:
            tier: The tier the step would run on. Its ``tier:<name>`` tag is the estimator's key;
                a tier names exactly one LoadCoach task profile in configuration, so the
                ``(tier, task_profile)`` pair lifecycle §6 names is the tier.

        Returns:
            The :class:`~promptcadence.domain.policy.StepEstimate` for the record — carrying its
            source and the sample count behind it — and the
            :class:`~promptcadence.services.budget.PricedUsage` the pre-flight asks the ledger
            about. The money estimate is ``None`` on a local tier, whose cost is ``UNSUPPORTED``
            and never ``$0.00`` (ADR-0016), and ``None`` on a remote tier whose estimate could not
            be totalled — an estimate is not a floor to render, it is either a figure or absent.
        """
        entries = self._budget.entries(tag=tier_tag(tier))
        minimum = self._settings.budget.estimate_min_samples
        if len(entries) >= minimum:
            usage = _p80_usage(entries)
            source, samples = EstimateSource.HISTORICAL, len(entries)
        else:
            usage = self._configured(tier)
            source, samples = EstimateSource.CONFIGURED_DEFAULT, 0
        priced = self._budget.price_estimate(tier=tier, usage=usage, at=self._clock())
        money = (
            priced.cost.total
            if priced.cost is not None and is_supported(priced.cost.total)
            else None
        )
        estimate = StepEstimate(
            token_estimate=_total(usage),
            money_estimate=money,
            source=source,
            sample_count=samples,
        )
        return estimate, priced

    def _configured(self, tier: str) -> TokenUsage:
        """The tier's configured per-step defaults as a usage, or a conservative pair if unknown.

        A tier absent from configuration cannot be run — the router refuses it before this is
        reached — so the fallback exists to keep this function total rather than to be used.
        """
        configured = self._settings.tiers.get(tier)
        if configured is None:
            return TokenUsage(input_tokens=0, output_tokens=0)
        return TokenUsage(
            input_tokens=configured.default_step_input_tokens,
            output_tokens=configured.default_step_output_tokens,
            cache_write_tokens=0,
            cache_read_tokens=0,
        )


def _p80_usage(entries: Sequence[LedgerEntry]) -> TokenUsage:
    """The p80 of each token class over the recorded entries, as one usage."""
    counts: dict[str, int] = {}
    for name in _CLASSES:
        samples = [
            int(count)
            for entry in entries
            if is_supported(count := getattr(entry.debit.usage, name))
        ]
        counts[name] = p80(samples)
    return TokenUsage(**counts)


def _total(usage: TokenUsage) -> int:
    """Sum the reported classes. An unreported class contributes nothing, never a zero it claims."""
    return sum(int(count) for count in usage.as_counts().values() if is_supported(count))
