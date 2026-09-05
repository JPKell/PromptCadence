"""promptcadence.cli.commands.approvals — ``approvals list``, ``approve <id>``, ``deny <id>``.

Spec §7.2's spellings. ``approve`` and ``deny`` are **client** commands (CLI standards §6): a grant
mints intents under an identity, and that identity is the bearer token the server resolves, so
they go over HTTP and present ``PROMPTCADENCE_API_TOKEN`` (or ``--token``) when one is set. On an
open loopback install with no tokens the server records the grant as ``approver:loopback``.
``approvals list`` is **either**: the server when one answers, the database otherwise.

Only ``typer`` and ``json`` load at module level (CLI standards §12).
"""

from __future__ import annotations

import json as json_module
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from collections.abc import Iterator

    import httpx

    from promptcadence.config import Settings

__all__ = ["app", "approve", "deny"]

app = typer.Typer(help="Pending approval requests.")

TOKEN_ENV = "PROMPTCADENCE_API_TOKEN"  # noqa: S105 — an environment variable's *name*


def _settings(config: str | None) -> Settings:
    from promptcadence.config import ConfigurationError, load_settings

    try:
        return load_settings(config_path=config).settings
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc


def _headers(token: str | None) -> dict[str, str]:
    presented = token or os.environ.get(TOKEN_ENV)
    return {"Authorization": f"Bearer {presented}"} if presented else {}


@contextmanager
def _client(settings: Settings) -> Iterator[httpx.Client]:
    from promptcadence.cli.commands.trajectories import http_client_factory

    client = http_client_factory(settings)
    try:
        yield client
    finally:
        client.close()


def _fail(response: httpx.Response, *, json_output: bool) -> typer.Exit:
    from promptcadence.cli.commands.trajectories import _envelope_error

    return _envelope_error(response, json_output=json_output)


def _line(item: dict[str, Any]) -> str:
    steps = ",".join(item.get("step_ids", [])) or "-"
    return (
        f"{item['request_id']}  {item['trajectory_id']}  {item['kind']:<13} "
        f"{item['reason']:<24} steps={steps:<10} age={item['age_seconds']:.0f}s  "
        f"expires={item['expires_at']}"
    )


