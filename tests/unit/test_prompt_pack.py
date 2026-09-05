"""The prompt pack: ADR-0012 and ADR-0028 — records, never string literals.

Two tests keep it true as the pack grows: one walks the source for inline prompt strings, and one
rebuilds the manifest and asserts nothing drifted.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from setspec.prompts import PromptNotFound, PromptVariableError, build_manifest

from promptcadence.services.prompts import (
    PACK_ROOT,
    PLANNER_CORRECTIVE_PROMPT_ID,
    PLANNER_DRAFT_PROMPT_ID,
    STEP_EXECUTE_PROMPT_ID,
    library,
    render,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "promptcadence"


def test_the_pack_parses_and_names_itself() -> None:
    pack = library()
    assert pack.pack_id == "promptcadence.prompts"
    assert set(pack.ids()) == {
        PLANNER_DRAFT_PROMPT_ID,
        PLANNER_CORRECTIVE_PROMPT_ID,
        STEP_EXECUTE_PROMPT_ID,
    }


def test_the_manifest_is_current() -> None:
    """A record edited without regenerating the manifest silently changes what a model was asked."""
    _, drift = build_manifest(PACK_ROOT, generated_at="2026-09-04T00:00:00Z")
    assert drift.added == ()
    assert drift.removed == ()
    assert drift.changed == ()


def test_every_record_declares_every_variable_its_template_uses() -> None:
    for record in library().all_records():
        declared = set(record.variables)
        used = {
            token.split("|")[0].strip()
            for token in record.template.split("{{")[1:]
            for token in [token.split("}}")[0]]
        }
        assert used <= declared, f"{record.prompt_id} uses undeclared: {used - declared}"


def test_every_record_states_a_change_reason_and_its_owner() -> None:
    for path in sorted(PACK_ROOT.rglob("*.json")):
        if path.name == "manifest.json":
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["metadata"]["change_reason"].strip(), path.name
        assert record["metadata"]["owner"] == "promptcadence"


def test_the_planner_draft_shows_the_schema_and_carries_provenance() -> None:
    rendered = render(
        PLANNER_DRAFT_PROMPT_ID,
        {
            "task": "summarize ./notes",
            "classification": "confidential",
            "tools": "- read_file: read a file",
            "tiers": "- local_fast: local, admits data up to 'confidential'",
            "max_steps": 20,
            "schema": '{"$id": "plan"}',
        },
    )
    assert rendered.prompt_id == PLANNER_DRAFT_PROMPT_ID
    assert rendered.version == "1.0.0"
    assert rendered.sha256.startswith("sha256:")
    assert rendered.system and "Return only the JSON document" in rendered.system
    assert "summarize ./notes" in rendered.user
    assert '{"$id": "plan"}' in rendered.user
    assert "local_fast" in rendered.user


def test_the_corrective_names_every_issue_at_once() -> None:
    rendered = render(
        PLANNER_CORRECTIVE_PROMPT_ID,
        {"issue_count": 2, "issues": "- step s1 has no 'tier' field\n- step s2 depends on itself"},
    )
    assert rendered.system is None
    assert "2 issue(s)" in rendered.user
    assert "step s2 depends on itself" in rendered.user


def test_the_step_framing_renders_dependencies_and_tools() -> None:
    rendered = render(
        STEP_EXECUTE_PROMPT_ID,
        {
            "step_id": "s2",
            "description": "write the summary",
            "dependency_results": "\nRESULTS OF EARLIER STEPS\n- s1: three meetings\n",
            "tools": "read_file, write_file",
        },
    )
    assert "step s2" in rendered.user
    assert "three meetings" in rendered.user
    assert "read_file, write_file" in rendered.user


def test_a_missing_required_variable_is_refused() -> None:
    with pytest.raises(PromptVariableError):
        render(PLANNER_CORRECTIVE_PROMPT_ID, {"issue_count": 1})


def test_an_unknown_prompt_is_refused() -> None:
    with pytest.raises(PromptNotFound):
        render("planner.no_such_prompt", {})


def test_no_inline_prompt_strings_in_python() -> None:
    """ADR-0012. A long imperative string literal outside the pack is a prompt in hiding.

    The heuristic: a string constant of 200+ characters that addresses a model in the second
    person. Docstrings are skipped (they address the reader). Anything this catches belongs in a
    record.
    """
    offenders: list[str] = []
    markers = ("you are ", "you must ", "return only", "respond with", "your task")
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and len(node.value) >= 200
                and any(marker in node.value.lower() for marker in markers)
            ):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == [], f"prompt text outside the pack: {offenders}"
