"""promptcadence.services.budget — the ledger, the three ceilings, and what binds.

LoadLedger keeps the balances and decides every ``exceeded``; this module decides **what the
ceilings are** and **what PromptCadence does about a verdict**. That split is the whole of
ADR-0050: the package accumulates and judges, the application configures and reacts. Nothing here
adds up tokens, resolves a UTC-day window or compares a balance to a cap — a helper that did would
be ledger logic living in an application, which is the mistake the mount exists to avoid.

**Three ceilings may be active at once and the most restrictive binds.** A labelled trajectory has
its own (``per_run``, from the request's ``budget`` or the configured default), the ``per_day``
ceiling every trajectory shares, and its project's (``per_tag`` on ``project:<name>``, a lifetime
cap that never resets). Resolving them needs no arithmetic: every ceiling is evaluated, each
verdict says which cap fired, and *any* ``exceeded`` binds. :meth:`BudgetService.position` returns
all three so the record shows the balance against each, not only against the one that stopped it.

**The per-day ceiling binds money and not tokens, deliberately** (spec §11.5, lifecycle §6). It is
what lets any amount of work run while only so much is *spent* in a day: local work is unpriced and
never counts against it. A per-day token ceiling would make the local half of a mixed installation
stop at midnight for no reason anyone budgeted, so ``[budget]`` has no ``daily_token_ceiling`` and
this module does not invent one.

**Debits store usage and a pricing hash, never money** (ADR-0030 rule 1). :meth:`BudgetService.
debit` rebuilds :class:`~baseaicore.TokenUsage` from **all four** classes on LoadCoach's job
document (ADR-0070, row C6) — a class the protocol cannot bill is ``0``, a class that is simply
missing stays ``UNSUPPORTED`` and is excluded from the balance rather than counted as zero — prices
it against the tier's own price record at the instant the turn happened, and hands both to the
ledger. The money is re-derived from those two facts whenever a price is corrected, and is never
the stored fact.

**A local step's cost is ``UNSUPPORTED``, never ``$0.00``** (ADR-0016). A local tier holds no price
records, so its debit carries ``cost=None``: the tokens accumulate against every token ceiling and
no money balance moves. Rendering that as ``$0.00`` would claim the work was free; every surface in
this application renders it ``—``.

**Reconciliation is idempotent by ``source_ref``.** The turn row is the source of truth and the
debit is a separate write after it, so a crash between the two loses the debit and never the turn.
:meth:`BudgetService.undebited_turn_ids` asks the ledger which turns it has already seen, and
recovery debits only the rest — run it twice and the second run does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from baseaicore import Money, estimate_cost, is_supported
from loadledger import (
    BudgetCeiling,
    CeilingScope,
    CurrencyMismatch,
    Debit,
    PartialPricing,
    UnknownRun,
    utc_day_start,
)
from loadledger.sql import SqlLedger
from sqlalchemy.orm import Session

from promptcadence.domain.policy import BudgetDebited, BudgetHeadroom
from promptcadence.domain.policy import PartialPricing as DomainPartialPricing

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from baseaicore import CostEstimate, TokenUsage
    from loadledger import CeilingVerdict, LedgerEntry

    from promptcadence.config import Settings
    from promptcadence.services.database import Database
    from promptcadence.services.pricing import PricingCatalog
    from promptcadence.services.views import TrajectoryView

__all__ = [
    "TIER_TAG_PREFIX",
    "BudgetPosition",
    "BudgetService",
    "CurrencyMismatchError",
    "PricedUsage",
    "project_tag",
    "tier_tag",
]

TIER_TAG_PREFIX: Final = "tier:"
_UNTOTALLED_IS_WORST: Final = 2**62
"""A sentinel rank, not an amount. Never rendered, never stored, never compared to real money."""
_PROJECT_TAG_PREFIX: Final = "project:"


def tier_tag(tier: str) -> str:
    """Return the tag every debit carries for its tier — the estimator's key (lifecycle §6)."""
    return f"{TIER_TAG_PREFIX}{tier}"


def project_tag(project: str) -> str:
    """Return the tag a project's ``per_tag`` ceiling binds on."""
    return f"{_PROJECT_TAG_PREFIX}{project}"


class CurrencyMismatchError(ValueError):
    """A debit priced in a currency an active money ceiling caps in another. Never converted."""


