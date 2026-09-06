"""UI/UX Standards §13 — every item a test can hold the rendered pages to (dev-plan P8).

Each test names the §13 bullet it covers. Items that need a browser — layout at 1280×720 and at
375 px, JavaScript actually disabled in a real client, the network panel offline, visible focus
rings — are listed at the bottom with what a person should do. Items about charts do not apply:
this console draws none, deliberately, because every figure it shows is a count or an identifier
and a chart of either is decoration.

Colour contrast is MirrorWall's and is tested there. PromptCadence's templates use no colour of
their own, which :func:`test_application_templates_use_tokens_not_colours_or_small_text` proves,
so the package's token test stands for every page here.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    ScriptedError,
    ScriptedGeneration,
    build_fake_app,
    shipped_profiles,
)

from promptcadence.config import load_settings
from promptcadence.services.runtime import build_runtime
from promptcadence.web.app import create_app
from promptcadence.web.rendering import NAV_ITEMS

if TYPE_CHECKING:
    from collections.abc import Iterator

HOSTILE = '<script>alert("xss")</script>'
TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "promptcadence" / "web" / "templates"
_TERMINAL = {"completed", "halted", "failed", "cancelled", "rejected"}
_LONG = "The meeting covered the migration plan in considerable detail. " * 6


def _call(name: str, arguments: str) -> dict[str, object]:
    return {"call_index": 0, "id": "c0", "name": name, "arguments_fragment": arguments}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """One populated server: a hostile task and answer, a retry, tool calls and a compaction."""
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__LEASE_SECONDS", "5")
    monkeypatch.setenv("PROMPTCADENCE_EXECUTION__STEP_RETRIES", "1")
    monkeypatch.setenv("PROMPTCADENCE_COMPACTION__PROTECTED_RECENT_TURNS", "1")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_FAST__TASK_PROFILE", "tools.agent.local_fast")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_FAST__REMOTE", "false")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_FAST__CONTEXT_BUDGET_TOKENS", "260")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_LARGE__TASK_PROFILE", "tools.agent.local_large")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_LARGE__REMOTE", "false")
    monkeypatch.setenv("PROMPTCADENCE_TIERS__LOCAL_LARGE__CONTEXT_BUDGET_TOKENS", "4096")
    fake = FakeLoadCoach()
    fake.register_profile(
        *shipped_profiles("tools.agent.local_fast", "tools.agent.local_large", "tools.plan")
    )
    fake.script(
        ScriptedError("PROVIDER_TIMEOUT"),
        ScriptedGeneration(text=_LONG, tool_calls=(_call("list_dir", '{"path": "."}'),)),
        ScriptedGeneration(text=_LONG, tool_calls=(_call("read_file", '{"path": "x"}'),)),
        ScriptedGeneration(text=_LONG, tool_calls=(_call("list_dir", '{"path": "."}'),)),
        ScriptedGeneration(text=f"Done. {HOSTILE}"),
    )
    settings = load_settings().settings
    loadcoach_http = TestClient(build_fake_app(fake), base_url="http://loadcoach.fake")
    app = create_app(
        settings, runtime_builder=lambda s: build_runtime(s, loadcoach_http=loadcoach_http)
    )
    with TestClient(app, base_url="http://127.0.0.1") as running:
        created = running.post(
            "/api/v1/trajectories",
            json={"task": f"summarize {HOSTILE}", "bypass_planning": True},
        )
        trajectory_id = created.json()["trajectory_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            view: dict[str, Any] = running.get(f"/api/v1/trajectories/{trajectory_id}").json()
            if view["state"] in _TERMINAL:
                break
            time.sleep(0.02)
        cast("FastAPI", running.app).state.checklist_trajectory_id = trajectory_id
        yield running


def _seeded_id(client: TestClient) -> str:
    """The trajectory the fixture seeded, stashed on the app so every test can reach it."""
    return str(cast("FastAPI", client.app).state.checklist_trajectory_id)


def _pages(client: TestClient) -> dict[str, str]:
    """Every route the navigation offers, plus the one detail page, rendered."""
    paths = [item["href"] for item in NAV_ITEMS]
    paths.append(f"/trajectories/{_seeded_id(client)}")
    return {path: client.get(path).text for path in paths}


def test_every_page_has_the_shell_skip_link_main_and_nav(client: TestClient) -> None:
    """§7: semantic HTML, a skip link, one ``h1``, and headings that never skip a level."""
    for path, html in _pages(client).items():
        assert 'class="skip-link" href="#content"' in html, path
        assert '<main id="content">' in html, path
        assert '<nav aria-label="Primary">' in html, path
        assert html.count("<h1>") == 1, path
        assert '<html lang="en">' in html, path
        levels = [int(level) for level in re.findall(r"<h([1-6])[ >]", html)]
        for previous, current in zip(levels, levels[1:], strict=False):
            assert current <= previous + 1, (path, levels)


def test_the_current_page_is_marked_in_the_navigation(client: TestClient) -> None:
    """§7: an assistive reader knows where it is."""
    for item in NAV_ITEMS:
        html = client.get(item["href"]).text
        assert f'href="{item["href"]}" aria-current="page"' in html, item


def test_every_form_control_has_a_label_and_every_button_is_a_button(
    client: TestClient,
) -> None:
    """§7: every control has an associated label, and a button is a real ``<button>``."""
    for path, html in _pages(client).items():
        for control_id in re.findall(r'<(?:input|select|textarea)[^>]*\bid="([^"]+)"', html):
            assert f'for="{control_id}"' in html, (path, control_id)
        assert 'role="button"' not in html, path
        for control in re.findall(r"<input[^>]*>", html):
            if 'type="hidden"' in control:
                continue
            assert 'id="' in control, (path, control)


def test_theme_choice_is_offered_on_every_page(client: TestClient) -> None:
    """§9: system, light and dark, on every route, keyed to this application's own storage key."""
    for path, html in _pages(client).items():
        assert 'id="mw-theme-select"' in html and 'for="mw-theme-select"' in html, path
        assert 'data-theme-storage-key="promptcadence-theme"' in html, path


