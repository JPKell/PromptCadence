"""The plant: assembly, the withheld tools, the roots it refuses, artifacts and the sweep.

Unit-level, so nothing here runs a model or opens a database. What it pins is the set of decisions
Phase 4 made *around* ToolYard rather than inside it — which handlers get registered, what happens
to ``http_fetch``, which configurations are refused before any call, and how an oversize output is
filed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from baseaicore import ConfigurationError, sha256_of
from toolyard import (
    IsolationTier,
    ResourceLimits,
    SandboxPaths,
    TieredSandbox,
    ToolCallRequest,
    ToolStatus,
)

from promptcadence.config import Settings, load_settings
from promptcadence.domain.tools import ToolOutcome
from promptcadence.services.tools import (
    ARTIFACT_CEILING_BYTES,
    DEFERRED_TOOL_CAUSE,
    UNSHIPPED_TOOL_CAUSE,
    ArtifactStore,
    ToolPlant,
    outcome_of,
    tools_health_component,
)


def settings(**environment: str) -> Settings:
    """Load settings, having set the given ``PROMPTCADENCE_*`` variables for this call only."""
    import os

    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    try:
        return load_settings().settings
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def plant(**environment: str) -> ToolPlant:
    """A plant over a sandbox that has been shown no runtime, so the probe is deterministic."""
    return ToolPlant(settings(**environment), sandbox=TieredSandbox(which=lambda _name: None))


# --------------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------------


def test_the_four_filesystem_and_command_tools_are_registered_from_the_shipped_default() -> None:
    assert set(plant().registry.names()) == {"read_file", "list_dir", "write_file", "run_command"}


def test_http_fetch_is_listed_and_withheld_with_the_cause_the_operator_reads() -> None:
    """The P6 decision, made visible. Configuration names it; the registry does not hold it."""
    built = plant()
    entry = built.entry("http_fetch")
    assert entry is not None
    assert entry.registered is False
    assert entry.withheld_cause == DEFERRED_TOOL_CAUSE
    assert "http_fetch" not in built.registry.names()
    assert built.registry.get("http_fetch") is None


def test_a_name_nothing_ships_is_withheld_rather_than_crashing_the_process() -> None:
    """A typo in ``[tools] enabled`` should be visible, not fatal: registration is code, so no
    configuration could have supplied a handler anyway, and a server that will not boot tells an
    operator less than a catalog line naming their typo."""
    built = plant(PROMPTCADENCE_TOOLS__ENABLED="read_file,reed_file")
    entry = built.entry("reed_file")
    assert entry is not None
    assert entry.withheld_cause == UNSHIPPED_TOOL_CAUSE
    assert set(built.registry.names()) == {"read_file"}


def test_the_catalog_keeps_configured_order_and_covers_both_halves() -> None:
    built = plant(PROMPTCADENCE_TOOLS__ENABLED="write_file,http_fetch,read_file")
    assert [entry.name for entry in built.catalog()] == ["write_file", "http_fetch", "read_file"]
    assert [entry.registered for entry in built.catalog()] == [True, False, True]


def test_a_registered_entry_carries_the_specs_own_description_and_schema() -> None:
    """The catalog and the wire definition cannot drift, because they read the same spec."""
    built = plant()
    entry = built.entry("read_file")
    registered = built.registry.get("read_file")
    assert entry is not None and registered is not None
    assert entry.description == registered.spec.description
    assert entry.parameters == registered.spec.args_schema
    assert entry.risk_class == "read_only"
    assert entry.egress == "none"


def test_run_command_declares_isolation_and_the_others_do_not() -> None:
    built = plant()
    flags = {entry.name: entry.requires_isolation for entry in built.catalog() if entry.registered}
    assert flags == {
        "read_file": False,
        "list_dir": False,
        "write_file": False,
        "run_command": True,
    }


def test_redact_args_reaches_the_registered_spec_not_only_the_record() -> None:
    built = plant(PROMPTCADENCE_TOOLS__REDACT_ARGS="write_file")
    registered = built.registry.get("write_file")
    assert registered is not None
    assert registered.spec.redact_args is True
    assert built.registry.get("read_file") is not None
    other = built.registry.get("read_file")
    assert other is not None and other.spec.redact_args is False


def test_run_command_holds_the_same_sandbox_instance_the_executor_checks() -> None:
    """D1 finding 1: two sandboxes would be two answers to one question about the host."""
    sandbox = TieredSandbox(which=lambda _name: None)
    built = ToolPlant(settings(), sandbox=sandbox)
    assert built.sandbox is sandbox
    registered = built.registry.get("run_command")
    assert registered is not None
    # The handler closed over the injected sandbox: its refusal is that sandbox's tier.
    assert built.isolation().tier is IsolationTier.UNAVAILABLE


# --------------------------------------------------------------------------------------------
# Roots
# --------------------------------------------------------------------------------------------


def test_a_relative_workspace_root_is_refused_before_any_call() -> None:
    with pytest.raises(ConfigurationError, match="absolute"):
        plant(PROMPTCADENCE_TOOLS__WORKSPACE_ROOT="relative/workspaces")


def test_a_read_root_inside_the_workspace_root_is_refused_at_startup(tmp_path: Path) -> None:
    """``SandboxPaths`` would catch this on the first tool call of the first trajectory. Catching
    it over the *parent* moves it to startup and covers every trajectory at once."""
    with pytest.raises(ConfigurationError, match="must not overlap"):
        plant(
            PROMPTCADENCE_TOOLS__WORKSPACE_ROOT=str(tmp_path / "work"),
            PROMPTCADENCE_TOOLS__READ_ROOTS=str(tmp_path / "work" / "shared"),
        )


def test_a_workspace_root_inside_a_read_root_is_refused_too(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="must not overlap"):
        plant(
            PROMPTCADENCE_TOOLS__WORKSPACE_ROOT=str(tmp_path / "shared" / "work"),
            PROMPTCADENCE_TOOLS__READ_ROOTS=str(tmp_path / "shared"),
        )


def test_a_process_count_below_the_documented_floor_is_refused() -> None:
    """A limit of 1 or 2 counts bwrap's own init and prlimit, so it refuses every command —
    including the probe's canary, which would read as "this host has no isolation"."""
    with pytest.raises(ConfigurationError, match="process_count"):
        ToolPlant(settings(), limits=ResourceLimits(process_count=2))


