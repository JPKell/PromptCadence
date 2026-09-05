"""promptcadence.services.prompts — this application's prompt pack, on ``setspec.prompts``.

ADR-0012: prompts are versioned JSON records, never Python string literals. ADR-0028: the loader,
the renderer and the hashing come from the package; PromptCadence supplies only its own pack. This
module is the one-function shim adoption asks for — it names where the pack lives so the planner
and the step framing do not each have to know.

Two tests hold the rule: one walks the source for inline prompt strings, and one rebuilds the
manifest and asserts nothing drifted. A record edited without regenerating the manifest fails at
load rather than silently changing what a model was asked.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from setspec.prompts import load_pack

if TYPE_CHECKING:
    from collections.abc import Mapping

    from setspec.prompts import PromptLibrary, RenderedPrompt

__all__ = [
    "PACK_ROOT",
    "PLANNER_CORRECTIVE_PROMPT_ID",
    "PLANNER_DRAFT_PROMPT_ID",
    "STEP_EXECUTE_PROMPT_ID",
    "library",
    "render",
]

PACK_ROOT: Final = Path(__file__).resolve().parent.parent / "prompts"
"""Where PromptCadence's pack lives. Package data, present in the built wheel."""

PLANNER_DRAFT_PROMPT_ID: Final = "planner.draft"
PLANNER_CORRECTIVE_PROMPT_ID: Final = "planner.corrective"
STEP_EXECUTE_PROMPT_ID: Final = "step.execute"


@lru_cache(maxsize=1)
def library() -> PromptLibrary:
    """Return the loaded prompt pack, reading it once per process.

    Returns:
        The library. Loading validates every record against the manifest's hashes.

    Raises:
        PromptPackInvalid: A record is malformed, or the manifest does not describe the pack.
    """
    return load_pack(PACK_ROOT)


def render(
    prompt_id: str, variables: Mapping[str, Any], *, version: str | None = None
) -> RenderedPrompt:
    """Render one prompt record.

    Args:
        prompt_id: The record's identifier, e.g. ``planner.draft``.
        variables: Every variable the record declares required.
        version: Pin a version; ``None`` takes the latest in the pack.

    Returns:
        The rendered prompt, carrying the ``prompt_id``, ``version`` and ``sha256`` recorded on
        whatever used it — the plan attempt or the turn — so provenance is checkable.

    Raises:
        PromptNotFound: No record with that identifier.
        PromptVariableError: A required variable was not supplied, or one was of the wrong type.
    """
    return library().render(prompt_id, variables, version=version)
