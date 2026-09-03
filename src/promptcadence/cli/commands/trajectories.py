"""promptcadence.cli.commands.trajectories — ``run`` and ``trajectory list|show|cancel|wait``.

Modes (CLI standards §6): ``run`` and ``trajectory cancel`` are **client** commands — they
mutate state a running server owns, so they talk to it over HTTP and fail with exit 4 and the URL
when it is not reachable. ``trajectory list``, ``show`` and ``wait`` are **either**: the server
when one answers, the database directly otherwise, and the output says which was used.

Exit codes (CLI standards §4) for ``run --follow`` and ``wait`` reflect the terminal state:
0 completed, 5 halted or failed, 6 cancelled.

Only ``typer`` and ``json`` load at module level; httpx and the service layer are imported inside
the command bodies (CLI standards §12).
"""

from __future__ import annotations

import json as json_module
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    import httpx

    from promptcadence.config import Settings

__all__ = ["app", "http_client_factory", "run"]

app = typer.Typer(help="Trajectories: list, show, cancel, wait.")

_TERMINAL_EXIT: dict[str, int] = {
    "completed": 0,
    "halted": 5,
    "failed": 5,
    "rejected": 5,
    "cancelled": 6,
}

_TERMINAL_STATES = frozenset(_TERMINAL_EXIT)


def _default_client(settings: Settings) -> httpx.Client:
    import httpx

    base = f"http://{settings.server.host}:{settings.server.port}"
    return httpx.Client(base_url=base, timeout=httpx.Timeout(30.0, connect=3.0))


http_client_factory: Callable[[Settings], httpx.Client] = _default_client
"""How a client-mode command reaches the server. A documented seam: the e2e suite replaces it
with a factory returning Starlette's ``TestClient`` over the in-process application, so the CLI
is exercised end to end without a socket. Production code never reassigns it. The client is
closed, never *entered* — entering a ``TestClient`` would run the application's lifespan a
second time."""


@contextmanager
def _client(settings: Settings) -> Iterator[httpx.Client]:
    client = http_client_factory(settings)
    try:
        yield client
    finally:
        client.close()


def _settings(config: str | None) -> Settings:
    from promptcadence.config import ConfigurationError, load_settings

    try:
        return load_settings(config_path=config).settings
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc


def _fail_unreachable(settings: Settings, exc: Exception) -> typer.Exit:
    url = f"http://{settings.server.host}:{settings.server.port}"
    typer.echo(f"Error: the PromptCadence server at {url} is not reachable ({exc}).", err=True)
    return typer.Exit(4)


def _envelope_error(response: httpx.Response, *, json_output: bool) -> typer.Exit:
    """Print the standard error envelope and choose the exit code (CLI standards §8)."""
    try:
        body = response.json()
    except ValueError:
        body = {"error": {"code": "HTTP_ERROR", "message": response.text}}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    if json_output:
        typer.echo(json_module.dumps(body))
    else:
        typer.echo(
            f"Error: {error.get('message', 'request failed')} ({error.get('code')})", err=True
        )
    code = str(error.get("code", ""))
    if code in {"VALIDATION_ERROR", "CLASSIFICATION_INVALID", "PROJECT_UNKNOWN", "TOOL_NOT_FOUND"}:
        return typer.Exit(2)
    if code in {"LOADCOACH_UNAVAILABLE", "TIER_UNAVAILABLE"}:
        return typer.Exit(4)
    return typer.Exit(1)


