"""Development plan Phase 3, acceptance criterion 1 — the live half (roadmap §9, I10).

``promptcadence run "…" --bypass-planning`` against a **real** LoadCoach at
``PROMPTCADENCE_LOADCOACH__BASE_URL`` (default ``http://127.0.0.1:8766``), with every configured
tier's task profile checked to exist there first.

This test asserts ``completed``. It passes against a LoadCoach at or after
``846348b``, which renders the provider's declared reason at ``output.finish_reason``.
Against an older LoadCoach (``01170a7`` and before) it **fails** on a free-text tier, because that
wire carries no ``finish_reason`` and PromptCadence refuses to read an undeclared finish as
success; the failure message names the cause verbatim. That is the finding of ``D2_HANDOFF.md``
§2, not a flake.

The default tiers name ``tools.agent.local_fast`` and ``tools.agent.local_large``, and **LoadCoach
ships both** since E4 — so this runs with no ``PROMPTCADENCE_TIERS__*`` overrides at all, which is
that row's exit condition. The first assertion still names any profile the LoadCoach under test
lacks, because an older LoadCoach is the case that must fail loudly rather than by timeout;
``isolated_environment`` still keeps ``PROMPTCADENCE_TIERS__*`` for ``live`` tests, for an operator
pointing at a LoadCoach configured some other way.

The second test runs one **real** ``read_file`` through the runtime's own plant — the registry,
the sandbox and the per-trajectory workspace this process would use — so the tool path is
exercised against a real filesystem rather than only against the integration suite's. It is not
model-*directed*: the fake provider declares ``stop`` for every answer and requests no tools, so a
model actually choosing to call one is the operator's Ollama run (outstanding-work §4), not this.
"""

from __future__ import annotations

import os
import time

import pytest
from toolyard import ToolCallRequest, ToolStatus

from promptcadence.config import load_settings
from promptcadence.domain.trajectory import TrajectoryState
from promptcadence.services.budget import NOT_PRICED, render_money
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

        # P5: a real run leaves real debits and a running balance behind it. The tiers here are
        # local, so every debit is **unpriced** and the money balance is `None` — never `$0.00`
        # (ADR-0016). The token balance is what actually moved, and the running balance is read
        # from the verdicts the ledger recorded, not recomputed here.
        entries = list(runtime.budget.entries(run_id=final.trajectory_id))
        assert entries, "a completed live run recorded no debit"
        turn_ids = [turn.turn.turn_id for turn in runtime.trajectories.turns(final.trajectory_id)]
        assert [entry.debit.source_ref for entry in entries] == [
            turn_id for turn_id in turn_ids if turn_id in {e.debit.source_ref for e in entries}
        ], "every debit is keyed by the turn it came from, in turn order"
        assert all(entry.unpriced for entry in entries), "a local tier prices nothing"
        running = [
            verdict.tokens_spent
            for entry in entries
            for verdict in entry.verdicts
            if verdict.ceiling.scope.value == "per_run"
        ]
        assert running == sorted(running) and running[-1] > 0, "a monotonic running balance"
        position = runtime.budget.position(final)
        trajectory = position.headroom[0]
        assert trajectory.money_remaining is not None, "the configured cap is a real figure"
        assert trajectory.money_is_floor, "nothing was priced, so the money figure is a floor"
        rendered = render_money(None, is_floor=True)
        assert rendered == NOT_PRICED and "0.00" not in rendered
    finally:
        runtime.close()


def test_a_real_read_file_runs_under_the_runtimes_own_plant(tmp_path: object) -> None:
    """One real tool call, through the objects a serving process holds.

    The integration suite proves the loop's handling of a result; this proves the *plant* — the
    registry assembled from ``[tools]``, the workspace built under the configured root, and
    containment over a real filesystem — on the machine the operator is actually running. The two
    together are what "the loop executes tool calls under full ToolYard discipline" means.
    """
    base_url = os.environ.get("PROMPTCADENCE_LOADCOACH__BASE_URL", "http://127.0.0.1:8766")
    os.environ["PROMPTCADENCE_LOADCOACH__BASE_URL"] = base_url
    settings = load_settings().settings
    runtime = Runtime(settings)
    try:
        tools = runtime.tools.for_trajectory("01LIVEREADFILE", allowlist=frozenset({"read_file"}))
        (tools.workspace.write_root / "notes.md").write_text("three meetings", encoding="utf-8")
        result = tools.executor(None).execute(
            ToolCallRequest(name="read_file", args={"path": "notes.md"}),
            tools.context("01LIVEINVOCATION", approved_tools=frozenset({"read_file"})),
        )
        assert result.status is ToolStatus.OK, f"{result.reason}: {result.reason_detail}"
        assert "three meetings" in result.content

        escape = tools.executor(None).execute(
            ToolCallRequest(name="read_file", args={"path": "../../../../etc/passwd"}),
            tools.context("01LIVEESCAPE", approved_tools=frozenset({"read_file"})),
        )
        assert escape.status is ToolStatus.REFUSED
        assert escape.reason == "path_escape"
    finally:
        runtime.tools.sweep_workspace("01LIVEREADFILE")
        runtime.close()
