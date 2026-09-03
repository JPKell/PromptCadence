"""promptcadence.infrastructure.loadcoach — the httpx client, the only door to a model.

ADR-0045 rule 2: PromptCadence reaches a model only through LoadCoach's HTTP API. This module is
that door, and it is deliberately a *thin, strict* one. Thin: every method is one documented
LoadCoach endpoint (``api.md`` §§1-5), a typed request in, a typed response out, and no policy —
which tier, which retry, which halt is the loop's business. Strict: a response PromptCadence
cannot read is refused with the field named rather than read as a number that was never there.

**What the parser knows about the wire, and where each fact comes from.** The response shapes are
transcribed from ``docs/apps/loadcoach/api.md`` §4 and §5 and checked against LoadCoach's own
``ExecutionOutcome.as_json`` / ``job_document`` (LoadCoach ``01170a7``). Three facts matter:

* ``usage`` carries four disjoint token classes plus ``thinking_tokens``. A **number** is a count,
  ``0`` included — under ADR-0070 a zero is the provider's protocol saying nothing could have been
  billed to that class. The **string** ``"unsupported"`` (ADR-0016 rule 4) and JSON ``null`` both
  mean *never reported* and become :data:`baseaicore.UNSUPPORTED`; neither is ever coerced to
  ``0`` or totalled. Until ``modelrack 0.7.0`` ships and LoadCoach adopts it, every real adapter
  reports the cache classes ``"unsupported"``; afterwards ``0`` or a count. The parser reads both.
* ``finish_reason`` is **not on the wire today.** LoadCoach records the provider's declared reason
  on every attempt (``job_attempts.finish_reason``) and renders it nowhere. The parser reads
  ``output.finish_reason`` when a response carries it — the location proposed to LoadCoach in
  ``D2_HANDOFF.md`` — and reports ``None`` otherwise, which the loop treats as *absence*, never as
  success (spec §11 contract 6).
* A repeated ``idempotency_key`` from the same caller returns the **original job's document**
  rather than executing again. That document is a superset of the ``/generate`` response with the
  same keys for everything this parser reads, except that its ``validation`` block carries no
  ``checks``; :attr:`ValidationInfo.checks_reported` says which shape was read.

Every request carries ``X-Client-Name: promptcadence`` (api.md §12): on a loopback bind that name
is the idempotency scope and the ``source`` LoadCoach attributes the job to, and it is what lets
recovery find a job this application started with only ``GET /jobs?source=promptcadence``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
from baseaicore import UNSUPPORTED, ProviderKind, SuiteError, TokenCount, TokenUsage

from promptcadence.domain.errors import (
    CompactionFailedError,
    ErrorCode,
    LoadCoachError,
    LoadCoachUnavailableError,
    SchemaVersionUnsupportedError,
    TierUnavailableError,
)
from promptcadence.domain.threads import FinishReason
from promptcadence.observability.logging import current_correlation

__all__ = [
    "CLIENT_NAME",
    "LOADCOACH_CODE_MAP",
    "NON_TERMINAL_JOB_STATES",
    "CancelOutcome",
    "GenerateRequest",
    "GenerationResponse",
    "JobSummary",
    "LoadCoachClient",
    "Message",
    "ModelEntry",
    "ModelInfo",
    "RoutingInfo",
    "TaskProfileInfo",
    "TimingInfo",
    "ValidationInfo",
    "VersionInfo",
    "map_error",
    "parse_generation",
    "resolve_api_key",
    "token_count_from_wire",
]

CLIENT_NAME: Final = "promptcadence"
"""The ``X-Client-Name`` every request carries; the ``source`` LoadCoach records (api.md §12)."""

API_PREFIX: Final = "/api/v1"
SUPPORTED_API_MAJOR: Final = "v1"

NON_TERMINAL_JOB_STATES: Final[tuple[str, ...]] = (
    "queued",
    "leased",
    "admitted",
    "waiting_resources",
    "executing",
    "validating",
    "retrying",
    "cancelling",
)
"""LoadCoach's lease-holding and waiting job states (queue §2, ADR-0036): a job in any of them is
still in flight, and is what recovery looks for when a turn started and never committed."""

_SCHEMA_CHECK_KIND: Final = "json_schema"
"""The ``validation.checks[].kind`` LoadCoach's ``validate_schema`` writes. Only this kind is a
schema validation; ``json``, ``required_fields``, ``regex`` and the length check are not."""

LOADCOACH_CODE_MAP: Final[Mapping[str, ErrorCode]] = {
    # spec §13's table, row for row.
    "PROVIDER_UNAVAILABLE": ErrorCode.LOADCOACH_ERROR,
    "PROVIDER_TIMEOUT": ErrorCode.LOADCOACH_ERROR,
    "NO_ELIGIBLE_MODEL": ErrorCode.TIER_UNAVAILABLE,
    "QUEUE_FULL": ErrorCode.LOADCOACH_ERROR,
    "MAX_WAIT_EXCEEDED": ErrorCode.LOADCOACH_ERROR,
    "CONTEXT_LIMIT_EXCEEDED": ErrorCode.COMPACTION_FAILED,
    "VALIDATION_FAILED": ErrorCode.LOADCOACH_ERROR,
    "STRUCTURED_OUTPUT_INVALID": ErrorCode.LOADCOACH_ERROR,
    # The rest of LoadCoach spec §13, and its web layer's own codes. A tier whose task profile
    # LoadCoach does not have is a tier LoadCoach cannot serve — TIER_UNAVAILABLE, with the
    # reason, rather than a generic error a caller cannot act on.
    "TASK_PROFILE_NOT_FOUND": ErrorCode.TIER_UNAVAILABLE,
    "MODEL_NOT_FOUND": ErrorCode.LOADCOACH_ERROR,
    "PROVIDER_PROTOCOL_ERROR": ErrorCode.LOADCOACH_ERROR,
    "PROVIDER_REJECTED": ErrorCode.LOADCOACH_ERROR,
    "ALL_CANDIDATES_FAILED": ErrorCode.LOADCOACH_ERROR,
    "INSUFFICIENT_RESOURCES": ErrorCode.LOADCOACH_ERROR,
    "CAPABILITY_UNSUPPORTED": ErrorCode.LOADCOACH_ERROR,
    "GENERATION_CANCELLED": ErrorCode.LOADCOACH_ERROR,
    "JOB_NOT_FOUND": ErrorCode.LOADCOACH_ERROR,
    "JOB_NOT_CANCELLABLE": ErrorCode.LOADCOACH_ERROR,
    "EVIDENCE_IMPORT_FAILED": ErrorCode.LOADCOACH_ERROR,
    "EVIDENCE_SOURCE_REFUSED": ErrorCode.LOADCOACH_ERROR,
    "SCHEMA_VERSION_UNSUPPORTED": ErrorCode.LOADCOACH_ERROR,
    "VALIDATION_ERROR": ErrorCode.LOADCOACH_ERROR,
    "UNAUTHORIZED": ErrorCode.LOADCOACH_ERROR,
    "FORBIDDEN": ErrorCode.LOADCOACH_ERROR,
    "RATE_LIMITED": ErrorCode.LOADCOACH_ERROR,
    "MISDIRECTED_REQUEST": ErrorCode.LOADCOACH_ERROR,
    "NOT_FOUND": ErrorCode.LOADCOACH_ERROR,
    "TRANSITION_REFUSED": ErrorCode.LOADCOACH_ERROR,
    "ILLEGAL_TRANSITION": ErrorCode.LOADCOACH_ERROR,
    "ATTEMPT_REFUSED": ErrorCode.LOADCOACH_ERROR,
    "INTERNAL_ERROR": ErrorCode.LOADCOACH_ERROR,
}
"""Every LoadCoach error code, mapped onto exactly one spec §13 code.

