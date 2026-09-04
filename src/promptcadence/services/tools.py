"""promptcadence.services.tools — the registry, the sandbox, the workspaces and the artifacts.

Everything about *how* a model-directed tool call is authorized, contained and refused belongs to
ToolYard (ADR-0053) and is not re-implemented here. What this module owns is the four things that
package deliberately does not: which handlers this application registers, where a trajectory's
workspace is, where an oversize output goes, and how a ToolYard result becomes a row and a turn.

**One sandbox, one registry, one process.** :class:`ToolPlant` builds a single
:class:`toolyard.TieredSandbox` and hands *that same instance* to ``run_command_tool`` before
registering it, so the tier the executor checks at containment and the rung the command actually
runs under are one answer to one question (D1's first finding). The probe runs once, on the first
call that needs it, and :meth:`ToolPlant.isolation` shows what it found — including the reason,
which is what ``doctor`` renders. Registration happens here, in code, at startup, or it does not
happen: there is no path that loads a handler from configuration, an entry point or model output.

**What narrows is the allowlist, not the registry.** Every trajectory shares the registry and gets
its own executor, whose ``allowlist`` is the caller's declared tool set; the intent narrows that
again per invocation through ``ToolContext.approved_tools``. The direction is structural — an
intersection has no widening case — and it is what splits an ``undeclared_tool`` deviation into a
drift the policy may continue past and a refusal that is never re-approvable (lifecycle §5).

**`http_fetch` is registered from Phase 6, and egress-checked.** Before it, the tool was listed in
``[tools] enabled`` and deliberately withheld from the registry, because a fetch tool without
egress governance in place is exactly the hole this application exists to close. The governance now
exists, so the tool is real and the withheld cause is gone.

Two independent gates stand between a model and a socket, and they answer different questions.
ToolYard's own host allowlist (ADR-0026 §3, ``[tools] fetch_allowed_hosts``) answers *may anyone
reach this host*; the :class:`~commissioner.EgressDecision` the loop records before the call
answers *may this trajectory's data reach it*, from the trajectory's declared classification and
never from model text (spec §14). A denial leaves the invocation's ``max_egress`` at
:attr:`toolyard.EgressClass.NONE`, which ToolYard refuses with ``egress_not_permitted`` as a
structured result — so a refusal travels the ordinary recorded path rather than a second one.

**Workspace lifecycle.** One absolute directory per trajectory under ``[tools] workspace_root``,
created on the first call that needs it, disjoint by construction from every configured read root
(``SandboxPaths`` refuses relative or overlapping roots, so a misconfiguration is caught at
startup here rather than on the one call that reached the shared directory). Retention follows
transcript content: :meth:`ToolPlant.sweep_workspace` is what the retention sweep calls, and the
hashes in ``tool_call_records`` outlive it.
"""

from __future__ import annotations

import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx
from baseaicore import sha256_of
from mirrorwall import ComponentHealth, ComponentStatus
from toolyard import (
    DEFAULT_MAX_SUMMARY_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    MIN_PROCESS_COUNT,
    EgressClass,
    IsolationTier,
    ResourceLimits,
    SandboxPaths,
    TieredSandbox,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolStatus,
    http_fetch_tool,
    list_dir_tool,
    read_file_tool,
    run_command_tool,
    write_file_tool,
)

from promptcadence.config import ConfigurationError, data_dir
from promptcadence.domain.tools import ToolOutcome

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from toolyard import Resolver, TierReport, ToolCallStore

    from promptcadence.config import Settings

__all__ = [
    "ARTIFACT_CEILING_BYTES",
    "BUILTIN_TOOL_NAMES",
    "UNSHIPPED_TOOL_CAUSE",
    "ArtifactStore",
    "ToolCatalogEntry",
    "ToolPlant",
    "TrajectoryTools",
    "isolation_payload",
    "outcome_of",
    "tools_health_component",
]

BUILTIN_TOOL_NAMES: Final[tuple[str, ...]] = (
    "read_file",
    "list_dir",
    "write_file",
    "run_command",
    "http_fetch",
)
"""The five tools ToolYard ships, and the only names this application can ever register.

Registration is code (ADR-0053 decision 1), so a name outside this tuple in ``[tools] enabled``
cannot become a tool however it is spelled. It is reported as withheld rather than refused at
startup: a typo should be *visible* to the operator who made it, and a server that will not boot
tells them less than a catalog line that names the tool and says nothing ships under that name.
"""

