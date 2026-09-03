"""Tests for promptcadence.infrastructure.loadcoach: the parser, the error map, the calls.

The wire shapes come from api.md §4 and LoadCoach ``01170a7``; the strictness is this module's
own. Every LoadCoach code is walked against the map (spec §13: one mapping each, never
``INTERNAL_ERROR``), and both ``usage`` wires are parsed with the three answers kept distinct.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from baseaicore import UNSUPPORTED, ProviderKind
from fastapi.testclient import TestClient
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    ScriptedError,
    ScriptedGeneration,
    Wire,
    build_fake_app,
    schema_profile,
    text_profile,
)

from promptcadence.domain.errors import (
    CompactionFailedError,
    ErrorCode,
    LoadCoachError,
    LoadCoachUnavailableError,
    SchemaVersionUnsupportedError,
    TierUnavailableError,
)
from promptcadence.domain.threads import FinishReason
from promptcadence.infrastructure.loadcoach import (
    CLIENT_NAME,
    LOADCOACH_CODE_MAP,
    GenerateRequest,
    LoadCoachClient,
    Message,
    map_error,
    parse_generation,
    token_count_from_wire,
)

_BASE = "http://loadcoach.test"


def _generate_document(**overrides: Any) -> dict[str, Any]:
    """api.md §4's response example, byte for byte in shape."""
    document: dict[str, Any] = {
        "job_id": "01J9K0000000000000000000A0",
        "status": "completed",
        "output": {
            "text": "Local inference keeps data on the machine.",
            "structured": None,
            "tool_calls": [],
        },
        "reasoning": {"available": False, "summary": None, "source": None},
        "model": {
            "canonical_id": "ollama/qwen3.5:9b-q8_0@sha256:" + "1f" * 32,
            "model_ref": "01J9K0000000000000000000M0",
            "runtime_profile_hash": "8f2c" + "0" * 60,
            "served_context": 32768,
            "served_context_source": "configured",
            "target_gpu_index": 0,
        },
        "routing": {
            "decision_id": "01J9KD",
            "rank": 1,
            "final_score": 0.71,
            "flags": ["low_evidence"],
            "explanation_url": "/api/v1/jobs/x/explanation",
        },
        "usage": {
            "input_tokens": 812,
            "output_tokens": 1104,
            "cache_write_tokens": 0,
            "cache_read_tokens": 128,
            "thinking_tokens": "unsupported",
        },
        "timing": {
            "total_ms": 18422,
            "provider_ms": 18310,
            "loadcoach_overhead_ms": 112,
            "ttft_ms": 640,
            "queue_wait_ms": 0,
        },
        "validation": {"performed": False, "passed": None, "attempts": 1, "checks": []},
        "attempts": [
            {
                "attempt": 1,
                "model": "ollama/qwen3.5:9b-q8_0@sha256:" + "1f" * 32,
                "outcome": "completed",
            }
        ],
        "degradations": [],
    }
    document.update(overrides)
    return document


# --------------------------------------------------------------------------------------------
# Token classes: number, zero, "unsupported", null — never conflated (ADR-0016, ADR-0070)
# --------------------------------------------------------------------------------------------


def test_a_zero_is_a_count_and_unsupported_is_unsupported() -> None:
    assert token_count_from_wire(0, field_name="cache_read_tokens") == 0
    assert token_count_from_wire(128, field_name="cache_read_tokens") == 128
    assert token_count_from_wire("unsupported", field_name="cache_read_tokens") is UNSUPPORTED
    assert token_count_from_wire(None, field_name="input_tokens") is UNSUPPORTED


@pytest.mark.parametrize("bad", [-1, 1.5, True, "12", "n/a", {}])
def test_any_other_token_value_is_refused_not_guessed(bad: object) -> None:
    with pytest.raises(LoadCoachError, match="usage.input_tokens"):
        token_count_from_wire(bad, field_name="input_tokens")


def test_the_interim_wire_parses_with_cache_classes_unsupported() -> None:
    """Every real adapter before modelrack 0.7.0 (C6_HANDOFF §6)."""
    document = _generate_document(
        usage={
            "input_tokens": 812,
            "output_tokens": 1104,
            "cache_write_tokens": "unsupported",
            "cache_read_tokens": "unsupported",
            "thinking_tokens": "unsupported",
        }
    )
    parsed = parse_generation(document)
    assert parsed.usage.input_tokens == 812
    assert parsed.usage.cache_write_tokens is UNSUPPORTED
    assert parsed.usage.cache_read_tokens is UNSUPPORTED
    assert parsed.thinking_tokens is UNSUPPORTED


