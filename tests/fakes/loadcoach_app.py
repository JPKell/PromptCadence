"""The fake LoadCoach: an in-process ASGI application speaking LoadCoach's documented wire.

Built **before** the loop, as the development plan requires, and for the same reason
``FakeProvider`` exists one layer down: every downstream phase (E4, F1, F2, G1, the 1.0
verification) tests against this and not against a GPU. A fake that is more permissive than the
real thing converts a would-be integration failure into a green suite, so the rule here is
*stricter wherever they differ, and every difference written down*.

**Sources of every shape.** ``docs/apps/loadcoach/api.md`` §§1-5 and §10-11, and LoadCoach's own
code at ``01170a7`` (``ExecutionOutcome.as_json``, ``job_document``, ``post_cancel``,
``list_jobs``, ``_STATUS_BY_CODE``). Request bodies are validated by a pydantic mirror of
``GenerateBody``/``RouteBody`` that ``tests/contract/test_loadcoach_contract.py`` asserts equal
in shape to the committed OpenAPI snapshot, which is what stops this file drifting (roadmap §9,
I10).

**What it models.**

* ``GET /version``, ``/health``, ``/system/status``, ``/models``, ``/task-profiles(/{id})``,
  ``POST /route``, ``POST /generate``, ``GET /jobs`` (``source``/``state`` filters, cursor
  pagination), ``GET /jobs/{id}``, ``POST /jobs/{id}/cancel``.
* Idempotency exactly as api.md §4 states it: scoped per caller (``X-Client-Name``), a repeated
  key returns the **original job's document** — the job-document shape, not the ``/generate``
  shape — and never executes twice.
* The ``usage`` block in **both** wires: :attr:`Wire.INTERIM` (every real adapter before
  ``modelrack 0.7.0``: the cache classes are the string ``"unsupported"``) and
  :attr:`Wire.POST_MODELRACK_070` (``0`` or a count). ``thinking_tokens`` is ``"unsupported"``
  in both, as every shipped adapter reports it.
* ``validation`` derived from the task profile's own policy the way ``validate_output`` does: a
  ``length`` check for ``max_output_chars``; ``json``, ``json_schema`` and ``required_fields``
  checks when the profile requires a schema. Only a ``json_schema`` check is a schema validation.
* Cancellation: a scripted generation can be *held* on a ``threading.Event`` so a test can catch
  a job in flight (a kill −9, a cancel mid-turn); ``POST /jobs/{id}/cancel`` releases it and the
  ``/generate`` call answers with the cancelled job's document (LoadCoach spec §13:
  ``GENERATION_CANCELLED`` — "200 with a cancelled job").
* Every error as the standard envelope with LoadCoach's own status for the code.

**Where it is stricter than LoadCoach.**

* ``X-Client-Name`` is **required** on every request. LoadCoach accepts an anonymous loopback
  caller; this fake refuses one with ``VALIDATION_ERROR``, because recovery finds a dead
  worker's job by ``GET /jobs?source=promptcadence`` and a request that dropped the header would
  pass every other test while making that lookup silently return nothing.
* An ``idempotency_key`` is **required** on ``POST /generate`` for the same reason.
* A task profile the fake was not given is ``TASK_PROFILE_NOT_FOUND``; there is no default
  registry, so a tier naming a profile nobody registered fails rather than being served.

**Where it is looser, or does not model the real thing at all — read this before trusting it.**

* ``output.finish_reason`` is rendered the way LoadCoach renders it since its commit
  ``846348b``: the provider's declared reason for the attempt, ``stop`` unless a script
  says otherwise, and the job document carries it and the ``validation.checks`` too. A script
  with ``finish_reason=None`` reproduces the wire of a LoadCoach **before** that commit, which
  rendered none; the loop must halt on it, never complete.
* Cancellation takes effect at once; LoadCoach honours it within one stream chunk, or only at
  completion for a provider that cannot stream (``cancellation_deferred_to_completion``).
* Routing is not modelled: every generation is served by the one scripted model, ``/route``
  returns a minimal explanation, and there is no evidence, scoring, fallback, corrective retry or
  circuit breaker. ``attempts`` is always one entry unless a script says otherwise.
* No authentication, no rate limiting, no queue admission (``QUEUE_FULL``,
  ``INSUFFICIENT_RESOURCES`` only when scripted), no ``/generate/stream``, no
  ``/jobs/{id}/stream``, no retention sweep, no feedback, no idempotency TTL (a key never
  expires here; in LoadCoach it does after ``queue.idempotency_ttl_hours``).
* The job document's timestamps, priority and lease blocks carry plausible constants, not a
  queue's real bookkeeping.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from baseaicore import new_id, sha256_of
from baseaicore.timeutil import to_rfc3339
from fastapi import APIRouter, FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mirrorwall import error_body
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "FakeLoadCoach",
    "FakeModel",
    "DERIVED_FINISH",
    "ScriptedError",
    "ScriptedGeneration",
    "Wire",
    "build_fake_app",
    "text_profile",
    "schema_profile",
]

UNSUPPORTED_WIRE: Final = "unsupported"
"""ADR-0016 rule 4 on the wire: an unreported class is this string, never ``null`` and never 0."""

DERIVED_FINISH: Final = "<derived>"
"""The :attr:`ScriptedGeneration.finish_reason` default: derive the declared reason from the
script — ``tool_calls`` when tool calls were requested, otherwise ``stop`` — as a real provider
declares it and as ModelRack's own ``FakeGeneration`` derives it. Never rendered on the wire."""