Spec §13 promises that no LoadCoach failure reaches a caller as ``INTERNAL_ERROR``; the promise is
kept for codes this table does not know too, because :func:`map_error`'s default is
``LOADCOACH_ERROR`` with the original code preserved. The table exists so the *deliberate*
mappings — a tier LoadCoach cannot serve, a context it cannot fit — are data a test can walk
against LoadCoach's own spec §13 list rather than branches nobody reviews."""


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of a caller-supplied transcript, as ``GenerateBody.messages`` takes it."""

    role: str
    content: str
    tool_call_id: str | None = None

    def as_body(self) -> dict[str, Any]:
        """Return the ``MessageBody`` mapping."""
        body: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id is not None:
            body["tool_call_id"] = self.tool_call_id
        return body


@dataclass(frozen=True, slots=True)
class GenerateRequest:
    """One ``POST /generate`` body (api.md §4), with exactly the fields ``GenerateBody`` accepts.

    ``GenerateBody`` is ``extra="forbid"``: api.md §4's *example* shows ``constraints`` and
    ``priority`` blocks the schema does not accept, and sending either is a ``VALIDATION_ERROR``.
    This type cannot express them, so the client cannot send them.

    Attributes:
        task: The task profile — the tier's ``task_profile``, never chosen here.
        messages: The transcript. Exactly one of ``messages`` or ``prompt`` is set.
        prompt: The single-prompt form, with an optional ``system``.
        system: Only with ``prompt``; a system turn belongs in ``messages`` otherwise.
        idempotency_key: Makes a retried POST safe (api.md §4). The loop sets it to the turn id,
            which is what lets recovery find the job a dead worker started.
        response_format: ``text``, ``json`` or ``json_schema``, or ``None`` for the profile's own.
        sampling: Sampling overrides; empty for the profile's own.

    Raises:
        ValueError: If both or neither of ``prompt``/``messages`` is given, or ``system`` is
            given with ``messages`` — the same refusals ``GenerateBody`` makes, made here so a
            malformed request never leaves the process.
    """

    task: str
    messages: tuple[Message, ...] | None = None
    prompt: str | None = None
    system: str | None = None
    idempotency_key: str | None = None
    response_format: str | None = None
    sampling: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse a body ``GenerateBody`` would refuse."""
        if (self.prompt is None) == (self.messages is None):
            message = "supply exactly one of 'prompt' or 'messages'"
            raise ValueError(message)
        if self.messages is not None and self.system is not None:
            message = "'system' belongs with 'prompt'; put a system turn in 'messages' instead"
            raise ValueError(message)
        if not self.task.strip():
            message = "task must name a task profile"
            raise ValueError(message)

    def as_body(self) -> dict[str, Any]:
        """Return the JSON body, omitting every field left at its default."""
        body: dict[str, Any] = {"task": self.task}
        if self.prompt is not None:
            body["prompt"] = self.prompt
        if self.system is not None:
            body["system"] = self.system
        if self.messages is not None:
            body["messages"] = [message.as_body() for message in self.messages]
        if self.idempotency_key is not None:
            body["idempotency_key"] = self.idempotency_key
        if self.response_format is not None:
            body["response_format"] = self.response_format
        if self.sampling:
            body["sampling"] = dict(self.sampling)
        return body


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """The ``model`` block: who answered (api.md §4)."""

    canonical_id: str
    model_ref: str | None
    runtime_profile_hash: str | None
    served_context: int | None
    served_context_source: str | None
    target_gpu_index: int | None

    @property
    def provider_kind(self) -> ProviderKind:
        """The runtime named by the canonical id's ``provider/`` prefix (ADR-0008).

        Raises:
            LoadCoachError: If the prefix is not a :class:`baseaicore.ProviderKind` member — a
                canonical id this suite cannot read is a response it cannot verify.
        """
        prefix, _, _ = self.canonical_id.partition("/")
        try:
            return ProviderKind(prefix)
        except ValueError as exc:
            message = (
                f"LoadCoach named model {self.canonical_id!r}, whose provider prefix "
                f"{prefix!r} is not a known provider kind"
            )
            raise LoadCoachError(
                message, details={"field": "model.canonical_id", "value": self.canonical_id}
            ) from exc


