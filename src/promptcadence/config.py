"""promptcadence.config — typed settings, source-tracked, per Configuration Standards.

Precedence, lowest to highest: built-in defaults, ``config.toml``, ``PROMPTCADENCE_``-prefixed
environment variables, then explicit overrides (the CLI's highest layer). Overriding is per leaf
field, not per section (configuration standards §1): setting one field of ``[server]`` never
discards its siblings.

The merge is performed here rather than by ``pydantic-settings``'s own source machinery, as in the
three sibling applications: ``promptcadence config show`` has to report *which* layer produced
every leaf value, which is easiest to get right by building the merged dict and tracking
provenance alongside it, then handing the result to pydantic once for validation.

Spec §12 says startup validation *refuses* rather than warns. What this module can check needs no
database: remote-tier rules, classification values, project-budget rules, and the config-only half
of ADR-0026's binding refusal (bind acknowledgement, ``server.allowed_hosts``). The database-backed
half — an active token before a non-loopback bind, and an ``approve``-scoped token before
``approval.mode = "manual"`` (ADR-0049 rule 2) — needs the ``api_tokens`` table and runs in
:mod:`promptcadence.bootstrap`, once the database is ready, the same split LoadCoach uses.
"""

from __future__ import annotations

import difflib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final, Literal

from baseaicore import ConfigurationError, DataClassification, normalize_currency
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from toolyard import DEFAULT_CONTAINER_IMAGE

__all__ = [
    "ENV_PREFIX",
    "EXAMPLE_CONFIG_TOML",
    "LOOPBACK_HOSTS",
    "ApprovalSettings",
    "BudgetSettings",
    "CompactionSettings",
    "ConfigurationError",
    "ExecutionSettings",
    "InsecureBindingError",
    "LoadCoachSettings",
    "LoadedSettings",
    "LoggingSettings",
    "MoneyAmount",
    "PlanningSettings",
    "PolicySettings",
    "ProjectBudget",
    "ServerSettings",
    "Settings",
    "StorageSettings",
    "Tier",
    "ToolsSettings",
    "config_dir",
    "data_dir",
    "load_settings",
    "resolve_config_path",
    "state_dir",
]

ENV_PREFIX: Final = "PROMPTCADENCE_"
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
_ALL_INTERFACES_HOST = "0.0.0.0"  # noqa: S104 — compared against, never bound to, by this module
_RESERVED_ENV_SUFFIXES: Final[frozenset[str]] = frozenset({"CONFIG", "DATA_DIR", "LOG_LEVEL"})
_DEFAULT_PORT: Final = 8768


class InsecureBindingError(ConfigurationError):
    """A configured bind/auth combination would expose the service unsafely.

    Raised by :func:`load_settings` before anything opens a socket (configuration standards §4).
    Every rule here has a documented, deliberate acknowledgement that lifts it; none can be
    satisfied by accident.
    """

    code: ClassVar[str] = "INSECURE_BINDING"


def _split_csv(value: Any) -> Any:
    """Accept a comma-separated string for a tuple field, as environment variables must (§3)."""
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return value


