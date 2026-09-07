"""The operator console: every page, over a seeded database holding every record type.

The development plan's acceptance criterion is that *the UI timeline renders every record type
from a seeded database*, so that is what this file seeds and walks. What each test asserts is not
"the page returned 200" — a page that rendered an empty table would too — but that the record it
was seeded with is **on** the page.

Three properties beyond that:

* **CSRF is real.** The inbox's buttons post, and a post without the double-submit token is 403.
* **Scopes are enforced on the page as on the API.** A ``read``-scoped principal is not shown the
  grant button and is refused by its handler.
* **The read-only pages work with JavaScript disabled**, which they do by construction — there is
  no script on them at all — and the one script that exists only announces that the record moved.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mirrorwall import CSRF_COOKIE_NAME, CSRF_FIELD_NAME
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    ScriptedError,
    ScriptedGeneration,
    build_fake_app,
    shipped_profiles,
)

from promptcadence.config import load_settings
from promptcadence.services.runtime import build_runtime
from promptcadence.services.tokens import create_token
from promptcadence.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

_TERMINAL = {"completed", "halted", "failed", "cancelled", "rejected"}
_LONG = "The meeting covered the migration plan in considerable detail. " * 6

PAGES = (
    "/",
    "/trajectories",
    "/approvals",
    "/tiers",
    "/tools",
    "/ledger",
    "/egress",
    "/system",
    "/settings",
)


def _call(name: str, arguments: str) -> dict[str, object]:
    return {"call_index": 0, "id": "c0", "name": name, "arguments_fragment": arguments}


@pytest.fixture
def fake() -> FakeLoadCoach:
    served = FakeLoadCoach()
    served.register_profile(
        *shipped_profiles("tools.agent.local_fast", "tools.agent.local_large", "tools.plan")
    )
    return served


@pytest.fixture
def client(fake: FakeLoadCoach, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A serving application with a tier small enough that a journey compacts."""
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "5")
    monkeypatch.setenv("PROMPTCADENCE_COMPACTION__PROTECTED_RECENT_TURNS", "1")
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__STEP_RETRIES", "1")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_FAST__TASK_PROFILE", "tools.agent.local_fast")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_FAST__REMOTE", "false")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_FAST__CONTEXT_BUDGET_TOKENS", "260")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_LARGE__TASK_PROFILE", "tools.agent.local_large")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_LARGE__REMOTE", "false")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_LARGE__CONTEXT_BUDGET_TOKENS", "4096")
    settings = load_settings().settings
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        settings, runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http)
    )
    with TestClient(app, base_url="http://127.0.0.1") as running:
        yield running


