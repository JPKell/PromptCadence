"""promptcadence.cli.commands.egress — ``promptcadence egress list`` (spec §7.2).

Mode: **either** (CLI standards §6). Commissioner's table lives in this application's own database,
so an operator with no server running still gets the answer; when a server is up the command asks
it, so what the CLI prints and what the API returns come from one code path and cannot drift.

The command body calls one service method and renders. It interprets no verdict and re-derives no
reason: both come from the recorded decision, which is Commissioner's rendering of what its policy
decided (ADR-0054).

**Approvals are listed alongside denials, and that is the point.** A tool that showed only refusals
would answer "what was blocked" and not "where did this trajectory's data go" — and an operator
who can only see refusals cannot tell governed egress from ungoverned egress.

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

app = typer.Typer(help="Recorded egress decisions: what was approved, denied and violated.")

_VERDICTS = ("approved", "denied", "violation")


def _settings(config: str | None) -> Settings:
    from promptcadence.config import ConfigurationError, load_settings

    try:
        return load_settings(config_path=config).settings
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc


@contextmanager
def _local(settings: Settings) -> Iterator[Any]:
    """Yield an :class:`~promptcadence.services.egress.EgressService` over the database."""
    from promptcadence.services.database import Database, ensure_ready
    from promptcadence.services.egress import EgressService
    from promptcadence.services.runtime import utc_now

    database_url = settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        typer.echo("Error: no database_url configured (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    with Database.from_url(database_url) as database:
        ensure_ready(database, auto_migrate=settings.storage.auto_migrate)
        yield EgressService(database, clock=utc_now)


def _documents(
    settings: Settings, *, trajectory_id: str | None, verdict: str | None, limit: int
) -> list[dict[str, Any]]:
    """Ask the server if one is answering, else read the database directly."""
    import httpx

    from promptcadence.cli.commands.trajectories import http_client_factory

    query: dict[str, Any] = {"limit": limit}
    if trajectory_id:
        query["trajectory_id"] = trajectory_id
    if verdict:
        query["verdict"] = verdict
    client = http_client_factory(settings)
    try:
        response = client.get("/api/v1/egress-decisions", params=query)
        if response.status_code == 200:
            body: dict[str, Any] = response.json()
            rows: list[dict[str, Any]] = list(body.get("items", []))
            return rows
        error = response.json().get("error", {})
        typer.echo(f"Error: {error.get('message', 'request failed')}", err=True)
        raise typer.Exit(1)
    except httpx.HTTPError:
        pass  # no server: fall through to the local read, which is this command's other mode
    finally:
        client.close()
    from promptcadence.services.egress import decision_view

    with _local(settings) as egress:
        from commissioner import Verdict

        decisions = egress.decisions(
            run_id=trajectory_id, verdict=Verdict(verdict) if verdict else None
        )
        return [decision_view(decision) for decision in decisions][:limit]


def _line(document: dict[str, Any]) -> str:
    """One rendered decision.

    The target's egress class is printed as ``remote``/``local`` rather than inferred from the
    tier's name, and the reason is the policy's own — neither is restated in this application's
    words, because a reason paraphrased at a surface is a reason that can disagree with the record.
    """
    request = document["request"]
    target = request["target"]
    where = "remote" if target["remote"] else "local"
    return (
        f"{document['decided_at']:<26} {document['verdict']:<9} "
        f"{request['data_classification']:<12} -> {target['name']:<18} ({where:<6}) "
        f"{document['reason']}"
    )


@app.command("list")
def list_decisions(
    trajectory_id: Annotated[
        str | None, typer.Option("--trajectory", help="Only this trajectory's decisions.")
    ] = None,
    verdict: Annotated[
        str | None, typer.Option("--verdict", help=f"One of: {', '.join(_VERDICTS)}.")
    ] = None,
    denied_only: Annotated[
        bool, typer.Option("--denied-only", help="Shorthand for --verdict denied.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="How many to show.")] = 50,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """List recorded egress decisions, oldest first. Mode: either.

    Every decision this build made — approvals, denials, and the violations the verification step
    writes after the fact. Each row names the classification that was evaluated, the target it was
    evaluated against, and the policy's reason. ``--json`` prints SetSpec's
    ``governance.egress_decision`` 1.0 documents unchanged.

    ``--denied-only`` is spec §7.2's shipped flag and is a shorthand for ``--verdict denied``.
    ``--verdict`` exists beside it because the vocabulary has **three** members: a violation is
    neither an approval nor a denial — it is written after the fact by the verification step
    (ADR-0054 rule 7) — and a boolean flag cannot ask for one.
    """
    if verdict is not None and verdict not in _VERDICTS:
        typer.echo(
            f"Error: --verdict must be one of {', '.join(_VERDICTS)} (VALIDATION_ERROR)", err=True
        )
        raise typer.Exit(2)
    if denied_only and verdict not in (None, "denied"):
        # Refused rather than resolved by precedence: whichever one silently won, the operator
        # would be reading a list filtered by the flag they did not intend.
        typer.echo(
            f"Error: --denied-only contradicts --verdict {verdict} (VALIDATION_ERROR)", err=True
        )
        raise typer.Exit(2)
    if denied_only:
        verdict = "denied"
    settings = _settings(config)
    documents = _documents(settings, trajectory_id=trajectory_id, verdict=verdict, limit=limit)
    if json_output:
        typer.echo(json_module.dumps(documents, indent=2, sort_keys=True))
        return
    if not documents:
        typer.echo("No egress decisions recorded.")
        return
    for document in documents:
        typer.echo(_line(document))
    typer.echo(f"\n{len(documents)} decision(s).")
