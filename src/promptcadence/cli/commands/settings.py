"""promptcadence.cli.commands.settings — ``settings list``, ``settings get``, ``settings set``.

Spec §7.2's spelling of the runtime-changeable surface ADR-0100 opened, so a setting can be
changed from a script or over ssh without hand-writing ``curl``.

These are **client** commands (CLI standards §6), the way ``approve`` and ``deny`` are: they talk
to a running server over HTTP so the scopes the API enforces are the scopes the CLI enforces —
``read`` to look, ``admin`` to change — and a refusal reaches the operator as the code and message
the API gave. The read-side question a *stopped* install can answer is already answered by
``promptcadence config show``, which opens the database directly and marks what the settings table
decides ``(database)``; this verb is deliberately the other kind and does not fall back to it.

The registry is never restated here. ``list`` renders whatever the document carries, ``get``
refuses a key the document does not define and lists the ones it does, and ``set`` sends the value
for the server to validate against :data:`~promptcadence.services.settings.RUNTIME_SETTINGS` — so
a key added there needs no change in this module.

Only ``typer`` and ``json`` load at module level (CLI standards §12).
"""

from __future__ import annotations

import json as json_module
from typing import TYPE_CHECKING, Annotated, Any

import typer

from promptcadence.cli._backend import load_settings_or_exit
from promptcadence.cli.commands.trajectories import TOKEN_ENV

if TYPE_CHECKING:
    import httpx

    from promptcadence.config import Settings

__all__ = ["app"]

app = typer.Typer(help="Runtime-changeable settings on a running server.")

_CONFIG_OPTION = typer.Option("--config", help="Path to a config.toml file.")
_JSON_OPTION = typer.Option("--json", help="Print JSON.")


def _request(
    settings: Settings,
    *,
    method: str,
    body: dict[str, Any] | None,
    token: str | None,
    json_output: bool,
) -> dict[str, Any]:
    """Call ``/api/v1/settings`` and return the document, or exit the way its refusal says.

    Args:
        settings: The loaded configuration, for the base URL.
        method: ``"GET"`` or ``"PUT"``.
        body: The changes, for ``PUT``.
        token: ``--token``, else ``$PROMPTCADENCE_API_TOKEN``.
        json_output: Whether a refusal prints the error envelope as JSON.

    Returns:
        The ``GET``/``PUT /settings`` document.

    Raises:
        typer.Exit: The server refused (its code chooses the exit status: ``VALIDATION_ERROR``
            is 2, everything else 1) or is not reachable (4). Never a traceback.
    """
    from promptcadence.cli.commands.trajectories import (
        _envelope_error,
        _fail_unreachable,
        auth_headers,
        http_client_factory,
    )

    client = http_client_factory(settings)
    try:
        try:
            response = client.request(
                method, "/api/v1/settings", json=body, headers=auth_headers(token)
            )
        except Exception as exc:  # noqa: BLE001 — every transport failure is "unreachable"
            raise _fail_unreachable(settings, exc) from exc
        if response.status_code != 200:
            if not json_output:
                _echo_changeable_set(response)
            raise _envelope_error(response, json_output=json_output)
        document: dict[str, Any] = response.json()
        return document
    finally:
        client.close()


def _echo_changeable_set(response: httpx.Response) -> None:
    """Print the changeable set the API listed with a ``VALIDATION_ERROR``, when it listed one.

    The envelope's message names the key it refused; ``details.runtime_changeable`` is the set,
    and printing it is how the operator learns the vocabulary without this module holding a copy
    of the registry that could fall behind it.
    """
    try:
        details = response.json()["error"]["details"]
    except (ValueError, KeyError, TypeError):
        return
    changeable = details.get("runtime_changeable") if isinstance(details, dict) else None
    if isinstance(changeable, list):
        typer.echo(f"changeable: {', '.join(str(key) for key in changeable)}", err=True)


def _source(definition: dict[str, Any]) -> str:
    """How one key's effective value is decided, in the console's vocabulary."""
    shadowed_by = definition["shadowed_by"]
    if shadowed_by is not None:
        return f"{shadowed_by} (stored {definition['stored']} shadowed)"
    return str(definition["source"])