NON_TERMINAL: Final[frozenset[str]] = frozenset(
    {
        "queued",
        "leased",
        "admitted",
        "waiting_resources",
        "executing",
        "validating",
        "retrying",
        "cancelling",
    }
)
TERMINAL: Final[frozenset[str]] = frozenset({"completed", "failed", "cancelled"})

# LoadCoach's own code → HTTP status map (web/app.py `_STATUS_BY_CODE` at 01170a7).
STATUS_BY_CODE: Final[dict[str, int]] = {
    "VALIDATION_ERROR": 400,
    "TASK_PROFILE_NOT_FOUND": 404,
    "NO_ELIGIBLE_MODEL": 422,
    "ALL_CANDIDATES_FAILED": 502,
    "PROVIDER_UNAVAILABLE": 503,
    "PROVIDER_TIMEOUT": 504,
    "PROVIDER_PROTOCOL_ERROR": 502,
    "MODEL_NOT_FOUND": 404,
    "CONTEXT_LIMIT_EXCEEDED": 422,
    "CAPABILITY_UNSUPPORTED": 422,
    "INSUFFICIENT_RESOURCES": 503,
    "QUEUE_FULL": 429,
    "JOB_NOT_FOUND": 404,
    "JOB_NOT_CANCELLABLE": 409,
    "MAX_WAIT_EXCEEDED": 504,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "RATE_LIMITED": 429,
    "INTERNAL_ERROR": 500,
}


class Wire(StrEnum):
    """Which ``usage`` shape the fake speaks (C6_HANDOFF §6)."""

    INTERIM = "interim"
    """Before ``modelrack 0.7.0`` (rows H1/H2): the cache classes are ``"unsupported"``."""

    POST_MODELRACK_070 = "post_modelrack_0.7.0"
    """After: the cache classes are ``0`` (nothing billable to that class) or a count."""


# --------------------------------------------------------------------------------------------
# Request bodies — mirrors of LoadCoach's, asserted against the OpenAPI snapshot by the
# contract tests. Every ``extra="forbid"`` here is LoadCoach's own.
# --------------------------------------------------------------------------------------------


class MessageBody(BaseModel):
    """One turn of a caller-supplied transcript (api.md §4)."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern="^(system|user|assistant|tool)$")
    content: str
    tool_call_id: str | None = Field(default=None)


class RuntimeProfileOverrideBody(BaseModel):
    """The ``overrides.runtime_profile`` block (routing §10)."""

    model_config = ConfigDict(extra="forbid")

    context_size: int | None = Field(default=None, gt=0)
    kv_cache_precision: str | None = Field(default=None)
    gpu_layers: int | None = Field(default=None, ge=0)
    flash_attention: bool | None = Field(default=None)
    threads: int | None = Field(default=None, gt=0)
    batch_size: int | None = Field(default=None, gt=0)
    keep_alive: str | None = Field(default=None)


class OverridesBody(BaseModel):
    """Routing §10's overrides."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None)
    runtime_profile: RuntimeProfileOverrideBody | None = Field(default=None)
    disallow_fallback: bool = Field(default=False)
    require_evidence: bool = Field(default=False)


class GenerateBody(BaseModel):
    """``POST /generate``'s request body, exactly as LoadCoach declares it."""

    model_config = ConfigDict(extra="forbid")

    task: str
    system: str | None = Field(default=None)
    prompt: str | None = Field(default=None)
    messages: list[MessageBody] | None = Field(default=None)
    response_format: str | None = Field(default=None, pattern="^(text|json|json_schema)$")
    sampling: dict[str, Any] = Field(default_factory=dict)
    overrides: OverridesBody | None = Field(default=None)
    idempotency_key: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _exactly_one_form(self) -> GenerateBody:
        if (self.prompt is None) == (self.messages is None):
            message = "supply exactly one of 'prompt' or 'messages'"
            raise ValueError(message)
        if self.messages is not None and self.system is not None:
            message = "'system' belongs with 'prompt'; put a system turn in 'messages' instead"
            raise ValueError(message)
        return self


class TaskProfileConstraints(BaseModel):
    """Hard filters (routing §3)."""

    model_config = ConfigDict(extra="forbid")

    requires_capabilities: list[str] = Field(default_factory=list)
    min_capability_scores: dict[str, float] = Field(default_factory=dict)
    min_context_tokens: int = Field(default=0, ge=0)
    max_latency_p95_seconds: float | None = Field(default=None, gt=0)
    exclude_models: list[str] = Field(default_factory=list)
    allow_remote_providers: bool = Field(default=False)


class RouteBody(BaseModel):
    """``POST /route``'s request body (api.md §3)."""

    model_config = ConfigDict(extra="forbid")

    task: str
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    constraints: TaskProfileConstraints | None = Field(default=None)
    overrides: OverridesBody | None = Field(default=None)


