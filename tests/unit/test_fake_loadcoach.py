"""The fake LoadCoach's own behaviour: the parts three later phases will lean on.

Each test here is a promise the module docstring of ``tests/fakes/loadcoach_app.py`` makes.
"""

from __future__ import annotations

import threading
import time

import pytest
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

_BASE = "http://loadcoach.test"


@pytest.fixture
def fake() -> FakeLoadCoach:
    fake = FakeLoadCoach()
    fake.register_profile(text_profile("tools.agent.local_fast"))
    fake.register_profile(schema_profile("structured.answer"))
    return fake


@pytest.fixture
def http(fake: FakeLoadCoach) -> TestClient:
    client = TestClient(build_fake_app(fake), base_url=_BASE)
    client.headers["X-Client-Name"] = "promptcadence"
    return client


def _generate(http: TestClient, **body: object) -> dict:  # type: ignore[type-arg]
    payload = {"task": "tools.agent.local_fast", "prompt": "hello", "idempotency_key": "k"} | body
    response = http.post("/api/v1/generate", json=payload)
    return {"status": response.status_code, "body": response.json()}


def test_x_client_name_is_required_stricter_than_loadcoach(fake: FakeLoadCoach) -> None:
    anonymous = TestClient(build_fake_app(fake), base_url=_BASE)
    response = anonymous.post("/api/v1/generate", json={"task": "t", "prompt": "p"})
    assert response.status_code == 400
    assert "X-Client-Name" in response.json()["error"]["message"]
    assert anonymous.get("/api/v1/version").status_code == 200  # never authenticated


def test_an_idempotency_key_is_required_stricter_than_loadcoach(http: TestClient) -> None:
    response = http.post("/api/v1/generate", json={"task": "tools.agent.local_fast", "prompt": "p"})
    assert response.status_code == 400
    assert response.json()["error"]["details"]["fields"][0]["path"] == "idempotency_key"


def test_finish_reason_is_never_emitted(http: TestClient) -> None:
    """LoadCoach 01170a7 renders none, so neither does the fake."""
    result = _generate(http)
    assert result["status"] == 200
    assert "finish_reason" not in result["body"]["output"]
    assert "finish_reason" not in result["body"]["attempts"][0]


def test_the_interim_wire_reports_cache_classes_unsupported(http: TestClient) -> None:
    usage = _generate(http)["body"]["usage"]
    assert usage["cache_write_tokens"] == "unsupported"
    assert usage["cache_read_tokens"] == "unsupported"
    assert usage["thinking_tokens"] == "unsupported"
    assert usage["input_tokens"] == 812


def test_the_post_070_wire_reports_zero_or_a_count() -> None:
    fake = FakeLoadCoach(wire=Wire.POST_MODELRACK_070)
    fake.register_profile(text_profile("tools.agent.local_fast"))
    fake.script(ScriptedGeneration(cache_read_tokens=128))
    http = TestClient(build_fake_app(fake), base_url=_BASE)
    http.headers["X-Client-Name"] = "promptcadence"
    usage = _generate(http)["body"]["usage"]
    assert usage["cache_write_tokens"] == 0
    assert usage["cache_read_tokens"] == 128
    assert usage["thinking_tokens"] == "unsupported"


def test_validation_is_derived_from_the_profile_policy(
    http: TestClient, fake: FakeLoadCoach
) -> None:
    text = _generate(http)["body"]["validation"]
    assert text["performed"] is True and text["passed"] is True
    assert [check["kind"] for check in text["checks"]] == ["length"]
    fake.script(ScriptedGeneration(text='{"answer": 42}'))
    structured = _generate(http, task="structured.answer", idempotency_key="s1")["body"]
    kinds = [check["kind"] for check in structured["validation"]["checks"]]
    assert kinds == ["json", "json_schema", "required_fields", "length"]
    assert structured["output"]["structured"] == {"answer": 42}
    fake.script(ScriptedGeneration(text="not json"))
    failed = _generate(http, task="structured.answer", idempotency_key="s2")["body"]
    assert failed["validation"]["passed"] is False
    assert failed["validation"]["checks"][0] == {
        "kind": "json",
        "passed": False,
        "detail": {"problem": failed["validation"]["checks"][0]["detail"]["problem"]},
    }


