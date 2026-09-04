"""promptcadence.services.egress — every egress verdict, rendered by Commissioner and acted on here.

**The direction of this integration is easy to get backwards.** Commissioner *renders and records*
a verdict; enforcing it is this application's job (ADR-0054). Nothing in ``commissioner`` blocks a
call, opens a connection or knows what a turn is. So the shape of every use here is the same three
steps in the same order: build the request, ask the policy, **act on the verdict** — and the acting
is always PromptCadence's own code path, never something the package arranged.

**Every verdict is recorded, not only the refusals** (spec §11 contract 3: "a declined call is as
auditable as an approved one"). An approval is written with the same durability as a denial, which
is what lets ``GET /egress-decisions`` answer "where did this trajectory's data go" rather than only
"what was refused". :meth:`EgressService.evaluate` therefore has no path that returns a verdict it
did not also persist.

**Two evaluation points, one policy.** Before every turn, against the tier that would serve it; and
before every ``NETWORK`` tool call, against the host it would reach. They differ only in how the
:class:`~commissioner.EgressTarget` is built (:func:`tier_target`, :func:`fetch_target`) — the
policy, the ledger and the recording are identical, because a second policy is a second place for
the answer to differ.

**The classification is always the trajectory's declaration, never model text** (spec §14). A model
can influence *which target* it asks for; it can never influence how sensitive the data is said to
be. That asymmetry is the whole reason a fetch to a host the model chose is still governed.

**Defaults are closed** (ADR-0046). A remote target with no declared ceiling is denied with
``no_ceiling_declared`` — Commissioner's own fail-closed branch — and this module's job is to make
sure an undeclared target reaches the policy *as undeclared* rather than being given a ceiling to
make it pass. That is why :func:`fetch_target` gives a non-allowlisted host no ceiling instead of
refusing it here: the refusal is then a recorded decision with a reason, not an unlogged early
return.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from baseaicore import DataClassification
from commissioner import (
    EgressDecision,
    EgressRequest,
    EgressTarget,
    OrderedClassificationPolicy,
    Verdict,
)
from commissioner.sql import SqlEgressLedger
from sqlalchemy.orm import Session

from promptcadence.domain.tiers import Tier

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from commissioner import EgressPolicy

    from promptcadence.services.database import Database

__all__ = [
    "LOOPBACK_HOSTS",
    "EgressService",
    "VIOLATION_POLICY_NAME",
    "fetch_target",
    "host_of",
    "tier_target",
]

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
"""Hosts that do not leave this machine, matching ``toolyard.tools.fetch.LOOPBACK_HOSTS``.

Duplicated deliberately rather than imported: ToolYard's copy is the *tool's* allowlist default,
and this one is the *egress class* of a host. They agree today and are free to diverge — a future
ToolYard that allowed an extra loopback alias would not thereby change what "leaves the machine"
means here. A test asserts they are equal, so a divergence is a decision rather than an accident.
"""

VIOLATION_POLICY_NAME: str = "promptcadence.verification"
"""The policy name on a ``VIOLATION`` record.

A violation is not produced by :class:`~commissioner.OrderedClassificationPolicy` — that policy
answers "may this go?" before the fact, and a violation is the answer to "did something go that was
never approved?" after it (ADR-0054 rule 7). Recording it under the shipped policy's name would
claim it made a decision it never made, so the verification step signs its own records.
"""


def tier_target(tier: Tier) -> EgressTarget:
    """Describe a tier as an egress target.

    Args:
        tier: The tier a turn would be dispatched to.

    Returns:
        The target: the tier's name, its egress class, its declared ceiling and no provider kind.
        ``provider_kind`` stays ``None`` because *which* provider serves a tier is LoadCoach's
        decision and is not known until the response comes back — putting a guess here would make
        the recorded decision claim a fact it did not have.

    Note:
        A remote tier always carries a ceiling: ``domain.tiers.Tier`` refuses to be constructed
        without one (ADR-0046 rule 3). So Commissioner's ``no_ceiling_declared`` branch is
        unreachable through *this* function, and reachable through :func:`fetch_target` — which is
        the point of keeping the two separate rather than sharing one "build a target" helper.
    """
    return EgressTarget(
        name=tier.name,
        remote=tier.is_remote,
        max_data_classification=tier.max_data_classification,
        provider_kind=None,
    )


def host_of(url: str) -> str | None:
    """Extract the lowercase host from a URL, or ``None`` when there is not one to extract.

    Args:
        url: The URL a tool call asked for. **Model-supplied and therefore untrusted** — this
            function parses it and never executes, resolves or normalizes it beyond casefolding.

    Returns:
        The hostname, lowercased, or ``None`` if the URL is unparseable or names no host. ``None``
        is not an error here: it is a target that cannot be identified, and
        :func:`fetch_target` turns it into a denial with a reason rather than into an exception
        that would have to be caught at every call site.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    host = parsed.hostname
    return host.lower() if host else None