def test_a_trajectory_workspace_is_absolute_disjoint_and_made_on_demand(tmp_path: Path) -> None:
    built = plant(
        PROMPTCADENCE_TOOLS__WORKSPACE_ROOT=str(tmp_path / "work"),
        PROMPTCADENCE_TOOLS__READ_ROOTS=str(tmp_path / "reference"),
    )
    assert not (tmp_path / "work").exists()
    tools = built.for_trajectory("01ABC", allowlist=frozenset({"read_file"}))
    assert tools.workspace.write_root == tmp_path / "work" / "01ABC"
    assert tools.workspace.write_root.is_dir()
    assert tools.workspace.read_roots == (tmp_path / "reference",)
    # SandboxPaths would have raised had they overlapped; assert the object exists and is frozen.
    assert isinstance(tools.workspace, SandboxPaths)


def test_two_trajectories_get_disjoint_workspaces(tmp_path: Path) -> None:
    built = plant(PROMPTCADENCE_TOOLS__WORKSPACE_ROOT=str(tmp_path / "work"))
    first = built.for_trajectory("01AAA", allowlist=frozenset())
    second = built.for_trajectory("01BBB", allowlist=frozenset())
    assert first.workspace.write_root != second.workspace.write_root


# --------------------------------------------------------------------------------------------
# Workspace retention
# --------------------------------------------------------------------------------------------


def test_the_sweep_removes_a_workspace_and_says_so(tmp_path: Path) -> None:
    built = plant(PROMPTCADENCE_TOOLS__WORKSPACE_ROOT=str(tmp_path / "work"))
    tools = built.for_trajectory("01ABC", allowlist=frozenset())
    (tools.workspace.write_root / "note.txt").write_text("x", encoding="utf-8")
    assert built.sweep_workspace("01ABC") is True
    assert not tools.workspace.write_root.exists()


def test_sweeping_a_trajectory_that_called_no_tool_is_not_an_error(tmp_path: Path) -> None:
    built = plant(PROMPTCADENCE_TOOLS__WORKSPACE_ROOT=str(tmp_path / "work"))
    assert built.sweep_workspace("01NEVER") is False


# --------------------------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------------------------


def test_the_artifact_store_refuses_a_relative_root() -> None:
    with pytest.raises(ConfigurationError, match="absolute"):
        ArtifactStore(Path("artifacts"))


