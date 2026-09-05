"""I10: the fake LoadCoach and the client, against LoadCoach's committed OpenAPI snapshot.

``loadcoach_openapi.json`` is a byte copy of LoadCoach ``docs/openapi.json`` (LoadCoach
``f5f3b81``; the file last moved at ``8815dae``, the G2 tool wire), recorded with its digest
below. This is what
keeps the fake honest: every request body the client can send validates against the snapshot's
schemas, every path the client and the fake use exists in the snapshot with that method, and the
fake's own request models are the snapshot's, shape for shape. LoadCoach ``846348b``
(the ``output.finish_reason`` render) leaves the snapshot byte-identical — responses are open
objects there — which is why the digest below did not move with it.

**What the snapshot cannot pin, stated plainly.** LoadCoach's route handlers return plain
dictionaries, so the snapshot types every *response* as an open object (``additionalProperties:
true``) and lists only FastAPI's own statuses (``200``/``202``/``422``). Response shapes are
therefore pinned by transcription — ``tests/unit/test_loadcoach_client.py`` holds api.md §4's
example and asserts the fake's keys equal it — not by this file. When LoadCoach types its
responses, this file grows the assertion.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from tests.fakes import loadcoach_app
from tests.fakes.loadcoach_app import FakeLoadCoach, build_fake_app, text_profile

from promptcadence.infrastructure.loadcoach import (
    API_PREFIX,
    GenerateRequest,
    LoadCoachClient,
    Message,
    RequestedToolCall,
    ToolDefinition,
)

pytestmark = pytest.mark.contract

SNAPSHOT = Path(__file__).resolve().parent / "loadcoach_openapi.json"
SNAPSHOT_SHA256 = "def4271ece90f73bcb31bef0bb014c763433e2adda925ee4cfb165d14e138692"
SNAPSHOT_SOURCE = (
    "LoadCoach docs/openapi.json at f5f3b81 (the G2 tool wire; last changed at 8815dae)"
)


@pytest.fixture(scope="module")
def snapshot() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(SNAPSHOT.read_text(encoding="utf-8")))


def test_the_vendored_snapshot_is_the_one_recorded() -> None:
    """A drifted copy is a fake checked against nothing; the digest names which copy this is."""
    digest = hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest()
    assert digest == SNAPSHOT_SHA256, (
        f"snapshot changed; update SNAPSHOT_SHA256 and SNAPSHOT_SOURCE ({SNAPSHOT_SOURCE})"
    )


# Every (method, path) the client calls, and every one the fake serves.
_CLIENT_ENDPOINTS = {
    ("get", "/version"),
    ("get", "/system/status"),
    ("get", "/models"),
    ("get", "/task-profiles"),
    ("get", "/task-profiles/{profile_id}"),
    ("post", "/route"),
    ("post", "/generate"),
    ("get", "/jobs"),
    ("get", "/jobs/{job_id}"),
    ("post", "/jobs/{job_id}/cancel"),
}


@pytest.mark.parametrize("method,path", sorted(_CLIENT_ENDPOINTS))
def test_every_endpoint_the_client_uses_is_in_the_snapshot(
    snapshot: dict[str, Any], method: str, path: str
) -> None:
    assert method in snapshot["paths"][f"{API_PREFIX}{path}"], (method, path)


def test_every_route_the_fake_serves_is_in_the_snapshot(snapshot: dict[str, Any]) -> None:
    app = build_fake_app(FakeLoadCoach())
    served = {
        (method.lower(), str(getattr(route, "path", "")))
        for route in app.routes
        for method in getattr(route, "methods", ())
        if str(getattr(route, "path", "")).startswith(API_PREFIX)
    }
    for method, path in served:
        assert method in snapshot["paths"][path], (method, path)


def test_the_fakes_generate_body_is_the_snapshots_shape_for_shape(snapshot: dict[str, Any]) -> None:
    """Same properties, same required set, same ``additionalProperties: false``."""
    expected = snapshot["components"]["schemas"]["GenerateBody"]
    actual = loadcoach_app.GenerateBody.model_json_schema()
    assert set(actual["properties"]) == set(expected["properties"])
    assert set(actual.get("required", [])) == set(expected.get("required", []))
    assert actual.get("additionalProperties") is False
    for name in (
        "MessageBody",
        "OverridesBody",
        "RuntimeProfileOverrideBody",
        "ToolCallBody",
        "ToolDefinitionBody",
    ):
        mirror = getattr(loadcoach_app, name).model_json_schema()
        assert set(mirror["properties"]) == set(
            snapshot["components"]["schemas"][name]["properties"]
        ), name


def test_the_fakes_route_body_is_the_snapshots_shape_for_shape(snapshot: dict[str, Any]) -> None:
    expected = snapshot["components"]["schemas"]["RouteBody"]
    actual = loadcoach_app.RouteBody.model_json_schema()
    assert set(actual["properties"]) == set(expected["properties"])
    assert set(actual.get("required", [])) == set(expected.get("required", []))
    constraints = loadcoach_app.TaskProfileConstraints.model_json_schema()
    assert set(constraints["properties"]) == set(
        snapshot["components"]["schemas"]["TaskProfileConstraints"]["properties"]
    )


def test_every_body_the_client_can_send_is_accepted_by_the_snapshots_generate_body(
    snapshot: dict[str, Any],
) -> None:
    """The client's body keys are a subset of GenerateBody's; the client cannot send an extra."""
    allowed = set(snapshot["components"]["schemas"]["GenerateBody"]["properties"])
    bodies = [
        GenerateRequest(
            task="t",
            prompt="p",
            system="s",
            idempotency_key="k",
            response_format="json",
            sampling={"temperature": 0.1},
        ).as_body(),
        GenerateRequest(
            task="t", messages=(Message("user", "u"), Message("tool", "r", "c1"))
        ).as_body(),
        GenerateRequest(
            task="t",
            messages=(
                Message("user", "u"),
                Message(
                    "assistant",
                    "",
                    tool_calls=(
                        RequestedToolCall(
                            call_id="c1",
                            name="list_dir",
                            arguments={"path": "./notes"},
                            arguments_parsed=True,
                        ),
                    ),
                ),
                Message("tool", "a.md", "c1"),
            ),
            tools=(
                ToolDefinition(
                    name="list_dir",
                    description="List a directory in the workspace.",
                    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                ),
            ),
        ).as_body(),
    ]
    for body in bodies:
        assert set(body) <= allowed, body


def test_the_fake_refuses_what_the_snapshot_refuses() -> None:
    """api.md §4's *example* shows ``constraints``/``priority``; the schema forbids both."""
    fake = FakeLoadCoach()
    fake.register_profile(text_profile("tools.agent.local_fast"))
    http = TestClient(build_fake_app(fake), base_url="http://loadcoach.test")
    http.headers["X-Client-Name"] = "promptcadence"
    response = http.post(
        f"{API_PREFIX}/generate",
        json={
            "task": "tools.agent.local_fast",
            "prompt": "p",
            "idempotency_key": "k",
            "constraints": {"max_latency_seconds": 120},
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    both = http.post(
        f"{API_PREFIX}/generate",
        json={
            "task": "tools.agent.local_fast",
            "prompt": "p",
            "messages": [],
            "idempotency_key": "k",
        },
    )
    assert both.status_code == 400


def test_the_fake_refuses_every_transcript_the_real_server_refuses() -> None:
    """api.md §4's four rules, each one a `VALIDATION_ERROR` naming its field.

    Verified against the real LoadCoach as well as against the fake: the same four bodies were
    posted to a `loadcoach serve` on 127.0.0.1:8766 (LoadCoach f5f3b81, the working tree of this
    row) and each returned the same code and the same `details.fields[0].path`. See the G2 handoff
    for the transcript.
    """
    fake = FakeLoadCoach()
    fake.register_profile(text_profile("tools.agent.local_fast"))
    http = TestClient(build_fake_app(fake), base_url="http://loadcoach.test")
    http.headers["X-Client-Name"] = "promptcadence"
    call = {"id": "c1", "name": "list_dir", "arguments": {}}
    cases = {
        "messages[0].tool_calls": [{"role": "user", "content": "u", "tool_calls": [call]}],
        "messages[0].tool_call_id": [{"role": "tool", "content": "r"}],
        "messages[0].content": [{"role": "assistant", "content": ""}],
        "messages[1].tool_call_id": [
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "content": "r", "tool_call_id": "c9"},
        ],
    }
    for path, messages in cases.items():
        response = http.post(
            f"{API_PREFIX}/generate",
            json={
                "task": "tools.agent.local_fast",
                "messages": messages,
                "idempotency_key": f"k-{path}",
            },
        )
        assert response.status_code == 400, path
        body = response.json()["error"]
        assert body["code"] == "VALIDATION_ERROR", path
        assert body["details"]["fields"][0]["path"] == path


def test_the_fake_accepts_the_transcript_the_real_server_accepts() -> None:
    """The declared call a hostile-model journey needs the fake to be able to script."""
    fake = FakeLoadCoach()
    fake.register_profile(text_profile("tools.agent.local_fast"))
    http = TestClient(build_fake_app(fake), base_url="http://loadcoach.test")
    http.headers["X-Client-Name"] = "promptcadence"
    response = http.post(
        f"{API_PREFIX}/generate",
        json={
            "task": "tools.agent.local_fast",
            "messages": [
                {"role": "user", "content": "List ./notes."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "name": "list_dir", "arguments": {"path": "./notes"}}
                    ],
                },
                {"role": "tool", "content": "a.md", "tool_call_id": "c1"},
            ],
            "tools": [{"name": "list_dir", "description": "", "parameters": {}}],
            "idempotency_key": "k-ok",
        },
    )
    assert response.status_code == 200, response.text
    sent = fake.requests[-1]["body"]
    assert sent["tools"][0]["name"] == "list_dir"
    assert sent["messages"][1]["tool_calls"][0]["id"] == "c1"


def test_the_prompt_loadcoach_forwards_equals_the_prompt_promptcadence_sent() -> None:
    """I10's third clause: the fake records the body it received; nothing rewrote it."""
    fake = FakeLoadCoach()
    fake.register_profile(text_profile("tools.agent.local_fast"))
    client = LoadCoachClient(TestClient(build_fake_app(fake), base_url="http://loadcoach.test"))
    sent = GenerateRequest(
        task="tools.agent.local_fast",
        messages=(Message("user", "Summarise ./notes — verbatim, please."),),
        idempotency_key="k9",
    )
    client.generate(sent)
    assert fake.requests[-1]["body"] == sent.as_body()


def test_every_configured_tier_profile_check_is_answerable() -> None:
    """I10's second clause: the question ``tiers check`` asks has a yes and a no on this fake."""
    fake = FakeLoadCoach()
    fake.register_profile(text_profile("tools.agent.local_fast"))
    client = LoadCoachClient(TestClient(build_fake_app(fake), base_url="http://loadcoach.test"))
    assert client.task_profile("tools.agent.local_fast") is not None
    assert client.task_profile("tools.agent.local_large") is None
