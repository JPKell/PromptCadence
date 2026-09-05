"""promptcadence.services.planner — draft a plan under ``tools.plan``, validate it here, retry.

The ADR-0041 pattern, exactly as IdeaPress does it: LoadCoach is asked for JSON and told nothing
about the plan's shape beyond what the prompt says; PromptCadence validates the answer against
**its own** schema (:func:`~promptcadence.domain.plan.validate_plan_document`) and, when the
document is refused, feeds **every** issue back at once within a bounded corrective budget
(``[planning] corrective_retries``). ``plan.py``'s module docstring explains why every issue at
once: a two-attempt budget is spent immediately by a validator that reports one problem at a time.

What this module does not do, each by decision:

* It never hands the schema to LoadCoach for validation (ADR-0041), and since ``planner.draft``
  1.1.0 it does not show the JSON Schema document to the model either — the prompt's field list
  is the schema, stated once — because the schema block made a reasoning model think its output
  budget away. The profile's own ``require_valid_json`` is all LoadCoach checks.
* It never lets a plan leave PromptCadence (ADR-0051): the document goes to the validator and the
  record, and nowhere else.
* It never decides anything about the plan. A draft is a proposal; approval is
  :mod:`promptcadence.services.approvals`'s, and the model's ``expected_turns`` sizes nothing
  (ADR-0047).
* It never strips a code fence or repairs the text. ``tools.plan`` requires valid JSON and LoadCoach
  retries a model that fences; a document that still is not JSON when it arrives here is an issue
  the corrective names, and if a local model cannot stop fencing inside the budget that is a
  finding for the ``native.plan`` benchmark, not something to paper over.

Every attempt carries the prompt record it was rendered from — the draft record on attempt 1, the
corrective on every retry — so the ``plans`` row can say which text the model was shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from baseaicore import ValidationError, sha256_of

from promptcadence.domain.errors import PlanDraftFailedError, PlanInvalidError
from promptcadence.domain.plan import Plan, PlanIssue, PlanIssueReason
from promptcadence.infrastructure.loadcoach import GenerateRequest, Message
from promptcadence.services.prompts import (
    PLANNER_CORRECTIVE_PROMPT_ID,
    PLANNER_DRAFT_PROMPT_ID,
    render,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from baseaicore import DataClassification, TokenUsage
    from setspec.prompts import RenderedPrompt

    from promptcadence.domain.tiers import TierSnapshot
    from promptcadence.infrastructure.loadcoach import GenerationResponse, LoadCoachClient

__all__ = [
    "PLANNER_TASK_PROFILE",
    "DraftAttempt",
    "Planner",
    "PlanningCancelled",
    "PlanningInputs",
    "plan_job_key_prefix",
]

PLANNER_TASK_PROFILE: Final = "tools.plan"
"""The LoadCoach profile the planner calls — shipped by LoadCoach since E4, local-only by its
own constraints, JSON by its own validation, and never a tier (ADR-0047 §1)."""


class PlanningCancelled(Exception):  # noqa: N818 — an internal signal, not a caller-facing error
    """The trajectory was cancelled between drafting attempts; the caller ends it there."""


def plan_job_key_prefix(trajectory_id: str) -> str:
    """The idempotency-key prefix every planning call for one trajectory carries.

    Recovery cancels a dead worker's in-flight plan job by listing this application's non-terminal
    jobs and matching this prefix (lifecycle §8.3). A drafting session adds its own nonce after it
    so a redraft never replays a cancelled job's document under the crashed session's key.
    """
    return f"plan:{trajectory_id}:"


@dataclass(frozen=True, slots=True)
class PlanningInputs:
    """Everything the planner prompt renders from — the caller's, never the model's.

    Attributes:
        task: The caller's task, verbatim.
        classification: The trajectory's declared classification.
        tool_allowlist: The trajectory allowlist, in registry order.
        tool_descriptions: Each allowlisted tool's registered description, so the model declares
            names it did not invent.
        tier_snapshot: The trajectory's recorded tier snapshot.
        max_plan_steps: ``[planning] max_plan_steps``.
    """

    task: str
    classification: DataClassification
    tool_allowlist: tuple[str, ...]
    tool_descriptions: Mapping[str, str]
    tier_snapshot: TierSnapshot
    max_plan_steps: int


@dataclass(frozen=True, slots=True)
class DraftAttempt:
    """One drafting attempt, as the ``plans`` row records it.

    Attributes:
        attempt: 1 for the first draft, then one per corrective retry.
        idempotency_key: The key the ``/generate`` call carried.
        raw_document: What the model returned, verbatim.
        plan: The validated plan, or ``None`` when the document was refused.
        issues: Every issue the validator named; empty on a valid attempt.
        job_id: The LoadCoach job.
        model_canonical_id: What answered, as LoadCoach named it.
        usage: The four token classes of the planning call.
        loadcoach_ms: LoadCoach's own total time for the call.
        prompt_id: The prompt record the attempt's newest turn was rendered from.
        prompt_version: Its version.
        prompt_sha256: Its hash.
    """

    attempt: int
    idempotency_key: str
    raw_document: str
    plan: Plan | None
    issues: tuple[PlanIssue, ...]
    job_id: str | None
    model_canonical_id: str | None
    usage: TokenUsage | None
    loadcoach_ms: float | None
    prompt_id: str
    prompt_version: str
    prompt_sha256: str

    @property
    def valid(self) -> bool:
        """Whether the attempt produced a plan."""
        return self.plan is not None

    @property
    def document_sha256(self) -> str:
        """The digest of the verbatim document, valid or not."""
        return sha256_of(self.raw_document)


class Planner:
    """Draft, validate, and retry correctively within the configured budget."""

    __slots__ = ("_corrective_retries", "_ids", "_loadcoach", "_render")

    def __init__(
        self,
        loadcoach: LoadCoachClient,
        *,
        corrective_retries: int,
        id_factory: Callable[[], str],
        prompt_renderer: Callable[..., RenderedPrompt] = render,
    ) -> None:
        """Bind the planner to the LoadCoach client and the budget.

        Args:
            loadcoach: The LoadCoach client.
            corrective_retries: ``[planning] corrective_retries`` — retries after an invalid draft.
            id_factory: The id source for the drafting session's nonce.
            prompt_renderer: How prompt records are rendered; injected so a test can watch what
                the model was shown without a pack on disk.

        Raises:
            ValidationError: If the budget is negative.
        """
        if corrective_retries < 0:
            message = "corrective_retries must not be negative"
            raise ValidationError(message, details={"field": "corrective_retries"})
        self._loadcoach = loadcoach
        self._corrective_retries = corrective_retries
        self._ids = id_factory
        self._render = prompt_renderer

    @property
    def max_attempts(self) -> int:
        """One draft plus the corrective retries."""
        return 1 + self._corrective_retries

    def draft(
        self,
        inputs: PlanningInputs,
        *,
        trajectory_id: str,
        on_attempt: Callable[[DraftAttempt], None],
        should_stop: Callable[[], bool] = lambda: False,
    ) -> Plan:
        """Draft a plan, validating each attempt and retrying correctively within the budget.

        Args:
            inputs: What the prompt renders from.
            trajectory_id: The trajectory, for the idempotency keys.
            on_attempt: Called with every attempt, valid or not, before the next one is made — the
                loop persists each as a ``plans`` row with its ``plan.drafted`` event, so a crash
                between attempts leaves what was drafted on the record.
            should_stop: Read before every attempt; ``True`` means the trajectory was cancelled or
                the lease was lost, and drafting stops at that boundary.

        Returns:
            The validated plan.

        Raises:
            PlanDraftFailedError: Every attempt within the budget was refused; ``details``
                carries each attempt's issue reasons and the last attempt's issues in full.
            PlanningCancelled: ``should_stop`` answered ``True``.
            LoadCoachUnavailableError: LoadCoach could not be reached.
            LoadCoachError: LoadCoach failed the call, or a planning job ended other than
                completed — a cancelled job is not a draft.
        """
        session_nonce = self._ids()
        first = self._render(
            PLANNER_DRAFT_PROMPT_ID,
            {
                "task": inputs.task,
                "classification": inputs.classification.value,
                "tools": _render_tools(inputs),
                "tiers": _render_tiers(inputs.tier_snapshot),
                "max_steps": inputs.max_plan_steps,
            },
        )
        messages: list[Message] = []
        if first.system:
            messages.append(Message(role="system", content=first.system))
        messages.append(Message(role="user", content=first.user))
        history: list[DraftAttempt] = []
        prompt = first
        for attempt in range(1, self.max_attempts + 1):
            if should_stop():
                raise PlanningCancelled(trajectory_id)
            key = f"{plan_job_key_prefix(trajectory_id)}{session_nonce}:{attempt}"
            response = self._loadcoach.generate(
                GenerateRequest(
                    task=PLANNER_TASK_PROFILE,
                    messages=tuple(messages),
                    idempotency_key=key,
                    response_format="json",
                )
            )
            record = _attempt_of(
                response,
                attempt=attempt,
                key=key,
                prompt=prompt,
                inputs=inputs,
            )
            history.append(record)
            on_attempt(record)
            if record.plan is not None:
                return record.plan
            if attempt == self.max_attempts:
                break
            corrective = self._render(
                PLANNER_CORRECTIVE_PROMPT_ID,
                {
                    "issue_count": len(record.issues),
                    "issues": "\n".join(f"- {issue.message}" for issue in record.issues),
                },
            )
            if record.raw_document.strip():
                # An empty answer is not appended: a transcript turn with no content is refused
                # at the provider boundary, and the issue the validator named already says the
                # document was empty. The model is corrected on what it did, not shown a blank.
                messages.append(Message(role="assistant", content=record.raw_document))
            messages.append(Message(role="user", content=corrective.user))
            prompt = corrective
        last = history[-1]
        reasons = [[issue.reason.value for issue in item.issues] for item in history]
        message = (
            f"the planner produced no valid plan in {len(history)} attempt(s) "
            f"(corrective_retries = {self._corrective_retries}); the last attempt was refused "
            f"for: {'; '.join(issue.message for issue in last.issues[:5])}"
        )
        raise PlanDraftFailedError(
            message,
            details={
                "attempts": reasons,
                "issues": [issue.as_canonical() for issue in last.issues],
                "attempt_count": len(history),
            },
        )


def _attempt_of(
    response: GenerationResponse,
    *,
    attempt: int,
    key: str,
    prompt: RenderedPrompt,
    inputs: PlanningInputs,
) -> DraftAttempt:
    """Validate one response into an attempt record, never raising for a refused document."""
    if not response.completed:
        message = (
            f"planning job {response.job_id} ended {response.status!r} rather than completed; "
            "a job that did not finish is not a draft"
        )
        raise PlanDraftFailedError(message, details={"job_id": response.job_id})
    raw = response.text
    plan: Plan | None = None
    issues: tuple[PlanIssue, ...] = ()
    try:
        plan = validate(raw, inputs)
    except PlanInvalidError as exc:
        issues = tuple(
            PlanIssue(
                reason=PlanIssueReason(str(issue["reason"])),
                field_name=str(issue["field"]),
                step_id=issue.get("step_id"),
                message=str(issue["message"]),
            )
            for issue in exc.details.get("issues", [])
        )
    return DraftAttempt(
        attempt=attempt,
        idempotency_key=key,
        raw_document=raw,
        plan=plan,
        issues=issues,
        job_id=response.job_id,
        model_canonical_id=response.model.canonical_id,
        usage=response.usage,
        loadcoach_ms=response.timing.total_ms,
        prompt_id=prompt.prompt_id,
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
    )


def validate(raw_document: str, inputs: PlanningInputs) -> Plan:
    """Validate a document against the trajectory's own declarations.

    Args:
        raw_document: The planner's answer, verbatim.
        inputs: The declarations it is validated against.

    Returns:
        The plan.

    Raises:
        PlanInvalidError: Every issue found, together.
    """
    from promptcadence.domain.plan import validate_plan_document

    return validate_plan_document(
        raw_document,
        trajectory_allowlist=frozenset(inputs.tool_allowlist),
        trajectory_classification=inputs.classification,
        configured_tiers=frozenset(inputs.tier_snapshot.by_name),
        max_plan_steps=inputs.max_plan_steps,
    )


def _render_tools(inputs: PlanningInputs) -> str:
    """One line per allowlisted tool: its name and the first sentence of its description.

    The first sentence and not the page: the registered descriptions are written for the model
    that *calls* a tool, and on the reference machine the planner shown all five in full thought
    its output budget away and returned nothing (the ``planner.draft`` 1.1.0 change reason).
    """
    if not inputs.tool_allowlist:
        return "(none — every step must declare an empty tools list)"
    return "\n".join(
        f"- {name}: {_brief(inputs.tool_descriptions.get(name, ''))}".rstrip(": ")
        for name in inputs.tool_allowlist
    )


def _brief(description: str) -> str:
    """The first sentence of a description, without its full stop."""
    first = description.strip().split(". ")[0].rstrip(".")
    return first


def _render_tiers(snapshot: TierSnapshot) -> str:
    """One line per configured tier: name, surface and ceiling."""
    lines: list[str] = []
    for tier in snapshot.tiers:
        surface = "remote" if tier.is_remote else "local"
        ceiling = tier.effective_max_classification.value
        lines.append(f"- {tier.name}: {surface}, admits data up to {ceiling!r}")
    return "\n".join(lines)


def issue_reasons(attempts: Sequence[DraftAttempt]) -> list[list[str]]:
    """Every attempt's issue reasons, for a cause that names what the model could not fix."""
    return [[issue.reason.value for issue in item.issues] for item in attempts]
