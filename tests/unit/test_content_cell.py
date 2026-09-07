"""The console's one rendering branch over model output: retained, scrubbed, or genuinely empty.

`content_cell` is the only place a turn's body reaches HTML, and the three cases it separates are
three different facts. A scrubbed body is an absence the retention sweep created; an empty body is
a fact about the model — a provider that declared `finish_reason=stop` and returned no text (I2's
finding F2, kept by contract 6). Rendering the second as a blank cell reports it as neither.
"""

from __future__ import annotations

from promptcadence.domain.explanation import content_or_removed
from promptcadence.web.rendering import templates


def _cell(text: str | None) -> str:
    macro = templates().get_template("_macros.html").module.content_cell
    return str(macro(content_or_removed(text, "sha256:d")))


def test_a_retained_body_renders_its_own_text() -> None:
    assert "a plan" in _cell("a plan")


def test_a_scrubbed_body_says_retention_removed_it() -> None:
    assert "content removed by retention" in _cell(None)


def test_an_empty_completion_says_so_rather_than_rendering_a_blank_cell() -> None:
    """Contract 6 completes the step on the declared reason; the console still shows what came."""
    rendered = _cell("")
    assert "no text" in rendered
    assert "content removed by retention" not in rendered