def test_the_post_070_wire_parses_with_zero_as_a_real_count() -> None:
    parsed = parse_generation(_generate_document())
    assert parsed.usage.cache_write_tokens == 0
    assert parsed.usage.cache_read_tokens == 128
    assert parsed.usage.total_tokens == 812 + 1104 + 0 + 128


def test_null_input_tokens_is_unsupported_never_zero() -> None:
    document = _generate_document()
    document["usage"]["input_tokens"] = None
    assert parse_generation(document).usage.input_tokens is UNSUPPORTED


# --------------------------------------------------------------------------------------------
# The parser refuses what it cannot read
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "job_id",
        "status",
        "output",
        "model.canonical_id",
        "usage",
        "timing",
        "routing",
        "usage.cache_read_tokens",
        "validation",
    ],
)
def test_a_missing_field_is_refused_by_name(path: str) -> None:
    document = _generate_document()
    node: Any = document
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[part]
    del node[parts[-1]]
    with pytest.raises(LoadCoachError) as excinfo:
        parse_generation(document)
    assert excinfo.value.details["field"].startswith(parts[0])


def test_finish_reason_is_absent_on_todays_wire_and_read_when_present() -> None:
    """LoadCoach 01170a7 renders none; the proposed location is ``output.finish_reason``."""
    assert parse_generation(_generate_document()).finish_reason is None
    document = _generate_document()
    document["output"]["finish_reason"] = "stop"
    assert parse_generation(document).finish_reason is FinishReason.STOP
    document["output"]["finish_reason"] = "content_filter"
    parsed = parse_generation(document)
    assert parsed.finish_reason is None
    assert parsed.undeclared_finish_reason == "content_filter"


def test_only_a_json_schema_check_counts_as_schema_validation() -> None:
    length_only = _generate_document(
        validation={
            "performed": True,
            "passed": True,
            "attempts": 1,
            "checks": [{"kind": "length", "passed": True, "detail": {}}],
        }
    )
    assert parse_generation(length_only).validation.schema_validated is False
    with_schema = _generate_document(
        validation={
            "performed": True,
            "passed": True,
            "attempts": 1,
            "checks": [
                {"kind": "json", "passed": True, "detail": {}},
                {"kind": "json_schema", "passed": True, "detail": {}},
            ],
        }
    )
    assert parse_generation(with_schema).validation.schema_validated is True


def test_a_job_document_without_checks_cannot_prove_schema_validation() -> None:
    """The replayed-key shape carries ``{"passed", "attempts"}`` only (LoadCoach job_document)."""
    document = _generate_document(validation={"passed": True, "attempts": 1})
    parsed = parse_generation(document)
    assert parsed.validation.checks_reported is False
    assert parsed.validation.schema_validated is False
    assert parsed.validation.passed is True


def test_provider_kind_comes_from_the_canonical_id_prefix() -> None:
    parsed = parse_generation(_generate_document())
    assert parsed.model.provider_kind is ProviderKind.OLLAMA
    document = _generate_document()
    document["model"]["canonical_id"] = "mystery/thing@sha256:" + "a" * 64
    with pytest.raises(LoadCoachError, match="provider prefix"):
        _ = parse_generation(document).model.provider_kind


# --------------------------------------------------------------------------------------------
# The error map: every LoadCoach code, one PromptCadence code, never INTERNAL_ERROR
# --------------------------------------------------------------------------------------------

_LOADCOACH_SPEC_13 = {
    "NO_ELIGIBLE_MODEL",
    "VALIDATION_FAILED",
    "QUEUE_FULL",
    "TASK_PROFILE_NOT_FOUND",
    "STRUCTURED_OUTPUT_INVALID",
    "JOB_NOT_FOUND",
    "MODEL_NOT_FOUND",
    "INSUFFICIENT_RESOURCES",
    "JOB_NOT_CANCELLABLE",
    "PROVIDER_UNAVAILABLE",
    "CONTEXT_LIMIT_EXCEEDED",
    "MAX_WAIT_EXCEEDED",
    "PROVIDER_TIMEOUT",
    "CAPABILITY_UNSUPPORTED",
    "EVIDENCE_IMPORT_FAILED",
    "PROVIDER_PROTOCOL_ERROR",
    "ALL_CANDIDATES_FAILED",
    "SCHEMA_VERSION_UNSUPPORTED",
    "PROVIDER_REJECTED",
    "GENERATION_CANCELLED",
    "EVIDENCE_SOURCE_REFUSED",
}


def test_every_loadcoach_spec_13_code_has_exactly_one_mapping() -> None:
    assert _LOADCOACH_SPEC_13 <= set(LOADCOACH_CODE_MAP)
    for code, mapped in LOADCOACH_CODE_MAP.items():
        assert isinstance(mapped, ErrorCode), code
        assert mapped is not ErrorCode.SCHEMA_VERSION_UNSUPPORTED or code == "x"


