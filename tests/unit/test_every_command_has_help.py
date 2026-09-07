"""tests/unit/test_every_command_has_help.py — every CLI command and option has help text (M9
audit Group 5, item D4).

Walks `typer.main.get_command(app)` recursively: every command (and sub-command, at any depth)
must have a help string, and every one of its options must too — so a command shipped with no
docstring, or an option with no `help=`, fails the suite instead of reaching a person's `--help`
silently blank.

Command/option grouping below is duck-typed (``getattr(node, "commands", None)``) rather than
checked with ``isinstance(node, click.Group)``: Typer vendors its own command classes under
``typer._click.core``, and — at least as installed here — they do not subclass the separately
installed ``click`` package's ``Group``/``Command``, so an ``isinstance`` check against ``click``
silently walks nothing and this test would pass by finding no commands at all.
"""

from __future__ import annotations

from typing import Any

from typer.main import get_command

from promptcadence.cli.main import app


def _problems(command: Any, path: str) -> list[str]:
    """Every command or option under ``command`` that has no help text."""
    found: list[str] = []
    commands = getattr(command, "commands", None)
    if commands is not None:
        for name, sub in commands.items():
            found.extend(_problems(sub, f"{path} {name}".strip()))
        return found
    if not (command.help or command.short_help):
        found.append(f"{path}: command has no help text")
    for param in command.params:
        if getattr(param, "hidden", False):
            continue
        if not getattr(param, "help", None):
            flag = param.opts[0] if getattr(param, "opts", None) else param.name
            found.append(f"{path} {flag}: option has no help text")
    return found


def test_every_command_and_option_has_help_text() -> None:
    problems = _problems(get_command(app), "")
    assert problems == [], "\n".join(problems)
