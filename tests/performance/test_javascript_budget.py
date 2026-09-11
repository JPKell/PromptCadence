"""ADR-0139, as this application inherits it at row WM2: a page loads at most 120 KB of
JavaScript in total, ECharts and mermaid aside. Marked ``performance`` like every budget."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from tests.fakes.loadcoach_app import FakeLoadCoach, build_fake_app

from promptcadence.config import load_settings
from promptcadence.services.runtime import build_runtime
from promptcadence.web.app import create_app

pytestmark = pytest.mark.performance

_SCRIPT_SRC = re.compile(r'<script[^>]+src="([^"]+)"')
_INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def test_every_page_stays_under_the_total_budget() -> None:
    loadcoach_http = TestClient(build_fake_app(FakeLoadCoach()), base_url="http://loadcoach.fake")
    app = create_app(
        load_settings().settings,
        runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http),
    )
    sizes: dict[str, int] = {}
    totals: dict[str, int] = {}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        trajectory_id = client.post(
            "/api/v1/trajectories", json={"task": "t", "bypass_planning": True}
        ).json()["trajectory_id"]
        for path in (
            "/",
            "/trajectories",
            f"/trajectories/{trajectory_id}",
            "/approvals",
            "/tiers",
            "/tools",
            "/ledger",
            "/system",
            "/settings",
        ):
            response = client.get(path)
            if response.status_code != 200:
                continue
            html = response.text
            total = sum(len(script) for script in _INLINE.findall(html))
            for src in _SCRIPT_SRC.findall(html):
                if "vendor/echarts" in src or "vendor/mermaid" in src:
                    continue
                name = src.split("?")[0]
                if name not in sizes:
                    asset = client.get(src)
                    assert asset.status_code == 200, (path, src)
                    sizes[name] = len(asset.content)
                total += sizes[name]
            totals[path] = total
    assert max(totals.values()) <= 120 * 1024, totals