@dataclass(frozen=True, slots=True)
class RoutingInfo:
    """The ``routing`` block: LoadCoach's decision reference, never re-derived here."""

    decision_id: str | None
    rank: int | None
    final_score: float | None
    flags: tuple[str, ...]
    explanation_url: str | None


@dataclass(frozen=True, slots=True)
class TimingInfo:
    """The ``timing`` block. LoadCoach time and overhead are always separate (spec §15)."""

    total_ms: int | None
    provider_ms: int | None
    loadcoach_overhead_ms: int | None
    ttft_ms: int | None
    queue_wait_ms: int | None


@dataclass(frozen=True, slots=True)
class ValidationInfo:
    """The ``validation`` block, read for one question: did a **schema** check pass?

    Attributes:
        performed: Whether the profile asked for any validation. ``False`` is distinct from
            ``passed=True``; a profile that validates nothing has verified nothing.
        passed: Whether every performed check passed; ``None`` when none was performed.
        attempts: How many attempts the execution took.
        schema_validated: ``performed and passed`` **and** a ``json_schema`` check passed. A
            length or regex check passing is not a schema validation.
        checks_reported: Whether the response carried ``checks`` at all. The ``/generate``
            response does; the job document a replayed key returns does not, and without them a
            schema validation cannot be confirmed, so ``schema_validated`` is ``False``.
    """

    performed: bool
    passed: bool | None
    attempts: int | None
    schema_validated: bool
    checks_reported: bool


@dataclass(frozen=True, slots=True)
class GenerationResponse:
    """One ``POST /generate`` response (or the job document a replayed key returns), typed.

    Attributes:
        job_id: LoadCoach's job — the reference the turn row keeps and cancel needs.
        status: ``completed`` for a fresh execution; a replayed document may say ``failed`` or
            ``cancelled``. Anything but ``completed`` is not a turn that finished.
        text: The generated text; empty when the model answered only with tool calls.
        structured: The parsed structured output, when validation produced one.
        tool_calls: Tool invocations the model requested, verbatim.
        model: Who answered.
        routing: LoadCoach's decision reference.
        usage: The four token classes, ``UNSUPPORTED`` where unreported (never ``0``).
        thinking_tokens: Reasoning tokens, or ``UNSUPPORTED``.
        timing: LoadCoach's own timings.
        validation: What LoadCoach checked.
        finish_reason: The provider's declared reason, when the response carried one this suite
            names; ``None`` for absence.
        undeclared_finish_reason: A reason string the response carried that
            :class:`~promptcadence.domain.threads.FinishReason` does not name
            (``content_filter``, ``cancelled``, ``unknown``), kept for the halt's cause.
        attempt_count: How many attempts the response lists.
        degradations: LoadCoach's degradation markers, verbatim.
    """

    job_id: str
    status: str
    text: str
    structured: Any
    tool_calls: tuple[Mapping[str, Any], ...]
    model: ModelInfo
    routing: RoutingInfo
    usage: TokenUsage
    thinking_tokens: TokenCount
    timing: TimingInfo
    validation: ValidationInfo
    finish_reason: FinishReason | None
    undeclared_finish_reason: str | None
    attempt_count: int
    degradations: tuple[str, ...]

    @property
    def completed(self) -> bool:
        """Whether LoadCoach reports the job as completed (not the same as a declared finish)."""
        return self.status == "completed"


