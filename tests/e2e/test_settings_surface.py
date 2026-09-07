"""``GET``/``PUT /settings`` over HTTP (spec §7.1), and the Settings page (ADR-0094).

What a reader should take from this file: the refusals name what they refused. A
security-relevant key is ``403 FORBIDDEN`` **naming the key**, an unknown one is
``400 VALIDATION_ERROR`` naming it and listing what may change, and a valid key from a principal
without ``admin`` is ``403`` — three different answers, none of them a silent ignore.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mirrorwall import CSRF_COOKIE_NAME, CSRF_FIELD_NAME
from tests.fakes.loadcoach_app import FakeLoadCoach, build_fake_app, shipped_profiles
from typer.testing import CliRunner

from promptcadence.cli import main as cli_main
from promptcadence.cli.commands import trajectories as trajectory_commands
from promptcadence.config import load_settings
from promptcadence.services.runtime import build_runtime
from promptcadence.services.tokens import create_token
from promptcadence.web.app import create_app

_PROFILES = ("tools.agent.local_fast", "tools.agent.local_large", "tools.plan")


@pytest.fixture
def client() -> Iterator[TestClient]:
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles(*_PROFILES))
    settings = load_settings().settings
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        settings, runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http)
    )
    with TestClient(app, base_url="http://127.0.0.1") as running:
        yield running


def _document(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/v1/settings")
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json())


def test_the_document_names_the_effective_value_its_definition_and_the_config_only_keys(
    client: TestClient,
) -> None:
    document = _document(client)
    assert document["settings"]["execution.step_retries"] == 1
    definition = document["definitions"]["execution.step_retries"]
    assert definition["type"] == "int"
    assert (definition["minimum"], definition["maximum"]) == (0, 25)
    assert definition["source"] == "configuration" and definition["stored"] is None
    assert "server.host" in document["config_only"]
    assert "execution.step_retries" not in document["config_only"]


def test_a_put_stores_the_value_and_the_answer_shows_it_as_database_sourced(
    client: TestClient,
) -> None:
    response = client.put("/api/v1/settings", json={"execution.step_retries": 4})
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["settings"]["execution.step_retries"] == 4
    assert document["definitions"]["execution.step_retries"]["source"] == "database"
    assert _document(client)["settings"]["execution.step_retries"] == 4, "durable, not per-request"


def test_a_security_relevant_key_is_forbidden_naming_it_and_stores_nothing(
    client: TestClient,
) -> None:
    response = client.put(
        "/api/v1/settings",
        json={"execution.step_retries": 9, "budget.daily_money_ceiling": {"nanos": 1}},
    )
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "FORBIDDEN"
    assert "budget.daily_money_ceiling" in error["message"]
    assert error["details"]["key"] == "budget.daily_money_ceiling"
    assert _document(client)["settings"]["execution.step_retries"] == 1, "refused whole"


@pytest.mark.parametrize(
    "key", ["server.host", "tools.workspace_root", "tiers.local_fast.remote", "approval.mode"]
)
def test_every_kind_of_security_relevant_key_is_refused_by_name(
    client: TestClient, key: str
) -> None:
    response = client.put("/api/v1/settings", json={key: "anything"})
    assert response.status_code == 403
    assert key in response.json()["error"]["message"]


def test_an_unknown_key_names_it_and_lists_what_can_be_changed(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"execution.max_steps": 40})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "execution.max_steps" in error["message"]
    assert "execution.step_retries" in error["details"]["runtime_changeable"]


def test_a_value_outside_its_bounds_is_refused_with_the_range(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"compaction.threshold": 2.0})
    assert response.status_code == 400
    assert "between 0.01 and 1.0" in response.json()["error"]["message"]


def test_a_stored_row_shadowed_by_the_environment_is_shown_as_shadowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__STEP_RETRIES", "3")
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles(*_PROFILES))
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        load_settings().settings,
        runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        answered = client.put("/api/v1/settings", json={"execution.step_retries": 8}).json()
        assert answered["settings"]["execution.step_retries"] == 3, "the environment wins"
        definition = answered["definitions"]["execution.step_retries"]
        assert definition["stored"] == 8, "the row is kept and shown"
        assert definition["shadowed_by"] == "env PROMPTCADENCE_EXECUTION__STEP_RETRIES"


def test_the_scopes_are_read_to_look_and_admin_to_change(client: TestClient) -> None:
    runtime = cast("FastAPI", client.app).state.runtime
    now = datetime.now(UTC)
    reader = create_token(runtime.database, name="reader", scopes=["read", "write"], now=now)
    administrator = create_token(runtime.database, name="ops", scopes=["admin"], now=now)
    as_reader = {"Authorization": f"Bearer {reader.token}"}

    assert client.get("/api/v1/settings").status_code == 401, "a token exists now"
    assert client.get("/api/v1/settings", headers=as_reader).status_code == 200, "read may look"
    refused = client.put("/api/v1/settings", json={"execution.step_retries": 2}, headers=as_reader)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "FORBIDDEN"
    assert refused.json()["error"]["details"]["required"] == "admin"
    allowed = client.put(
        "/api/v1/settings",
        json={"execution.step_retries": 2},
        headers={"Authorization": f"Bearer {administrator.token}"},
    )
    assert allowed.status_code == 200


# --------------------------------------------------------------------------------------------
# The console page (ADR-0094: the page enforces what the API enforces)
# --------------------------------------------------------------------------------------------


def _csrf(client: TestClient) -> str:
    """The token the Settings page rendered, with the cookie half planted on the client.

    The cookie carries ``Secure`` — the ``__Host-`` prefix requires it — and the test client talks
    plain ``http``, so httpx declines to store it. A browser does store it on loopback, which is
    the bind this console is reachable on; planting it reproduces that rather than weakening the
    cookie to suit the transport.
    """
    page = client.get("/settings")
    assert page.status_code == 200
    match = re.search(rf'name="{CSRF_FIELD_NAME}" value="([^"]+)"', page.text)
    assert match is not None, "the Settings page rendered no CSRF token"
    client.cookies.set(CSRF_COOKIE_NAME, match.group(1))
    return match.group(1)


def test_the_page_renders_the_form_the_navigation_and_the_config_only_list(
    client: TestClient,
) -> None:
    page = client.get("/settings")
    assert page.status_code == 200
    assert 'href="/settings" aria-current="page"' in page.text
    assert 'name="execution.step_retries"' in page.text
    assert "Save settings" in page.text
    assert "server.host" in page.text, "what cannot be changed here is named, not omitted"
    assert "budget.daily_money_ceiling" in page.text


def test_the_form_saves_and_redirects_to_the_page_saying_so(client: TestClient) -> None:
    token = _csrf(client)
    saved = client.post(
        "/settings",
        data={CSRF_FIELD_NAME: token, "execution.step_retries": "3", "compaction.threshold": ""},
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/settings?saved=1"
    document = _document(client)
    assert document["settings"]["execution.step_retries"] == 3
    assert document["definitions"]["compaction.threshold"]["stored"] is None, "empty is no change"
    assert "Saved." in client.get("/settings?saved=1").text


def test_a_post_without_the_token_never_reaches_the_handler(client: TestClient) -> None:
    refused = client.post(
        "/settings",
        data={"execution.step_retries": "3"},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "CSRF_FAILED"
    assert _document(client)["settings"]["execution.step_retries"] == 1


def test_a_number_field_holding_something_else_is_refused_by_name(client: TestClient) -> None:
    token = _csrf(client)
    refused = client.post(
        "/settings",
        data={CSRF_FIELD_NAME: token, "execution.step_retries": "soon"},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert refused.status_code == 400
    assert "execution.step_retries" in refused.json()["error"]["message"]


def test_a_read_scoped_principal_sees_the_values_but_no_form_and_its_post_is_refused(
    client: TestClient,
) -> None:
    """ADR-0094 rule 4: the page enforces what the API enforces, and shows no button it would."""
    runtime = cast("FastAPI", client.app).state.runtime
    reader = create_token(runtime.database, name="reader", scopes=["read"], now=datetime.now(UTC))
    headers = {"Authorization": f"Bearer {reader.token}"}
    page = client.get("/settings", headers=headers)
    assert page.status_code == 200
    assert "Save settings" not in page.text
    assert "holds the <code>read</code> scope" in page.text
    assert "execution.step_retries" in page.text, "a reader still sees what is effective"
    assert re.search(rf'name="{CSRF_FIELD_NAME}" value="([^"]+)"', page.text) is None

    client.cookies.set(CSRF_COOKIE_NAME, "planted")
    refused = client.post(
        "/settings",
        data={CSRF_FIELD_NAME: "planted", "execution.step_retries": "3"},
        headers={**headers, "content-type": "application/x-www-form-urlencoded"},
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["details"]["required"] == "admin"


# --------------------------------------------------------------------------------------------
# The CLI half (spec §7.2): a client-mode verb over the same endpoints and the same refusals.
# --------------------------------------------------------------------------------------------


@pytest.fixture
def cli(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    """A runner whose client-mode commands reach ``client`` instead of a socket."""
    monkeypatch.setattr(
        trajectory_commands,
        "http_client_factory",
        lambda settings: TestClient(cast("FastAPI", client.app), base_url="http://127.0.0.1"),
    )
    return CliRunner()


def test_settings_list_and_get_render_every_key_and_its_source(
    client: TestClient, cli: CliRunner
) -> None:
    listed = cli.invoke(cli_main.app, ["settings", "list"])
    assert listed.exit_code == 0, listed.output
    assert "execution.step_retries" in listed.stdout and "(configuration)" in listed.stdout

    assert client.put("/api/v1/settings", json={"execution.step_retries": 3}).status_code == 200
    got = cli.invoke(cli_main.app, ["settings", "get", "execution.step_retries"])
    assert got.exit_code == 0
    assert got.stdout.strip() == "execution.step_retries = 3 (database)"
    payload = json.loads(
        cli.invoke(cli_main.app, ["settings", "get", "execution.step_retries", "--json"]).stdout
    )
    assert payload["execution.step_retries"] == 3 and payload["source"] == "database"


def test_settings_set_writes_and_get_reads_it_back(cli: CliRunner) -> None:
    written = cli.invoke(cli_main.app, ["settings", "set", "execution.step_retries", "3"])
    assert written.exit_code == 0, written.output
    assert written.stdout.strip() == "execution.step_retries = 3 (database)"
    read_back = cli.invoke(cli_main.app, ["settings", "get", "execution.step_retries"])
    assert read_back.stdout.strip() == "execution.step_retries = 3 (database)"


def test_settings_set_reports_a_shadowed_row_rather_than_a_success(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuration standards §7: a value the environment pins is not changed by a write."""
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__STEP_RETRIES", "5")
    result = cli.invoke(cli_main.app, ["settings", "set", "execution.step_retries", "3"])
    assert result.exit_code == 0, result.output
    assert "execution.step_retries = 1" in result.stdout, "the configured value, not the row"
    assert "stored 3, but env PROMPTCADENCE_EXECUTION__STEP_RETRIES beats it" in result.stdout


