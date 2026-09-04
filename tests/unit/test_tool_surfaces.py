"""``GET /tools``, ``promptcadence tools list|show``, and the tool-call assembler.

Two surfaces and one parser. The surfaces exist to answer the question a refused call raises —
"what can this run, and why not that" — so both are asserted to show the *withheld* tools and the
isolation rung, not only what works. The parser exists because LoadCoach forwards tool calls the
way providers stream them, in fragments, and reading a name off each fragment would count one call
several times.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from promptcadence.bootstrap import bootstrap
from promptcadence.cli.main import app
from promptcadence.infrastructure.loadcoach import assemble_tool_calls
from promptcadence.services.tools import DEFERRED_TOOL_CAUSE

runner = CliRunner()


# --------------------------------------------------------------------------------------------
# The assembler
# --------------------------------------------------------------------------------------------


def fragment(**fields: Any) -> dict[str, Any]:
    return fields


def test_fragments_of_one_call_are_joined_into_one_call() -> None:
    calls = assemble_tool_calls(
        [
            fragment(call_index=0, id="c1", name="read_file", arguments_fragment='{"path":'),
            fragment(call_index=0, id="c1", name=None, arguments_fragment=' "notes.md"}'),
        ]
    )
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "notes.md"}
    assert calls[0].arguments_parsed is True


def test_two_calls_to_the_same_tool_do_not_collapse() -> None:
    calls = assemble_tool_calls(
        [
            fragment(call_index=0, id="c1", name="read_file", arguments_fragment='{"path": "a"}'),
            fragment(call_index=1, id="c2", name="read_file", arguments_fragment='{"path": "b"}'),
        ]
    )
    assert [c.arguments for c in calls] == [{"path": "a"}, {"path": "b"}]


def test_calls_group_by_index_when_the_provider_supplies_no_id() -> None:
    calls = assemble_tool_calls(
        [
            fragment(call_index=0, name="list_dir", arguments_fragment="{"),
            fragment(call_index=1, name="read_file", arguments_fragment='{"path": "a"}'),
            fragment(call_index=0, arguments_fragment='"path": "."}'),
        ]
    )
    assert [c.name for c in calls] == ["list_dir", "read_file"]
    assert calls[0].arguments == {"path": "."}
    assert calls[0].call_id == "call-0"


def test_arguments_that_are_not_json_are_kept_as_text_and_flagged() -> None:
    """Never smoothed into an empty mapping: what the model actually said is what the record
    should show, and the executor refuses it with ``args_invalid`` either way."""
    (parsed,) = assemble_tool_calls([fragment(call_index=0, name="x", arguments_fragment="oops")])
    assert parsed.arguments == "oops"
    assert parsed.arguments_parsed is False


def test_arguments_that_parse_to_a_non_object_are_not_treated_as_parsed() -> None:
    (parsed,) = assemble_tool_calls([fragment(call_index=0, name="x", arguments_fragment="[1, 2]")])
    assert parsed.arguments_parsed is False


def test_a_whole_object_rather_than_a_text_fragment_is_accepted() -> None:
    """Some providers send the assembled object; one code path parses, so a mixture cannot
    half-parse."""
    (parsed,) = assemble_tool_calls(
        [fragment(call_index=0, name="read_file", arguments=({"path": "a.md"}))]
    )
    assert parsed.arguments == {"path": "a.md"}


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [fragment()],
        [fragment(name="")],
        [fragment(call_index=0, name=None, arguments_fragment=None)],
        [fragment(id="", call_index=None, name=123, arguments_fragment=456)],
    ],
)
def test_the_assembler_refuses_nothing(entries: list[dict[str, Any]]) -> None:
    """A parser that raised on model output would let the model choose when a turn ends."""
    assert isinstance(assemble_tool_calls(entries), tuple)


# --------------------------------------------------------------------------------------------
# GET /tools
# --------------------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The bootstrapped application, pointed at a closed port so no LoadCoach is needed."""
    monkeypatch.setenv("PROMPTCADENCE_LOADCOACH__BASE_URL", "http://127.0.0.1:9")
    with TestClient(bootstrap().app, base_url="http://localhost") as test_client:
        yield test_client


def test_get_tools_lists_the_registry_the_withheld_tool_and_the_isolation_rung(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/tools")
    assert response.status_code == 200
    body = response.json()
    by_name = {tool["name"]: tool for tool in body["tools"]}
    assert set(by_name) == {"read_file", "list_dir", "write_file", "run_command", "http_fetch"}
    assert by_name["read_file"]["registered"] is True
    assert by_name["read_file"]["parameters"]["type"] == "object"
    assert by_name["http_fetch"]["registered"] is False
    assert by_name["http_fetch"]["withheld_cause"] == DEFERRED_TOOL_CAUSE
    assert body["isolation"]["tier"]
    assert body["isolation"]["reason"]


def test_get_tools_by_name_finds_a_withheld_tool_too(client: TestClient) -> None:
    """Saying "no such tool" would hide the very thing an operator came to look up."""
    response = client.get("/api/v1/tools/http_fetch")
    assert response.status_code == 200
    assert response.json()["withheld_cause"] == DEFERRED_TOOL_CAUSE


def test_get_tools_by_name_refuses_a_name_configuration_does_not_hold(client: TestClient) -> None:
    response = client.get("/api/v1/tools/teleport")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TOOL_NOT_FOUND"


def test_no_tool_on_the_wire_advertises_network_egress_before_p6(client: TestClient) -> None:
    """The claim, asserted at the surface a caller reads: nothing registered reaches a socket."""
    registered = [t for t in client.get("/api/v1/tools").json()["tools"] if t["registered"]]
    assert registered
    assert {tool["egress"] for tool in registered} == {"none"}


# --------------------------------------------------------------------------------------------
# promptcadence tools
# --------------------------------------------------------------------------------------------


def test_tools_list_shows_registered_and_withheld_and_the_rung() -> None:
    result = runner.invoke(app, ["tools", "list"])
    assert result.exit_code == 0
    assert "read_file" in result.stdout
    assert f"http_fetch  withheld: {DEFERRED_TOOL_CAUSE}" in result.stdout
    assert "isolation:" in result.stdout


def test_the_http_and_cli_surfaces_report_the_same_isolation_shape(client: TestClient) -> None:
    """Both render `isolation_payload`, so the two cannot describe one probe differently.

    They were hand-built literals until an E4 review pointed out they would drift; the keys are
    asserted rather than the values, because the CLI probes the host it runs on and the route
    probes the serving process's — the same host here, but that is not the property being pinned.
    """
    from_http = client.get("/api/v1/tools").json()["isolation"]
    from_cli = json.loads(runner.invoke(app, ["tools", "list", "--json"]).stdout)["isolation"]
    assert set(from_http) == set(from_cli) == {"tier", "runtime", "reason", "limits_unenforced"}


def test_tools_list_json_is_valid_json_carrying_both_halves() -> None:
    result = runner.invoke(app, ["tools", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["tools"]) == 5
    assert payload["isolation"]["tier"]


def test_tools_show_prints_the_schema_the_model_is_given() -> None:
    result = runner.invoke(app, ["tools", "show", "read_file"])
    assert result.exit_code == 0
    assert "parameters:" in result.stdout
    assert "path" in result.stdout


def test_tools_show_exits_five_on_a_name_that_is_not_configured() -> None:
    result = runner.invoke(app, ["tools", "show", "teleport"])
    assert result.exit_code == 5
    assert "TOOL_NOT_FOUND" in result.stderr