def test_a_repeated_key_replays_the_job_document(http: TestClient, fake: FakeLoadCoach) -> None:
    first = _generate(http, idempotency_key="r1")["body"]
    replay = _generate(http, idempotency_key="r1", prompt="a different prompt")["body"]
    assert replay["job_id"] == first["job_id"]
    assert replay["state"] == "completed"  # the job-document shape
    assert len(fake.jobs) == 1
    assert len(fake.requests) == 2


def test_keys_are_scoped_per_caller(fake: FakeLoadCoach) -> None:
    app = build_fake_app(fake)
    one = TestClient(app, base_url=_BASE, headers={"X-Client-Name": "one"})
    two = TestClient(app, base_url=_BASE, headers={"X-Client-Name": "two"})
    a = one.post(
        "/api/v1/generate",
        json={"task": "tools.agent.local_fast", "prompt": "p", "idempotency_key": "shared"},
    ).json()
    b = two.post(
        "/api/v1/generate",
        json={"task": "tools.agent.local_fast", "prompt": "p", "idempotency_key": "shared"},
    ).json()
    assert a["job_id"] != b["job_id"]


def test_scripted_errors_use_loadcoachs_own_statuses(http: TestClient, fake: FakeLoadCoach) -> None:
    fake.script(
        ScriptedError("QUEUE_FULL"),
        ScriptedError("PROVIDER_UNAVAILABLE"),
        ScriptedError("INSUFFICIENT_RESOURCES"),
    )
    assert _generate(http, idempotency_key="e1")["status"] == 429
    assert _generate(http, idempotency_key="e2")["status"] == 503
    assert _generate(http, idempotency_key="e3")["status"] == 503
    assert _generate(http, task="nope", idempotency_key="e4")["status"] == 404


def test_a_held_generation_is_in_flight_until_released_or_cancelled(
    http: TestClient, fake: FakeLoadCoach
) -> None:
    hold = threading.Event()
    fake.script(ScriptedGeneration(hold=hold))
    outcome: dict = {}  # type: ignore[type-arg]

    def call() -> None:
        outcome.update(_generate(http, idempotency_key="held"))

    thread = threading.Thread(target=call)
    thread.start()
    deadline = time.monotonic() + 5
    while not fake.in_flight() and time.monotonic() < deadline:
        time.sleep(0.01)
    (job,) = fake.in_flight()
    listed = http.get(
        "/api/v1/jobs", params={"source": "promptcadence", "state": "executing"}
    ).json()
    assert [item["job_id"] for item in listed["items"]] == [job.job_id]
    assert listed["items"][0]["idempotency_key"] == "held"
    cancelled = http.post(f"/api/v1/jobs/{job.job_id}/cancel")
    assert cancelled.status_code == 202
    assert cancelled.json() == {"job_id": job.job_id, "state": "cancelling", "already": False}
    thread.join(timeout=5)
    assert outcome["status"] == 200
    assert outcome["body"]["state"] == "cancelled"  # "200 with a cancelled job"
    assert outcome["body"]["state_reason"] == "GENERATION_CANCELLED"
    assert http.post(f"/api/v1/jobs/{job.job_id}/cancel").status_code == 409
    assert http.post("/api/v1/jobs/absent/cancel").status_code == 404
    assert not fake.in_flight()


def test_jobs_list_pages_newest_first(http: TestClient) -> None:
    for index in range(5):
        _generate(http, idempotency_key=f"p{index}")
    first = http.get("/api/v1/jobs", params={"source": "promptcadence", "limit": 2}).json()
    assert len(first["items"]) == 2 and first["page"]["has_more"] is True
    second = http.get(
        "/api/v1/jobs",
        params={"source": "promptcadence", "limit": 2, "cursor": first["page"]["next_cursor"]},
    ).json()
    assert {i["idempotency_key"] for i in first["items"]}.isdisjoint(
        {i["idempotency_key"] for i in second["items"]}
    )
    assert first["items"][0]["idempotency_key"] == "p4"
    assert http.get("/api/v1/jobs/absent").status_code == 404


def test_the_system_status_carries_no_provider_information(http: TestClient) -> None:
    """The reason the provider surface is read from /models (services/loadcoach_surface)."""
    status = http.get("/api/v1/system/status").json()
    assert "provider" not in status
    assert "depth_by_state" in status
    models = http.get("/api/v1/models").json()["models"]
    assert models[0]["provider_kind"] == "ollama"
