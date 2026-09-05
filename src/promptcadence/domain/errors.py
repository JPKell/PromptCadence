"""promptcadence.domain.errors — the refusals the domain raises, with spec §13's codes.

Spec §13 fixes the error vocabulary PromptCadence surfaces to a caller. :class:`ErrorCode` holds
that list verbatim and nothing else: a refusal that needs a code the spec does not list is a
defect in the spec to close with an amendment, not a string to invent at the raise site.

Only the codes a built phase can actually raise have an exception class here. The rest arrive
with the phase that can produce them — a class for ``TOOL_EXECUTION_FAILED`` before ToolYard is
wired would be a promise with no code behind it. Phase 3 added the trajectory lookups, the two
LoadCoach failures, the unknown project and the unregistered tool.

:class:`IllegalTransitionError` is the one code **not** in spec §13, deliberately. An illegal state
transition is a programming error inside the application, never a caller's input: services map it
onto a §13 code (a refused cancel becomes ``TRAJECTORY_NOT_CANCELLABLE``) before it can reach an
API envelope. It carries its own code so logs can aggregate it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from baseaicore import SuiteError, ValidationError

__all__ = [
    "ApprovalInvalidStateError",
    "ClassificationInvalidError",
    "CompactionFailedError",
    "DeviationHaltedError",
    "ErrorCode",
    "IllegalTransitionError",
    "LoadCoachError",
    "LoadCoachUnavailableError",
    "PlanDraftFailedError",
    "PlanInvalidError",
    "ProjectUnknownError",
    "SchemaVersionUnsupportedError",
    "TierNotConfiguredError",
    "TierUnavailableError",
    "ToolNotFoundError",
    "TrajectoryNotCancellableError",
    "TrajectoryNotFoundError",
    "UnpricedEgressRefusedError",
]


class ErrorCode(StrEnum):
    """Every error code spec §13 lists, and no other.

    The members are the spec's own strings. A test asserts the set matches the specification
    table, so a code added to one and not the other fails the suite rather than diverging.
    """

    TRAJECTORY_NOT_FOUND = "TRAJECTORY_NOT_FOUND"
    TRAJECTORY_NOT_CANCELLABLE = "TRAJECTORY_NOT_CANCELLABLE"
    CLASSIFICATION_INVALID = "CLASSIFICATION_INVALID"
    TIER_NOT_CONFIGURED = "TIER_NOT_CONFIGURED"
    TIER_UNAVAILABLE = "TIER_UNAVAILABLE"
    LOADCOACH_UNAVAILABLE = "LOADCOACH_UNAVAILABLE"
    LOADCOACH_ERROR = "LOADCOACH_ERROR"
    SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
    PROJECT_UNKNOWN = "PROJECT_UNKNOWN"
    PLAN_DRAFT_FAILED = "PLAN_DRAFT_FAILED"
    PLAN_INVALID = "PLAN_INVALID"
    PLAN_REJECTED = "PLAN_REJECTED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_INVALID_STATE = "APPROVAL_INVALID_STATE"
    DEVIATION_HALTED = "DEVIATION_HALTED"
    STEP_LIMIT_EXCEEDED = "STEP_LIMIT_EXCEEDED"
    COMPACTION_FAILED = "COMPACTION_FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    TOKEN_BUDGET_EXCEEDED = "TOKEN_BUDGET_EXCEEDED"  # noqa: S105 — an error code, not a secret
    UNPRICED_EGRESS_REFUSED = "UNPRICED_EGRESS_REFUSED"
    EGRESS_DENIED = "EGRESS_DENIED"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_ARGS_INVALID = "TOOL_ARGS_INVALID"
    TOOL_REFUSED = "TOOL_REFUSED"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"


class PlanInvalidError(ValidationError):
    """A drafted plan failed PromptCadence's own schema or one of lifecycle §4.1's five rules.

    ``details["issues"]`` carries every :class:`~promptcadence.domain.plan.PlanIssue` found, not
    the first: the corrective retry (P7) has one round trip to fix everything, and a validator
    that reports one problem at a time turns a two-attempt budget into a queue. Issues name a
    ``step_id`` and a field; they never carry a step description, because that is model output and
    ``details`` travels into API envelopes and logs.
    """

    code: ClassVar[str] = ErrorCode.PLAN_INVALID


class ClassificationInvalidError(ValidationError):
    """A declared classification is not a :class:`baseaicore.DataClassification` member."""

    code: ClassVar[str] = ErrorCode.CLASSIFICATION_INVALID


class TierNotConfiguredError(ValidationError):
    """A tier was named that the trajectory's tier snapshot does not define."""

    code: ClassVar[str] = ErrorCode.TIER_NOT_CONFIGURED