class ServerSettings(BaseModel):
    """``[server]`` — bind address and HTTP-level limits."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(
        default="127.0.0.1",
        description=(
            "Interface to bind. Loopback by default; anything else requires allowed_hosts and at "
            "least one active API token (ADR-0026)."
        ),
        examples=["127.0.0.1"],
    )
    port: int = Field(default=_DEFAULT_PORT, ge=1, le=65535, examples=[_DEFAULT_PORT])
    allow_lan_exposure: bool = Field(
        default=False,
        description=(
            "Acknowledges a deliberate bind to every interface (0.0.0.0). Without it such a bind "
            "refuses to start."
        ),
    )
    allowed_hosts: tuple[str, ...] = Field(
        default=(),
        description=(
            "Host header values accepted on a non-loopback bind, against DNS rebinding. "
            "Comma-separated in the environment."
        ),
        examples=[["promptcadence.local"]],
    )
    rate_limit_per_minute: int = Field(default=600, ge=1, examples=[600])
    max_body_bytes: int = Field(default=1_048_576, ge=1024, examples=[1_048_576])

    _split_hosts = field_validator("allowed_hosts", mode="before")(_split_csv)


class StorageSettings(BaseModel):
    """``[storage]`` — database location and transcript retention (mirrors LoadCoach)."""

    model_config = ConfigDict(extra="forbid")

    database_url: str | None = Field(
        default=None,
        description="SQLAlchemy URL. Unset resolves to a SQLite file under the XDG data directory.",
    )
    auto_migrate: bool = Field(
        default=True,
        description=(
            "Migrate on startup. Defaults true on SQLite; a PostgreSQL URL turns it off, because "
            "a failed migration there cannot be rolled back automatically (database standards "
            "§5.1)."
        ),
    )
    content_retention_hours: int = Field(
        default=24,
        ge=0,
        description=(
            "How long transcript text (turn and tool-call content) is kept after a trajectory "
            "finishes; records, hashes, usage and decisions are kept forever regardless."
        ),
    )
    retain_content: bool = Field(
        default=False,
        description="Config-only switch mirroring LoadCoach's; the retention sweep arrives later.",
    )

    @model_validator(mode="after")
    def _apply_data_dir_default(self) -> StorageSettings:
        """Fill the zero-configuration database default, and relax auto_migrate on PostgreSQL."""
        if self.database_url is None:
            self.database_url = f"sqlite:///{data_dir() / 'promptcadence.sqlite3'}"
        if "auto_migrate" not in self.model_fields_set and not self.database_url.startswith(
            "sqlite"
        ):
            self.auto_migrate = False
        return self


class LoadCoachSettings(BaseModel):
    """``[loadcoach]`` — the only path to a model (ADR-0045). Never required at startup."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(default="http://127.0.0.1:8766", examples=["http://127.0.0.1:8766"])
    api_key_env: str = Field(
        default="",
        description="Name of the environment variable holding the token, or empty (ADR-0026).",
    )
    api_key_file: str = Field(
        default="",
        description="Path to a file holding the token, or empty. Mutually exclusive with above.",
    )
    timeout_seconds: float = Field(default=600.0, gt=0, examples=[600.0])

    @model_validator(mode="after")
    def _exclusive_credential(self) -> LoadCoachSettings:
        """Refuse naming both a credential environment variable and a credential file."""
        if self.api_key_env and self.api_key_file:
            message = (
                "loadcoach.api_key_env and loadcoach.api_key_file are both set; a credential has "
                "exactly one source. Clear one of the two."
            )
            raise ValueError(message)
        return self


class PlanningSettings(BaseModel):
    """``[planning]`` — the bypass switch. Governance is never bypassed, only planning is."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True)
    allow_request_override: bool = Field(
        default=True, description="Permit a per-request bypass_planning override."
    )
    reapproval_scope: Literal["on_tier_or_classification_change", "any_deviation"] = Field(
        default="on_tier_or_classification_change"
    )
    max_plan_steps: int = Field(default=20, ge=1)


class MoneyAmount(BaseModel):
    """A configured money amount: a currency code plus whole nanos (billionths of one unit).

    Kept as a small config-native shape rather than :class:`baseaicore.Money` itself — TOML gives
    us an untyped ``{currency, nanos}`` table, and this is that table validated. Convert with
    ``baseaicore.Money(currency=amount.currency, nanos=amount.nanos)`` at the point of use.
    """

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(default="USD", examples=["USD"])
    nanos: int = Field(default=0, ge=0, examples=[5_000_000_000])

    @field_validator("currency")
    @classmethod
    def _normalize(cls, value: str) -> str:
        """Normalize to the alpha-3 form :class:`baseaicore.Money` itself requires."""
        return normalize_currency(value)


class ApprovalSettings(BaseModel):
    """``[approval]`` — who authorizes the minting of an ``ExecutionIntent`` (ADR-0049)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["auto", "hybrid", "manual"] = Field(default="auto")
    gate_egress_at: DataClassification = Field(default=DataClassification.INTERNAL)
    gate_step_cost: MoneyAmount = Field(
        default_factory=lambda: MoneyAmount(currency="USD", nanos=1_000_000_000)
    )
    request_timeout_hours: float = Field(default=24.0, gt=0)