# --------------------------------------------------------------------------------------------
# Scripts and state
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FakeModel:
    """The one model the fake serves every generation from."""

    canonical_id: str = "ollama/qwen3:8b@sha256:" + "a" * 64
    provider_kind: str = "ollama"
    model_ref: str = "01FAKEMODEL000000000000000"
    runtime_profile_hash: str = "8f2c" + "0" * 60
    served_context: int = 32768

    def as_registry_entry(self) -> dict[str, Any]:
        """The ``GET /models`` entry (api.md §2, LoadCoach ``_model_to_json``)."""
        _, _, rest = self.canonical_id.partition("/")
        name, _, _ = rest.partition("@")
        return {
            "canonical_id": self.canonical_id,
            "model_ref": self.model_ref,
            "provider_kind": self.provider_kind,
            "provider_model_name": name,
            "identity_confidence": "verified",
            "family": "fake",
            "quantization": None,
            "max_context": self.served_context,
            "size_bytes": 4 * 1024**3,
            "parameter_count": 8_000_000_000,
            "available": True,
            "unavailable_reason": None,
            "declared_capabilities": ["tool_use", "agentic"],
            "first_seen_at": "2026-09-01T00:00:00+00:00",
            "last_seen_at": "2026-09-03T00:00:00+00:00",
        }


@dataclass(slots=True)
class ScriptedGeneration:
    """One scripted answer to ``POST /generate``.

    Attributes:
        text: What the model said.
        omit_subject: Render ``model.canonical_id`` as ``null``. A LoadCoach that names no
            execution subject is out of contract (LoadCoach spec §9), which is exactly why it is
            scriptable: contract 4 is fail-closed, so "the response declined to say what answered"
            is a case the suite must be able to produce and assert on, and no honest fake produces
            it by accident.
        structured: The parsed structured output, when the profile validates JSON.
        tool_calls: Tool calls the model requested, verbatim into ``output.tool_calls``.
        input_tokens: Reported input count, or ``None`` for unreported (``null`` on the wire).
        output_tokens: Reported output count, or ``None``.
        cache_read_tokens: Under the post-0.7.0 wire, the cache-hit count (``0`` by default).
        cache_write_tokens: Under the post-0.7.0 wire, the cache-write count.
        provider_ms: Reported provider time.
        delay_seconds: How long the fake sleeps before answering (slowness).
        hold: When given, the fake blocks on this event before answering — until a test sets it
            or a cancel releases it. This is how a test catches a job in flight.
        schema_passes: For a schema-validating profile, whether the ``json_schema`` check passes.
        degradations: Degradation markers to report.
        attempts: How many attempt rows to report.
        finish_reason: What ``output.finish_reason`` carries — a ``modelrack.FinishReason`` value
            (``stop``, ``length``, ``tool_calls``, ``content_filter``, ``cancelled``, ``error``,
            ``unknown``) exactly as LoadCoach renders the provider's declared reason. The default
            :data:`DERIVED_FINISH` derives it — ``tool_calls`` when tool calls were requested,
            otherwise ``stop`` — and ``None`` reproduces the wire of a LoadCoach older than
            ``846348b``, which rendered none.
    """

    text: str = "The notes describe three meetings."
    omit_subject: bool = False
    structured: Any = None
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    input_tokens: int | None = 812
    output_tokens: int | None = 104
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    provider_ms: int = 1830
    delay_seconds: float = 0.0
    hold: threading.Event | None = None
    schema_passes: bool = True
    degradations: tuple[str, ...] = ()
    attempts: int = 1
    finish_reason: str | None = DERIVED_FINISH

    @property
    def declared_finish_reason(self) -> str | None:
        """The value ``output.finish_reason`` carries once :data:`DERIVED_FINISH` is resolved."""
        if self.finish_reason != DERIVED_FINISH:
            return self.finish_reason
        return "tool_calls" if self.tool_calls else "stop"


@dataclass(frozen=True, slots=True)
class ScriptedError:
    """One scripted failure: the standard envelope with LoadCoach's own status for the code."""

    code: str
    message: str = "scripted failure"
    details: Mapping[str, Any] = field(default_factory=dict)
    http_status: int | None = None

    @property
    def status(self) -> int:
        """The HTTP status LoadCoach would send for this code."""
        return self.http_status if self.http_status is not None else STATUS_BY_CODE[self.code]


@dataclass(slots=True)
class FakeJob:
    """One job the fake created, with everything the two document shapes need."""

    job_id: str
    source: str
    idempotency_key: str | None
    task: str
    request_body: dict[str, Any]
    created_at: datetime
    state: str = "executing"
    state_reason: str | None = None
    cancel_requested: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    completed_at: datetime | None = None


