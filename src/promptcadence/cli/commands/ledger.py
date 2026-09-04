"""promptcadence.cli.commands.ledger — ``promptcadence ledger show`` (spec §7.2).

Mode: **either** (CLI standards §6). The ledger's tables live in this application's own database,
so an operator with no server running still gets the answer; when a server is up the command asks
it, so what the CLI prints and what the API returns come from one code path and cannot drift.

The command body calls one service method and renders. In particular it does **not** decide how a
floor is written down: ``render_money`` does, once, for the API, the CLI and every cause string
alike, which is what stops one surface printing "at least 0.004 USD" while another prints a bare
figure or — the failure spec §20 criterion 1 names — ``$0.00`` for work that was never priced.

Only ``typer`` and ``json`` load at module level (CLI standards §12).
"""

from __future__ import annotations

import json as json_module
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from promptcadence.config import Settings

__all__ = ["app"]

app = typer.Typer(help="The budget ledger: today's position and the recorded debits.")

_SCOPES = ("day", "project", "tier", "trajectory")


def _settings(config: str | None) -> Settings:
    from promptcadence.config import ConfigurationError, load_settings

    try:
        return load_settings(config_path=config).settings
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc


@contextmanager
def _local(settings: Settings) -> Iterator[tuple[Any, Any]]:
    """Yield ``(budget, trajectories)`` over the configured database, for local mode."""
    from promptcadence.services.budget import BudgetService
    from promptcadence.services.database import Database, ensure_ready
    from promptcadence.services.events import TrajectoryEventSink
    from promptcadence.services.pricing import PricingCatalog
    from promptcadence.services.runtime import utc_now
    from promptcadence.services.trajectories import TrajectoryService

    database_url = settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        typer.echo("Error: no database_url configured (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    with Database.from_url(database_url) as database:
        ensure_ready(database, auto_migrate=settings.storage.auto_migrate)
        budget = BudgetService(
            database, settings, PricingCatalog.from_settings(settings), clock=utc_now
        )
        yield (
            budget,
            TrajectoryService(database, TrajectoryEventSink(database), settings, budget=budget),
        )


def _document(settings: Settings, trajectory_id: str | None) -> dict[str, Any]:
    """Ask the server if one is answering, else read the database directly."""
    import httpx

    from promptcadence.cli.commands.trajectories import http_client_factory

    client = http_client_factory(settings)
    try:
        query = {"trajectory_id": trajectory_id} if trajectory_id else None
        response = client.get("/api/v1/ledger", params=query)
        if response.status_code == 200:
            body: dict[str, Any] = response.json()
            return dict(body.get("data", body))
        if response.status_code != 404 or trajectory_id is not None:
            error = response.json().get("error", {})
            typer.echo(f"Error: {error.get('message', 'request failed')}", err=True)
            raise typer.Exit(1)
    except httpx.HTTPError:
        pass  # no server: fall through to the local read, which is this command's other mode
    finally:
        client.close()
    with _local(settings) as (budget, trajectories):
        view = trajectories.get(trajectory_id) if trajectory_id else None
        reference = view.trajectory_id if view is not None else trajectories.most_recent_id()
        document: dict[str, Any] = budget.ledger_view(
            reference_run=reference, trajectory=view
        ).as_json()
        return document


def _line(label: str, headroom: dict[str, Any]) -> str:
    """One rendered position line, money and tokens both already floor-aware."""
    return (
        f"{label:<24} money {headroom['money_remaining_display']:>22} left   "
        f"tokens {headroom['tokens_remaining_display']:>14} left"
        f"{'   EXCEEDED' if headroom['binds'] else ''}"
    )


@app.command("show")
def show(
    scope: Annotated[str, typer.Option("--scope", help=f"One of: {', '.join(_SCOPES)}.")] = "day",
    trajectory_id: Annotated[
        str | None, typer.Option("--trajectory", help="Required for --scope trajectory.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Show the ledger position for one scope. Mode: either.

    ``day`` is the shared per-day ceiling, ``project`` each configured project's lifetime cap,
    ``trajectory`` one trajectory's own, and ``tier`` the recorded debits per tier — a **count**,
    not a balance, because no tier ceiling is configured (lifecycle §6) and this application does
    not compute a balance the ledger was not asked for.
    """
    if scope not in _SCOPES:
        typer.echo(
            f"Error: --scope must be one of {', '.join(_SCOPES)} (VALIDATION_ERROR)", err=True
        )
        raise typer.Exit(2)
    if scope == "trajectory" and not trajectory_id:
        typer.echo("Error: --scope trajectory needs --trajectory <id> (VALIDATION_ERROR)", err=True)
        raise typer.Exit(2)
    settings = _settings(config)
    document = _document(settings, trajectory_id)
    if json_output:
        typer.echo(json_module.dumps(document, indent=2, sort_keys=True))
        return
    typer.echo(f"UTC day {document['utc_day']}  (as of {document['as_of']})")
    if scope == "day" and document["day"] is not None:
        typer.echo(_line("day", document["day"]))
    elif scope == "project":
        if not document["projects"]:
            typer.echo("no [budget.projects.<name>] is configured")
        for project in document["projects"]:
            typer.echo(_line(f"project:{project['project']}", project))
    elif scope == "trajectory" and document["trajectory"] is not None:
        typer.echo(_line(f"trajectory {trajectory_id}", document["trajectory"]))
    elif scope == "tier":
        for tier in document["tiers"]:
            typer.echo(f"{tier['tier']:<24} {tier['debit_count']} debit(s) recorded")
        typer.echo("no tier ceiling is configured, so a tier has a history and not a balance")
