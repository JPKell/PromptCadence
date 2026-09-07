"""tests/unit/test_readme_compatibility.py — the README's ``## Compatibility`` table cannot drift.

Packaging standards §7 requires every application to declare, in its README, the tested version
range of every suite package it depends on. This test parses `pyproject.toml`'s own dependency
specifiers — the single source of truth for a range — and asserts the README's table names exactly
the same suite packages at exactly the same ranges, so an edited pin with no README update fails
the suite rather than going unnoticed until an operator installs the wrong version.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SUITE_PACKAGES = frozenset(
    {
        "baseaicore",
        "setspec",
        "modelrack",
        "sweatmeter",
        "weightsdb",
        "mirrorwall",
        "loadledger",
        "cutctx",
        "toolyard",
        "commissioner",
    }
)
"""Every package this suite's own components may declare — never an application."""

_SPEC_RE = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9,_-]+\])?\s*(.*)$")


def _requirements(specs: list[str]) -> dict[str, str]:
    """Map each suite package named in ``specs`` to its version range, extras stripped."""
    found: dict[str, str] = {}
    for spec in specs:
        match = _SPEC_RE.match(spec.strip())
        if match is None:  # pragma: no cover — every declared specifier matches
            continue
        name = match.group(1).lower()
        if name in SUITE_PACKAGES:
            found[name] = match.group(2).strip()
    return found


def _declared_ranges() -> dict[str, str]:
    """Every suite package `pyproject.toml` declares, in `dependencies` or any optional extra."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    project = data["project"]
    ranges = _requirements(project.get("dependencies", []))
    for extra_specs in project.get("optional-dependencies", {}).values():
        ranges.update(_requirements(extra_specs))
    return ranges


def _table_ranges() -> dict[str, str]:
    """Every suite package named in the README's ``## Compatibility`` table, with its range."""
    text = (REPO_ROOT / "README.md").read_text()
    match = re.search(r"^## Compatibility\n(.*?)(?:\n## |\Z)", text, re.S | re.M)
    assert match is not None, "README.md has no '## Compatibility' section"
    rows: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = cells[0].strip("`").lower()
        if name in ("package", "") or set(cells[0]) <= {"-", ":"}:
            continue
        if name in SUITE_PACKAGES:
            rows[name] = cells[1].strip("`")
    return rows


def test_readme_compatibility_matches_pyproject() -> None:
    """The README's ``## Compatibility`` table names exactly `pyproject.toml`'s suite ranges."""
    declared = _declared_ranges()
    table = _table_ranges()
    assert table == declared