@dataclass(frozen=True, slots=True)
class PricedUsage:
    """One turn's usage and what it was estimated to cost, ready to become a debit.

    Attributes:
        usage: The four token classes as LoadCoach reported them, ``UNSUPPORTED`` where it
            reported none (ADR-0070).
        cost: The estimate, or ``None`` when no pricing was applied at all — the local case, and
            the case of a remote tier whose price list does not cover the answering model.
        unpriced_reason: Why ``cost`` is ``None``, or why the estimate did not total. Empty when
            the estimate is complete. Carried so a surface can say *why* a figure is a floor.
    """

    usage: TokenUsage
    cost: CostEstimate | None
    unpriced_reason: str = ""


@dataclass(frozen=True, slots=True)
class BudgetPosition:
    """What every active ceiling says right now, in the domain's own vocabulary.

    Attributes:
        headroom: One :class:`~promptcadence.domain.policy.BudgetHeadroom` per active ceiling, in
            configuration order — trajectory, day, then project. All of them, always: an entry
            records its balance against **each** active ceiling, not only against the one that
            stopped it.
    """

    headroom: tuple[BudgetHeadroom, ...]
    step_is_priced: bool = True
    """Whether the step this position was taken for would spend priced usage.

    ``False`` for a step on a local tier, whose cost is ``UNSUPPORTED`` and never ``$0.00``, and
    it changes which ceilings can refuse: **money ceilings bind priced usage** (ADR-0047 §3), so a
    money cap someone else's remote work exhausted must not stop a local step that cannot add a
    nano to it. The token ceiling is the universal brake and binds every step on every tier.

    A *balance report* — :meth:`BudgetService.position` — leaves this ``True``: it describes what
    the ceilings say, not what one particular step may do.
    """

    @property
    def binding(self) -> BudgetHeadroom | None:
        """The most restrictive ceiling that refuses further work, or ``None`` if none does.

        "Most restrictive" needs no arithmetic: any ceiling whose
        :attr:`~promptcadence.domain.policy.BudgetHeadroom.binds` is true refuses, and the first
        such in configuration order is reported so the cause names one cap rather than a set —
        except that for an unpriced step a ceiling refuses only through its *token* bound, per
        :attr:`step_is_priced`.
        """
        return next((one for one in self.headroom if self._refuses(one)), None)

    def _refuses(self, headroom: BudgetHeadroom) -> bool:
        """Whether this one ceiling refuses the step this position was taken for."""
        if not headroom.binds:
            return False
        if self.step_is_priced:
            return True
        return headroom.tokens_remaining is not None and headroom.tokens_remaining < 0

    @property
    def daily_binds(self) -> bool:
        """Whether it is the ``per_day`` ceiling that refuses — the one that waiting can fix."""
        binding = self.binding
        return binding is not None and binding.scope == "day"