@pytest.mark.parametrize("code", sorted(_LOADCOACH_SPEC_13) + ["SOMETHING_NEW"])
def test_no_loadcoach_failure_maps_to_internal_error(code: str) -> None:
    error = map_error(
        http_status=500,
        body={"error": {"code": code, "message": "m", "details": {"k": 1}}},
        endpoint="/api/v1/generate",
    )
    assert error.code != "INTERNAL_ERROR"
    assert error.details["loadcoach_code"] == code
    assert error.details["loadcoach_details"] == {"k": 1}
    assert error.details["http_status"] == 500


def test_the_deliberate_mappings() -> None:
    def mapped(code: str) -> Any:
        return map_error(
            http_status=422,
            body={"error": {"code": code, "message": "m"}},
            endpoint="/api/v1/generate",
        )

    assert isinstance(mapped("NO_ELIGIBLE_MODEL"), TierUnavailableError)
    assert mapped("NO_ELIGIBLE_MODEL").details["reason"] == "no_eligible_model"
    assert isinstance(mapped("TASK_PROFILE_NOT_FOUND"), TierUnavailableError)
    assert mapped("TASK_PROFILE_NOT_FOUND").details["reason"] == "task_profile_not_found"
    assert isinstance(mapped("CONTEXT_LIMIT_EXCEEDED"), CompactionFailedError)
    assert isinstance(mapped("PROVIDER_TIMEOUT"), LoadCoachError)


def test_a_transport_failure_is_unavailable_and_a_timeout_is_an_error() -> None:
    unavailable = map_error(
        http_status=0,
        body=None,
        endpoint="/api/v1/generate",
        exception=httpx.ConnectError("refused"),
    )
    assert isinstance(unavailable, LoadCoachUnavailableError)
    timeout = map_error(
        http_status=0, body=None, endpoint="/api/v1/generate", exception=httpx.ReadTimeout("slow")
    )
    assert isinstance(timeout, LoadCoachError)
    assert timeout.details["reason"] == "client_timeout"


def test_a_non_envelope_body_is_a_loadcoach_error_with_the_status() -> None:
    error = map_error(http_status=502, body="<html>", endpoint="/api/v1/generate")
    assert isinstance(error, LoadCoachError)
    assert error.details["http_status"] == 502
    assert error.details["loadcoach_code"] is None


# --------------------------------------------------------------------------------------------
# The request body: exactly what GenerateBody accepts
# --------------------------------------------------------------------------------------------


def test_generate_request_refuses_what_generate_body_refuses() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        GenerateRequest(task="t")
    with pytest.raises(ValueError, match="exactly one"):
        GenerateRequest(task="t", prompt="p", messages=(Message("user", "hi"),))
    with pytest.raises(ValueError, match="system"):
        GenerateRequest(task="t", messages=(Message("user", "hi"),), system="s")
    body = GenerateRequest(
        task="t", messages=(Message("user", "hi"),), idempotency_key="k"
    ).as_body()
    assert body == {
        "task": "t",
        "messages": [{"role": "user", "content": "hi"}],
        "idempotency_key": "k",
    }


# --------------------------------------------------------------------------------------------
# The calls, against the fake
# --------------------------------------------------------------------------------------------


@pytest.fixture
def fake() -> FakeLoadCoach:
    fake = FakeLoadCoach(wire=Wire.POST_MODELRACK_070)
    fake.register_profile(text_profile("tools.agent.local_fast"))
    fake.register_profile(schema_profile("structured.answer"))
    return fake


@pytest.fixture
def client(fake: FakeLoadCoach) -> LoadCoachClient:
    return LoadCoachClient(TestClient(build_fake_app(fake), base_url=_BASE))


def test_every_request_carries_the_client_name(
    fake: FakeLoadCoach, client: LoadCoachClient
) -> None:
    client.generate(
        GenerateRequest(task="tools.agent.local_fast", prompt="hi", idempotency_key="k1")
    )
    assert fake.requests[-1]["source"] == CLIENT_NAME


def test_version_verifies_the_api_major(client: LoadCoachClient) -> None:
    info = client.version()
    assert info.api_current == "v1"
    with respx.mock(base_url=_BASE) as mock:
        mock.get("/api/v1/version").mock(
            return_value=httpx.Response(
                200,
                json={
                    "application": {"version": "9"},
                    "api": {"current": "v2", "supported": ["v2"]},
                },
            )
        )
        other = LoadCoachClient(httpx.Client(base_url=_BASE))
        with pytest.raises(SchemaVersionUnsupportedError):
            other.version()