def _defined(document: dict[str, Any], key: str) -> dict[str, Any]:
    """The definition of ``key``, or exit 2 the way the API refuses an unknown key.

    Raises:
        typer.Exit: ``key`` is not runtime-changeable. The changeable set is listed from the
            document, never from a copy of the registry kept here.
    """
    definitions: dict[str, Any] = document["definitions"]
    if key not in definitions:
        typer.echo(
            f"Error: {key} is not runtime-changeable "
            f"(VALIDATION_ERROR); changeable: {', '.join(sorted(definitions))}",
            err=True,
        )
        raise typer.Exit(2)
    definition: dict[str, Any] = definitions[key]
    return definition


@app.command("list")
def list_settings(
    json_output: Annotated[bool, _JSON_OPTION] = False,
    config: Annotated[str | None, _CONFIG_OPTION] = None,
) -> None:
    """Every runtime-changeable key, its effective value and which layer decided it.

    Mode: client; needs the ``read`` scope.

    Example:
        promptcadence settings list
    """
    document = _request(
        load_settings_or_exit(config), method="GET", body=None, token=None, json_output=json_output
    )
    if json_output:
        typer.echo(json_module.dumps(document, sort_keys=True))
        return
    for key in sorted(document["definitions"]):
        value = document["settings"][key]
        typer.echo(f"{key:<40} {value!s:<12} ({_source(document['definitions'][key])})")


@app.command("get")
def get_setting(
    key: Annotated[
        str, typer.Argument(help="A runtime-changeable key, e.g. compaction.threshold.")
    ],
    json_output: Annotated[bool, _JSON_OPTION] = False,
    config: Annotated[str | None, _CONFIG_OPTION] = None,
) -> None:
    """One key's effective value, and what decided it. Mode: client; ``read`` scope.

    Exits 2 naming the changeable set when ``key`` is not one of them.

    Example:
        promptcadence settings get execution.step_retries
    """
    document = _request(
        load_settings_or_exit(config), method="GET", body=None, token=None, json_output=json_output
    )
    definition = _defined(document, key)
    if json_output:
        typer.echo(
            json_module.dumps({key: document["settings"][key], **definition}, sort_keys=True)
        )
        return
    typer.echo(f"{key} = {document['settings'][key]} ({_source(definition)})")


@app.command("set")
def set_setting(
    key: Annotated[str, typer.Argument(help="The key to change.")],
    value: Annotated[str, typer.Argument(help="The new value; JSON, so 3, 0.8, true.")],
    token: Annotated[
        str | None, typer.Option("--token", help=f"Bearer token; else ${TOKEN_ENV}.")
    ] = None,
    json_output: Annotated[bool, _JSON_OPTION] = False,
    config: Annotated[str | None, _CONFIG_OPTION] = None,
) -> None:
    """Store one runtime-changeable key. Mode: client; needs the ``admin`` scope.

    The value is read as JSON (``3``, ``0.8``, ``true``) and sent for the server to validate
    against its registry, so the refusals are the API's: ``FORBIDDEN`` names a security-relevant
    key (exit 1), ``VALIDATION_ERROR`` names an unknown key and lists what may change (exit 2).

    What is printed afterwards is the key's **effective** value, not what was sent: a stored row
    the environment shadows does nothing (configuration standards §7), and this says so rather
    than reading as a success.

    Example:
        promptcadence settings set execution.step_retries 3
    """
    settings = load_settings_or_exit(config)
    try:
        parsed: Any = json_module.loads(value)
    except ValueError:
        parsed = value  # not JSON: let the server refuse it by name and type
    document = _request(
        settings, method="PUT", body={key: parsed}, token=token, json_output=json_output
    )
    definition = _defined(document, key)
    effective = document["settings"][key]
    if json_output:
        typer.echo(json_module.dumps({key: effective, **definition}, sort_keys=True))
        return
    typer.echo(f"{key} = {effective} ({_source(definition)})")
    if definition["shadowed_by"] is not None:
        typer.echo(
            f"stored {definition['stored']}, but {definition['shadowed_by']} beats it: the row "
            "does nothing until that variable is unset."
        )