@app.command("list")
def list_approvals(
    trajectory_id: Annotated[
        str | None, typer.Option("--trajectory", help="Only this trajectory's requests.")
    ] = None,
    all_statuses: Annotated[
        bool, typer.Option("--all", help="Include granted, denied and expired requests.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """List pending approval requests with their ages, oldest first. Mode: either."""
    settings = _settings(config)
    params: dict[str, Any] = {}
    if trajectory_id:
        params["trajectory_id"] = trajectory_id
    if all_statuses:
        params["status"] = "all"
    items: list[dict[str, Any]]
    with _client(settings) as client:
        try:
            response = client.get("/api/v1/approvals", params=params, headers=_headers(None))
            if response.status_code != 200:
                raise _fail(response, json_output=json_output)
            items = list(response.json()["items"])
            source = "server"
        except typer.Exit:
            raise
        except Exception:  # noqa: BLE001 — no server: the local read is this command's other mode
            items = _local_items(settings, trajectory_id=trajectory_id, all_statuses=all_statuses)
            source = "local"
    if json_output:
        typer.echo(json_module.dumps({"items": items, "source": source}, sort_keys=True))
        return
    typer.echo(f"({source})")
    if not items:
        typer.echo("No pending approval requests.")
        return
    for item in items:
        typer.echo(_line(item))


def _local_items(
    settings: Settings, *, trajectory_id: str | None, all_statuses: bool
) -> list[dict[str, Any]]:
    from datetime import UTC, datetime

    from promptcadence.services.approvals import ApprovalService
    from promptcadence.services.budget import BudgetService
    from promptcadence.services.database import Database, ensure_ready
    from promptcadence.services.estimates import StepEstimator
    from promptcadence.services.events import TrajectoryEventSink
    from promptcadence.services.pricing import PricingCatalog
    from promptcadence.services.runtime import utc_now

    database_url = settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        typer.echo("Error: no database_url configured (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    with Database.from_url(database_url) as database:
        ensure_ready(database, auto_migrate=settings.storage.auto_migrate)
        budget = BudgetService(database, settings, PricingCatalog(by_tier={}), clock=utc_now)
        service = ApprovalService(
            database,
            TrajectoryEventSink(database),
            settings,
            estimator=StepEstimator(budget, settings, clock=utc_now),
            budget=budget,
            clock=utc_now,
        )
        views = (
            service.requests(trajectory_id)
            if all_statuses and trajectory_id
            else service.pending(trajectory_id=trajectory_id)
        )
        now = datetime.now(UTC)
        return [view.as_json(now=now) for view in views]


def approve(
    trajectory_id: Annotated[str, typer.Argument(help="The trajectory whose request to grant.")],
    tokens: Annotated[
        int | None, typer.Option("--tokens", help="New token ceiling (ceiling_raise only).")
    ] = None,
    money_nanos: Annotated[
        int | None, typer.Option("--money-nanos", help="New money ceiling in USD nanos.")
    ] = None,
    token: Annotated[
        str | None, typer.Option("--token", help=f"Bearer token; else ${TOKEN_ENV}.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Grant the trajectory's pending approval request. Mode: client; needs the approve scope.

    Idempotent per request. For a ``ceiling_raise`` request pass the new ceiling with
    ``--tokens`` and/or ``--money-nanos``.
    """
    settings = _settings(config)
    body: dict[str, Any] = {}
    if tokens is not None or money_nanos is not None:
        body["budget"] = {}
        if tokens is not None:
            body["budget"]["tokens"] = tokens
        if money_nanos is not None:
            body["budget"]["money"] = {"currency": "USD", "nanos": money_nanos}
    with _client(settings) as client:
        try:
            response = client.post(
                f"/api/v1/trajectories/{trajectory_id}/approve",
                json=body or None,
                headers=_headers(token),
            )
        except Exception as exc:  # noqa: BLE001 — every transport failure is "unreachable"
            from promptcadence.cli.commands.trajectories import _fail_unreachable

            raise _fail_unreachable(settings, exc) from exc
        if response.status_code != 200:
            raise _fail(response, json_output=json_output)
        document = response.json()
    if json_output:
        typer.echo(json_module.dumps(document, sort_keys=True))
        return
    request = document["request"]
    typer.echo(f"trajectory   {document['trajectory_id']}")
    typer.echo(f"request      {request['request_id']} ({request['kind']}, {request['status']})")
    typer.echo(f"state        {document['state']}")
    for minted in document["minted"]:
        typer.echo(
            f"minted       {minted['intent_id']}@{minted['revision']} for step {minted['step_id']}"
        )
    if document["already_resolved"]:
        typer.echo("already resolved; nothing changed")


def deny(
    trajectory_id: Annotated[str, typer.Argument(help="The trajectory whose request to deny.")],
    reason: Annotated[
        str | None, typer.Option("--reason", help="Why, recorded on the request.")
    ] = None,
    token: Annotated[
        str | None, typer.Option("--token", help=f"Bearer token; else ${TOKEN_ENV}.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Deny the trajectory's pending approval request; it halts. Mode: client; approve scope."""
    settings = _settings(config)
    with _client(settings) as client:
        try:
            response = client.post(
                f"/api/v1/trajectories/{trajectory_id}/deny",
                json={"reason": reason} if reason else None,
                headers=_headers(token),
            )
        except Exception as exc:  # noqa: BLE001 — every transport failure is "unreachable"
            from promptcadence.cli.commands.trajectories import _fail_unreachable

            raise _fail_unreachable(settings, exc) from exc
        if response.status_code != 200:
            raise _fail(response, json_output=json_output)
        document = response.json()
    if json_output:
        typer.echo(json_module.dumps(document, sort_keys=True))
        return
    request = document["request"]
    typer.echo(f"trajectory   {document['trajectory_id']}")
    typer.echo(f"request      {request['request_id']} ({request['kind']}, {request['status']})")
    typer.echo(f"state        {document['state']}")
    if request.get("resolution_reason"):
        typer.echo(f"reason       {request['resolution_reason']}")