def text_profile(profile_id: str, *, max_output_chars: int = 100_000) -> dict[str, Any]:
    """A ``tools.agent``-shaped profile: free text, validated only for length.

    This is what LoadCoach ships for ``tools.agent`` (``config/task_profiles.toml``), and what a
    tier's ``tools.agent.<name>`` specialization inherits: no schema, so nothing on today's wire
    declares a finish for it.
    """
    return {
        "profile_id": profile_id,
        "version": "1.0.0",
        "description": "Use tools to accomplish a multi-step task.",
        "weights": {"agentic": 0.35, "tool_use": 0.3, "reasoning": 0.2, "reliability": 0.15},
        "constraints": {"min_context_tokens": 8192, "requires_capabilities": ["tool_use"]},
        "execution": {
            "temperature": 0.2,
            "max_output_tokens": 4096,
            "response_format": "text",
            "max_attempts": 3,
            "fallback_depth": 2,
        },
        "validation": {"max_output_chars": max_output_chars},
        "enabled": True,
        "updated_at": "2026-09-03T00:00:00+00:00",
    }


def schema_profile(
    profile_id: str, *, required_fields: tuple[str, ...] = ("answer",)
) -> dict[str, Any]:
    """A ``structured.extract``-shaped profile: JSON output validated against a schema.

    The only profile shape on today's wire for which a turn can *complete* (spec §11 contract 6's
    "schema-validated structured result"), because LoadCoach renders no ``finish_reason``.
    """
    return {
        "profile_id": profile_id,
        "version": "1.0.0",
        "description": "Answer as a JSON document matching the schema.",
        "weights": {"structured_output": 0.45, "reasoning": 0.3, "reliability": 0.25},
        "constraints": {"min_context_tokens": 8192, "requires_capabilities": ["structured_output"]},
        "execution": {
            "temperature": 0.0,
            "max_output_tokens": 2048,
            "response_format": "json_schema",
            "json_schema_ref": f"{profile_id}.json",
            "max_attempts": 3,
            "fallback_depth": 2,
        },
        "validation": {
            "require_valid_json": True,
            "require_schema": True,
            "required_fields": list(required_fields),
            "max_output_chars": 50_000,
        },
        "enabled": True,
        "updated_at": "2026-09-03T00:00:00+00:00",
    }


def shipped_profiles(*profile_ids: str) -> tuple[dict[str, Any], ...]:
    """Return the named profiles **as LoadCoach ships them**, from the vendored TOML.

    ``text_profile`` and ``schema_profile`` are hand-shaped stand-ins, and they remain — a test
    that needs a profile with a particular validation policy should say so rather than hunt for a
    shipped one that happens to have it. This is the other case: a test asserting how the loop
    behaves against *the tiers this application actually ships* should register the profile
    LoadCoach actually ships, so a change to either side fails a test here rather than at an
    operator's first turn.

    The fake stays an empty registry regardless. It registers nothing by default — stricter than
    LoadCoach, by decision (D2) — because a fake that quietly serves a profile the real thing has
    never heard of is a fake that hides exactly the defect E4 exists to close.

    Args:
        *profile_ids: The profiles to fetch. Empty means every shipped profile.

    Returns:
        One wire-shaped document per profile, in the order asked for, each carrying the ``enabled``
        and ``updated_at`` fields LoadCoach's ``/task-profiles`` adds and the file does not.

    Raises:
        KeyError: A profile id the vendored file does not hold. A caller bug, and one worth
            raising for: a test registering a profile that does not ship would assert against a
            world that does not exist.
    """
    from tests.contract.test_loadcoach_task_profiles import shipped_profiles as _from_toml

    profiles = _from_toml()
    wanted = profile_ids if profile_ids else tuple(sorted(profiles))
    return tuple(
        {
            "profile_id": profile_id,
            **profiles[profile_id],
            "enabled": True,
            "updated_at": "2026-09-04T00:00:00+00:00",
        }
        for profile_id in wanted
    )