def fetch_target(
    host: str | None,
    *,
    allowed_hosts: frozenset[str],
    ceiling: DataClassification | None,
) -> EgressTarget:
    """Describe the destination of one ``http_fetch`` as an egress target.

    Three cases, and each maps onto one of Commissioner's four documented reasons, so the recorded
    denial says *why* in the package's own vocabulary rather than in a string invented here:

    * **Loopback** — ``remote=False``, so the policy approves with ``target_not_remote``. A fetch
      that does not leave the machine is not egress, and treating it as such would either block
      the local-service case ``fetch_allowed_hosts = []`` exists for, or force a ceiling to be
      declared for something that never leaves.
    * **Allowlisted and not loopback** — ``remote=True`` carrying the operator's declared
      ``ceiling``, so the policy answers ``within_ceiling`` or ``classification_exceeds_ceiling``.
    * **Not allowlisted, or unidentifiable** — ``remote=True`` with **no ceiling**, so the policy
      denies with ``no_ceiling_declared``. This is the fail-closed default (ADR-0046), and it is
      expressed by *withholding* a ceiling rather than by refusing here, so that the refusal is a
      recorded, queryable decision instead of an early return nobody can audit.

    Args:
        host: The lowercase host from :func:`host_of`, or ``None`` when the URL named none.
        allowed_hosts: ``[tools] fetch_allowed_hosts``, lowercased. The operator's declaration of
            which hosts may be reached at all.
        ceiling: ``[tools] fetch_max_data_classification``. ``None`` means the operator declared no
            ceiling for fetch egress, which denies every non-loopback fetch — deliberately, and
            visibly, rather than defaulting to a permissive value nobody chose.

    Returns:
        The target. ``name`` is the host, or ``"<unparseable>"`` when there is none, so that a
        recorded decision always names something an operator can search for.
    """
    if host is not None and host in LOOPBACK_HOSTS:
        return EgressTarget(name=host, remote=False, max_data_classification=None)
    if host is None:
        return EgressTarget(name="<unparseable>", remote=True, max_data_classification=None)
    if host not in allowed_hosts:
        return EgressTarget(name=host, remote=True, max_data_classification=None)
    return EgressTarget(name=host, remote=True, max_data_classification=ceiling)