UNSHIPPED_TOOL_CAUSE: Final[str] = "not_a_shipped_tool"
"""Why a name in ``[tools] enabled`` that ToolYard does not ship is absent from the registry."""

ARTIFACT_CEILING_BYTES: Final[int] = 4 * 1024 * 1024
"""The executor's ``max_content_bytes``, and therefore the largest output this application holds.

Above every shipped tool's own cap (``read_file`` loads at most 1 MiB, ``run_command`` keeps at
most 1 MiB per stream), which is the point: while the whole cleaned output fits under this, the
result the executor returns **is** the whole output, so its digest is ToolYard's ``result_sha256``
and an artifact written under that digest is genuinely the thing the record names. The model-facing
cap is a separate, much smaller number this application applies itself
(``[tools] max_result_chars``) — two caps, because "what may be stored" and "what a model is shown"
are different questions and one number cannot answer both.

Should an output ever exceed this, ToolYard truncates and labels it, the digests no longer agree,
and :meth:`ToolPlant.spill` writes **nothing**: a prefix filed under the whole output's hash is the
truncated body pretending to be complete that the record exists to prevent.
"""


def isolation_payload(report: TierReport) -> dict[str, Any]:
    """Render the probe's answer as the mapping every surface shows.

    One function rather than a method on :class:`toolyard.TierReport`, which is that package's
    frozen dataclass and not ours to extend — and one function rather than two hand-built literals,
    because ``GET /tools`` and ``promptcadence tools list --json`` describing the same probe
    differently is a drift nobody would notice until an operator compared them.

    Args:
        report: What :meth:`ToolPlant.isolation` returned.

    Returns:
        ``tier``, ``runtime``, ``reason`` and ``limits_unenforced``. ``reason`` is the operator's
        whole answer to "why this rung" — it names every rung the probe visited and why each was
        skipped or failed — and it never reaches a model.
    """
    return {
        "tier": report.tier.value,
        "runtime": report.runtime,
        "reason": report.reason,
        "limits_unenforced": list(report.limits_unenforced),
    }


def outcome_of(status: ToolStatus) -> ToolOutcome:
    """Translate ToolYard's status into the domain's recorded outcome.

    The one place the translation happens, so the domain can stay free of ``toolyard`` (which
    depends on ``httpx``, forbidden under ``promptcadence.domain``) without the mapping being
    re-derived at each call site.

    Args:
        status: What the executor reported.

    Returns:
        The matching :class:`~promptcadence.domain.tools.ToolOutcome`.
    """
    return ToolOutcome(status.value)