def test_generate_returns_a_typed_response(fake: FakeLoadCoach, client: LoadCoachClient) -> None:
    fake.script(ScriptedGeneration(text="four words of answer", input_tokens=10, output_tokens=4))
    response = client.generate(
        GenerateRequest(
            task="tools.agent.local_fast", messages=(Message("user", "hi"),), idempotency_key="k2"
        )
    )
    assert response.completed
    assert response.text == "four words of answer"
    assert response.usage.input_tokens == 10
    assert response.usage.cache_read_tokens == 0
    assert response.finish_reason is None
    assert response.validation.schema_validated is False
    assert response.model.provider_kind is ProviderKind.OLLAMA


def test_a_repeated_key_returns_the_original_job_not_a_second_execution(
    fake: FakeLoadCoach, client: LoadCoachClient
) -> None:
    request = GenerateRequest(task="tools.agent.local_fast", prompt="hi", idempotency_key="same")
    first = client.generate(request)
    second = client.generate(request)
    assert first.job_id == second.job_id
    assert len(fake.jobs_with_key("same")) == 1
    assert second.validation.checks_reported is False  # the job-document shape


def test_scripted_errors_arrive_mapped(fake: FakeLoadCoach, client: LoadCoachClient) -> None:
    fake.script(
        ScriptedError(
            "NO_ELIGIBLE_MODEL", details={"candidates": [{"reason": "insufficient_vram"}]}
        ),
        ScriptedError("PROVIDER_TIMEOUT"),
        ScriptedError("CONTEXT_LIMIT_EXCEEDED"),
    )
    with pytest.raises(TierUnavailableError) as tier:
        client.generate(
            GenerateRequest(task="tools.agent.local_fast", prompt="a", idempotency_key="e1")
        )
    assert tier.value.details["loadcoach_details"]["candidates"][0]["reason"] == "insufficient_vram"
    with pytest.raises(LoadCoachError) as err:
        client.generate(
            GenerateRequest(task="tools.agent.local_fast", prompt="a", idempotency_key="e2")
        )
    assert err.value.details["loadcoach_code"] == "PROVIDER_TIMEOUT"
    with pytest.raises(CompactionFailedError):
        client.generate(
            GenerateRequest(task="tools.agent.local_fast", prompt="a", idempotency_key="e3")
        )


def test_an_unknown_task_profile_is_tier_unavailable(client: LoadCoachClient) -> None:
    with pytest.raises(TierUnavailableError) as excinfo:
        client.generate(GenerateRequest(task="tools.agent.nope", prompt="a", idempotency_key="p1"))
    assert excinfo.value.details["reason"] == "task_profile_not_found"
    assert client.task_profile("tools.agent.nope") is None
    assert client.task_profile("tools.agent.local_fast") is not None


def test_task_profiles_models_status_and_route(
    fake: FakeLoadCoach, client: LoadCoachClient
) -> None:
    assert {p.profile_id for p in client.task_profiles()} == {
        "tools.agent.local_fast",
        "structured.answer",
    }
    (entry,) = client.models()
    assert entry.provider_kind == "ollama"
    assert "depth_by_state" in client.system_status()
    assert (
        client.route(task="tools.agent.local_fast")["task_profile"]["id"]
        == "tools.agent.local_fast"
    )
    with pytest.raises(TierUnavailableError):
        client.route(task="nope")


def test_find_job_by_key_and_cancel(fake: FakeLoadCoach, client: LoadCoachClient) -> None:
    client.generate(
        GenerateRequest(task="tools.agent.local_fast", prompt="a", idempotency_key="f1")
    )
    found = client.find_job("f1")
    assert found is not None and found.state == "completed"
    assert client.find_job("f1", states=("executing",)) is None
    assert client.find_job("absent") is None
    with pytest.raises(LoadCoachError) as excinfo:
        client.cancel_job(found.job_id)
    assert excinfo.value.details["loadcoach_code"] == "JOB_NOT_CANCELLABLE"
    assert client.job(found.job_id).idempotency_key == "f1"


def test_the_transport_failure_surfaces_as_unavailable() -> None:
    client = LoadCoachClient(httpx.Client(base_url="http://127.0.0.1:9", timeout=0.2))
    with pytest.raises(LoadCoachUnavailableError):
        client.version()


def test_the_wire_dump_matches_api_md_example_keys(
    fake: FakeLoadCoach, client: LoadCoachClient
) -> None:
    """The fake's /generate body carries exactly the top-level keys api.md §4 shows."""
    client.generate(
        GenerateRequest(task="tools.agent.local_fast", prompt="a", idempotency_key="w1")
    )
    (job,) = fake.jobs.values()
    assert job.result is not None
    assert set(job.result) == set(_generate_document())
    assert set(job.result["usage"]) == set(_generate_document()["usage"])
    json.dumps(job.result)  # serialisable