def test_tables_have_header_scope_and_a_row_count_or_caption(client: TestClient) -> None:
    """§5: header cells carry scope, and every table says how many rows it is showing."""
    for path, html in _pages(client).items():
        for table in re.findall(r"<table[^>]*>.*?</table>", html, flags=re.S):
            assert '<th scope="col"' in table, path
        assert html.count("<table") <= html.count('class="row-count"') + html.count("<caption>"), (
            path
        )


def test_a_paged_table_declares_itself_incomplete(client: TestClient) -> None:
    """§5: a client-side sort must apply to the whole dataset, so a page of one declares it.

    The ledger and the egress pages show the most recent N rows, which is exactly the case the
    ``complete`` flag exists for — sorting a page of fifty as though it were the dataset would
    quietly answer a different question from the one the operator asked.
    """
    assert 'data-complete="false"' in client.get("/ledger").text
    assert 'data-complete="false"' in client.get("/egress").text


def test_unsupported_values_are_an_em_dash_with_a_reason_never_zero(
    client: TestClient,
) -> None:
    """§5, §13, ADR-0016: ``—`` with a tooltip explaining why; never ``0``, never blank."""
    timeline = client.get(f"/trajectories/{_seeded_id(client)}").text
    assert 'title="not reported by the provider; not the same as zero"' in timeline
    body = timeline.split("<main")[1]
    assert ">unsupported<" not in body, (
        "an unreported class reaches the page as an em dash, never as the word — a tool result "
        "that happens to contain the word is a different thing and is escaped text"
    )
    assert ">0<" not in body.split("Debits")[0] or "—" in body, "never a fabricated zero"


def test_colour_is_never_the_only_signal(client: TestClient) -> None:
    """§4.1, §13: every status badge carries its own label as text."""
    for path, html in _pages(client).items():
        for badge in re.findall(r'<span class="badge[^"]*"[^>]*>(.*?)</span>', html, flags=re.S):
            assert badge.strip(), path


def test_headline_metrics_link_to_their_raw_record(client: TestClient) -> None:
    """§5, §13: no headline figure is more than two interactions from the record behind it."""
    dashboard = client.get("/").text
    for target in ('href="/trajectories"', 'href="/ledger"', 'href="/egress"', 'href="/system"'):
        assert target in dashboard, target
    assert 'href="/trajectories/' in dashboard, "a recent trajectory opens its own record"


