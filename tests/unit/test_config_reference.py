"""``docs/configuration.md`` is generated from the settings model and cannot drift (config §8)."""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from promptcadence.cli.main import app
from promptcadence.config import Settings
from promptcadence.services.config_reference import render_configuration_reference
from promptcadence.services.settings import RUNTIME_SETTINGS

REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "configuration.md"


def test_the_committed_reference_matches_the_model() -> None:
    assert REFERENCE.is_file(), (
        "docs/configuration.md is missing; run `promptcadence config reference`"
    )
    assert REFERENCE.read_text(encoding="utf-8") == render_configuration_reference(), (
        "docs/configuration.md drifted from the settings model: "
        "run `promptcadence config reference --output docs/configuration.md`"
    )


def test_every_field_appears_with_its_columns() -> None:
    rendered = render_configuration_reference()
    lines = rendered.splitlines()
    for section_name, section_field in Settings.model_fields.items():
        model = section_field.annotation
        if section_name == "tiers":
            assert "## `[tiers.<name>]`" in rendered
            line = next(one for one in lines if one.startswith("| `tiers.<name>.remote` |"))
            assert "`PROMPTCADENCE_TIERS__<NAME>__REMOTE`" in line
            continue
        assert f"## `[{section_name}]`" in rendered
        for field_name in model.model_fields:  # type: ignore[union-attr]  # every other section is a model
            key = f"{section_name}.{field_name}"
            line = next(one for one in lines if one.startswith(f"| `{key}` |"))
            assert f"`PROMPTCADENCE_{section_name.upper()}__{field_name.upper()}`" in line
            cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
            assert len(cells) == 9, line
            expected = "yes" if key in RUNTIME_SETTINGS else "no"
            assert cells[5] == expected, f"{key}: the column is the registry's, not a literal"
    assert "## `[budget.projects.<name>]`" in rendered
    assert "| `budget.projects.<name>.money_ceiling` |" in rendered
    assert sum(1 for line in lines if line.startswith("| `") and "| yes |" in line) == len(
        RUNTIME_SETTINGS
    ), "exactly the registry's keys are documented as runtime-changeable"


def test_the_security_relevant_keys_carry_a_note() -> None:
    rendered = render_configuration_reference()
    for key in ("server.host", "storage.retain_content", "tools.fetch_allowed_hosts"):
        line = next(one for one in rendered.splitlines() if one.startswith(f"| `{key}` |"))
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
        assert cells[6] != "—", key


def test_the_check_command_reports_drift(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "configuration.md"
    written = runner.invoke(app, ["config", "reference", "--output", str(target)])
    assert written.exit_code == 0 and target.read_text() == render_configuration_reference()
    assert (
        runner.invoke(app, ["config", "reference", "--check", "--output", str(target)]).exit_code
        == 0
    )
    target.write_text("stale")
    drifted = runner.invoke(app, ["config", "reference", "--check", "--output", str(target)])
    assert drifted.exit_code == 1 and "differs" in drifted.output