def test_settings_set_refuses_a_security_relevant_key_by_name(cli: CliRunner) -> None:
    result = cli.invoke(cli_main.app, ["settings", "set", "server.host", '"10.0.0.5"'])
    assert result.exit_code == 1
    assert "FORBIDDEN" in result.stderr and "server.host" in result.stderr


def test_settings_set_refuses_an_unknown_key_and_lists_what_may_change(cli: CliRunner) -> None:
    result = cli.invoke(cli_main.app, ["settings", "set", "execution.nonsense", "1"])
    assert result.exit_code == 2
    assert "VALIDATION_ERROR" in result.stderr
    assert "compaction.threshold" in result.stderr, "the changeable set is listed"


def test_settings_get_refuses_an_unknown_key_without_asking_the_server_to(cli: CliRunner) -> None:
    result = cli.invoke(cli_main.app, ["settings", "get", "server.host"])
    assert result.exit_code == 2
    assert "not runtime-changeable" in result.stderr and "compaction.threshold" in result.stderr


def test_settings_needs_the_token_once_the_install_has_one(
    client: TestClient, cli: CliRunner
) -> None:
    """A tokened install refuses an unauthenticated read, and ``--token`` is how the CLI passes."""
    runtime = cast("FastAPI", client.app).state.runtime
    administrator = create_token(
        runtime.database, name="ops", scopes=["admin", "read"], now=datetime.now(UTC)
    )
    missing = cli.invoke(cli_main.app, ["settings", "list"])
    assert missing.exit_code == 1 and "UNAUTHORIZED" in missing.stderr
    with_token = cli.invoke(
        cli_main.app,
        ["settings", "set", "execution.step_retries", "3", "--token", administrator.token],
    )
    assert with_token.exit_code == 0, with_token.output


def test_settings_reports_an_unreachable_server_rather_than_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Refusing:
        def request(self, *args: object, **kwargs: object) -> None:
            message = "connection refused"
            raise RuntimeError(message)

        def close(self) -> None:
            return None

    monkeypatch.setattr(trajectory_commands, "http_client_factory", lambda settings: _Refusing())
    result = CliRunner().invoke(cli_main.app, ["settings", "list"])
    assert result.exit_code == 4
    assert "not reachable" in result.stderr