class TierUnavailableError(SuiteError):
    """A configured tier cannot serve right now; ``details["reason"]`` says why.

    Two reasons exist. ``loadcoach_has_no_remote_provider`` (lifecycle §3): remote tiers are
    unavailable until LC-E1 registers a second provider in LoadCoach. And, from Phase 3,
    LoadCoach's own ``NO_ELIGIBLE_MODEL`` or ``TASK_PROFILE_NOT_FOUND`` for the tier's task
    profile — the tier is configured here and cannot be served there; ``details`` preserves every
    candidate and its rejection reason exactly as LoadCoach reported them (spec §13).
    """

    code: ClassVar[str] = ErrorCode.TIER_UNAVAILABLE


class TrajectoryNotFoundError(SuiteError):
    """No trajectory has that identifier."""

    code: ClassVar[str] = ErrorCode.TRAJECTORY_NOT_FOUND


class TrajectoryNotCancellableError(SuiteError):
    """A cancel reached a trajectory that is already terminal.

    The service-layer name for :class:`IllegalTransitionError` on T14: a completed trajectory is
    not cancellable, and the caller is told so with a spec §13 code rather than an internal one.
    """

    code: ClassVar[str] = ErrorCode.TRAJECTORY_NOT_CANCELLABLE


class ProjectUnknownError(ValidationError):
    """A request named a ``project`` no ``[budget.projects.<name>]`` section configures."""

    code: ClassVar[str] = ErrorCode.PROJECT_UNKNOWN


class ToolNotFoundError(ValidationError):
    """A request's tool allowlist named a tool the registry (``[tools] enabled``) does not list."""

    code: ClassVar[str] = ErrorCode.TOOL_NOT_FOUND


class LoadCoachUnavailableError(SuiteError):
    """LoadCoach could not be reached at all: connection refused, DNS failure, a dropped socket.

    Distinct from :class:`LoadCoachError`, which is LoadCoach *answering* with a failure. Spec §13
    parks the trajectory with the reason and degrades health; it never fails the request as
    ``INTERNAL_ERROR``.
    """

    code: ClassVar[str] = ErrorCode.LOADCOACH_UNAVAILABLE


class CompactionFailedError(SuiteError):
    """LoadCoach reported ``CONTEXT_LIMIT_EXCEEDED`` and nothing here could make the context fit.

    Spec §13 maps that code to "trigger compaction and retry once; then halt". Compaction arrives
    in Phase 8, so until then every occurrence is the "then halt" half, surfaced as this code with
    LoadCoach's own in ``details`` — never as a silent retry of the same request.
    """

    code: ClassVar[str] = ErrorCode.COMPACTION_FAILED


class SchemaVersionUnsupportedError(SuiteError):
    """LoadCoach serves no API major this build speaks (api.md §12 rule 1)."""

    code: ClassVar[str] = ErrorCode.SCHEMA_VERSION_UNSUPPORTED


class LoadCoachError(SuiteError):
    """LoadCoach answered with an error, or with a response PromptCadence cannot read.

    ``details["loadcoach_code"]`` carries LoadCoach's own code verbatim and
    ``details["loadcoach_details"]`` its details, so no LoadCoach failure loses its identity on the
    way to a caller (spec §13: "with the original code in ``details``"). ``details["http_status"]``
    is the status the answer came with.
    """

    code: ClassVar[str] = ErrorCode.LOADCOACH_ERROR


class UnpricedEgressRefusedError(SuiteError):
    """A remote tier named no pricing source. Unpriced egress is refused, not free (ADR-0030)."""

    code: ClassVar[str] = ErrorCode.UNPRICED_EGRESS_REFUSED


class PlanDraftFailedError(SuiteError):
    """The planner spent its corrective budget without producing a valid plan (T7).

    ``details["attempts"]`` lists every attempt's issue reasons, so the failure names what the
    model could not fix rather than only that it did not.
    """

    code: ClassVar[str] = ErrorCode.PLAN_DRAFT_FAILED.value


class ApprovalInvalidStateError(SuiteError):
    """An approval was resolved against a request that is not pending, or in the wrong mode."""

    code: ClassVar[str] = ErrorCode.APPROVAL_INVALID_STATE


class DeviationHaltedError(SuiteError):
    """A violation, a denial or the deviation limit stopped the trajectory (lifecycle §5)."""

    code: ClassVar[str] = ErrorCode.DEVIATION_HALTED


class IllegalTransitionError(SuiteError):
    """A state transition was attempted that lifecycle §8.2 does not list, or whose guard failed.

    Not a spec §13 code: this is an internal defect, and a service maps it onto the caller-facing
    code for the operation that hit it. ``details`` names ``from``, ``to`` (when a target was
    named), the transition label when one applies, and the guard that refused.
    """

    code: ClassVar[str] = "TRANSITION_NOT_PERMITTED"