class ExecutionSettings(BaseModel):
    """``[execution]`` — concurrency and loop bounds. Nothing here executes until Phase 3+."""

    model_config = ConfigDict(extra="forbid")

    max_concurrent_trajectories: int = Field(default=1, ge=1)
    max_concurrent_steps: int = Field(default=1, ge=1)
    max_concurrent_remote_steps: int = Field(default=2, ge=1)
    max_turns_per_step: int = Field(default=8, ge=1)
    max_steps: int = Field(default=20, ge=1)
    lease_seconds: int = Field(default=60, ge=1)


class ProjectBudget(BaseModel):
    """One ``[budget.projects.<name>]`` entry: a labelled ceiling binding a project's work."""

    model_config = ConfigDict(extra="forbid")

    money_ceiling: MoneyAmount | None = Field(default=None)
    token_ceiling: int | None = Field(default=None, ge=1)


class BudgetSettings(BaseModel):
    """``[budget]`` — the two ceilings, because one alone cannot bind (ADR-0047 §3)."""

    model_config = ConfigDict(extra="forbid")

    default_money_ceiling: MoneyAmount = Field(
        default_factory=lambda: MoneyAmount(currency="USD", nanos=5_000_000_000)
    )
    default_token_ceiling: int = Field(default=2_000_000, ge=1)
    daily_money_ceiling: MoneyAmount = Field(
        default_factory=lambda: MoneyAmount(currency="USD", nanos=20_000_000_000)
    )
    estimate_min_samples: int = Field(default=20, ge=1)
    partial_pricing: Literal["floor", "strict"] = Field(default="floor")
    on_exhausted: Literal["approval", "halt"] = Field(default="approval")
    on_daily_exhausted: Literal["window", "approval", "halt"] = Field(default="window")
    window_wait_max_days: int = Field(default=3, ge=1)
    projects: dict[str, ProjectBudget] = Field(default_factory=dict)