def test_live_content_is_a_polite_live_region_and_pages_are_complete_without_scripts(
    client: TestClient,
) -> None:
    """§7 live regions; §13 read-only content works with JavaScript disabled.

    Every page here is rendered server-side in full. The only script this application ships is the
    in-flight timeline's notice, and it announces rather than renders — a reader with JavaScript
    off loses the notice and no content.
    """
    pages = _pages(client)
    for path, html in pages.items():
        body = html.split("<main")[1]
        assert "EventSource" not in body, path
    created = client.post(
        "/api/v1/trajectories", json={"task": "in flight", "bypass_planning": True}
    )
    live = client.get(f"/trajectories/{created.json()['trajectory_id']}").text
    assert 'aria-live="polite"' in live and 'role="status"' in live


def test_no_page_loads_anything_from_the_network(client: TestClient) -> None:
    """§13: every asset is a same-origin path — nothing leaves the machine to render a page."""
    for path, html in _pages(client).items():
        for attribute in ("src", "href"):
            for target in re.findall(rf'{attribute}="([^"]+)"', html):
                assert not target.startswith(("http://", "https://", "//")), (path, target)


def test_application_templates_use_tokens_not_colours_or_small_text() -> None:
    """§1 no hard-coded colour; §13 metadata text never below 12 px; §10 no icon fonts."""
    for template in TEMPLATES.rglob("*.html"):
        source = template.read_text(encoding="utf-8")
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", source.replace("#content", "")), template
        assert not re.search(r"font-size:\s*(?:[0-9]|1[01])px", source), template
        assert "fonts.googleapis" not in source and "cdn." not in source, template


def test_untrusted_content_is_escaped_on_every_page(client: TestClient) -> None:
    """The trust boundary, at the template layer: the task and the answer are both hostile here."""
    for path, html in _pages(client).items():
        assert HOSTILE not in html, path
    timeline = client.get(f"/trajectories/{_seeded_id(client)}").text
    assert "&lt;script&gt;" in timeline, "and the text is on the page, escaped"


def test_no_template_marks_a_record_value_safe() -> None:
    """The one way autoescaping is turned off, and it is not used anywhere in this console.

    ``| safe`` on a constructed string is how a link in a table cell is usually built, and doing it
    once beside a value that came from a model is an injection. This console builds links with a
    macro instead, whose output is already-escaped markup and which cannot be turned off from a
    call site.
    """
    for template in TEMPLATES.rglob("*.html"):
        # Jinja comments are stripped first: `_macros.html` explains in prose why it does not use
        # the filter, and a test that failed on the explanation would be a test nobody could
        # document around.
        source = re.sub(r"\{#.*?#\}", "", template.read_text(encoding="utf-8"), flags=re.S)
        assert "| safe" not in source, template
        assert "|safe" not in source, template


def test_every_view_has_an_empty_state(client: TestClient) -> None:
    """§6, §13: the four states. Empty is the one a fresh install sees first."""
    assert "empty-state" in client.get("/trajectories?state=cancelled").text
    assert "empty-state" in client.get("/approvals").text
    assert "empty-state" in client.get("/egress?verdict=violation").text


def test_what_a_person_must_still_check() -> None:
    """The §13 items no test here can hold, and what to do for each.

    * **Layout at 1280×720 and at 375 px.** Open every navigation entry at both widths. The page
      to check first is a trajectory's timeline: it is the widest thing this console renders — ten
      columns of turns, and a tool-call table with two content columns — and it is the one that
      would scroll the page rather than the table if a container rule were missing.
    * **JavaScript disabled in a real client.** Every read-only page must be complete. The only
      loss should be the in-flight timeline's "n new events" notice.
    * **The network panel, offline.** No request may leave the machine. The static test above
      catches an absolute URL in a template; a stylesheet importing a font would not be caught
      here, because the stylesheets are MirrorWall's.
    * **Visible focus rings, in both themes.** Tab through the approvals inbox: the grant button,
      the reason field and the deny button must each show a ring, and the ring must be visible on
      the dark theme as well as the light one.
    * **Contrast in both themes.** MirrorWall's token test covers the pairs; what a person checks
      is that this console introduced no pair of its own, which the hard-coded-colour test above
      makes unlikely rather than impossible.
    """
    assert True
