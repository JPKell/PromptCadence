"""promptcadence.cli.commands.tools — ``tools list`` and ``tools show``.

Mode: **local** (CLI standards §6). The registry is a function of ``[tools]`` and of this host —
which isolation rung exists here decides whether ``run_command`` can run at all — so both commands
answer from configuration and a probe of the machine they are run on, with no server needed. That
is deliberately the *useful* answer: an operator asking "why was my command refused" is usually
asking about the host, and a question routed through a server would answer about the server's host
instead.

Both commands report withheld tools beside registered ones. A list of only what works cannot
distinguish a tool nobody enabled from one that was enabled and held back, and before Phase 6
``http_fetch`` is exactly the second.

Only ``typer`` and ``json`` load at module level; the service layer is imported inside the command
bodies (CLI standards §12).
"""

from __future__ import annotations

import json as json_module
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from promptcadence.services.tools import ToolPlant

__all__ = ["app"]

app = typer.Typer(help="Tools: list the registry and show one tool.")


def _plant() -> ToolPlant:
    """Build the plant from configuration, or exit 3 with the configuration error."""
    from promptcadence.config import ConfigurationError, load_settings
    from promptcadence.services.tools import ToolPlant

    try:
        settings = load_settings().settings
        return ToolPlant(settings)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc


def _isolation(plant: ToolPlant) -> dict[str, Any]:
    """The probe's answer, as the payload both commands print."""
    report = plant.isolation()
    return {
        "tier": report.tier.value,
        "runtime": report.runtime,
        "reason": report.reason,
        "limits_unenforced": list(report.limits_unenforced),
    }


@app.command("list")
def list_tools(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
) -> None:
    """List every tool ``[tools] enabled`` names, registered or withheld. Mode: local."""
    plant = _plant()
    entries = plant.catalog()
    if json_output:
        payload = {
            "tools": [entry.as_payload() for entry in entries],
            "isolation": _isolation(plant),
        }
        typer.echo(json_module.dumps(payload, indent=2, sort_keys=True))
        return
    for entry in entries:
        if entry.registered:
            flags = [entry.risk_class or "", entry.egress or ""]
            if entry.requires_isolation:
                flags.append("isolated")
            if entry.redact_args:
                flags.append("args redacted")
            typer.echo(f"  ✓ {entry.name}  [{', '.join(f for f in flags if f)}]")
        else:
            typer.echo(f"  · {entry.name}  withheld: {entry.withheld_cause}")
    report = _isolation(plant)
    typer.echo(f"\nisolation: {report['tier']} ({report['runtime'] or 'none'})")
    typer.echo(f"  {report['reason']}")
    if report["limits_unenforced"]:
        typer.echo(f"  limits this host cannot apply: {', '.join(report['limits_unenforced'])}")


@app.command("show")
def show_tool(
    name: Annotated[str, typer.Argument(help="The tool name, matched exactly.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
) -> None:
    """Show one tool: what the model is told, what it may do, and its argument schema.

    Exits 5 when configuration names no such tool — the registry's lookup is by exact name, so a
    near miss is a miss.
    """
    plant = _plant()
    entry = plant.entry(name)
    if entry is None:
        typer.echo(f"Error: no tool named {name!r} is configured (TOOL_NOT_FOUND)", err=True)
        raise typer.Exit(5)
    if json_output:
        typer.echo(json_module.dumps(entry.as_payload(), indent=2, sort_keys=True))
        return
    typer.echo(f"{entry.name}")
    typer.echo(f"  registered: {entry.registered}")
    if not entry.registered:
        typer.echo(f"  withheld:   {entry.withheld_cause}")
    else:
        typer.echo(f"  risk:       {entry.risk_class}")
        typer.echo(f"  egress:     {entry.egress}")
        typer.echo(f"  isolated:   {entry.requires_isolation}")
        typer.echo(f"  redact args:{entry.redact_args}")
    typer.echo(f"  description: {entry.description}")
    if entry.parameters is not None:
        typer.echo("  parameters:")
        typer.echo(json_module.dumps(dict(entry.parameters), indent=4, sort_keys=True))