def test_an_artifact_is_written_once_under_its_own_digest(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    digest = sha256_of("hello")
    assert store.put("hello", digest=digest) == digest
    target = store.path_for(digest)
    assert target.read_text(encoding="utf-8") == "hello"
    written_at = target.stat().st_mtime_ns
    assert store.put("hello", digest=digest) == digest
    assert target.stat().st_mtime_ns == written_at


def test_path_for_tolerates_a_labelled_digest(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    bare = sha256_of("hello")
    assert store.path_for(bare) == store.path_for(f"sha256:{bare}")


def test_a_refusal_is_never_spilled(tmp_path: Path) -> None:
    """There is no output to file, and a refusal sentence under a result digest would be a lie."""
    built = plant(PROMPTCADENCE_TOOLS__WORKSPACE_ROOT=str(tmp_path / "work"))
    tools = built.for_trajectory("01ABC", allowlist=frozenset({"read_file"}))
    result = tools.executor(None).execute(
        ToolCallRequest(name="read_file", args={"path": "../escape"}),
        tools.context("inv-1", approved_tools=frozenset({"read_file"})),
    )
    assert result.status is ToolStatus.REFUSED
    assert built.spill(result, result_sha256=sha256_of(result.content)) is None


def test_a_truncated_result_is_not_filed_under_the_whole_outputs_hash(tmp_path: Path) -> None:
    """The rule the record exists to enforce: never a prefix pretending to be the whole thing."""
    built = plant(PROMPTCADENCE_TOOLS__WORKSPACE_ROOT=str(tmp_path / "work"))
    tools = built.for_trajectory("01ABC", allowlist=frozenset({"read_file"}))
    workspace = tools.workspace.write_root
    (workspace / "a.txt").write_text("short", encoding="utf-8")
    result = tools.executor(None).execute(
        ToolCallRequest(name="read_file", args={"path": "a.txt"}),
        tools.context("inv-1", approved_tools=frozenset({"read_file"})),
    )
    # A digest that is not this content's stands in for "ToolYard truncated": nothing is written.
    assert built.spill(result, result_sha256=sha256_of("something else")) is None
    assert built.spill(result, result_sha256=sha256_of(result.content)) is not None


def test_the_executors_content_cap_is_above_every_shipped_tools_own_cap() -> None:
    """Why the application, not ToolYard, applies the model-facing cap: while the whole output
    fits under this, ``ToolResult.content`` *is* the whole output and its digest is the record's."""
    from toolyard import DEFAULT_MAX_OUTPUT_BYTES, DEFAULT_MAX_READ_BYTES

    assert ARTIFACT_CEILING_BYTES > DEFAULT_MAX_READ_BYTES
    assert ARTIFACT_CEILING_BYTES > 2 * DEFAULT_MAX_OUTPUT_BYTES


# --------------------------------------------------------------------------------------------
# Vocabulary and health
# --------------------------------------------------------------------------------------------


def test_the_recorded_outcome_vocabulary_matches_toolyards_status_set() -> None:
    """The domain restates ToolYard's four statuses rather than importing them; a divergence
    would mistranslate every record, so it is a failing test rather than a silent remap."""
    assert {member.value for member in ToolOutcome} == {member.value for member in ToolStatus}
    for status in ToolStatus:
        assert outcome_of(status).value == status.value


def test_only_a_refusal_guarantees_nothing_ran() -> None:
    assert ToolOutcome.REFUSED.executed is False
    assert all(o.executed for o in ToolOutcome if o is not ToolOutcome.REFUSED)


def test_health_is_degraded_when_run_command_is_enabled_and_no_rung_exists() -> None:
    component = tools_health_component(plant())
    assert component.name == "tools"
    assert component.status.value == "degraded"
    assert "isolation unavailable" in component.detail
    assert DEFERRED_TOOL_CAUSE in component.detail


def test_health_is_ok_when_nothing_enabled_needs_isolation() -> None:
    """A host with no rung is not a broken application when nothing asked to run a process."""
    component = tools_health_component(plant(PROMPTCADENCE_TOOLS__ENABLED="read_file,list_dir"))
    assert component.status.value == "ok"


def test_health_never_reports_unavailable() -> None:
    """Taking the server down over a tool nobody may have asked for is the wrong failure."""
    for enabled in ("read_file", "run_command", "read_file,run_command,http_fetch"):
        component = tools_health_component(plant(PROMPTCADENCE_TOOLS__ENABLED=enabled))
        assert component.status.value in {"ok", "degraded"}