class ToolsSettings(BaseModel):
    """``[tools]`` — the registry the loop draws from, and where its side effects land.

    ``enabled`` keeps spec §12's shipped list, ``http_fetch`` included, even though no tool
    performs network egress before Phase 6. The tool is **withheld** from the registry with a named
    cause the catalog shows rather than removed from this default: an operator who copied the
    documented configuration keeps working, P6 flips one guard instead of editing a shipped list,
    and a model asking for it is refused as an unknown tool and recorded. See
    :mod:`promptcadence.services.tools`.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: tuple[str, ...] = Field(
        default=("read_file", "list_dir", "write_file", "run_command", "http_fetch")
    )
    workspace_root: str = Field(
        default="", description="Default: <data>/workspaces, per-trajectory subdirectory."
    )
    artifact_root: str = Field(
        default="",
        description=(
            "Where an oversize tool output is filed, keyed by the digest of the whole output. "
            "Default: <data>/artifacts."
        ),
    )
    read_roots: tuple[str, ...] = Field(default=())
    fetch_allowed_hosts: tuple[str, ...] = Field(default=())
    redact_args: tuple[str, ...] = Field(default=())
    container_image: str = Field(
        default=DEFAULT_CONTAINER_IMAGE,
        description=(
            "The image run_command's container rung uses. Probed and run with --pull=never, so it "
            "must already be present locally; `doctor` shows which rung the ladder landed on."
        ),
    )
    max_result_chars: int = Field(
        default=8_192,
        ge=256,
        description=(
            "How much of a tool result the model is shown before a labelled truncation. Separate "
            "from what is stored: the whole output is kept as an artifact under its digest."
        ),
    )
    timeout_seconds: float = Field(
        default=30.0, gt=0, description="The per-call limit; there is no way to express no limit."
    )

    _split_enabled = field_validator("enabled", mode="before")(_split_csv)
    _split_read_roots = field_validator("read_roots", mode="before")(_split_csv)
    _split_fetch_hosts = field_validator("fetch_allowed_hosts", mode="before")(_split_csv)
    _split_redact_args = field_validator("redact_args", mode="before")(_split_csv)


class CompactionSettings(BaseModel):
    """``[compaction]`` — the CutCtx trigger, from Phase 8 onward."""

    model_config = ConfigDict(extra="forbid")

    threshold: float = Field(default=0.8, gt=0, le=1)
    policy_chain: tuple[str, ...] = Field(
        default=("observation_masking", "summarizing", "drop_oldest")
    )
    protected_recent_turns: int = Field(default=4, ge=0)

    _split_chain = field_validator("policy_chain", mode="before")(_split_csv)


class Tier(BaseModel):
    """One ``[tiers.<name>]`` entry: configuration over exactly one LoadCoach task profile.

    PromptCadence performs no routing math of its own (ADR-0047) — *which* model within the tier
    stays LoadCoach's filter/score/rank/select, driven by ``task_profile``.
    """

    model_config = ConfigDict(extra="forbid")

    task_profile: str = Field(default="")
    remote: bool = Field(default=False, description="The egress class.")
    max_data_classification: DataClassification | None = Field(
        default=None, description="Required when remote; never meaningful when local."
    )
    context_budget_tokens: int = Field(default=16_384, ge=1)
    pricing_file: str = Field(
        default="", description="ModelPricing records; required when remote (ADR-0047 §3)."
    )


class PolicySettings(BaseModel):
    """``[policy]`` — where an unplanned or bypass turn starts, and how it escalates."""

    model_config = ConfigDict(extra="forbid")

    default_tier: str = Field(default="local_fast")
    escalation_order: tuple[str, ...] = Field(default=("local_fast", "local_large"))

    _split_order = field_validator("escalation_order", mode="before")(_split_csv)


class LoggingSettings(BaseModel):
    """Structured-logging behaviour."""

    model_config = ConfigDict(extra="forbid")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")
    format: Literal["text", "json", "auto"] = Field(default="auto")
    include_content: bool = Field(
        default=False,
        description="Log full prompts and responses. Off by default: only hashes are logged.",
    )


def _default_tiers() -> dict[str, Tier]:
    """The two tiers a zero-configuration install can actually use.

    Spec §12's example config shows four shipped defaults, including two remote ones whose
    ``pricing_file`` is empty — but an empty ``pricing_file`` on a remote tier is exactly what
    startup validation refuses (`§12`: "a remote tier without a pricing source"). Shipping the
    remote pair as *active* defaults would make a zero-configuration ``promptcadence serve`` refuse
    to start, breaking spec §20 AC1. Resolved here by shipping only the two local tiers as active
    defaults; `remote_cheap` and `remote_frontier` are documented, commented out, in
    :data:`EXAMPLE_CONFIG_TOML`, for an operator to uncomment once pricing is configured — matching
    ADR-0047's "remote tiers are unusable until an operator supplies pricing" intent. Recorded in
    the Phase 1 handoff as a spec ambiguity this resolves.
    """
    return {
        "local_fast": Tier(
            task_profile="tools.agent.local_fast", remote=False, context_budget_tokens=16_384
        ),
        "local_large": Tier(
            task_profile="tools.agent.local_large", remote=False, context_budget_tokens=32_768
        ),
    }


class Settings(BaseModel):
    """The complete, validated PromptCadence configuration.

    Constructed only by :func:`load_settings`, which resolves the precedence chain first — never
    call ``Settings(**raw)`` on unmerged input, or the file/env/CLI layering is bypassed.
    """

    model_config = ConfigDict(extra="forbid")

    server: ServerSettings = Field(default_factory=ServerSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    loadcoach: LoadCoachSettings = Field(default_factory=LoadCoachSettings)
    planning: PlanningSettings = Field(default_factory=PlanningSettings)
    approval: ApprovalSettings = Field(default_factory=ApprovalSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    compaction: CompactionSettings = Field(default_factory=CompactionSettings)
    tiers: dict[str, Tier] = Field(default_factory=_default_tiers)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


@dataclass(frozen=True, slots=True)
class LoadedSettings:
    """The result of resolving configuration: the settings, and where every value came from."""

    settings: Settings
    config_path: Path
    config_file_used: bool
    sources: dict[str, str]


def config_dir() -> Path:
    """Return ``$XDG_CONFIG_HOME/promptcadence``, falling back to ``~/.config/promptcadence``."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "promptcadence"