@dataclass(frozen=True, slots=True)
class JobSummary:
    """The fields of a job document recovery reads (api.md §5)."""

    job_id: str
    state: str
    source: str | None
    idempotency_key: str | None
    document: Mapping[str, Any]

    @property
    def terminal(self) -> bool:
        """Whether the job has ended, in any way."""
        return self.state in {"completed", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    """What ``POST /jobs/{id}/cancel`` answered: the state, and whether it was already stopping."""

    job_id: str
    state: str
    already: bool


@dataclass(frozen=True, slots=True)
class TaskProfileInfo:
    """One task profile, as ``GET /task-profiles`` lists it (api.md §2)."""

    profile_id: str
    version: str
    enabled: bool
    validation: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One registry entry from ``GET /models`` (api.md §2): identity, kind, availability."""

    canonical_id: str
    provider_kind: str
    available: bool


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """``GET /version``: the application version and the API majors served."""

    application_version: str
    api_current: str
    api_supported: tuple[str, ...]


def resolve_api_key(*, api_key_env: str, api_key_file: str) -> str | None:
    """Read the LoadCoach bearer token from its configured source (ADR-0026 §4).

    Never both — :mod:`promptcadence.config` refuses a configuration naming both a source. The
    token is returned to be placed in a header and nowhere else: never logged, never in
    ``details`` (spec §14).

    Args:
        api_key_env: The environment variable holding the token, or empty.
        api_key_file: The file holding the token, or empty.

    Returns:
        The token, or ``None`` when no source is configured or the file cannot be read.
    """
    if api_key_env:
        return os.environ.get(api_key_env)
    if api_key_file:
        try:
            return Path(api_key_file).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return None


def token_count_from_wire(value: object, *, field_name: str) -> TokenCount:
    """Read one token class off the wire, keeping the three answers distinct (ADR-0070).

    Args:
        value: The JSON value: a non-negative integer, the string ``"unsupported"``, or ``null``.
        field_name: For the refusal.

    Returns:
        The integer — ``0`` included, a real count — or :data:`baseaicore.UNSUPPORTED` for
        ``"unsupported"`` and ``null`` alike. Never ``0`` for an unreported class.

    Raises:
        LoadCoachError: For any other value. A float, a negative number or an unknown string is a
            wire this parser does not know, and guessing is how a ledger ends up totalling a
            floor as though it were complete.
    """
    if value is None or value == "unsupported":
        return UNSUPPORTED
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        message = (
            f"LoadCoach reported usage.{field_name}={value!r}; a token class is a non-negative "
            "integer, the string 'unsupported', or null"
        )
        raise LoadCoachError(message, details={"field": f"usage.{field_name}", "value": value})
    return value


def _require(document: Mapping[str, Any], path: str) -> Any:
    """Return the value at a dotted path, refusing a document that lacks it."""
    node: Any = document
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            message = f"LoadCoach's response carries no {path!r}; PromptCadence cannot read it"
            raise LoadCoachError(message, details={"field": path})
        node = node[part]
    return node


def _optional(document: Mapping[str, Any], path: str) -> Any:
    """Return the value at a dotted path, or ``None`` when any segment is absent."""
    node: Any = document
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _optional_int(document: Mapping[str, Any], path: str) -> int | None:
    value = _optional(document, path)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        message = f"LoadCoach reported {path}={value!r}; expected a number or null"
        raise LoadCoachError(message, details={"field": path, "value": value})
    return int(value)


def _validation_of(document: Mapping[str, Any]) -> ValidationInfo:
    block = _optional(document, "validation")
    if not isinstance(block, Mapping):
        message = "LoadCoach's response carries no 'validation' block"
        raise LoadCoachError(message, details={"field": "validation"})
    passed = block.get("passed")
    checks = block.get("checks")
    checks_reported = isinstance(checks, list)
    performed = bool(block.get("performed", passed is not None))
    schema_validated = bool(
        performed
        and passed is True
        and checks_reported
        and any(
            isinstance(check, Mapping)
            and check.get("kind") == _SCHEMA_CHECK_KIND
            and check.get("passed") is True
            for check in checks or ()
        )
    )
    attempts = block.get("attempts")
    return ValidationInfo(
        performed=performed,
        passed=passed if isinstance(passed, bool) else None,
        attempts=int(attempts) if isinstance(attempts, int) else None,
        schema_validated=schema_validated,
        checks_reported=checks_reported,
    )


def _finish_reason_of(document: Mapping[str, Any]) -> tuple[FinishReason | None, str | None]:
    """Read ``output.finish_reason``: a member, an undeclared string, or absence."""
    raw = _optional(document, "output.finish_reason")
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        message = f"LoadCoach reported output.finish_reason={raw!r}; expected a string or null"
        raise LoadCoachError(message, details={"field": "output.finish_reason", "value": raw})
    try:
        return FinishReason(raw), None
    except ValueError:
        return None, raw


def parse_generation(document: Mapping[str, Any]) -> GenerationResponse:
    """Turn a ``/generate`` response, or a job document, into a :class:`GenerationResponse`.

    Args:
        document: The decoded JSON body.

    Returns:
        The typed response.

    Raises:
        LoadCoachError: If a field this application reads is missing or carries a value of the
            wrong shape, naming the field. Refusing is the point: a missing usage block read as
            zero tokens, or a missing model read as "some model", would be a record that lies.
    """
    job_id = _require(document, "job_id")
    status = _require(document, "status")
    output = _require(document, "output")
    if not isinstance(output, Mapping):
        message = "LoadCoach's 'output' is not an object"
        raise LoadCoachError(message, details={"field": "output"})
    text = output.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        message = f"LoadCoach's output.text is {type(text).__name__}, not a string"
        raise LoadCoachError(message, details={"field": "output.text"})
    tool_calls_raw = output.get("tool_calls") or []
    if not isinstance(tool_calls_raw, list):
        message = "LoadCoach's output.tool_calls is not a list"
        raise LoadCoachError(message, details={"field": "output.tool_calls"})
    canonical_id = _require(document, "model.canonical_id")
    if not isinstance(canonical_id, str) or not canonical_id:
        message = "LoadCoach's response names no model.canonical_id; a turn's subject is verified"
        raise LoadCoachError(message, details={"field": "model.canonical_id"})
    usage_block = _require(document, "usage")
    if not isinstance(usage_block, Mapping):
        message = "LoadCoach's 'usage' is not an object"
        raise LoadCoachError(message, details={"field": "usage"})
    for name in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens"):
        if name not in usage_block:
            message = f"LoadCoach's usage carries no {name!r}; four classes are on the wire"
            raise LoadCoachError(message, details={"field": f"usage.{name}"})
    usage = TokenUsage(
        input_tokens=token_count_from_wire(usage_block["input_tokens"], field_name="input_tokens"),
        output_tokens=token_count_from_wire(
            usage_block["output_tokens"], field_name="output_tokens"
        ),
        cache_write_tokens=token_count_from_wire(
            usage_block["cache_write_tokens"], field_name="cache_write_tokens"
        ),
        cache_read_tokens=token_count_from_wire(
            usage_block["cache_read_tokens"], field_name="cache_read_tokens"
        ),
    )
    thinking = token_count_from_wire(
        usage_block.get("thinking_tokens"), field_name="thinking_tokens"
    )
    _require(document, "timing")
    _require(document, "routing")
    finish_reason, undeclared = _finish_reason_of(document)
    attempts = document.get("attempts")
    degradations = document.get("degradations") or []
    flags = _optional(document, "routing.flags") or []
    return GenerationResponse(
        job_id=str(job_id),
        status=str(status),
        text=text,
        structured=output.get("structured"),
        tool_calls=tuple(call for call in tool_calls_raw if isinstance(call, Mapping)),
        model=ModelInfo(
            canonical_id=canonical_id,
            model_ref=_optional(document, "model.model_ref"),
            runtime_profile_hash=_optional(document, "model.runtime_profile_hash"),
            served_context=_optional_int(document, "model.served_context"),
            served_context_source=_optional(document, "model.served_context_source"),
            target_gpu_index=_optional_int(document, "model.target_gpu_index"),
        ),
        routing=RoutingInfo(
            decision_id=_optional(document, "routing.decision_id"),
            rank=_optional_int(document, "routing.rank"),
            final_score=_optional(document, "routing.final_score"),
            flags=tuple(str(flag) for flag in flags),
            explanation_url=_optional(document, "routing.explanation_url"),
        ),
        usage=usage,
        thinking_tokens=thinking,
        timing=TimingInfo(
            total_ms=_optional_int(document, "timing.total_ms"),
            provider_ms=_optional_int(document, "timing.provider_ms"),
            loadcoach_overhead_ms=_optional_int(document, "timing.loadcoach_overhead_ms"),
            ttft_ms=_optional_int(document, "timing.ttft_ms"),
            queue_wait_ms=_optional_int(document, "timing.queue_wait_ms"),
        ),
        validation=_validation_of(document),
        finish_reason=finish_reason,
        undeclared_finish_reason=undeclared,
        attempt_count=len(attempts) if isinstance(attempts, list) else 0,
        degradations=tuple(str(item) for item in degradations),
    )


def map_error(
    *, http_status: int, body: Any, endpoint: str, exception: httpx.HTTPError | None = None
) -> SuiteError:
    """Map one failed LoadCoach exchange onto exactly one spec §13 error.

    Args:
        http_status: The HTTP status the answer came with; ``0`` when no answer came.
        body: The decoded JSON body, or ``None`` when the body was not JSON or there was none.
        endpoint: The path called, for the message.
        exception: The transport error, when the failure was one.

    Returns:
        :class:`LoadCoachUnavailableError` for a transport failure — LoadCoach could not be
        reached, or the connection died before an answer. Otherwise the error the standard
        envelope's ``code`` maps to in :data:`LOADCOACH_CODE_MAP`, with the original code and
        details preserved in ``details``; ``LOADCOACH_ERROR`` for a code the table does not know
        and for a body that is not the standard envelope. Never ``INTERNAL_ERROR``.
    """
    if exception is not None:
        if isinstance(exception, httpx.TimeoutException):
            message = f"LoadCoach did not answer {endpoint} within the configured timeout"
            return LoadCoachError(
                message,
                details={
                    "loadcoach_code": None,
                    "reason": "client_timeout",
                    "endpoint": endpoint,
                    "http_status": None,
                },
            )
        message = f"LoadCoach is unreachable at {endpoint}: {type(exception).__name__}"
        return LoadCoachUnavailableError(
            message, details={"endpoint": endpoint, "transport_error": type(exception).__name__}
        )
    error = body.get("error") if isinstance(body, Mapping) else None
    if not isinstance(error, Mapping) or not isinstance(error.get("code"), str):
        message = f"LoadCoach answered {endpoint} with HTTP {http_status} and no error envelope"
        return LoadCoachError(
            message,
            details={"loadcoach_code": None, "http_status": http_status, "endpoint": endpoint},
        )
    code = str(error["code"])
    upstream_message = str(error.get("message", ""))
    upstream_details = error.get("details")
    details: dict[str, Any] = {
        "loadcoach_code": code,
        "http_status": http_status,
        "endpoint": endpoint,
        "loadcoach_details": dict(upstream_details)
        if isinstance(upstream_details, Mapping)
        else {},
    }
    mapped = LOADCOACH_CODE_MAP.get(code, ErrorCode.LOADCOACH_ERROR)
    message = f"LoadCoach refused {endpoint} with {code}: {upstream_message}"
    if mapped is ErrorCode.TIER_UNAVAILABLE:
        reason = "no_eligible_model" if code == "NO_ELIGIBLE_MODEL" else "task_profile_not_found"
        return TierUnavailableError(message, details={"reason": reason, **details})
    if mapped is ErrorCode.COMPACTION_FAILED:
        return CompactionFailedError(message, details=details)
    return LoadCoachError(message, details=details)


class LoadCoachClient:
    """The LoadCoach HTTP client. One documented endpoint per method, no policy.

    Built over an injected :class:`httpx.Client` so a test can hand it Starlette's
    ``TestClient`` over the fake LoadCoach and exercise every byte of this module without a
    socket, and the served application can hand it a real client from configuration.
    """

    __slots__ = ("_http",)

    def __init__(self, http: httpx.Client) -> None:
        """Wrap an httpx client whose ``base_url`` is LoadCoach's root (no ``/api/v1``).

        Args:
            http: The client. Its default headers gain ``X-Client-Name`` if absent; its timeout
                is its own — the composition root sets ``loadcoach.timeout_seconds`` there.
        """
        self._http = http
        if "x-client-name" not in {key.lower() for key in http.headers}:
            http.headers["X-Client-Name"] = CLIENT_NAME

    @classmethod
    def from_settings(
        cls,
        *,
        base_url: str,
        timeout_seconds: float,
        api_key_env: str = "",
        api_key_file: str = "",
    ) -> LoadCoachClient:
        """Build a client for the configured LoadCoach (``[loadcoach]``).

        Args:
            base_url: ``settings.loadcoach.base_url``.
            timeout_seconds: ``settings.loadcoach.timeout_seconds`` — the read timeout a
                generation may take; connecting is bounded separately and briefly, because a
                LoadCoach that is down should be reported down in seconds, not in ten minutes.
            api_key_env: ``settings.loadcoach.api_key_env``.
            api_key_file: ``settings.loadcoach.api_key_file``.

        Returns:
            The client. Opens no connection until first use.
        """
        headers = {"X-Client-Name": CLIENT_NAME}
        token = resolve_api_key(api_key_env=api_key_env, api_key_file=api_key_file)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        return cls(httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout))

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._http.close()

    # ----------------------------------------------------------------------------------------
    # Transport
    # ----------------------------------------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        """Make one request and return its decoded body, or raise the mapped error."""
        endpoint = f"{API_PREFIX}{path}"
        headers: dict[str, str] = {}
        request_id = current_correlation().get("request_id")
        if request_id:
            headers["X-Request-ID"] = request_id
        try:
            response = self._http.request(
                method, endpoint, json=json, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            raise map_error(http_status=0, body=None, endpoint=endpoint, exception=exc) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code not in expected:
            raise map_error(http_status=response.status_code, body=body, endpoint=endpoint)
        if body is None:
            message = f"LoadCoach answered {endpoint} with HTTP {response.status_code} and no JSON"
            raise LoadCoachError(
                message,
                details={
                    "loadcoach_code": None,
                    "http_status": response.status_code,
                    "endpoint": endpoint,
                },
            )
        return body

    # ----------------------------------------------------------------------------------------
    # §1 System
    # ----------------------------------------------------------------------------------------

    def version(self) -> VersionInfo:
        """``GET /version`` (api.md §1) — never authenticated; the first call a client makes.

        Returns:
            The application version and the API majors LoadCoach serves.

        Raises:
            SchemaVersionUnsupportedError: If LoadCoach does not serve API ``v1`` (api.md §12
                rule 1: verify the API major on first contact).
            LoadCoachUnavailableError: If LoadCoach cannot be reached.
        """
        body = self._call("GET", "/version")
        application = body.get("application") if isinstance(body, Mapping) else None
        api = body.get("api") if isinstance(body, Mapping) else None
        if not isinstance(application, Mapping) or not isinstance(api, Mapping):
            message = "LoadCoach's /version answer has no 'application' and 'api' blocks"
            raise LoadCoachError(message, details={"field": "version"})
        supported = tuple(str(major) for major in api.get("supported") or ())
        info = VersionInfo(
            application_version=str(application.get("version", "")),
            api_current=str(api.get("current", "")),
            api_supported=supported,
        )
        if SUPPORTED_API_MAJOR not in supported:
            message = (
                f"LoadCoach {info.application_version} serves API majors {list(supported)}; "
                f"this build speaks {SUPPORTED_API_MAJOR}"
            )
            raise SchemaVersionUnsupportedError(
                message, details={"supported": list(supported), "required": SUPPORTED_API_MAJOR}
            )
        return info

    def system_status(self) -> Mapping[str, Any]:
        """``GET /system/status`` (api.md §1): queue depth, executions, residency, telemetry.

        Returned as LoadCoach reports it, for passthrough into ``/system/status`` here (spec
        §17). It carries **no provider information** — that comes from :meth:`models`.
        """
        body = self._call("GET", "/system/status")
        if not isinstance(body, Mapping):
            message = "LoadCoach's /system/status answer is not an object"
            raise LoadCoachError(message, details={"field": "system_status"})
        return body

    # ----------------------------------------------------------------------------------------
    # §2 Models and task profiles
    # ----------------------------------------------------------------------------------------

    def models(self) -> tuple[ModelEntry, ...]:
        """``GET /models`` (api.md §2): every registered model with its provider kind.

        The provider surface LoadCoach serves is read from here: LoadCoach 1.0 configures
        exactly one provider, so every entry names the same kind, and that kind is what a
        response's ``model.canonical_id`` is verified against (spec §11 contract 4).
        """
        body = self._call("GET", "/models")
        entries = body.get("models") if isinstance(body, Mapping) else None
        if not isinstance(entries, list):
            message = "LoadCoach's /models answer carries no 'models' list"
            raise LoadCoachError(message, details={"field": "models"})
        result = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            result.append(
                ModelEntry(
                    canonical_id=str(entry.get("canonical_id", "")),
                    provider_kind=str(entry.get("provider_kind", "")),
                    available=bool(entry.get("available", False)),
                )
            )
        return tuple(result)

    def task_profiles(self) -> tuple[TaskProfileInfo, ...]:
        """``GET /task-profiles`` (api.md §2): every profile with its validation policy."""
        body = self._call("GET", "/task-profiles")
        entries = body.get("task_profiles") if isinstance(body, Mapping) else None
        if not isinstance(entries, list):
            message = "LoadCoach's /task-profiles answer carries no 'task_profiles' list"
            raise LoadCoachError(message, details={"field": "task_profiles"})
        return tuple(_task_profile_of(entry) for entry in entries if isinstance(entry, Mapping))

    def task_profile(self, profile_id: str) -> TaskProfileInfo | None:
        """``GET /task-profiles/{id}`` (api.md §2), or ``None`` when LoadCoach has no such profile.

        The ``None`` is deliberate: ``promptcadence tiers check`` asks this question for every
        configured tier and reports, and a missing profile is its answer, not its failure.
        """
        try:
            body = self._call("GET", f"/task-profiles/{profile_id}")
        except TierUnavailableError as exc:
            if exc.details.get("loadcoach_code") == "TASK_PROFILE_NOT_FOUND":
                return None
            raise
        if not isinstance(body, Mapping):
            message = "LoadCoach's /task-profiles/{id} answer is not an object"
            raise LoadCoachError(message, details={"field": "task_profile"})
        return _task_profile_of(body)

    # ----------------------------------------------------------------------------------------
    # §3 Routing without execution
    # ----------------------------------------------------------------------------------------

    def route(
        self,
        *,
        task: str,
        estimated_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
    ) -> Mapping[str, Any]:
        """``POST /route`` (api.md §3): the routing explanation, without executing.

        Args:
            task: The task profile.
            estimated_input_tokens: The estimate, when the caller has one.
            max_output_tokens: The output bound, when the caller has one.

        Returns:
            The explanation document, verbatim.

        Raises:
            TierUnavailableError: ``TASK_PROFILE_NOT_FOUND`` or ``NO_ELIGIBLE_MODEL``, with every
                candidate and its rejection reason preserved in ``details``.
        """
        body: dict[str, Any] = {"task": task}
        if estimated_input_tokens is not None:
            body["estimated_input_tokens"] = estimated_input_tokens
        if max_output_tokens is not None:
            body["max_output_tokens"] = max_output_tokens
        answer = self._call("POST", "/route", json=body)
        if not isinstance(answer, Mapping):
            message = "LoadCoach's /route answer is not an object"
            raise LoadCoachError(message, details={"field": "route"})
        return answer

    # ----------------------------------------------------------------------------------------
    # §4 Synchronous generation
    # ----------------------------------------------------------------------------------------

    def generate(self, request: GenerateRequest) -> GenerationResponse:
        """``POST /generate`` (api.md §4): route, execute, validate, and return the result.

        Args:
            request: The body. Its ``idempotency_key`` makes a repeat safe: the same key from
                this caller returns the original job's document rather than executing again.

        Returns:
            The typed response. Check :attr:`GenerationResponse.completed` before reading it as
            a finished job — a replayed key may return a failed or cancelled document.

        Raises:
            TierUnavailableError: LoadCoach cannot serve the task profile (no eligible model, or
                no such profile).
            CompactionFailedError: ``CONTEXT_LIMIT_EXCEEDED``; compaction arrives in Phase 8, so
                today nothing can fit it.
            LoadCoachError: Any other LoadCoach failure, with its code in ``details``, or a
                response this client cannot read.
            LoadCoachUnavailableError: LoadCoach could not be reached.
        """
        body = self._call("POST", "/generate", json=request.as_body())
        if not isinstance(body, Mapping):
            message = "LoadCoach's /generate answer is not an object"
            raise LoadCoachError(message, details={"field": "generate"})
        return parse_generation(body)

    # ----------------------------------------------------------------------------------------
    # §5 Jobs
    # ----------------------------------------------------------------------------------------

    def list_jobs(
        self,
        *,
        source: str = CLIENT_NAME,
        states: Sequence[str] | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> tuple[tuple[JobSummary, ...], str | None]:
        """``GET /jobs`` (api.md §5), filtered to this application's own jobs.

        Args:
            source: The ``source`` filter; this application's client name by default.
            states: A state filter, or ``None`` for every state.
            limit: Page size.
            cursor: The previous page's ``next_cursor``.

        Returns:
            The page, and the cursor for the next page or ``None`` at the end.
        """
        params: dict[str, Any] = {"source": source, "limit": limit}
        if states:
            params["state"] = ",".join(states)
        if cursor:
            params["cursor"] = cursor
        body = self._call("GET", "/jobs", params=params)
        items = body.get("items") if isinstance(body, Mapping) else None
        page = body.get("page") if isinstance(body, Mapping) else None
        if not isinstance(items, list) or not isinstance(page, Mapping):
            message = "LoadCoach's /jobs answer is not a paginated collection"
            raise LoadCoachError(message, details={"field": "jobs"})
        summaries = tuple(_job_summary_of(item) for item in items if isinstance(item, Mapping))
        next_cursor = page.get("next_cursor")
        return summaries, str(next_cursor) if next_cursor else None

    def job(self, job_id: str) -> JobSummary:
        """``GET /jobs/{id}`` (api.md §5): the full job document.

        Raises:
            LoadCoachError: ``JOB_NOT_FOUND`` (in ``details["loadcoach_code"]``) among others.
        """
        body = self._call("GET", f"/jobs/{job_id}")
        if not isinstance(body, Mapping):
            message = "LoadCoach's /jobs/{id} answer is not an object"
            raise LoadCoachError(message, details={"field": "job"})
        return _job_summary_of(body)

    def cancel_job(self, job_id: str) -> CancelOutcome:
        """``POST /jobs/{id}/cancel`` (api.md §5): at once for a waiting job, at the next chunk
        boundary for an executing one.

        Raises:
            LoadCoachError: ``JOB_NOT_CANCELLABLE`` for a terminal job, ``JOB_NOT_FOUND`` for an
                unknown one — both in ``details["loadcoach_code"]``.
        """
        body = self._call("POST", f"/jobs/{job_id}/cancel", expected=(202,))
        if not isinstance(body, Mapping):
            message = "LoadCoach's cancel answer is not an object"
            raise LoadCoachError(message, details={"field": "cancel"})
        return CancelOutcome(
            job_id=str(body.get("job_id", job_id)),
            state=str(body.get("state", "")),
            already=bool(body.get("already", False)),
        )

    def find_job(
        self, idempotency_key: str, *, states: Sequence[str] | None = None, max_pages: int = 25
    ) -> JobSummary | None:
        """Find this application's job holding ``idempotency_key``, newest first.

        The recovery primitive (lifecycle §8.3): a ``turn.started`` event with no turn row names
        the key, and this is how the job it started is found without starting another. It pages
        ``GET /jobs?source=promptcadence`` rather than re-POSTing ``/generate`` with the key,
        because a re-POST after LoadCoach released the key (``queue.idempotency_ttl_hours``)
        would start *new* work, and a client cannot tell a replay from a fresh execution on
        ``/generate``.

        Args:
            idempotency_key: The turn id the request carried.
            states: Restrict to these states (``NON_TERMINAL_JOB_STATES`` for in-flight work), or
                ``None`` for any.
            max_pages: A bound on the search; this application's jobs are newest first, so a
                dangling one is on the first page in practice.

        Returns:
            The job, or ``None`` when no job of this application's holds the key.
        """
        cursor: str | None = None
        for _ in range(max_pages):
            page, cursor = self.list_jobs(states=states, cursor=cursor)
            for job in page:
                if job.idempotency_key == idempotency_key:
                    return job
            if cursor is None:
                break
        return None


def _task_profile_of(entry: Mapping[str, Any]) -> TaskProfileInfo:
    validation = entry.get("validation")
    return TaskProfileInfo(
        profile_id=str(entry.get("profile_id", "")),
        version=str(entry.get("version", "")),
        enabled=bool(entry.get("enabled", True)),
        validation=dict(validation) if isinstance(validation, Mapping) else {},
    )


def _job_summary_of(document: Mapping[str, Any]) -> JobSummary:
    state = document.get("state", document.get("status"))
    return JobSummary(
        job_id=str(document.get("job_id", "")),
        state=str(state) if state is not None else "",
        source=document.get("source"),
        idempotency_key=document.get("idempotency_key"),
        document=dict(document),
    )