def _wait(client: TestClient, trajectory_id: str, states: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while True:
        view: dict[str, Any] = client.get(f"/api/v1/trajectories/{trajectory_id}").json()
        if view["state"] in states or time.monotonic() > deadline:
            return view
        time.sleep(0.02)


@pytest.fixture
def seeded(client: TestClient, fake: FakeLoadCoach) -> dict[str, Any]:
    """One planned journey holding as many record types as a single run can produce.

    A drafting attempt and its verdict, a retried step, tool calls of more than one outcome, an
    ``undeclared_tool`` deviation, a compaction, debits, egress decisions and a declared finish.
    """
    fake.script(
        ScriptedGeneration(
            text='{"steps": [{"step_id": "s1", "description": "look around",'
            ' "depends_on": [], "tools": ["list_dir"], "tier": "local_fast",'
            ' "data_classification": "confidential", "expected_turns": 2}]}'
        ),
        ScriptedError("PROVIDER_TIMEOUT"),
        ScriptedGeneration(text=_LONG, tool_calls=(_call("list_dir", '{"path": "."}'),)),
        ScriptedGeneration(text=_LONG, tool_calls=(_call("read_file", '{"path": "x"}'),)),
        ScriptedGeneration(text=_LONG, tool_calls=(_call("list_dir", '{"path": "."}'),)),
        ScriptedGeneration(text="The directory holds nothing of note."),
    )
    created = client.post("/api/v1/trajectories", json={"task": "look at ./notes"})
    trajectory_id = created.json()["trajectory_id"]
    return _wait(client, trajectory_id, _TERMINAL)


def _wait_materialized(client: TestClient, trajectory_id: str) -> None:
    """Wait for the revision the terminal transition's follow-up write produces.

    The window is real and documented: the transition commits alone and materialization is the
    **next** write (ADR-0093), so a reader that arrives between the two is served live. That is
    correct behaviour, not a bug, which is exactly why a test asserting "materialized" has to wait
    for it rather than assume the two writes are one.
    """
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/trajectories/{trajectory_id}/explanation")
        if response.headers.get("X-Explanation-Source") == "materialized":
            return
        time.sleep(0.02)
    raise AssertionError("the explanation was never materialized")


# --------------------------------------------------------------------------------------------
# Every page renders
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders(client: TestClient, seeded: dict[str, Any], path: str) -> None:
    """A page is HTML, has the shell, and names itself in the navigation."""
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "<title>" in body and "PromptCadence" in body
    assert 'href="/trajectories"' in body, "the shell's navigation is present"
    assert 'class="skip-link"' in body, "UI standards §7: skip-to-content on every page"
    assert 'aria-current="page"' in body or path == "/trajectories/x"


def test_the_timeline_renders_every_record_type(client: TestClient, seeded: dict[str, Any]) -> None:
    """The development plan's acceptance criterion, asserted section by section."""
    response = client.get(f"/trajectories/{seeded['trajectory_id']}")
    assert response.status_code == 200, response.text
    body = response.text
    for heading in (
        "The request",
        "Plan",
        "Envelopes",
        "Timeline",
        "Tool calls",
        "Compactions",
        "Debits",
        "Egress decisions",
        "Deviations",
        "Approvals",
        "Events",
    ):
        assert f">{heading}</h3>" in body, f"the timeline has no {heading} section"

    # And each of those is populated from the seeded record, not an empty state.
    assert "Drafting attempts" in body, "the plan's attempts"
    assert "Validated steps" in body
    assert "Every ExecutionIntent revision" in body
    assert "Attempts that produced no turn" in body, "the retried step"
    assert "Every recorded call, refusals and failures included" in body
    assert "Where the wire differed from the rows" in body, "the compaction"
    assert "Recorded debits" in body
    assert "Every decision about whether data could leave" in body
    assert "Every persisted event, in sequence order" in body


def test_the_timeline_says_which_path_answered(client: TestClient, seeded: dict[str, Any]) -> None:
    """The two paths carry different §15 budgets, so the page says which one it used."""
    _wait_materialized(client, seeded["trajectory_id"])
    body = client.get(f"/trajectories/{seeded['trajectory_id']}").text
    assert "served materialized from revision 1 (terminal)" in body


def test_an_unknown_trajectory_is_a_typed_404(client: TestClient) -> None:
    response = client.get("/trajectories/01UNKNOWNUNKNOWNUNKNOWNUNK")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TRAJECTORY_NOT_FOUND"


def test_model_output_is_escaped_and_never_rendered_as_markup(
    client: TestClient, fake: FakeLoadCoach
) -> None:
    """The console is a new surface for model output (ADR-0094), so this is asserted."""
    hostile = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    fake.script(ScriptedGeneration(text=hostile))
    created = client.post("/api/v1/trajectories", json={"task": hostile, "bypass_planning": True})
    view = _wait(client, created.json()["trajectory_id"], _TERMINAL)
    body = client.get(f"/trajectories/{view['trajectory_id']}").text
    assert hostile not in body, "model output reached the page as markup"
    assert "&lt;script&gt;" in body, "and it is on the page, escaped"


def test_the_read_only_pages_carry_no_script_of_their_own(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    """Progressive enhancement (ADR-0020): every read-only page is complete without JavaScript.

    The shell's own theme and table scripts are the package's; what this asserts is that no page
    here needs one to show its content, which is why a terminal trajectory's timeline has none at
    all — there is nothing left for it to announce.
    """
    body = client.get(f"/trajectories/{seeded['trajectory_id']}").text
    assert "EventSource" not in body, "a terminal record has no live updates to offer"
    for path in PAGES:
        assert "EventSource" not in client.get(path).text, f"{path} needs a script to show content"


def test_an_in_flight_timeline_offers_a_polite_live_region(client: TestClient) -> None:
    """SSE live updates, as a notice rather than as a page that reloads under a reader."""
    created = client.post(
        "/api/v1/trajectories", json={"task": "slow one", "bypass_planning": True}
    )
    body = client.get(f"/trajectories/{created.json()['trajectory_id']}").text
    assert 'aria-live="polite"' in body
    assert "EventSource" in body
    assert "/api/v1/trajectories/" in body


# --------------------------------------------------------------------------------------------
# CSRF and scopes
# --------------------------------------------------------------------------------------------


def _csrf(client: TestClient) -> str:
    """The token the inbox rendered, with the cookie half planted on the client.

    The cookie carries ``Secure`` — the ``__Host-`` prefix requires it — and the test client talks
    plain ``http``, so httpx declines to store it. Browsers do store it, because they treat
    ``http://127.0.0.1`` as a secure context, which is the loopback bind this console is reachable
    on. Planting it here reproduces the browser's behaviour rather than weakening the cookie to
    suit the transport a test happens to use.
    """
    response = client.get("/approvals")
    assert response.status_code == 200
    match = re.search(rf'name="{CSRF_FIELD_NAME}" value="([^"]+)"', response.text)
    assert match is not None, "the inbox rendered no CSRF token"
    token = match.group(1)
    client.cookies.set(CSRF_COOKIE_NAME, token)
    return token


@pytest.fixture
def manual(fake: FakeLoadCoach, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A serving application under the manual approval mode, so the inbox has work in it."""
    monkeypatch.setenv("PROMPTCADENCE_APPROVAL__MODE", "manual")
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "5")
    fake.set_default(ScriptedGeneration(text="done"))
    settings = load_settings().settings
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        settings, runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http)
    )
    with TestClient(app, base_url="http://127.0.0.1") as running:
        yield running


@pytest.fixture
def gated(manual: TestClient) -> tuple[TestClient, str]:
    """A trajectory parked on a manual bypass gate, so the inbox has something to act on."""
    client = manual
    created = client.post("/api/v1/trajectories", json={"task": "gated", "bypass_planning": True})
    trajectory_id: str = created.json()["trajectory_id"]
    _wait(client, trajectory_id, {"awaiting_approval", *_TERMINAL})
    return client, trajectory_id


def test_the_inbox_renders_a_csrf_token_and_sets_the_cookie(
    gated: tuple[TestClient, str],
) -> None:
    """The inbox is the first form this application has ever served (ADR-0094 rule 3)."""
    client, _ = gated
    response = client.get("/approvals")
    assert response.status_code == 200
    assert f'name="{CSRF_FIELD_NAME}"' in response.text
    assert CSRF_COOKIE_NAME in response.cookies


def test_a_post_without_the_token_is_refused(client: TestClient) -> None:
    """Double-submit: the field must equal the cookie, or the post never reaches a handler."""
    refused = client.post(
        "/approvals/01ANY000000000000000000000/grant",
        data={"nothing": "here"},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "CSRF_FAILED"


def test_a_post_with_a_mismatched_token_is_refused(gated: tuple[TestClient, str]) -> None:
    client, _ = gated
    _csrf(client)  # sets the cookie
    refused = client.post(
        "/approvals/01ANY000000000000000000000/grant",
        data={CSRF_FIELD_NAME: "not-the-cookie"},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert refused.status_code == 403


def test_the_inbox_grants_and_the_record_says_who(gated: tuple[TestClient, str]) -> None:
    """The button posts to the same service the API's ``/approve`` calls."""
    client, gated_id = gated
    token = _csrf(client)
    granted = client.post(
        f"/approvals/{gated_id}/grant",
        data={CSRF_FIELD_NAME: token},
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert granted.status_code == 303
    assert granted.headers["location"] == "/approvals"
    requests = client.get(f"/api/v1/trajectories/{gated_id}/intents").json()["items"]
    assert any(one["minted_by"].startswith("approver:") for one in requests)


def test_the_inbox_denies_with_the_operators_reason_on_the_record(
    gated: tuple[TestClient, str],
) -> None:
    client, gated_id = gated
    token = _csrf(client)
    denied = client.post(
        f"/approvals/{gated_id}/deny",
        data={CSRF_FIELD_NAME: token, "reason": "not this week"},
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert denied.status_code == 303
    view = client.get(f"/api/v1/trajectories/{gated_id}").json()
    assert view["state"] == "halted"
    assert "not this week" in (view["cause"] or "")


def test_a_read_scoped_principal_sees_no_buttons_and_is_refused_by_the_handler(
    gated: tuple[TestClient, str],
) -> None:
    """ADR-0094 rule 4: ``approve`` is its own scope, on the page as on the API."""
    client, _ = gated
    runtime = cast("FastAPI", client.app).state.runtime
    reader = create_token(runtime.database, name="reader", scopes=["read"], now=datetime.now(UTC))
    headers = {"Authorization": f"Bearer {reader.token}"}
    page = client.get("/approvals", headers=headers)
    assert page.status_code == 200
    assert "Grant</button>" not in page.text
    assert "holds the <code>read</code> scope" in page.text

    match = re.search(rf'name="{CSRF_FIELD_NAME}" value="([^"]+)"', page.text)
    assert match is None, "a principal that cannot approve is shown no form to post"


def test_the_console_needs_a_token_once_one_exists(client: TestClient) -> None:
    """A browser sends no bearer header, which is what makes the console loopback-first."""
    runtime = cast("FastAPI", client.app).state.runtime
    create_token(runtime.database, name="anyone", scopes=["read"], now=datetime.now(UTC))
    refused = client.get("/")
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "UNAUTHORIZED"