def data_dir() -> Path:
    """Return ``$PROMPTCADENCE_DATA_DIR``, else ``$XDG_DATA_HOME/promptcadence``, else default."""
    override = os.environ.get(f"{ENV_PREFIX}DATA_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "promptcadence"


def state_dir() -> Path:
    """Return ``$XDG_STATE_HOME/promptcadence``, falling back to ``~/.local/state``."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "promptcadence"


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Resolve the configuration file location per Configuration Standards §2.

    Args:
        explicit: A path from ``--config``, if the caller was given one.

    Returns:
        The path to read. Order: the explicit path, then ``PROMPTCADENCE_CONFIG``, then a
        project-local ``./promptcadence.toml`` if one exists, then the XDG default. A missing
        file at the resolved path is not an error — :func:`load_settings` falls back to defaults,
        which is what makes "starts with zero configuration" true (spec §20 AC1).
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    env_path = os.environ.get(f"{ENV_PREFIX}CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    local = Path.cwd() / "promptcadence.toml"
    if local.is_file():
        return local
    return config_dir() / "config.toml"


def _read_env(prefix: str) -> dict[str, Any]:
    """Parse ``<prefix>SECTION__FIELD`` environment variables into a nested dict."""
    nested: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        if suffix in _RESERVED_ENV_SUFFIXES:
            continue
        path = suffix.lower().split("__")
        node = nested
        for part in path[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):  # a leaf and a section cannot share a name
                break
            node = child
        else:
            node[path[-1]] = value

    log_level = os.environ.get(f"{prefix}LOG_LEVEL")
    if log_level and "level" not in nested.get("logging", {}):
        nested.setdefault("logging", {})["level"] = log_level
    return nested


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` onto ``base``, recursively, per leaf field rather than per section."""
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _known_dotted_keys() -> list[str]:
    """Every ``section`` and ``section.field`` name Settings recognizes, for typo suggestions."""
    known: list[str] = []
    for section_name, section_field in Settings.model_fields.items():
        known.append(section_name)
        section_model = section_field.annotation
        if isinstance(section_model, type) and issubclass(section_model, BaseModel):
            known.extend(f"{section_name}.{name}" for name in section_model.model_fields)
    return known


def _translate_validation_error(
    exc: PydanticValidationError, config_path: Path
) -> ConfigurationError:
    """Turn a pydantic ``ValidationError`` into a :class:`ConfigurationError` naming the field."""
    known_keys = _known_dotted_keys()
    problems: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"])
        if error["type"] == "extra_forbidden":
            suggestion = difflib.get_close_matches(loc, known_keys, n=1)
            hint = f" (did you mean '{suggestion[0]}'?)" if suggestion else ""
            problems.append(f"unknown configuration key '{loc}'{hint}")
        else:
            problems.append(f"{loc}: {error['msg']}")
    message = f"Configuration invalid ({config_path}): " + "; ".join(problems)
    return ConfigurationError(message, details={"file": str(config_path), "problems": problems})


def _validate_security(settings: Settings) -> None:
    """Refuse the config-only half of ADR-0026's non-loopback bind refusal set.

    The database-backed half — at least one active API token, and (ADR-0049 rule 2) an
    ``approve``-scoped one when ``approval.mode = "manual"`` — is checked in
    :func:`promptcadence.bootstrap.bootstrap`, once the database is ready.
    """
    server = settings.server
    if server.host == _ALL_INTERFACES_HOST and not server.allow_lan_exposure:
        raise InsecureBindingError(
            "server.host is '0.0.0.0' (all interfaces) but server.allow_lan_exposure is false. "
            "Exposing the service beyond this machine must be a deliberate act: set "
            "server.allow_lan_exposure = true if that is intended.",
            details={"field": "server.allow_lan_exposure", "host": server.host},
        )
    if server.host not in LOOPBACK_HOSTS and not server.allowed_hosts:
        raise InsecureBindingError(
            "server.host is not loopback but server.allowed_hosts is empty. A non-loopback bind "
            "must name every hostname it will accept, or DNS rebinding can reach it (ADR-0026).",
            details={"field": "server.allowed_hosts", "host": server.host},
        )


def _validate_tiers(settings: Settings) -> None:
    """Refuse a tier naming no task profile, or a remote tier missing its ceiling data (§12)."""
    for name, tier in settings.tiers.items():
        if not tier.task_profile.strip():
            message = (
                f"tiers.{name} has no task_profile. Every tier names exactly one LoadCoach task "
                "profile (ADR-0047 §1)."
            )
            raise ConfigurationError(message, details={"field": f"tiers.{name}.task_profile"})
        if not tier.remote:
            continue
        if tier.max_data_classification is None:
            message = (
                f"tiers.{name} is remote but sets no max_data_classification. A remote tier must "
                "declare the classification ceiling data may be sent under (ADR-0047 §2)."
            )
            raise ConfigurationError(
                message, details={"field": f"tiers.{name}.max_data_classification"}
            )
        if not tier.pricing_file.strip():
            message = (
                f"tiers.{name} is remote but sets no pricing_file. Unpriced egress is refused, "
                "not free: a remote tier needs a pricing source before any call can be budgeted "
                "(ADR-0047 §3)."
            )
            raise ConfigurationError(message, details={"field": f"tiers.{name}.pricing_file"})


def _validate_project_budgets(settings: Settings) -> None:
    """Refuse a ``[budget.projects.<name>]`` entry that binds neither ceiling (spec §12)."""
    for name, project in settings.budget.projects.items():
        if project.money_ceiling is None and project.token_ceiling is None:
            message = (
                f"budget.projects.{name} sets neither money_ceiling nor token_ceiling. A project "
                "binding neither ceiling constrains nothing; set at least one."
            )
            raise ConfigurationError(message, details={"field": f"budget.projects.{name}"})


def _track_sources(
    file_data: dict[str, Any], env_data: dict[str, Any], cli_data: dict[str, Any]
) -> dict[str, str]:
    """Report, for every leaf field, which layer produced its effective value."""
    sources: dict[str, str] = {}
    for section_name, section_field in Settings.model_fields.items():
        section_model = section_field.annotation
        if not (isinstance(section_model, type) and issubclass(section_model, BaseModel)):
            continue
        for field_name in section_model.model_fields:
            path = f"{section_name}.{field_name}"
            if field_name in cli_data.get(section_name, {}):
                sources[path] = "cli"
            elif field_name in env_data.get(section_name, {}):
                sources[path] = f"env {ENV_PREFIX}{section_name.upper()}__{field_name.upper()}"
            elif field_name in file_data.get(section_name, {}):
                sources[path] = "file"
            else:
                sources[path] = "default"
    return sources


def load_settings(
    *,
    config_path: str | Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> LoadedSettings:
    """Resolve configuration through the full precedence chain and validate it.

    Args:
        config_path: An explicit ``--config`` path. See :func:`resolve_config_path` for the
            fallback order when this is ``None``.
        cli_overrides: Explicit values from CLI flags, nested as the TOML file is
            (``{"server": {"port": 9000}}``). The highest-precedence layer.

    Returns:
        The validated :class:`LoadedSettings`, with a ``sources`` map naming the layer behind
        every leaf value.

    Raises:
        ConfigurationError: The file is not valid TOML, a key is unrecognized, a value fails a
            field's type or range (including an unknown ``DataClassification`` value), a tier
            names no task profile or (when remote) no classification ceiling or pricing source, a
            project budget binds neither ceiling, or a bind combination is unsafe
            (:class:`InsecureBindingError`, a subclass). Does **not** check for an active or
            ``approve``-scoped API token — see :mod:`promptcadence.bootstrap`.
    """
    resolved_path = resolve_config_path(config_path)
    file_data: dict[str, Any] = {}
    file_used = False
    if resolved_path.is_file():
        try:
            with resolved_path.open("rb") as handle:
                file_data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(
                f"Configuration file {resolved_path} is not valid TOML: {exc}",
                details={"file": str(resolved_path)},
            ) from exc
        file_used = True

    env_data = _read_env(ENV_PREFIX)
    cli_data = cli_overrides or {}
    merged = _deep_merge(_deep_merge(file_data, env_data), cli_data)

    try:
        settings = Settings.model_validate(merged)
    except PydanticValidationError as exc:
        raise _translate_validation_error(exc, resolved_path) from exc

    _validate_security(settings)
    _validate_tiers(settings)
    _validate_project_budgets(settings)

    sources = _track_sources(file_data, env_data, cli_data)
    return LoadedSettings(
        settings=settings, config_path=resolved_path, config_file_used=file_used, sources=sources
    )


EXAMPLE_CONFIG_TOML: Final = """\
# PromptCadence configuration.
# Every key below is optional; a fresh install with no file at all is fully functional and starts
# with no LoadCoach reachable (health reports it degraded, never a startup failure).
# Precedence: defaults -> this file -> PROMPTCADENCE_* environment variables -> CLI flags.

[server]
host = "127.0.0.1"
port = 8768
allow_lan_exposure = false
allowed_hosts = []          # required when host is not loopback (ADR-0026)
rate_limit_per_minute = 600
max_body_bytes = 1048576

[storage]
# database_url defaults to a location under the XDG data directory.
content_retention_hours = 24    # transcript text; records/hashes kept forever
retain_content = false

[loadcoach]
base_url = "http://127.0.0.1:8766"
api_key_env = ""            # or api_key_file (ADR-0026); never both
timeout_seconds = 600.0

[planning]
enabled = true               # the bypass switch -- governance is never bypassed
allow_request_override = true
reapproval_scope = "on_tier_or_classification_change"   # or "any_deviation"
max_plan_steps = 20

[approval]
mode = "auto"                # auto | hybrid | manual
gate_egress_at = "internal"  # hybrid: a step at/above this classification needs a human
request_timeout_hours = 24.0

[approval.gate_step_cost]
currency = "USD"
nanos = 1000000000           # $1.00/step

[execution]
max_concurrent_trajectories = 1
max_concurrent_steps = 1
max_concurrent_remote_steps = 2
max_turns_per_step = 8
max_steps = 20
lease_seconds = 60

[budget]
default_token_ceiling = 2000000
estimate_min_samples = 20
partial_pricing = "floor"    # floor | strict (ADR-0069)
on_exhausted = "approval"    # approval | halt
on_daily_exhausted = "window"  # window | approval | halt
window_wait_max_days = 3

[budget.default_money_ceiling]
currency = "USD"
nanos = 5000000000            # $5.00

[budget.daily_money_ceiling]
currency = "USD"
nanos = 20000000000           # $20.00

# [budget.projects.research]
# token_ceiling = 100000000   # a project binding neither ceiling is refused
# [budget.projects.research.money_ceiling]
# currency = "USD"
# nanos = 50000000000         # $50.00, lifetime, until raised

[tools]
# http_fetch is listed and deliberately NOT registered before Phase 6: no tool performs network
# egress until egress governance is in place. `promptcadence tools list` shows it as withheld with
# the cause, and a model that asks for it is refused as an unknown tool and recorded.
enabled = ["read_file", "list_dir", "write_file", "run_command", "http_fetch"]
workspace_root = ""          # default: <data>/workspaces; per-trajectory subdirectory
artifact_root = ""           # default: <data>/artifacts; oversize output, keyed by its digest
read_roots = []              # extra read-only roots; absolute, and disjoint from workspace_root
fetch_allowed_hosts = []
redact_args = []             # these tools' arguments are stored as a hash only
container_image = "python:3.12-slim"   # run_command's container rung; --pull=never, so it must
                                       # already be present. `doctor` shows the rung and why.
max_result_chars = 8192      # what the model sees of a result; the whole output is kept as an
                             # artifact under its digest, never a truncated body pretending to be
                             # the whole one
timeout_seconds = 30.0       # per call; there is no way to express "no timeout"

[compaction]
threshold = 0.8               # compact when estimate > 0.8 x tier context budget
policy_chain = ["observation_masking", "summarizing", "drop_oldest"]
protected_recent_turns = 4

# The two tiers below are the active zero-configuration defaults.
[tiers.local_fast]
task_profile = "tools.agent.local_fast"
remote = false                # local => max classification implicitly confidential
context_budget_tokens = 16384

[tiers.local_large]
task_profile = "tools.agent.local_large"
remote = false
context_budget_tokens = 32768

# Remote tiers are unusable until pricing is supplied (ADR-0047 §3) -- uncomment and fill in
# pricing_file before adding either to policy.escalation_order.
# [tiers.remote_cheap]
# task_profile = "tools.agent.remote_cheap"
# remote = true
# max_data_classification = "internal"      # never confidential
# context_budget_tokens = 128000
# pricing_file = ""            # ModelPricing records; required for a remote tier
#
# [tiers.remote_frontier]
# task_profile = "tools.agent.remote_frontier"
# remote = true
# max_data_classification = "public"
# context_budget_tokens = 200000
# pricing_file = ""

[policy]
default_tier = "local_fast"     # bypass mode and unplanned turns start here
escalation_order = ["local_fast", "local_large"]

[logging]
level = "INFO"
format = "auto"               # text | json | auto (text on a TTY, json otherwise)
include_content = false
"""