def _print_view(view: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        typer.echo(json_module.dumps(view))
        return
    typer.echo(f"trajectory   {view['trajectory_id']}")
    typer.echo(f"state        {view['state']}")
    typer.echo(f"class        {view['data_classification']}")
    typer.echo(f"bypass       {view['bypass_planning']}")
    if view.get("tier"):
        typer.echo(f"tier         {view['tier']}")
    if view.get("cause"):
        typer.echo(f"cause        {view['cause']}")
    if view.get("error_code"):
        typer.echo(f"error_code   {view['error_code']}")


def _print_event(event: dict[str, Any]) -> None:
    data = event.get("data", {})
    detail = ""
    if "cause" in data and data["cause"]:
        detail = f" — {data['cause']}"
    elif "decision" in data:
        detail = f" — {data['decision']}"
    typer.echo(f"[{event.get('sequence')}] {event.get('event_type')}{detail}")


def _follow_stream(client: httpx.Client, trajectory_id: str) -> str | None:
    """Read the SSE stream, printing each event, and return the terminal event type."""
    terminal: str | None = None
    with client.stream(
        "GET",
        f"/api/v1/trajectories/{trajectory_id}/stream",
        headers={"Accept": "text/event-stream"},
    ) as response:
        event_type: str | None = None
        for line in response.iter_lines():
            if line.startswith("event:"):
                event_type = line.partition(":")[2].strip()
            elif line.startswith("data:") and event_type is not None:
                payload = json_module.loads(line.partition(":")[2].strip())
                inner = payload.get("payload", payload)
                _print_event(
                    {
                        "sequence": inner.get("sequence"),
                        "event_type": event_type,
                        "data": inner.get("data", {}),
                    }
                )
                if event_type.startswith("trajectory.") and event_type.split(".")[1] in {
                    "completed",
                    "halted",
                    "failed",
                    "cancelled",
                }:
                    terminal = event_type
                    break
            elif line == "":
                event_type = None
    return terminal


@contextmanager
def _local_service(settings: Settings) -> Iterator[Any]:
    """The trajectory service over the configured database, for an ``either`` command."""
    from promptcadence.services.database import Database, ensure_ready
    from promptcadence.services.events import TrajectoryEventSink
    from promptcadence.services.trajectories import TrajectoryService

    database_url = settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        typer.echo("Error: no database_url configured (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    with Database.from_url(database_url) as database:
        ensure_ready(database, auto_migrate=settings.storage.auto_migrate)
        yield TrajectoryService(database, TrajectoryEventSink(database), settings)


def _server_answers(client: httpx.Client) -> bool:
    try:
        return client.get("/api/v1/version").status_code == 200
    except Exception:  # noqa: BLE001 — any transport failure means "no server"
        return False


def run(
    task: Annotated[str, typer.Argument(help="The task text, passed to the model unmodified.")],
    classification: Annotated[
        str, typer.Option("--classification", help="public | internal | confidential.")
    ] = "confidential",
    budget: Annotated[
        int | None, typer.Option("--budget", help="Money ceiling in nanos (USD).")
    ] = None,
    tokens: Annotated[int | None, typer.Option("--tokens", help="Token ceiling.")] = None,
    tier: Annotated[str | None, typer.Option("--tier", help="Pin a configured tier.")] = None,
    bypass_planning: Annotated[
        bool, typer.Option("--bypass-planning", help="Skip planning; governance still applies.")
    ] = False,
    tool: Annotated[
        list[str] | None, typer.Option("--tool", help="Allowlist a tool (repeatable).")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", help="A configured [budget.projects.<name>].")
    ] = None,
    max_turns: Annotated[int | None, typer.Option("--max-turns", help="Bypass turn cap.")] = None,
    follow: Annotated[
        bool, typer.Option("--follow", help="Stream events until the trajectory ends.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Submit a trajectory. Mode: client — needs a running server (exit 4 otherwise).

    With ``--follow``, streams the trajectory's events and exits with its terminal state's code:
    0 completed, 5 halted or failed, 6 cancelled.

    Example:
        promptcadence run "summarize the files in ./notes" --bypass-planning --follow
    """
    settings = _settings(config)
    body: dict[str, Any] = {"task": task, "data_classification": classification}
    if bypass_planning:
        body["bypass_planning"] = True
    if tier is not None:
        body["tier"] = tier
    if tool:
        body["tools"] = list(tool)
    if project is not None:
        body["project"] = project
    if max_turns is not None:
        body["max_turns"] = max_turns
    if budget is not None or tokens is not None:
        body["budget"] = {}
        if budget is not None:
            body["budget"]["money"] = {"currency": "USD", "nanos": budget}
        if tokens is not None:
            body["budget"]["tokens"] = tokens
    with _client(settings) as client:
        try:
            response = client.post("/api/v1/trajectories", json=body)
        except Exception as exc:  # noqa: BLE001 — every transport failure is "unreachable"
            raise _fail_unreachable(settings, exc) from exc
        if response.status_code != 202:
            raise _envelope_error(response, json_output=json_output)
        view = response.json()
        if not follow:
            _print_view(view, json_output=json_output)
            return
        trajectory_id = view["trajectory_id"]
        if not json_output:
            typer.echo(f"trajectory   {trajectory_id}")
        _follow_stream(client, trajectory_id)
        final = client.get(f"/api/v1/trajectories/{trajectory_id}").json()
    _print_view(final, json_output=json_output)
    raise typer.Exit(_TERMINAL_EXIT.get(str(final.get("state")), 1))


@app.command("list")
def list_trajectories(
    state: Annotated[str | None, typer.Option("--state", help="Filter by state.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """List trajectories, newest first. Mode: either."""
    settings = _settings(config)
    params = {"state": state} if state else {}
    with _client(settings) as client:
        if _server_answers(client):
            response = client.get("/api/v1/trajectories", params=params)
            if response.status_code != 200:
                raise _envelope_error(response, json_output=json_output)
            items = response.json()["items"]
            source = "server"
        else:
            from promptcadence.domain.trajectory import TrajectoryState

            with _local_service(settings) as service:
                page, _ = service.list(state=TrajectoryState(state) if state else None)
            items = [view.as_json() for view in page]
            source = "local"
    if json_output:
        typer.echo(json_module.dumps({"items": items, "source": source}))
        return
    typer.echo(f"({source})")
    for item in items:
        typer.echo(f"{item['trajectory_id']}  {item['state']:<18} {item['task'][:60]}")


@app.command("show")
def show(
    trajectory_id: Annotated[str, typer.Argument(help="A trajectory id or unambiguous prefix.")],
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Show one trajectory, with every halt's cause verbatim (spec §13). Mode: either."""
    settings = _settings(config)
    with _client(settings) as client:
        if _server_answers(client):
            response = client.get(f"/api/v1/trajectories/{trajectory_id}")
            if response.status_code != 200:
                raise _envelope_error(response, json_output=json_output)
            view = response.json()
        else:
            from baseaicore import SuiteError

            with _local_service(settings) as service:
                try:
                    view = service.resolve(trajectory_id).as_json()
                except SuiteError as exc:
                    typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
                    raise typer.Exit(1) from exc
    _print_view(view, json_output=json_output)


@app.command("cancel")
def cancel(
    trajectory_id: Annotated[str, typer.Argument(help="The trajectory id.")],
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Cancel a trajectory. Mode: client (exit 4 when no server is reachable)."""
    settings = _settings(config)
    with _client(settings) as client:
        try:
            response = client.post(f"/api/v1/trajectories/{trajectory_id}/cancel")
        except Exception as exc:  # noqa: BLE001 — every transport failure is "unreachable"
            raise _fail_unreachable(settings, exc) from exc
        if response.status_code != 202:
            raise _envelope_error(response, json_output=json_output)
        _print_view(response.json(), json_output=json_output)


@app.command("wait")
def wait(
    trajectory_id: Annotated[str, typer.Argument(help="The trajectory id.")],
    timeout_seconds: Annotated[
        float, typer.Option("--timeout", help="Give up after this many seconds (exit 1).")
    ] = 600.0,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Wait for a trajectory to end; exit with its terminal state's code. Mode: either.

    Polls the state first and drains the events second (ADR-0044 rule 3), so the terminal event
    is always printed before this command returns.
    """
    import time

    settings = _settings(config)
    deadline = time.monotonic() + timeout_seconds
    with _client(settings) as client:
        remote = _server_answers(client)
        printed = 0
        while True:
            if remote:
                response = client.get(f"/api/v1/trajectories/{trajectory_id}")
                if response.status_code != 200:
                    raise _envelope_error(response, json_output=json_output)
                view = response.json()
            else:
                with _local_service(settings) as service:
                    view = service.get(trajectory_id).as_json()
                    events = [e.as_json() for e in service.events(trajectory_id)]
                    for event in events[printed:]:
                        if not json_output:
                            _print_event(event)
                    printed = len(events)
            if view["state"] in _TERMINAL_STATES:
                break
            if time.monotonic() >= deadline:
                typer.echo("Error: timed out waiting.", err=True)
                raise typer.Exit(1)
            time.sleep(0.2)
    _print_view(view, json_output=json_output)
    raise typer.Exit(_TERMINAL_EXIT.get(str(view["state"]), 1))