class EgressService:
    """Evaluate, record and answer for every egress decision this application makes.

    Stateless between calls, like :class:`~promptcadence.services.budget.BudgetService`: a ledger
    is built per operation over the mounted table, so a decision written inside a caller's
    transaction and one written in its own unit of work are the same code path with a different
    session.
    """

    __slots__ = ("_database", "_policy")

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime],
        policy: EgressPolicy | None = None,
    ) -> None:
        """Bind the service to the database and the policy that decides.

        Args:
            database: The application's database handle; Commissioner's table lives in it.
            clock: The instant source, injected and required. It reaches the policy, which stamps
                ``decided_at`` — a policy reading the system clock could not produce the
                reproducible decision spec §11 contract 3 asks for.
            policy: The policy to evaluate with. Defaults to Commissioner's shipped
                :class:`~commissioner.OrderedClassificationPolicy`. Injectable because the *policy*
                is the substitutable part of this design and the recording is not.
        """
        self._database = database
        self._policy: EgressPolicy = (
            policy if policy is not None else OrderedClassificationPolicy(clock=clock)
        )

    @property
    def policy(self) -> EgressPolicy:
        """The policy decisions are rendered by."""
        return self._policy

    def ledger(self, *, session: Session | None = None) -> SqlEgressLedger:
        """Build a ledger over the mounted table.

        Args:
            session: A session to join, or ``None`` for the ledger's own unit of work. When given,
                the write lands inside the caller's transaction as a savepoint, so a decision and
                whatever it gates commit together or not at all (ADR-0044).

        Returns:
            A ledger bound to this application's ``egress_`` tables.
        """
        if session is None:
            factory: Callable[[], Session] = self._database.sessions
        else:
            connection = session.connection()

            def factory() -> Session:
                return Session(bind=connection, join_transaction_mode="create_savepoint")

        return SqlEgressLedger(factory)

    def evaluate(
        self,
        *,
        run_id: str,
        source_ref: str,
        classification: DataClassification,
        target: EgressTarget,
        session: Session | None = None,
    ) -> EgressDecision:
        """Decide one egress request and record the verdict, whatever it is.

        There is no variant of this method that decides without recording. An approval that was
        not written down is indistinguishable, afterwards, from a call nobody governed.

        Args:
            run_id: The trajectory this request belongs to.
            source_ref: The turn id or tool invocation id being evaluated — the locator that ties
                a decision back to the exact thing it gated.
            classification: The **trajectory's** declared classification. Never derived from model
                output (spec §14).
            target: Where the data would go, from :func:`tier_target` or :func:`fetch_target`.
            session: A session to join, so the decision commits with its caller's write.

        Returns:
            The recorded decision. The caller inspects
            :attr:`~commissioner.EgressDecision.verdict` and acts; this method never raises to
            signal a denial, because a denial is an expected, recorded outcome rather than an
            error (spec §13: "a denied egress evaluation is not an exception path").
        """
        decision = self._policy.evaluate(
            EgressRequest(
                run_id=run_id,
                source_ref=source_ref,
                data_classification=classification,
                target=target,
            )
        )
        self.ledger(session=session).record(decision)
        return decision

    def record_violation(
        self,
        *,
        run_id: str,
        source_ref: str,
        classification: DataClassification,
        target: EgressTarget,
        reason: str,
        decided_at: datetime,
        decision_id: str,
        session: Session | None = None,
    ) -> EgressDecision:
        """Write a ``VIOLATION`` for egress that happened and was never approved.

        Contract 4's other half. The shipped policy never produces this verdict — it answers "may
        this go?" before the fact, and this answers "something went that policy did not permit"
        after it (ADR-0054 rule 7). It is therefore constructed rather than evaluated, and signed
        with :data:`VIOLATION_POLICY_NAME` so the record does not attribute a decision to a policy
        that never made one.

        Args:
            run_id: The trajectory.
            source_ref: The turn whose response contradicted its tier.
            classification: The trajectory's declared classification, unchanged.
            target: The tier that promised to serve the turn — the promise that was broken, not
                where the data actually went, which is exactly what the violation could not
                establish.
            reason: Why this is a violation, in this application's vocabulary.
            decided_at: When the verification ran. Injected, from the caller's clock.
            decision_id: The record's identity. Injected, from the caller's id source, so a
                violation is as reproducible in a test as an evaluated decision.
            session: A session to join, so the violation commits with the halt it causes.

        Returns:
            The recorded decision, with verdict :attr:`~commissioner.Verdict.VIOLATION`.
        """
        decision = EgressDecision(
            decision_id=decision_id,
            request=EgressRequest(
                run_id=run_id,
                source_ref=source_ref,
                data_classification=classification,
                target=target,
            ),
            verdict=Verdict.VIOLATION,
            reason=reason,
            policy_name=VIOLATION_POLICY_NAME,
            policy_version="1.0",
            decided_at=decided_at,
        )
        self.ledger(session=session).record(decision)
        return decision

    def decisions(
        self,
        *,
        run_id: str | None = None,
        verdict: Verdict | None = None,
        target: str | None = None,
        since: datetime | None = None,
    ) -> Sequence[EgressDecision]:
        """Read recorded decisions, oldest-decided first, narrowed by whichever filters are given.

        Args:
            run_id: Restrict to one trajectory.
            verdict: Restrict to approvals, denials or violations.
            target: Restrict to one target name — a tier or a host.
            since: Restrict to decisions at or after this instant. Must be timezone-aware.

        Returns:
            The matching decisions.

        Raises:
            ValueError: If ``since`` is naive. Propagated from Commissioner rather than wrapped: a
                naive instant has no defensible UTC reading, and the package's message says so
                better than a restatement would.
        """
        return self.ledger().decisions(run_id=run_id, verdict=verdict, target=target, since=since)
