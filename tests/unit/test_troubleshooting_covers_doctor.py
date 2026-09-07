"""``promptcadence doctor`` diagnoses every component the troubleshooting guide lists (M9 audit
Group 5, item D6 — ported from FreeWeight's `test_troubleshooting_covers_doctor.py`).

The guide's "`doctor`'s four components" table *is* the health components, and this test holds
the two in lockstep: every component `doctor` reports has a row in
`docs/troubleshooting.md`, and every row names a real component. Unlike FreeWeight's guide, which
gives each component its own `##` heading, this one lists them in a table under one heading — so
the row names are read from that table rather than from `##` headings.
"""

from __future__ import annotations

import re
from pathlib import Path

from promptcadence.services.diagnostics import health_report

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "troubleshooting.md"


def _guide_component_names() -> set[str]:
    """The backtick-quoted names in the first column of the "`doctor`'s four components" table."""
    text = GUIDE.read_text(encoding="utf-8")
    section = text.split("## `doctor`'s four components")[1].split("\n## ")[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", section, re.MULTILINE))


def test_every_reported_component_has_a_troubleshooting_row() -> None:
    report = health_report()
    reported = {component["name"] for component in report["components"]}
    documented = _guide_component_names()

    missing = reported - documented
    assert missing == set(), (
        f"components the doctor reports but the guide does not document: {missing}"
    )


def test_every_documented_component_is_a_real_component() -> None:
    report = health_report()
    reported = {component["name"] for component in report["components"]}
    documented = _guide_component_names()

    stale = documented - reported
    assert stale == set(), f"the guide documents components that no longer exist: {stale}"
