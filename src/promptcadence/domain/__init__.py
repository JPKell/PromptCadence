"""promptcadence.domain — pure entities, policies and state-machine logic.

Every governance decision that needs no I/O lives here as a pure, golden-tested function
(development plan Phase 2). Nothing in this package talks HTTP, touches a database or imports a
framework — ``.importlinter``'s ``domain-purity`` contract asserts it, and
``tests/unit/test_domain_purity.py`` asserts the contract exists as well as passes.

The modules, in dependency order:

* :mod:`~promptcadence.domain.errors` — spec §13's closed error vocabulary.
* :mod:`~promptcadence.domain.events` — spec §17's closed event vocabulary. Bodies live with the
  code that mints them.
* :mod:`~promptcadence.domain.threads` — ``Thread``, ``Turn``, ``ThreadSnapshot`` and the
  ``ThreadStore`` port, built package-shaped per the recorded ThreadRack rejection (spec §10).
* :mod:`~promptcadence.domain.tiers` — admission, escalation, availability and the tier snapshot a
  trajectory records.
* :mod:`~promptcadence.domain.trajectory` — lifecycle §8's states, the T1-T17 table and its guards.
* :mod:`~promptcadence.domain.plan` — the committed plan schema and lifecycle §4.1's five rules.
* :mod:`~promptcadence.domain.policy` — the automatic approval verdict and the gates.
* :mod:`~promptcadence.domain.intent` — the ``ExecutionIntent``: the reason no turn can execute
  ungoverned (ADR-0056).
* :mod:`~promptcadence.domain.deviation` — the closed taxonomy and the one comparison behind it.

Two things this package deliberately does **not** hold. Persistence, including the SQLAlchemy
``ThreadStore`` implementation, lives in :mod:`promptcadence.infrastructure` — the development
plan's Phase 2 list put ``SqlThreadStore`` in ``domain/threads.py``, which contradicts the
``domain-purity`` contract, and the contract wins (see ``C4_HANDOFF.md`` for the amendment). And
configuration: the adapter that turns validated ``Settings`` into these value objects is
:mod:`promptcadence.services.policy_assembly`, so the domain stays testable without constructing a
``Settings`` object and free of a validation framework's semantics.
"""

from __future__ import annotations