class FakeLoadCoach:
    """The scriptable state behind the ASGI app: profiles, the model, the script and the jobs.

    Thread-safe: handlers run in a threadpool (under Starlette's ``TestClient`` and under
    uvicorn alike), and a test inspects it from another thread while a generation is held.
    """

    def __init__(self, *, wire: Wire = Wire.INTERIM, model: FakeModel | None = None) -> None:
        """Create an empty fake: no profiles, one model, nothing scripted.

        Args:
            wire: Which ``usage`` shape to speak. Interim by default, because that is what every
                real LoadCoach produces until row H2 — a test that needs the post-0.7.0 shape
                says so.
            model: The model every generation is attributed to.
        """
        self.wire = wire
        self.model = model if model is not None else FakeModel()
        self.profiles: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, FakeJob] = {}
        self.requests: list[dict[str, Any]] = []
        self.version = "1.0.0"
        self._script: list[ScriptedGeneration | ScriptedError] = []
        self._default = ScriptedGeneration()
        self._lock = threading.RLock()
        self._idempotency: dict[tuple[str, str], str] = {}

    # ---- scripting ---------------------------------------------------------------------

    def register_profile(self, *profiles: Mapping[str, Any]) -> None:
        """Make one or more task profiles exist. A tier naming an unregistered one is refused.

        Variadic so ``fake.register_profile(*shipped_profiles("a", "b"))`` reads as one statement;
        the single-profile call it replaced still works unchanged.
        """
        for profile in profiles:
            self.profiles[str(profile["profile_id"])] = dict(profile)

    def script(self, *items: ScriptedGeneration | ScriptedError) -> None:
        """Queue answers, consumed in order by successive ``POST /generate`` calls.

        When the queue is empty the default generation answers, so a journey of unknown length
        still completes — a test that cares about every turn scripts every turn.
        """
        with self._lock:
            self._script.extend(items)

    def set_default(self, generation: ScriptedGeneration) -> None:
        """Replace the answer given once the script is exhausted."""
        with self._lock:
            self._default = generation

    def jobs_with_key(self, idempotency_key: str) -> list[FakeJob]:
        """Every job created under ``idempotency_key`` — the duplicate-turn assertion's input."""
        with self._lock:
            return [job for job in self.jobs.values() if job.idempotency_key == idempotency_key]

    def in_flight(self) -> list[FakeJob]:
        """Every job not yet terminal — the orphaned-job assertion's input."""
        with self._lock:
            return [job for job in self.jobs.values() if job.state in NON_TERMINAL]

    # ---- documents --------------------------------------------------------------------

    def _usage(self, gen: ScriptedGeneration) -> dict[str, Any]:
        if self.wire is Wire.INTERIM:
            cache_write: Any = UNSUPPORTED_WIRE
            cache_read: Any = UNSUPPORTED_WIRE
        else:
            cache_write = gen.cache_write_tokens
            cache_read = gen.cache_read_tokens
        return {
            "input_tokens": gen.input_tokens,
            "output_tokens": gen.output_tokens,
            "cache_write_tokens": cache_write,
            "cache_read_tokens": cache_read,
            "thinking_tokens": UNSUPPORTED_WIRE,
        }

    def _validation(self, profile: Mapping[str, Any], gen: ScriptedGeneration) -> dict[str, Any]:
        """Derive the validation block the way LoadCoach's ``validate_output`` would."""
        policy = profile.get("validation") or {}
        checks: list[dict[str, Any]] = []
        needs_json = bool(policy.get("require_valid_json") or policy.get("require_schema"))
        if needs_json or policy.get("required_fields"):
            try:
                parsed = json.loads(gen.text)
                checks.append({"kind": "json", "passed": True, "detail": {}})
            except ValueError as exc:
                parsed = None
                checks.append({"kind": "json", "passed": False, "detail": {"problem": str(exc)}})
            if parsed is not None:
                if policy.get("require_schema"):
                    checks.append(
                        {"kind": "json_schema", "passed": bool(gen.schema_passes), "detail": {}}
                    )
                required = list(policy.get("required_fields") or [])
                if required:
                    missing = [
                        name
                        for name in required
                        if not isinstance(parsed, dict) or name not in parsed
                    ]
                    checks.append(
                        {
                            "kind": "required_fields",
                            "passed": not missing,
                            "detail": {"missing": missing} if missing else {},
                        }
                    )
        if policy.get("max_output_chars") is not None:
            maximum = int(policy["max_output_chars"])
            passed = len(gen.text) <= maximum
            checks.append(
                {
                    "kind": "length",
                    "passed": passed,
                    "detail": {}
                    if passed
                    else {"chars": len(gen.text), "max_output_chars": maximum},
                }
            )
        performed = bool(checks)
        return {
            "performed": performed,
            "passed": all(check["passed"] for check in checks) if performed else None,
            "attempts": gen.attempts,
            "checks": checks,
        }

    def _generate_document(
        self, job: FakeJob, profile: Mapping[str, Any], gen: ScriptedGeneration
    ) -> dict[str, Any]:
        """``POST /generate``'s 200 body — api.md §4, ``ExecutionOutcome.as_json`` at
        ``846348b``."""
        validation = self._validation(profile, gen)
        structured = gen.structured
        if structured is None and validation["performed"] and validation["passed"]:
            parsed_check = next((c for c in validation["checks"] if c["kind"] == "json"), None)
            if parsed_check is not None and parsed_check["passed"]:
                structured = json.loads(gen.text)
        return {
            "job_id": job.job_id,
            "status": "completed",
            "output": {
                "text": gen.text,
                "finish_reason": gen.declared_finish_reason,
                "structured": structured,
                "tool_calls": [dict(call) for call in gen.tool_calls],
            },
            "reasoning": {"available": False, "summary": None, "source": None},
            "model": {
                "canonical_id": None if gen.omit_subject else self.model.canonical_id,
                "model_ref": self.model.model_ref,
                "runtime_profile_hash": self.model.runtime_profile_hash,
                "served_context": self.model.served_context,
                "served_context_source": "configured",
                "target_gpu_index": 0,
            },
            "routing": {
                "decision_id": f"01DECISION{job.job_id[-16:]}",
                "rank": 1,
                "final_score": 0.71,
                "flags": ["low_evidence"],
                "explanation_url": f"/api/v1/jobs/{job.job_id}/explanation",
            },
            "usage": self._usage(gen),
            "timing": {
                "total_ms": gen.provider_ms + 112,
                "provider_ms": gen.provider_ms,
                "loadcoach_overhead_ms": 112,
                "ttft_ms": 640,
                "queue_wait_ms": 0,
            },
            "validation": validation,
            "attempts": [
                {
                    "attempt": index + 1,
                    "model": self.model.canonical_id,
                    "runtime_profile_hash": self.model.runtime_profile_hash,
                    "rank": 1,
                    "outcome": "completed",
                    "provider_ms": gen.provider_ms,
                    "ttft_ms": 640,
                    "error_code": None,
                    "prompt_id": None,
                    "prompt_version": None,
                    "prompt_sha256": None,
                }
                for index in range(gen.attempts)
            ],
            "degradations": list(gen.degradations),
        }

    def job_document(self, job: FakeJob) -> dict[str, Any]:
        """``GET /jobs/{id}``'s body — api.md §5, ``job_document`` at ``846348b``.

        A superset of the ``/generate`` shape for a completed job, ``output.finish_reason`` and
        ``validation.checks`` included; for a cancelled or failed one the output is whatever was
        produced (nothing, here), the finish reason is ``null`` and no check was performed.
        """
        result = job.result or {}
        stamp = to_rfc3339(job.created_at)
        completed = None if job.completed_at is None else to_rfc3339(job.completed_at)
        return {
            "job_id": job.job_id,
            "status": job.state,
            "state": job.state,
            "state_reason": job.state_reason,
            "class": "normal",
            "priority": {"base": 500, "effective": 500},
            "source": job.source,
            "task": {"id": job.task, "version": "1.0.0"},
            "idempotent": True,
            "idempotency_key": job.idempotency_key,
            "cancel_requested": job.cancel_requested,
            "max_wait_seconds": None,
            "lease": {"owner": None, "expires_at": None},
            "timestamps": {
                "created_at": stamp,
                "queued_at": stamp,
                "scheduled_for": None,
                "started_at": stamp,
                "completed_at": completed,
            },
            "output": result.get(
                "output",
                {"text": "", "finish_reason": None, "structured": None, "tool_calls": []},
            ),
            "reasoning": result.get(
                "reasoning", {"available": False, "summary": None, "source": None}
            ),
            "model": result.get(
                "model",
                {
                    "canonical_id": self.model.canonical_id if result else None,
                    "model_ref": self.model.model_ref if result else None,
                    "runtime_profile_hash": None,
                    "served_context": None,
                    "served_context_source": None,
                    "target_gpu_index": None,
                },
            ),
            "routing": {
                "decision_id": result.get("routing", {}).get("decision_id"),
                "final_score": result.get("routing", {}).get("final_score"),
                "flags": result.get("routing", {}).get("flags", []),
                "explanation_url": f"/api/v1/jobs/{job.job_id}/explanation",
            },
            "usage": result.get(
                "usage",
                {
                    "input_tokens": None,
                    "output_tokens": None,
                    "cache_write_tokens": UNSUPPORTED_WIRE,
                    "cache_read_tokens": UNSUPPORTED_WIRE,
                    "thinking_tokens": UNSUPPORTED_WIRE,
                },
            ),
            "timing": result.get(
                "timing",
                {
                    "total_ms": None,
                    "provider_ms": None,
                    "loadcoach_overhead_ms": None,
                    "ttft_ms": None,
                    "queue_wait_ms": 0,
                },
            ),
            "validation": {
                "performed": result.get("validation", {}).get("performed", False),
                "passed": result.get("validation", {}).get("passed"),
                "attempts": result.get("validation", {}).get("attempts", 0),
                "checks": list(result.get("validation", {}).get("checks", [])),
            },
            "retention": {"content_scrubbed_at": None},
            "feedback": [],
            "attempts": [
                {key: value for key, value in attempt.items()}
                | {"started_at": stamp, "completed_at": completed}
                for attempt in result.get("attempts", [])
            ],
            "attempt": len(result.get("attempts", [])),
        }

    # ---- the generate call ------------------------------------------------------------

    def _next_script(self) -> ScriptedGeneration | ScriptedError:
        with self._lock:
            if self._script:
                return self._script.pop(0)
            return self._default

    def generate(self, body: GenerateBody, *, source: str) -> tuple[int, dict[str, Any]]:
        """Serve one ``POST /generate``: the (status, body) the route returns."""
        self.requests.append({"source": source, "body": body.model_dump(exclude_unset=True)})
        if body.idempotency_key is None:
            return 400, _error(
                "VALIDATION_ERROR",
                "the fake LoadCoach requires an idempotency_key on every /generate "
                "(stricter than LoadCoach; see tests/fakes/loadcoach_app.py)",
                details={"fields": [{"path": "idempotency_key", "problem": "required"}]},
            )
        with self._lock:
            existing = self._idempotency.get((source, body.idempotency_key))
            if existing is not None:
                job = self.jobs[existing]
                self._await_terminal(job)
                return 200, self.job_document(job)
            profile = self.profiles.get(body.task)
            if profile is None:
                return 404, _error(
                    "TASK_PROFILE_NOT_FOUND",
                    f"No task profile {body.task!r} is registered.",
                    details={"task": body.task},
                )
            scripted = self._next_script()
            if isinstance(scripted, ScriptedError):
                return scripted.status, _error(
                    scripted.code, scripted.message, details=dict(scripted.details)
                )
            job = FakeJob(
                job_id=new_id(),
                source=source,
                idempotency_key=body.idempotency_key,
                task=body.task,
                request_body=body.model_dump(exclude_none=True),
                created_at=datetime.now(UTC),
            )
            self.jobs[job.job_id] = job
            self._idempotency[(source, body.idempotency_key)] = job.job_id
        if scripted.delay_seconds:
            time.sleep(scripted.delay_seconds)
        if scripted.hold is not None:
            while not scripted.hold.is_set():
                if job.cancel_event.wait(0.01):
                    break
        with self._lock:
            if job.cancel_requested:
                job.state = "cancelled"
                job.state_reason = "GENERATION_CANCELLED"
                job.completed_at = datetime.now(UTC)
                job.cancel_event.set()
                return 200, self.job_document(job)
            job.result = self._generate_document(job, profile, scripted)
            job.state = "completed"
            job.completed_at = datetime.now(UTC)
            return 200, job.result

    def _await_terminal(self, job: FakeJob) -> None:
        """A replayed key waits for the original execution, as LoadCoach's does."""
        deadline = time.monotonic() + 30
        while job.state not in TERMINAL and time.monotonic() < deadline:
            self._lock.release()
            try:
                time.sleep(0.01)
            finally:
                self._lock.acquire()

    def cancel(self, job_id: str) -> tuple[int, dict[str, Any]]:
        """Serve ``POST /jobs/{id}/cancel``: 202, 404 or 409 exactly as LoadCoach answers."""
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return 404, _error(
                    "JOB_NOT_FOUND", f"No job {job_id!r}.", details={"job_id": job_id}
                )
            if job.state in TERMINAL:
                return 409, _error(
                    "JOB_NOT_CANCELLABLE",
                    f"Job {job_id} is already {job.state}; nothing to cancel.",
                    details={"job_id": job_id, "state": job.state},
                )
            already = job.cancel_requested
            job.cancel_requested = True
            if not already:
                job.state = "cancelling"
                job.state_reason = "cancel_requested"
            job.cancel_event.set()
            return 202, {"job_id": job_id, "state": job.state, "already": already}

    def list_jobs(
        self, *, source: str | None, states: set[str] | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        """Serve ``GET /jobs``: newest first, filtered, cursor-paginated (api.md §5)."""
        with self._lock:
            rows = sorted(self.jobs.values(), key=lambda j: (j.created_at, j.job_id), reverse=True)
        if source is not None:
            rows = [job for job in rows if job.source == source]
        if states is not None:
            rows = [job for job in rows if job.state in states]
        if cursor:
            after = base64.urlsafe_b64decode(cursor.encode()).decode()
            rows = [job for job in rows if f"{to_rfc3339(job.created_at)}|{job.job_id}" < after]
        page = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = (
            base64.urlsafe_b64encode(
                f"{to_rfc3339(page[-1].created_at)}|{page[-1].job_id}".encode()
            ).decode()
            if has_more
            else None
        )
        return {
            "items": [self.job_document(job) for job in page],
            "page": {
                "limit": limit,
                "next_cursor": next_cursor,
                "has_more": has_more,
                "total": None,
            },
        }

    def route_document(self, body: RouteBody) -> tuple[int, dict[str, Any]]:
        """Serve ``POST /route``: a minimal explanation, or the two documented errors."""
        profile = self.profiles.get(body.task)
        if profile is None:
            return 404, _error(
                "TASK_PROFILE_NOT_FOUND",
                f"No task profile {body.task!r} is registered.",
                details={"task": body.task},
            )
        return 200, {
            "decision_id": new_id(),
            "task_profile": {"id": body.task, "version": str(profile["version"])},
            "selected": {"canonical_id": self.model.canonical_id, "rank": 1, "final_score": 0.71},
            "candidates": [
                {"canonical_id": self.model.canonical_id, "rank": 1, "final_score": 0.71}
            ],
            "flags": ["low_evidence"],
        }

    def system_status_document(self) -> dict[str, Any]:
        """Serve ``GET /system/status`` (LoadCoach ``queue_status`` at 01170a7). No provider."""
        with self._lock:
            active = [job for job in self.jobs.values() if job.state in NON_TERMINAL]
        return {
            "depth_by_state": {"executing": len(active)},
            "depth_by_class": {},
            "active": len(active),
            "max_depth": 1000,
            "oldest_queued_age_seconds": None,
            "starving": [],
            "throughput": {"completed_last_5m": 0},
            "residency": [],
            "flags": {"paused": False, "draining": False},
            "executions": [
                {
                    "job_id": job.job_id,
                    "worker": "fake/0",
                    "state": job.state,
                    "class": "normal",
                    "canonical_id": self.model.canonical_id,
                    "target_gpu_index": 0,
                    "claimed_at": to_rfc3339(job.created_at),
                }
                for job in active
            ],
            "dispatch_latency_ms": {"samples": 0, "median": None, "max": None},
            "circuit_breakers": [],
            "last_recovery": None,
            "checked_at": to_rfc3339(datetime.now(UTC)),
        }


def _error(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return error_body(code=code, message=message, request_id=new_id(), details=details)


# --------------------------------------------------------------------------------------------
# The ASGI app
# --------------------------------------------------------------------------------------------


def _source_of(request: Request) -> str | None:
    header = request.headers.get("x-client-name", "").strip()
    return header[:64] if header else None


def build_fake_app(fake: FakeLoadCoach) -> FastAPI:
    """Build the ASGI application over one :class:`FakeLoadCoach`.

    Handlers are ``def`` (Starlette runs them in a threadpool), so a held generation blocks a
    worker thread and never the event loop — the same discipline LoadCoach's own routes keep
    (ADR-0003), and what lets the app serve under both ``TestClient`` and uvicorn.
    """
    app = FastAPI(title="LoadCoach (fake)", version=fake.version)
    router = APIRouter(prefix="/api/v1")

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {
                "path": ".".join(str(part) for part in error["loc"] if part != "body"),
                "problem": error["msg"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content=_error(
                "VALIDATION_ERROR", "Request body failed validation.", details={"fields": fields}
            ),
        )

    @app.middleware("http")
    async def _require_client_name(request: Request, call_next: Any) -> Response:
        if request.url.path != "/api/v1/version" and _source_of(request) is None:
            return JSONResponse(
                status_code=400,
                content=_error(
                    "VALIDATION_ERROR",
                    "the fake LoadCoach requires X-Client-Name on every request (stricter than "
                    "LoadCoach; see tests/fakes/loadcoach_app.py)",
                    details={"fields": [{"path": "X-Client-Name", "problem": "required"}]},
                ),
            )
        response: Response = await call_next(request)
        return response

    @router.get("/version")
    def version() -> dict[str, Any]:
        return {
            "application": {"name": "loadcoach", "version": fake.version, "git_commit": None},
            "api": {"current": "v1", "supported": ["v1"], "deprecated": []},
        }

    @router.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "application": "loadcoach",
            "version": fake.version,
            "checked_at": to_rfc3339(datetime.now(UTC)),
            "components": [
                {"name": "database", "status": "ok", "detail": "fake"},
                {"name": "provider", "status": "ok", "detail": "fake provider"},
            ],
        }

    @router.get("/system/status")
    def system_status() -> dict[str, Any]:
        return fake.system_status_document()

    @router.get("/models")
    def models() -> dict[str, Any]:
        return {"models": [fake.model.as_registry_entry()]}

    @router.get("/task-profiles")
    def task_profiles() -> dict[str, Any]:
        return {"task_profiles": [dict(profile) for profile in fake.profiles.values()]}

    @router.get("/task-profiles/{profile_id}")
    def task_profile(profile_id: str) -> Response:
        profile = fake.profiles.get(profile_id)
        if profile is None:
            return JSONResponse(
                status_code=404,
                content=_error(
                    "TASK_PROFILE_NOT_FOUND",
                    f"No task profile {profile_id!r} is registered.",
                    details={"task": profile_id},
                ),
            )
        return JSONResponse(status_code=200, content=dict(profile))

    @router.post("/route")
    def route(body: RouteBody) -> Response:
        code, document = fake.route_document(body)
        return JSONResponse(status_code=code, content=document)

    @router.post("/generate")
    def generate(request: Request, body: GenerateBody) -> Response:
        source = _source_of(request) or "anonymous"
        code, document = fake.generate(body, source=source)
        return JSONResponse(status_code=code, content=document)

    @router.get("/jobs")
    def jobs(
        source: str | None = Query(default=None),
        state: str | None = Query(default=None),
        limit: int | None = Query(default=None),
        cursor: str | None = Query(default=None),
    ) -> dict[str, Any]:
        effective = max(1, min(limit or 50, 200))
        states = None if state is None else {s for s in state.split(",") if s}
        return fake.list_jobs(source=source, states=states, limit=effective, cursor=cursor)

    @router.get("/jobs/{job_id}")
    def job(job_id: str) -> Response:
        with fake._lock:  # noqa: SLF001 — the app is the fake's own surface
            found = fake.jobs.get(job_id)
            if found is None:
                return JSONResponse(
                    status_code=404,
                    content=_error(
                        "JOB_NOT_FOUND", f"No job {job_id!r}.", details={"job_id": job_id}
                    ),
                )
            return JSONResponse(status_code=200, content=fake.job_document(found))

    @router.post("/jobs/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    def cancel(job_id: str) -> Response:
        code, document = fake.cancel(job_id)
        return JSONResponse(status_code=code, content=document)

    app.include_router(router)
    app.state.fake = fake
    return app


def held_generation(**overrides: Any) -> tuple[ScriptedGeneration, threading.Event]:
    """A generation that blocks until its event is set — for catching a job in flight."""
    hold = threading.Event()
    return ScriptedGeneration(hold=hold, **overrides), hold


def sha256_of_text(text: str) -> str:
    """The digest a turn row keeps of content; exposed so a test asserts the same value."""
    return sha256_of(text)


def iter_jobs(fake: FakeLoadCoach) -> Iterator[FakeJob]:
    """Every job, oldest first."""
    yield from sorted(fake.jobs.values(), key=lambda job: (job.created_at, job.job_id))