@dataclass(frozen=True, slots=True)
class ToolCatalogEntry:
    """One line of ``GET /tools`` and ``promptcadence tools list``.

    Covers both halves of the catalog: what is registered and callable, and what configuration
    names that is not. An operator reading a list of only the working tools cannot tell a tool
    that was never asked for from one that was asked for and withheld.

    Attributes:
        name: The tool name.
        description: What the model is told the tool does — verbatim from the registered spec, so
            the catalog and the wire definition cannot drift.
        registered: Whether a handler exists for it.
        risk_class: ``read_only`` or ``mutating``; ``None`` when not registered.
        egress: ``none`` or ``network``; ``None`` when not registered.
        requires_isolation: Whether the tool runs a subprocess and is refused where no isolation
            tier exists.
        redact_args: Whether ``[tools] redact_args`` names it, so its arguments are stored as a
            hash only.
        withheld_cause: Why it is not registered, or ``None`` when it is.
        parameters: The argument schema the model is given, or ``None`` when not registered.
    """

    name: str
    description: str
    registered: bool
    risk_class: str | None = None
    egress: str | None = None
    requires_isolation: bool = False
    redact_args: bool = False
    withheld_cause: str | None = None
    parameters: Mapping[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        """Return the API and CLI mapping form."""
        return {
            "name": self.name,
            "description": self.description,
            "registered": self.registered,
            "risk_class": self.risk_class,
            "egress": self.egress,
            "requires_isolation": self.requires_isolation,
            "redact_args": self.redact_args,
            "withheld_cause": self.withheld_cause,
            "parameters": dict(self.parameters) if self.parameters is not None else None,
        }


class ArtifactStore:
    """Content-addressed storage for tool output too large to keep in a transcript.

    A two-level fan-out under one root, keyed by the digest of the whole output: writing the same
    body twice is one file, and the record's ``result_sha256`` is the only thing needed to find it.
    Nothing here is ever overwritten with different bytes, because the name *is* the bytes.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        """Bind the store to a directory, created on first write.

        Args:
            root: The artifact directory. Must be absolute — a relative root would resolve against
                whatever directory the process happened to start in.

        Raises:
            ConfigurationError: If ``root`` is not absolute.
        """
        if not root.is_absolute():
            message = f"the artifact root must be an absolute path; got {str(root)!r}"
            raise ConfigurationError(message, details={"field": "tools.artifact_root"})
        self._root = root

    @property
    def root(self) -> Path:
        """The directory artifacts are written under."""
        return self._root

    def path_for(self, digest: str) -> Path:
        """Return where the body with this digest lives, whether or not it has been written.

        Args:
            digest: The hex digest of the whole output, as
                :func:`baseaicore.sha256_of` and ToolYard's ``result_sha256`` produce it. An
                ``algorithm:`` prefix is tolerated and stripped, so a caller that got the value
                from somewhere that labels its digests still lands on one file.

        Returns:
            The path.
        """
        bare = digest.split(":", 1)[-1]
        return self._root / bare[:2] / bare

    def put(self, content: str, *, digest: str) -> str:
        """Write one output under its digest and return the reference the record carries.

        Idempotent: an existing file with this name already holds these bytes, so a second write is
        skipped rather than repeated.

        Args:
            content: The whole cleaned output — never a prefix. The caller has already checked that
                its digest is the one the executor recorded.
            digest: The digest to file it under.

        Returns:
            The reference — the digest, verbatim, which is what ``tool_call_records.artifact_ref``
            stores and what a reader hands back to :meth:`path_for`.
        """
        target = self.path_for(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return digest


@dataclass(frozen=True, slots=True)
class TrajectoryTools:
    """The tool apparatus for one trajectory: shared registry and sandbox, private workspace.

    Attributes:
        plant: The process-wide registry, sandbox and artifact store.
        trajectory_id: The trajectory.
        workspace: This trajectory's roots — its own write root plus the configured read-only
            roots, absolute and disjoint.
        allowlist: The caller's declared tool set. The registry is never narrowed; this is.
    """

    plant: ToolPlant
    trajectory_id: str
    workspace: SandboxPaths
    allowlist: frozenset[str]

    def executor(self, store: ToolCallStore | None) -> ToolExecutor:
        """Build the executor for one turn's write.

        Args:
            store: Where records go — the loop's
                :class:`~promptcadence.infrastructure.tool_calls.CollectingToolCallStore`, which
                collects during the call and is flushed onto the session that turn commits on. It
                collects rather than writing through because a ``run_command`` may spend its whole
                timeout inside a container, and a write-through store would hold a SQLite write
                lock for exactly that long. ``None`` records nothing and is for a caller with no
                database; the record is built either way, so the path a test exercises is the path
                production runs.

        Returns:
            An executor over the shared registry and sandbox, narrowed to this trajectory's
            allowlist.
        """
        return ToolExecutor(
            self.plant.registry,
            self.plant.sandbox,
            allowlist=self.allowlist,
            store=store,
            default_timeout_seconds=self.plant.timeout_seconds,
            max_content_bytes=ARTIFACT_CEILING_BYTES,
            max_summary_bytes=DEFAULT_MAX_SUMMARY_BYTES,
        )

    def context(
        self,
        invocation_id: str,
        *,
        approved_tools: frozenset[str],
        max_egress: EgressClass = EgressClass.NONE,
    ) -> ToolContext:
        """Build the trusted half of one invocation.

        Args:
            invocation_id: This application's id for the call, which is also the ``TOOL`` turn's
                ``tool_call_id``. A model never supplies it.
            approved_tools: The intent's frozen subset of the trajectory allowlist. It can only
                narrow.
            max_egress: The egress ceiling for this one call. **Defaults closed**, and it is the
                caller's job to widen it only after an :class:`~commissioner.EgressDecision`
                approved this trajectory's data reaching this call's target (ADR-0046, ADR-0054).
                Passing :attr:`toolyard.EgressClass.NETWORK` here is the *enforcement* of a
                decision Commissioner rendered; the decision itself is recorded by
                :class:`~promptcadence.services.egress.EgressService` whatever the verdict, so a
                refusal is as auditable as a permission.

        Returns:
            The context. A ``NETWORK`` tool invoked under a ``NONE`` ceiling is refused by
            ToolYard's own egress check with ``egress_not_permitted`` — a structured
            :class:`~toolyard.ToolResult`, recorded through the ordinary path, never an exception.
        """
        return ToolContext(
            invocation_id,
            workspace=self.workspace,
            approved_tools=approved_tools,
            max_egress=max_egress,
        )


class ToolPlant:
    """The registry, the sandbox, the workspaces and the artifact store, for one process.

    Built once when the runtime opens. Everything configurable is a constructor argument or comes
    from ``[tools]``; nothing here reads the environment, and no handler is constructed from
    anything a model emitted.
    """

    __slots__ = (
        "_artifacts",
        "_catalog",
        "_fetch_hosts",
        "_fetch_transport",
        "_read_roots",
        "_redact_args",
        "_registry",
        "_resolver",
        "_sandbox",
        "_timeout_seconds",
        "_workspace_root",
    )

    def __init__(
        self,
        settings: Settings,
        *,
        sandbox: TieredSandbox | None = None,
        limits: ResourceLimits | None = None,
        resolver: Resolver | None = None,
        fetch_transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Assemble the registry over one sandbox, and validate the roots before any call.

        Args:
            settings: The validated configuration; ``[tools]`` and ``[storage]`` are what is read.
            sandbox: The sandbox to register ``run_command`` against, or ``None`` to build one from
                ``[tools] container_image``. Injected so a test can shape the probe's view of the
                host without mutating the host.
            resolver: How ``http_fetch`` resolves a hostname, or ``None`` for the real one.
                Injected because ADR-0026 §3's literal-IP and rebinding checks are only testable
                against a resolution the test controls.
            fetch_transport: ``http_fetch``'s httpx transport, or ``None`` for the real one.
                Injected so the whole fetch path — allowlist, redirects, media types, size caps —
                is exercised without opening a socket, which is what keeps spec §20 #10 ("the
                suite passes with no network") true now that a network tool ships.
            limits: The resource limits every isolated command runs under, or ``None`` for
                ToolYard's defaults.

        Raises:
            ConfigurationError: If ``workspace_root``, ``artifact_root`` or a read root is not
                absolute; if a read root would overlap the workspace root, which would make the
                path half and the subprocess half of containment disagree; or if ``limits`` sets a
                ``process_count`` below :data:`toolyard.MIN_PROCESS_COUNT`, which refuses every
                command including the probe's canary.
        """
        tools = settings.tools
        self._workspace_root = _absolute_root(
            tools.workspace_root, default=data_dir() / "workspaces", field="tools.workspace_root"
        )
        self._artifacts = ArtifactStore(
            _absolute_root(
                tools.artifact_root, default=data_dir() / "artifacts", field="tools.artifact_root"
            )
        )
        self._read_roots = tuple(
            _absolute_root(root, default=None, field="tools.read_roots")
            for root in tools.read_roots
        )
        _refuse_overlap(self._workspace_root, self._read_roots)
        if limits is not None and limits.process_count < MIN_PROCESS_COUNT:
            message = (
                f"tool resource limits set process_count={limits.process_count}, below "
                f"{MIN_PROCESS_COUNT}: the limit counts bwrap's own init and prlimit, so a value "
                "this low refuses every command including the probe's canary"
            )
            raise ConfigurationError(message, details={"field": "tools.process_count"})
        self._fetch_hosts = tuple(host.lower() for host in tools.fetch_allowed_hosts)
        self._resolver = resolver if resolver is not None else _resolve_host
        self._fetch_transport = fetch_transport
        self._redact_args = frozenset(tools.redact_args)
        self._timeout_seconds = float(tools.timeout_seconds or DEFAULT_TIMEOUT_SECONDS)
        self._sandbox = (
            sandbox
            if sandbox is not None
            else TieredSandbox(container_image=tools.container_image, limits=limits)
        )
        self._registry, self._catalog = self._assemble(tools.enabled)

    # -------------------------------------------------------------------------------------------
    # Assembly
    # -------------------------------------------------------------------------------------------

    def _assemble(
        self, enabled: Sequence[str]
    ) -> tuple[ToolRegistry, tuple[ToolCatalogEntry, ...]]:
        """Register every enabled tool that may run, and record why the others may not."""
        registry = ToolRegistry()
        catalog: list[ToolCatalogEntry] = []
        for name in dict.fromkeys(enabled):
            built = self._build(name)
            if built is None:
                catalog.append(_withheld(name, UNSHIPPED_TOOL_CAUSE, _UNSHIPPED_DESCRIPTION))
                continue
            spec, handler = built
            registry.register(spec, handler)
            catalog.append(
                ToolCatalogEntry(
                    name=spec.name,
                    description=spec.description,
                    registered=True,
                    risk_class=spec.risk_class.value,
                    egress=spec.egress.value,
                    requires_isolation=spec.requires_isolation,
                    redact_args=spec.redact_args,
                    parameters=spec.args_schema,
                )
            )
        return registry, tuple(catalog)

    def _build(self, name: str) -> tuple[ToolSpec, Any] | None:
        """Return the ``(spec, handler)`` pair for one shipped tool, honouring ``redact_args``."""
        pair: tuple[ToolSpec, Any] | None
        if name == "read_file":
            pair = read_file_tool()
        elif name == "list_dir":
            pair = list_dir_tool()
        elif name == "write_file":
            pair = write_file_tool()
        elif name == "run_command":
            # The same instance the executor is built with (D1 finding 1), and an explicit PATH
            # allowlist: under bwrap `--clearenv` the child has no environment at all, and
            # `os.environ` is never the answer because the environment is the caller's input.
            pair = run_command_tool(self._sandbox)
        elif name == "http_fetch":
            # Registered from Phase 6, egress-checked. The host allowlist here is ToolYard's own
            # ADR-0026 §3 check and is *not* the egress decision: an allowlisted host still has to
            # clear the classification ceiling, and that verdict is recorded by
            # `EgressService` before this handler is ever entered. Two independent gates, because
            # the allowlist answers "may anyone reach this host" and the decision answers "may
            # *this trajectory's data* reach it".
            pair = http_fetch_tool(
                self._fetch_hosts, resolve=self._resolver, transport=self._fetch_transport
            )
        else:
            return None
        if name in self._redact_args:
            spec, handler = pair
            pair = (_redacting(spec), handler)
        return pair

    # -------------------------------------------------------------------------------------------
    # What the process holds
    # -------------------------------------------------------------------------------------------

    @property
    def registry(self) -> ToolRegistry:
        """The registered tools. Assembled at startup; never added to afterwards."""
        return self._registry

    @property
    def sandbox(self) -> TieredSandbox:
        """The one sandbox every executor and ``run_command`` share."""
        return self._sandbox

    @property
    def artifacts(self) -> ArtifactStore:
        """Where oversize tool output is filed, by the digest of the whole output."""
        return self._artifacts

    @property
    def timeout_seconds(self) -> float:
        """The per-call limit applied when an invocation names none."""
        return self._timeout_seconds

    @property
    def workspace_root(self) -> Path:
        """The parent of every trajectory's workspace."""
        return self._workspace_root

    def catalog(self) -> tuple[ToolCatalogEntry, ...]:
        """Return every tool ``[tools] enabled`` names, registered or withheld, in configured order.

        Returns:
            The catalog. A withheld entry carries the cause, because "not in the list" and "in the
            list and refused" are different facts and an operator debugging a refusal needs to tell
            them apart.
        """
        return self._catalog

    def entry(self, name: str) -> ToolCatalogEntry | None:
        """Return one catalog entry by exact name, or ``None``.

        Args:
            name: The tool name.

        Returns:
            The entry, or ``None`` when configuration does not name it.
        """
        for entry in self._catalog:
            if entry.name == name:
                return entry
        return None

    def isolation(self) -> TierReport:
        """Probe the host once and report which isolation rung commands run under.

        Returns:
            ToolYard's :class:`toolyard.TierReport`. ``reason`` names every rung the probe visited
            and why it was skipped or failed — the string ``doctor`` prints, so an operator can see
            *which rung the ladder landed on and why* without reading logs.
        """
        return self._sandbox.report()

    # -------------------------------------------------------------------------------------------
    # Per-trajectory
    # -------------------------------------------------------------------------------------------

    def for_trajectory(self, trajectory_id: str, *, allowlist: frozenset[str]) -> TrajectoryTools:
        """Build one trajectory's workspace and its narrowed view of the registry.

        The directory is created here rather than at submission: a trajectory that never calls a
        tool never gets one, and nothing has to clean up after a trajectory that halted before its
        first turn.

        Args:
            trajectory_id: The trajectory. Used as the workspace directory's name, and it is a ULID
                minted by this application — never model text.
            allowlist: The caller's declared tool set.

        Returns:
            The apparatus for that trajectory.

        Raises:
            ConfigurationError: If the workspace directory cannot be created.
        """
        write_root = self._workspace_root / trajectory_id
        try:
            write_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            message = f"the workspace for trajectory {trajectory_id} could not be created: {exc}"
            raise ConfigurationError(
                message, details={"field": "tools.workspace_root", "path": str(write_root)}
            ) from exc
        return TrajectoryTools(
            plant=self,
            trajectory_id=trajectory_id,
            workspace=SandboxPaths(write_root=write_root, read_roots=self._read_roots),
            allowlist=frozenset(allowlist),
        )

    def sweep_workspace(self, trajectory_id: str) -> bool:
        """Remove one trajectory's workspace, keeping every record and hash that described it.

        The plan's decided answer to its own named risk: workspaces follow **content** retention,
        swept with transcript text rather than kept forever or deleted at the end of a run. The
        caller is therefore the retention sweep, not the loop — a trajectory that has just finished
        is inside its retention window, and deleting its workspace there would destroy the files an
        operator reads while diagnosing the run that produced them.

        Args:
            trajectory_id: The trajectory whose workspace to remove.

        Returns:
            ``True`` when a directory was removed, ``False`` when there was nothing to remove — a
            trajectory that called no tool has no workspace, and that is not an error.
        """
        target = self._workspace_root / trajectory_id
        if not target.is_dir():
            return False
        shutil.rmtree(target)
        return True

    def spill(self, result: ToolResult, *, result_sha256: str, limit: int) -> str | None:
        """File a tool output too large for a transcript, if it can be filed honestly.

        Three refusals, and the middle one is what makes ``artifact_ref`` mean something. An
        artifact exists to hold what the model was **not** shown; an output that fits in the turn
        was shown in full, so filing it would put a second copy on disk for every successful call
        and leave ``artifact_ref`` populated on every row — which is the same as it saying nothing.

        ``limit`` is deliberately the same number
        :func:`~promptcadence.services.loop._shown_result` truncates at, so for an ``OK`` result
        the two answers cannot disagree: an artifact is written exactly when the model saw a
        prefix. The one asymmetry is honest and intended — a result ToolYard itself truncated is
        shown as a prefix and filed **nowhere**, so ``output_truncated`` without an
        ``artifact_ref`` reads as "the whole output no longer exists to be filed".

        Args:
            result: The executor's result. Its ``content`` is the whole cleaned output while it
                fits under :data:`ARTIFACT_CEILING_BYTES`.
            result_sha256: The digest the record carries — of the **whole** output.
            limit: ``[tools] max_result_chars``. At or below it the output lives in the turn and
                nothing is written.

        Returns:
            The artifact reference, or ``None`` when nothing was written — because the call did not
            produce output, because the output is small enough to live in the turn, or because the
            content's own digest does not match ``result_sha256``, which means ToolYard truncated
            it and a prefix filed under the whole output's hash would be a record pointing at
            something it does not describe.
        """
        if result.status is not ToolStatus.OK:
            return None
        if len(result.content) <= limit:
            return None
        if sha256_of(result.content) != result_sha256:
            return None
        return self._artifacts.put(result.content, digest=result_sha256)


_UNSHIPPED_DESCRIPTION: Final[str] = (
    "Named by [tools] enabled and not shipped. Tool handlers are registered in code at startup "
    "(ADR-0053 decision 1), so no configuration can supply one; this line exists so a typo is "
    "visible rather than silent."
)


def _resolve_host(host: str) -> Sequence[str]:
    """Resolve a hostname to its addresses, for ``http_fetch``'s ADR-0026 §3 checks.

    The real resolution, kept to the one line ToolYard's :data:`~toolyard.Resolver` documents so
    that the injected test double and the shipped behaviour differ in nothing but the addresses
    they return.

    Args:
        host: The hostname from the URL the model asked for.

    Returns:
        Every address the host resolves to. ToolYard checks each one, so a name that resolves to a
        mix of public and loopback addresses is judged on all of them, not on the first.
    """
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


def _withheld(name: str, cause: str, description: str) -> ToolCatalogEntry:
    """Build the catalog entry for a configured tool that was not registered."""
    return ToolCatalogEntry(
        name=name, description=description, registered=False, withheld_cause=cause
    )


def _redacting(spec: ToolSpec) -> ToolSpec:
    """Return the same declaration with ``redact_args`` set.

    ``ToolSpec`` is frozen and the shipped builders take no such argument, so the flag is applied
    by rebuilding the declaration from its own fields. The schemas are re-validated and re-frozen
    by the constructor, which is the point: a redacted spec is a spec, checked the same way.
    """
    return ToolSpec(
        spec.name,
        spec.description,
        spec.args_schema,
        spec.result_schema,
        spec.risk_class,
        spec.egress,
        redact_args=True,
        path_args=spec.path_args,
        requires_isolation=spec.requires_isolation,
    )


def _absolute_root(configured: str, *, default: Path | None, field: str) -> Path:
    """Resolve one configured root to an absolute path, refusing a relative one.

    Args:
        configured: The configured value; empty means the default.
        default: What an empty value resolves to, or ``None`` when the value is required.
        field: The configuration key, for the error's details.

    Returns:
        The absolute path, with ``~`` expanded.

    Raises:
        ConfigurationError: If the value is empty with no default, or is relative. A relative root
            would resolve against whatever directory the process happened to start in, which is
            the thing containment refuses for a model's candidate path and must not be reachable
            through configuration instead.
    """
    raw = configured.strip()
    if not raw:
        if default is None:
            message = f"{field} must name a path"
            raise ConfigurationError(message, details={"field": field})
        return default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        message = (
            f"{field} must be an absolute path; got {raw!r}. A relative root resolves against "
            "whatever directory the process started in."
        )
        raise ConfigurationError(message, details={"field": field, "value": raw})
    return path


def _refuse_overlap(workspace_root: Path, read_roots: Sequence[Path]) -> None:
    """Refuse a read root that equals, contains or sits inside the workspace root.

    ``SandboxPaths`` makes the same check per trajectory, which would surface the mistake on the
    first tool call of the first trajectory. Making it here, over the *parent*, moves it to startup
    and covers every trajectory at once.

    Raises:
        ConfigurationError: If any read root overlaps the workspace root or another read root.
    """
    roots = (workspace_root, *read_roots)
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if root == other or root in other.parents or other in root.parents:
                message = (
                    f"tool roots must not overlap: {str(root)!r} and {str(other)!r}. A read root "
                    "inside the workspace root (or the reverse) is a workspace whose two "
                    "containment halves would disagree about it."
                )
                raise ConfigurationError(
                    message, details={"field": "tools.read_roots", "roots": [str(r) for r in roots]}
                )


def tools_health_component(plant: ToolPlant) -> ComponentHealth:
    """Report the tool registry and the isolation rung, for ``health`` and ``doctor``.

    Never ``unavailable``, and never a startup failure. A host with no isolation rung still runs
    every filesystem tool; what it cannot run is ``run_command``, which ToolYard refuses with
    ``isolation_unavailable`` — a refusal the model reads and a record an operator can see. That is
    a degraded capability, not a broken application, and reporting it as unavailable would take the
    server down over a tool nobody may have asked for.

    Args:
        plant: The process's plant. Its probe runs on the first call and is cached, so asking this
            repeatedly costs one canary for the life of the process.

    Returns:
        The component. ``detail`` carries the registered names, the withheld ones with their cause,
        and ToolYard's ``TierReport.reason`` — which names every rung the probe visited and why it
        was skipped or failed, so "why did my command refuse" is answerable without reading logs.
    """
    report = plant.isolation()
    registered = [entry.name for entry in plant.catalog() if entry.registered]
    withheld = [
        f"{entry.name} ({entry.withheld_cause})"
        for entry in plant.catalog()
        if not entry.registered
    ]
    parts = [f"{len(registered)} registered: {', '.join(registered) or 'none'}"]
    if withheld:
        parts.append(f"withheld: {', '.join(withheld)}")
    parts.append(f"isolation {report.tier.value}: {report.reason}")
    if report.limits_unenforced:
        parts.append(f"limits unenforced: {', '.join(report.limits_unenforced)}")
    unavailable = report.tier is IsolationTier.UNAVAILABLE
    needs_isolation = any(entry.requires_isolation for entry in plant.catalog())
    status = ComponentStatus.DEGRADED if unavailable and needs_isolation else ComponentStatus.OK
    return ComponentHealth(name="tools", status=status, detail="; ".join(parts))