class BudgetService:
    """The application's half of the budget: ceilings, debits, verdicts and reconciliation.

    Stateless and cheap. :class:`~loadledger.sql.SqlLedger` caches nothing between calls, so a
    ledger is built per operation from the ceilings that trajectory has *now* — which is what makes
    a per-request override and a per-project cap resolvable at all. The persisted balance key knows
    nothing about ceilings, so a ceiling configured today binds on the whole history rather than on
    the history since it was configured.
    """

    __slots__ = ("_clock", "_database", "_pricing", "_settings")

    def __init__(
        self,
        database: Database,
        settings: Settings,
        pricing: PricingCatalog,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        """Bind the service to the process's handles.

        Args:
            database: The application's database handle; the ledger's tables live in it.
            settings: The validated configuration — every ceiling's source.
            pricing: The price observations loaded at startup, one set per configured tier.
            clock: The instant source, injected and required. Every window, day edge and expiry
                test in this phase depends on it, and a ledger reading the system clock could not
                be tested across a UTC midnight.
        """
        self._database = database
        self._settings = settings
        self._pricing = pricing
        self._clock = clock

    # ---- ceilings ---------------------------------------------------------------------------

    def partial_pricing_for(self, view: TrajectoryView) -> PartialPricing:
        """Return the ``partial_pricing`` rule binding this trajectory's money ceilings.

        The per-request override if the request set one, else ``[budget] partial_pricing``. It
        rides on every money ceiling rather than on the trajectory, so ``would_exceed`` applies it
        at pre-flight and every verdict shows which rule it was judged under (ADR-0069).
        """
        configured = view.partial_pricing or self._settings.budget.partial_pricing
        return PartialPricing(configured)

    def ceilings_for(self, view: TrajectoryView) -> tuple[BudgetCeiling, ...]:
        """Build the ceilings active on one trajectory, in the order verdicts come back in.

        Args:
            view: The trajectory, carrying its own budget and its project label.

        Returns:
            The trajectory's own ``per_run`` ceiling, the shared ``per_day`` ceiling, and — when
            the trajectory is labelled — its project's ``per_tag`` ceiling. Between one and three
            ceilings; never zero, because the token ceiling always has a configured default and is
            the universal brake.

        Raises:
            InvalidCeiling: If a configured ceiling binds neither money nor tokens. Startup
                validation already refuses that for a project, and the trajectory's own always
                binds tokens, so reaching this is a configuration path that got past ``config``.
        """
        budget = self._settings.budget
        rule = self.partial_pricing_for(view)
        ceilings = [
            BudgetCeiling(
                scope=CeilingScope.PER_RUN,
                money=view.money_budget,
                tokens=view.token_budget,
                partial_pricing=rule if view.money_budget is not None else PartialPricing.FLOOR,
            ),
            BudgetCeiling(
                scope=CeilingScope.PER_DAY,
                money=_money(budget.daily_money_ceiling),
                partial_pricing=rule,
            ),
        ]
        if view.project is not None:
            configured = self._settings.budget.projects[view.project]
            project_money = _money(configured.money_ceiling) if configured.money_ceiling else None
            ceilings.append(
                BudgetCeiling(
                    scope=CeilingScope.PER_TAG,
                    tag=project_tag(view.project),
                    money=project_money,
                    tokens=configured.token_ceiling,
                    partial_pricing=rule if project_money is not None else PartialPricing.FLOOR,
                )
            )
        return tuple(ceilings)

    def tags_for(self, view: TrajectoryView, *, tier: str) -> tuple[str, ...]:
        """Return the tags every debit on this turn carries: its tier, and its project if any."""
        if view.project is None:
            return (tier_tag(tier),)
        return (tier_tag(tier), project_tag(view.project))

    # ---- the ledger -------------------------------------------------------------------------

    def ledger(
        self, *, ceilings: Sequence[BudgetCeiling] = (), session: Session | None = None
    ) -> SqlLedger:
        """Build a ledger over the mounted tables.

        Args:
            ceilings: The ceilings to evaluate. Empty for a read that needs no verdict — the
                ledger still accumulates and still answers ``entries``, it simply never refuses.
            session: A session to join, or ``None`` for the ledger's own unit of work. When given,
                the ledger's writes land inside the caller's transaction as a savepoint, so a
                debit and the ``budget.debited`` event announcing it commit together or not at all
                (ADR-0044 applied to money).

        Returns:
            A ledger bound to this application's tables and injected clock.
        """
        if session is None:
            factory: Callable[[], Session] = self._database.sessions
        else:
            connection = session.connection()

            def factory() -> Session:
                return Session(bind=connection, join_transaction_mode="create_savepoint")

        return SqlLedger(factory, tuple(ceilings), clock=self._clock)

    def declare(self, session: Session, trajectory_id: str) -> None:
        """Register a trajectory with the ledger, at creation and before plan approval.

        Called from ``TrajectoryService.submit`` inside the same write that persists the
        trajectory row, and from nowhere else. Declaring at creation rather than at the first turn
        is what makes a pre-flight check safe on a trajectory that has spent nothing: a run exists
        "once debited **or** declared" (LoadLedger spec §13), and without the declaration the very
        first ``would_exceed`` would raise :class:`~loadledger.UnknownRun` on every trajectory in
        the system.

        Args:
            session: The caller's session; the declaration joins its transaction.
            trajectory_id: The run identity, which for this application is the trajectory id.
        """
        self.ledger(session=session).declare_run(trajectory_id)

    # ---- verdicts ---------------------------------------------------------------------------

    def position(self, view: TrajectoryView) -> BudgetPosition:
        """Return every active ceiling's current balance for this trajectory.

        Args:
            view: The trajectory.

        Returns:
            The position. A trajectory the ledger has never seen reports full headroom rather
            than raising: ``submit`` declares every trajectory, so an unknown run here is a row
            written before this phase existed, and reporting it as "nothing spent" is both true
            and the only useful answer.
        """
        ceilings = self.ceilings_for(view)
        try:
            verdicts = self.ledger(ceilings=ceilings).remaining(view.trajectory_id)
        except UnknownRun:
            return BudgetPosition(headroom=tuple(_empty(ceiling) for ceiling in ceilings))
        return BudgetPosition(headroom=tuple(_headroom(verdict) for verdict in verdicts))

    def preflight(
        self, view: TrajectoryView, *, tier: str, estimate: PricedUsage
    ) -> BudgetPosition:
        """Ask what every ceiling would say if this step's estimated spend were recorded now.

        Side-effect-free, and therefore safe to call before every turn. This is where
        ``partial_pricing = "strict"`` refuses: a window holding an estimate that did not total
        exceeds its money ceiling *before* the call, so the cap is never crossed (ADR-0069). Under
        ``floor`` the same window continues and the brake may fire late by the unreported portion.

        Args:
            view: The trajectory.
            tier: The tier the step would run on; its ``tier:<name>`` tag rides the prospective
                spend so a per-tag ceiling sees it.
            estimate: The step's estimated usage and cost.

        Returns:
            The prospective position; :attr:`BudgetPosition.binding` is what refuses.

        Raises:
            CurrencyMismatchError: If the estimate is priced in a currency an active money ceiling
                caps in another. Refused, never converted (ADR-0030 rule 3).
        """
        ceilings = self.ceilings_for(view)
        ledger = self.ledger(ceilings=ceilings)
        try:
            verdicts = ledger.would_exceed(
                view.trajectory_id,
                usage=estimate.usage,
                cost=estimate.cost,
                tags=self.tags_for(view, tier=tier),
            )
        except UnknownRun:
            return BudgetPosition(
                headroom=tuple(_empty(ceiling) for ceiling in ceilings),
                step_is_priced=estimate.cost is not None,
            )
        except CurrencyMismatch as exc:
            raise CurrencyMismatchError(exc.message) from exc
        return BudgetPosition(
            headroom=tuple(_headroom(verdict) for verdict in verdicts),
            step_is_priced=estimate.cost is not None,
        )

    # ---- pricing and debiting ---------------------------------------------------------------

    def price(
        self, *, tier: str, canonical_id: str, usage: TokenUsage, at: datetime
    ) -> PricedUsage:
        """Cost one turn's usage against the tier's own price record at the instant it happened.

        Args:
            tier: The tier that ran the turn.
            canonical_id: LoadCoach's ``model.canonical_id`` for the model that answered.
            usage: The four token classes as reported.
            at: When the turn happened — not when this runs, so re-costing history later
                reproduces the same figure (ADR-0030).

        Returns:
            The usage with its estimate, or with ``cost=None`` and a reason. ``None`` is returned
            for a local tier, which holds no price records and whose cost is ``UNSUPPORTED`` and
            never ``$0.00`` (ADR-0016); and for a remote tier whose price list does not cover the
            answering model, which is unpriced egress the caller refuses rather than treats as
            free.
        """
        pricing = self._pricing.for_model(tier=tier, canonical_id=canonical_id, at=at)
        if pricing is None:
            return PricedUsage(
                usage=usage,
                cost=None,
                unpriced_reason=_no_price_reason(tier, self._settings, at, model=canonical_id),
            )
        cost = estimate_cost(usage, pricing, at=at)
        reason = "; ".join(cost.unpriced_reasons) if not cost.is_complete else ""
        return PricedUsage(usage=usage, cost=cost, unpriced_reason=reason)

    def price_estimate(self, *, tier: str, usage: TokenUsage, at: datetime) -> PricedUsage:
        """Cost a *prospective* step's usage against the worst price the tier still claims.

        A pre-flight has no model identity to price against: which model answers is LoadCoach's
        choice and is not known until it has answered. So an estimate is costed against **every**
        record the tier still claims and the largest total wins. Pricing an estimate at the tier's
        worst case is the only rule that cannot under-state a budget, and under-stating is the
        failure that matters — an over-stated estimate refuses a step that would have fitted and
        says which cap refused it, while an under-stated one crosses the cap silently.

        Args:
            tier: The tier the step would run on.
            usage: The estimated token usage.
            at: The instant to price at — "now", because the call has not happened yet.

        Returns:
            The usage with the most expensive estimate the tier's price list yields, or with
            ``cost=None`` and a reason when the tier states no price at all. A local tier always
            lands in the second case, which is correct: a local model's cost is ``UNSUPPORTED``,
            never ``$0.00`` (ADR-0016), and only the token ceiling can bind it.
        """
        candidates = self._pricing.claiming(tier=tier, at=at)
        if not candidates:
            return PricedUsage(
                usage=usage, cost=None, unpriced_reason=_no_price_reason(tier, self._settings, at)
            )
        estimates = [estimate_cost(usage, record, at=at) for record in candidates]
        priced = max(estimates, key=lambda estimate: _sortable_total(estimate))
        reason = "; ".join(priced.unpriced_reasons) if not priced.is_complete else ""
        return PricedUsage(usage=usage, cost=priced, unpriced_reason=reason)

    def debit(
        self,
        session: Session,
        *,
        view: TrajectoryView,
        turn_id: str,
        tier: str,
        priced: PricedUsage,
        at: datetime,
    ) -> BudgetDebited:
        """Record one turn's spend, inside the caller's transaction.

        Args:
            session: The caller's session. The debit rides its transaction as a savepoint, so the
                ledger rows and the ``budget.debited`` event this returns commit together.
            view: The trajectory, which supplies the run id, the ceilings and the project tag.
            turn_id: The debit's ``source_ref``. Reconciliation is idempotent by it, so it must be
                the turn's own id and never a fresh one.
            tier: The tier that ran the turn; becomes the ``tier:<name>`` tag.
            priced: The usage and its estimate.
            at: When the spend happened. A ``per_day`` verdict is resolved against this, not
                against "now", so a reconciled turn affects the day it actually landed in.

        Returns:
            The ``budget.debited`` event body, carrying the four token classes, the pricing hash
            and every active ceiling's verdict after the debit. No money figure: what is stored is
            usage plus a hash, and the money is re-derived from them (ADR-0030 rule 1).

        Raises:
            CurrencyMismatchError: If the estimate is priced in a currency an active money ceiling
                caps in another. Refused before anything is written, so the debit leaves no trace.
        """
        ceilings = self.ceilings_for(view)
        debit = Debit(
            run_id=view.trajectory_id,
            source_ref=turn_id,
            usage=priced.usage,
            cost=priced.cost,
            tags=self.tags_for(view, tier=tier),
            occurred_at=at,
        )
        try:
            entry = self.ledger(ceilings=ceilings, session=session).debit(debit)
        except CurrencyMismatch as exc:
            raise CurrencyMismatchError(exc.message) from exc
        return BudgetDebited(
            trajectory_id=view.trajectory_id,
            turn_id=turn_id,
            entry_id=entry.entry_id,
            tier=tier,
            project=view.project,
            usage={
                name: (count if is_supported(count) else "unsupported")
                for name, count in priced.usage.as_counts().items()
            },
            unpriced=entry.unpriced,
            pricing_hash=entry.pricing_hash,
            headroom=tuple(_headroom(verdict) for verdict in entry.verdicts),
        )

    # ---- reads and reconciliation -----------------------------------------------------------

    def entries(
        self, *, run_id: str | None = None, tag: str | None = None, since: datetime | None = None
    ) -> Sequence[LedgerEntry]:
        """Return recorded entries, oldest first, narrowed by whichever filters are given."""
        return self.ledger().entries(run_id=run_id, tag=tag, since=since)

    def debited_turn_ids(self, trajectory_id: str) -> frozenset[str]:
        """Return the turn ids this trajectory already has a debit for.

        The whole of reconciliation's idempotence. The turn row is the source of truth and the
        debit is written after it, so a crash in between leaves a turn with no debit; recovery
        asks this which turns are already accounted for and debits only the rest. Run recovery
        twice and the second run finds every turn here and changes nothing.
        """
        return frozenset(entry.debit.source_ref for entry in self.entries(run_id=trajectory_id))

    # ---- the daily window -------------------------------------------------------------------

    def next_day_edge(self, after: datetime) -> datetime:
        """Return the next UTC-day boundary strictly after ``after``.

        The edge a parked trajectory waits for (T15/T16). Resolved through LoadLedger's own
        :func:`~loadledger.utc_day_start` so the boundary a park waits on and the boundary the
        ``per_day`` balance resets at are the same instant computed by the same code — a
        second implementation here would be a budget that reset at a different moment than the
        one the worker woke for.
        """
        return utc_day_start(after) + timedelta(days=1)


def _sortable_total(estimate: CostEstimate) -> int:
    """Order estimates by their total, treating an untotalled one as the largest.

    An estimate that could not be totalled is not a small one — it is an unknown one, and choosing
    it as the worst case is what keeps ``strict`` honest: a tier holding one price list that cannot
    cover the call is a tier whose pre-flight must say so rather than quietly pick the list that
    happens to total.
    """
    return estimate.total.nanos if is_supported(estimate.total) else _UNTOTALLED_IS_WORST


def _no_price_reason(
    tier: str, settings: Settings, at: datetime, *, model: str | None = None
) -> str:
    """Say why nothing could be priced, distinguishing "local" from "not covered"."""
    configured = settings.tiers.get(tier)
    if configured is None or not configured.remote:
        return f"tier {tier!r} is local; a local model's cost is UNSUPPORTED, not $0.00"
    named = f" for {model!r}" if model else ""
    return (
        f"tier {tier!r} names a pricing file that states no rate{named} at {at.isoformat()}; "
        "unpriced egress is refused, not free (ADR-0030)"
    )


def _money(amount: object) -> Money | None:
    """Convert a configured ``MoneyAmount`` to :class:`~baseaicore.Money`, or ``None`` for zero.

    A zero configured ceiling means "no money ceiling here", not "spend nothing": a zero cap is
    exceeded before anything is spent, and an operator who wrote ``nanos = 0`` meant to disable
    the cap rather than to refuse every trajectory (``domain.trajectory`` refuses a zero
    ``money_budget`` on a request for the same reason).
    """
    if amount is None:
        return None
    currency = getattr(amount, "currency", None)
    nanos = getattr(amount, "nanos", 0)
    if not currency or nanos <= 0:
        return None
    return Money(currency=currency, nanos=nanos)


def _scope_label(verdict: CeilingVerdict) -> str:
    """Name a ceiling for a reader: ``trajectory``, ``day``, or ``project:<name>``."""
    if verdict.ceiling.scope is CeilingScope.PER_RUN:
        return "trajectory"
    if verdict.ceiling.scope is CeilingScope.PER_DAY:
        return "day"
    return verdict.ceiling.tag or "tag"


def _headroom(verdict: CeilingVerdict) -> BudgetHeadroom:
    """Adapt one LoadLedger verdict into the domain's shape, field for field.

    A copy rather than an interpretation, which is why
    :class:`~promptcadence.domain.policy.BudgetHeadroom` was built at Phase 2 to mirror
    ``CeilingVerdict``. The three honesty counts come across intact: dropping them would leave an
    approval judging a **floor** while believing it a total, which is exactly what ADR-0069 exists
    to prevent.
    """
    return BudgetHeadroom(
        scope=_scope_label(verdict),
        exceeded=verdict.exceeded,
        money_remaining=verdict.money_remaining,
        tokens_remaining=verdict.tokens_remaining,
        unpriced_debit_count=verdict.unpriced_debit_count,
        untotalled_debit_count=verdict.untotalled_debit_count,
        unmetered_debit_count=verdict.unmetered_debit_count,
        partial_pricing=DomainPartialPricing(verdict.ceiling.partial_pricing.value),
    )


def _empty(ceiling: BudgetCeiling) -> BudgetHeadroom:
    """The headroom of a ceiling nothing has been spent against: the whole cap, and no counts."""
    return BudgetHeadroom(
        scope=_scope_label_of(ceiling),
        exceeded=False,
        money_remaining=ceiling.money,
        tokens_remaining=ceiling.tokens,
        partial_pricing=DomainPartialPricing(ceiling.partial_pricing.value),
    )


def _scope_label_of(ceiling: BudgetCeiling) -> str:
    """Name a ceiling for a reader, from the ceiling rather than from a verdict."""
    if ceiling.scope is CeilingScope.PER_RUN:
        return "trajectory"
    if ceiling.scope is CeilingScope.PER_DAY:
        return "day"
    return ceiling.tag or "tag"
