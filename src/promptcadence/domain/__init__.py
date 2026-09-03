"""promptcadence.domain — pure entities, policies and state-machine logic.

Empty at Phase 1 by design (development plan Phase 1: "deferred: everything that executes").
``threads.py``, ``tiers.py``, ``plan.py``, ``intent.py``, ``deviation.py``, ``trajectory.py`` and
``policy.py`` arrive in Phase 2. This module exists so the ``domain`` layer in ``.importlinter``'s
``layers`` contract is a real, importable package from the first commit — no later phase adds a
directory the layering contract did not already know about.
"""

from __future__ import annotations
