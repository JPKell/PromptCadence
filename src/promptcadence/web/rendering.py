"""promptcadence.web.rendering — the one Jinja environment every page renders through.

MirrorWall's environment, not a local one: the shell, the component macros, the design tokens and
the shared filters all come from the package, and this module supplies only what is
PromptCadence's — the product name, the navigation, and the template directory holding this
application's own pages. Three implementations of one autoescaping rule are three chances to get
it subtly different, and the difference will be in the application nobody audited (ADR-0026 §1).

Built once and cached. Templates are compiled and cached on the environment, so a per-request
environment would recompile the layout on every page view.

``StrictUndefined`` comes from the package and stays: a template variable that does not exist
raises rather than rendering blank. That is ADR-0016's reasoning arriving through the template
layer — a missing figure must not look like an empty one — and it is why the render suite in
``tests/unit/test_console_templates.py`` is worth having: every page is rendered against a report
that exercises its branches, so a mistyped variable is a test failure rather than a blank cell an
operator has to notice.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mirrorwall import create_template_environment

from promptcadence.__about__ import __version__

if TYPE_CHECKING:
    from jinja2 import Environment

__all__ = [
    "NAV_ITEMS",
    "outcome_tone",
    "render",
    "state_tone",
    "templates",
    "verdict_tone",
]

_TEMPLATES_DIR = Path(__file__).parent / "templates"

NAV_ITEMS: tuple[dict[str, str], ...] = (
    {"key": "dashboard", "href": "/", "label": "Dashboard"},
    {"key": "trajectories", "href": "/trajectories", "label": "Trajectories"},
    {"key": "approvals", "href": "/approvals", "label": "Approvals"},
    {"key": "tiers", "href": "/tiers", "label": "Tiers"},
    {"key": "tools", "href": "/tools", "label": "Tools"},
    {"key": "ledger", "href": "/ledger", "label": "Ledger"},
    {"key": "egress", "href": "/egress", "label": "Egress"},
    {"key": "system", "href": "/system", "label": "System"},
    {"key": "settings", "href": "/settings", "label": "Settings"},
)
"""The information architecture UI standards §12 names for this application.

Trajectories first after the dashboard because a trajectory is the unit of everything else here:
approvals, debits and egress decisions all belong to one, and each of those pages links back.
"""


_STATE_TONES: Final[dict[str, str]] = {
    "queued": "neutral",
    "planning": "info",
    "executing": "accent",
    "awaiting_approval": "warning",
    "awaiting_window": "warning",
    "completed": "success",
    "halted": "danger",
    "failed": "danger",
    "cancelled": "neutral",
    "rejected": "danger",
}
"""UI standards §4.1's status vocabulary, for this application's states.

One mapping, used by every page, because a state that is a warning on one page and a danger on the
next is a console that has two opinions about the same fact. Colour is never the only signal: the
badge always carries the state's name as text.
"""

_VERDICT_TONES: Final[dict[str, str]] = {
    "approved": "success",
    "denied": "warning",
    "violation": "danger",
}
"""A denial is a working control and a violation is a failure of one, so they are not the same
colour. Both are recorded, and both are on the same page (spec §11 contract 3)."""


def state_tone(state: str) -> str:
    """The badge tone for a trajectory state, ``neutral`` for anything unrecognised."""
    return _STATE_TONES.get(state, "neutral")


def verdict_tone(verdict: str) -> str:
    """The badge tone for an egress verdict."""
    return _VERDICT_TONES.get(verdict, "neutral")


def outcome_tone(status: str) -> str:
    """The badge tone for a tool call's outcome or a health component's status."""
    if status in {"ok", "completed", "passed", "granted"}:
        return "success"
    if status in {"failed", "error", "unavailable", "denied"}:
        return "danger"
    if status in {"refused", "degraded", "pending", "expired", "not_configured"}:
        return "warning"
    return "neutral"


@lru_cache(maxsize=1)
def templates() -> Environment:
    """Return the process-wide Jinja environment, building it on first use.

    MirrorWall supplies autoescaping, ``StrictUndefined`` and every shared filter; this function
    adds only the shell's slot values. PromptCadence's own templates come first on the search
    path, so a page here can override a package template by name if it ever needs to.

    No telemetry bar: PromptCadence runs no models and reads no machine (spec §5) — what a
    telemetry bar would show belongs to LoadCoach, and this console links to it rather than
    restating a figure it does not measure.
    """
    return create_template_environment(
        app_template_dirs=(_TEMPLATES_DIR,),
        globals_={
            "product_name": "PromptCadence",
            "product_version": __version__,
            "nav_items": NAV_ITEMS,
            "theme_storage_key": "promptcadence-theme",
            "show_telemetry_bar": False,
            # The status vocabulary as functions rather than as a mapping every template has to
            # look up defensively: `StrictUndefined` turns a missing key into a raise, and a state
            # this build has never seen should render as neutral rather than as a 500.
            "state_tone": state_tone,
            "verdict_tone": verdict_tone,
            "outcome_tone": outcome_tone,
        },
    )


def render(template_name: str, /, **context: Any) -> str:
    """Render ``template_name`` with ``context``.

    Args:
        template_name: Path relative to ``web/templates/``, e.g. ``"trajectories/index.html"``.
        **context: Template variables. Every variable a template names must be here —
            ``StrictUndefined`` refuses the rest.

    Returns:
        The rendered HTML.
    """
    return templates().get_template(template_name).render(**context)
