"""Development plan Phase 3, acceptance criterion 1 — the live half (roadmap §9, I10).

``promptcadence run "…" --bypass-planning`` against a **real** LoadCoach at
``PROMPTCADENCE_LOADCOACH__BASE_URL`` (default ``http://127.0.0.1:8766``), with every configured
tier's task profile checked to exist there first.

This test asserts ``completed``. Against LoadCoach ``01170a7`` it is expected to **fail** on a
free-text tier, because LoadCoach renders no ``finish_reason`` and PromptCadence refuses to read
an undeclared finish as success; the failure message names the cause verbatim. That is the
finding, not a flake — see ``D2_HANDOFF.md``. It passes once LoadCoach carries
``output.finish_reason``, or against a tier whose profile validates a schema.
"""

from __future__ import annotations

import os
import time

import pytest

from promptcadence.config import load_settings
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.services.runtime import Runtime
from promptcadence.services.trajectories import TrajectorySubmission

pytestmark = pytest.mark.live


def test_every_configured_tier_profile_exists_and_a_bypass_journey_completes() -> None:
    base_url = os.environ.get("PROMPTCADENCE_LOADCOACH__BASE_URL", "http://127.0.0.1:8766")
    os.environ["PROMPTCADENCE_LOADCOACH__BASE_URL"] = base_url
    settings = load_settings().settings
    runtime = Runtime(settings)
    try:
        runtime.loadcoach.version()
        missing = {
            name: tier.task_profile
            for name, tier in settings.tiers.items()
            if runtime.loadcoach.task_profile(tier.task_profile) is None
        }
        assert not missing, f"tiers whose task profile LoadCoach lacks: {missing}"
        runtime.start()
        view = runtime.trajectories.submit(
            TrajectorySubmission(task="Reply with the single word: ready.", bypass_planning=True)
        )
        deadline = time.monotonic() + settings.loadcoach.timeout_seconds
        while not runtime.trajectories.get(view.trajectory_id).is_terminal:
            assert time.monotonic() < deadline, "the trajectory did not end in time"
            time.sleep(0.5)
        final = runtime.trajectories.get(view.trajectory_id)
        assert final.state is TrajectoryState.COMPLETED, (
            f"{final.state.value}: {final.halted_reason} ({final.error_code})"
        )
    finally:
        runtime.close()
