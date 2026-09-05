"""promptcadence.cli.commands.tiers — ``tiers list|show|check`` (spec §7.2).

``list`` and ``show`` read configuration (mode: local). ``check`` asks the running LoadCoach
whether each configured tier's task profile exists — and ``tools.plan``, which no tier names and
every planned trajectory calls — through the same function ``doctor`` and ``GET /health`` use, so
the three cannot disagree. Exit 4 when a profile is missing or LoadCoach is unreachable.

Only ``typer`` and ``json`` load at module level (CLI standards §12).
"""

from __future__ import annotations

import json as json_module
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from promptcadence.config import Settings

__all__ = ["app"]

app = typer.Typer(help="Configured tiers, and whether LoadCoach can serve them.")


def _settings(config: str | None) -> Settings:
    from promptcadence.config import ConfigurationError, load_settings

    try:
        return load_settings(config_path=config).settings
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc


def _tier_json(name: str, settings: Settings) -> dict[str, Any]:
    tier = settings.tiers[name]
    return {
        "name": name,
        "task_profile": tier.task_profile,
        "remote": tier.remote,
        "max_data_classification": (
            tier.max_data_classification.value if tier.max_data_classification else None
        ),
        "context_budget_tokens": tier.context_budget_tokens,
        "pricing_file": tier.pricing_file or None,
        "default_step_input_tokens": tier.default_step_input_tokens,
        "default_step_output_tokens": tier.default_step_output_tokens,
        "default": name == settings.policy.default_tier,
        "escalation_position": (
            settings.policy.escalation_order.index(name)
            if name in settings.policy.escalation_order
            else None
        ),
    }


@app.command("list")
def list_tiers(
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """List the configured tiers. Mode: local."""
    settings = _settings(config)
    items = [_tier_json(name, settings) for name in sorted(settings.tiers)]
    if json_output:
        typer.echo(json_module.dumps({"tiers": items}, sort_keys=True))
        return
    for item in items:
        surface = "remote" if item["remote"] else "local "
        marker = "*" if item["default"] else " "
        ceiling = item["max_data_classification"] or "confidential (local)"
        typer.echo(
            f"{marker} {item['name']:<16} {surface}  {item['task_profile']:<30} "
            f"ceiling={ceiling:<20} context={item['context_budget_tokens']}"
        )
    typer.echo(f"\nescalation order: {', '.join(settings.policy.escalation_order)}  (* default)")


@app.command("show")
def show_tier(
    name: Annotated[str, typer.Argument(help="The tier name.")],
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Show one configured tier. Mode: local."""
    settings = _settings(config)
    if name not in settings.tiers:
        typer.echo(
            f"Error: no tier named {name!r} is configured; configured: "
            f"{', '.join(sorted(settings.tiers))} (TIER_NOT_CONFIGURED)",
            err=True,
        )
        raise typer.Exit(2)
    item = _tier_json(name, settings)
    if json_output:
        typer.echo(json_module.dumps(item, sort_keys=True))
        return
    for key, value in item.items():
        typer.echo(f"{key:<28} {value}")


@app.command("check")
def check(
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Verify every tier's task profile, and tools.plan, exist in the running LoadCoach.

    Mode: local (it reads configuration and asks LoadCoach directly). Exit 0 when every profile
    resolves and is enabled, 4 when one is missing or LoadCoach is unreachable.
    """
    from promptcadence.infrastructure.loadcoach import LoadCoachClient
    from promptcadence.services.tiers import check_tiers

    settings = _settings(config)
    client = LoadCoachClient.from_settings(
        base_url=settings.loadcoach.base_url,
        timeout_seconds=min(settings.loadcoach.timeout_seconds, 10.0),
        api_key_env=settings.loadcoach.api_key_env,
        api_key_file=settings.loadcoach.api_key_file,
    )
    try:
        result = check_tiers(settings, client)
    finally:
        client.close()
    if json_output:
        typer.echo(json_module.dumps(result.as_json(), sort_keys=True))
    else:
        typer.echo(f"loadcoach {settings.loadcoach.base_url}: {result.detail}")
        for item in result.checks:
            symbol = "✓" if item.found and item.enabled else "✗"
            owner = item.tier if item.tier is not None else "(planner)"
            typer.echo(f"  {symbol} {owner:<16} {item.task_profile:<30} {item.detail}")
    if not result.ok:
        raise typer.Exit(4)
