"""The planner: drafts under ``tools.plan``, validates here, retries within the budget (Gate A)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from baseaicore import DataClassification
from fastapi.testclient import TestClient
from tests.fakes.loadcoach_app import (
    FakeLoadCoach,
    ScriptedError,
    ScriptedGeneration,
    build_fake_app,
    shipped_profiles,
)

from promptcadence.domain.errors import LoadCoachError, PlanDraftFailedError
from promptcadence.domain.plan import PlanIssueReason
from promptcadence.domain.tiers import TierSnapshot
from promptcadence.infrastructure.loadcoach import LoadCoachClient
from promptcadence.services.planner import (
    PLANNER_TASK_PROFILE,
    DraftAttempt,
    Planner,
    PlanningCancelled,
    PlanningInputs,
    plan_job_key_prefix,
)


def _step(step_id: str = "s1", **overrides: object) -> dict[str, object]:
    return {
        "step_id": step_id,
        "description": f"do {step_id}",
        "depends_on": [],
        "tools": ["read_file"],
        "tier": "local_fast",
        "data_classification": "confidential",
        "expected_turns": 1,
        **overrides,
    }


def _document(*steps: dict[str, object]) -> str:
    return json.dumps({"steps": list(steps)})


@pytest.fixture
def fake() -> FakeLoadCoach:
    fake = FakeLoadCoach()
    fake.register_profile(*shipped_profiles(PLANNER_TASK_PROFILE))
    return fake


@pytest.fixture
def client(fake: FakeLoadCoach) -> Iterator[LoadCoachClient]:
    yield LoadCoachClient(TestClient(build_fake_app(fake), base_url="http://loadcoach.test"))


@pytest.fixture
def inputs(tier_snapshot: TierSnapshot) -> PlanningInputs:
    return PlanningInputs(
        task="summarize ./notes",
        classification=DataClassification.CONFIDENTIAL,
        tool_allowlist=("read_file", "list_dir"),
        tool_descriptions={"read_file": "read a file", "list_dir": "list a directory"},
        tier_snapshot=tier_snapshot,
        max_plan_steps=20,
    )


def _ids() -> Iterator[str]:
    counter = 0
    while True:
        counter += 1
        yield f"01SESSION{counter:017d}"


def _planner(client: LoadCoachClient, retries: int = 2) -> Planner:
    ids = _ids()
    return Planner(client, corrective_retries=retries, id_factory=lambda: next(ids))


def test_a_valid_first_draft_is_returned_with_its_provenance(
    fake: FakeLoadCoach, client: LoadCoachClient, inputs: PlanningInputs
) -> None:
    fake.script(ScriptedGeneration(text=_document(_step())))
    attempts: list[DraftAttempt] = []
    plan = _planner(client).draft(inputs, trajectory_id="01T", on_attempt=attempts.append)
    assert plan.step_ids == ("s1",)
    (attempt,) = attempts
    assert attempt.valid and attempt.attempt == 1
    assert attempt.prompt_id == "planner.draft"
    assert attempt.prompt_sha256.startswith("sha256:")
    assert attempt.job_id in fake.jobs
    assert attempt.model_canonical_id == fake.model.canonical_id
    assert attempt.usage is not None and attempt.usage.input_tokens == 812
    # What LoadCoach was asked: the planner profile, JSON, the key under the trajectory's prefix,
    # a system turn and the caller's task verbatim in the user turn.
    request = fake.requests[-1]["body"]
    assert request["task"] == PLANNER_TASK_PROFILE
    assert request["response_format"] == "json"
    assert request["idempotency_key"].startswith(plan_job_key_prefix("01T"))
    assert request["messages"][0]["role"] == "system"
    assert "summarize ./notes" in request["messages"][1]["content"]


def test_the_schema_is_never_handed_to_loadcoach_and_tools_are_described_briefly(
    fake: FakeLoadCoach, client: LoadCoachClient, inputs: PlanningInputs
) -> None:
    """ADR-0041: the request carries no schema for LoadCoach; the prompt names the fields once
    and each tool by its first sentence (``planner.draft`` 1.1.0)."""
    fake.script(ScriptedGeneration(text=_document(_step())))
    _planner(client).draft(inputs, trajectory_id="01T", on_attempt=lambda _: None)
    request = fake.requests[-1]["body"]
    user = request["messages"][1]["content"]
    assert "promptcadence.local/schemas/plan" not in user and "$schema" not in user
    assert "- read_file: read a file" in user and "- list_dir: list a directory" in user
    assert "json_schema" not in request
    assert request.get("overrides") is None


def test_every_issue_is_fed_back_at_once_and_the_corrected_draft_is_taken(
    fake: FakeLoadCoach, client: LoadCoachClient, inputs: PlanningInputs
) -> None:
    broken = _document(
        _step("s1", tier="gpt_9", tools=["teleport"]), _step("s2", depends_on=["s9"])
    )
    fake.script(ScriptedGeneration(text=broken), ScriptedGeneration(text=_document(_step())))
    attempts: list[DraftAttempt] = []
    plan = _planner(client).draft(inputs, trajectory_id="01T", on_attempt=attempts.append)
    assert plan.step_ids == ("s1",)
    first, second = attempts
    assert not first.valid and second.valid
    assert {issue.reason for issue in first.issues} == {
        PlanIssueReason.TIER_NOT_CONFIGURED,
        PlanIssueReason.TOOL_NOT_ALLOWLISTED,
        PlanIssueReason.UNKNOWN_DEPENDENCY,
    }
    assert second.prompt_id == "planner.corrective"
    # The retry carries the refused answer and the corrective naming all three issues.
    messages = fake.requests[-1]["body"]["messages"]
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "user"]
    assert messages[2]["content"] == broken
    assert "3 issue(s)" in messages[3]["content"]
    assert "gpt_9" in messages[3]["content"] and "teleport" in messages[3]["content"]


def test_the_budget_is_one_draft_plus_the_configured_retries_then_plan_draft_failed(
    fake: FakeLoadCoach, client: LoadCoachClient, inputs: PlanningInputs
) -> None:
    fake.set_default(ScriptedGeneration(text='{"steps": []}'))
    attempts: list[DraftAttempt] = []
    with pytest.raises(PlanDraftFailedError) as caught:
        _planner(client, retries=2).draft(inputs, trajectory_id="01T", on_attempt=attempts.append)
    assert len(attempts) == 3 == len(fake.jobs)
    assert caught.value.details["attempt_count"] == 3
    assert caught.value.details["attempts"] == [["empty_plan"]] * 3
    assert "emptiness cannot pass a gate" in caught.value.message

    fake.jobs.clear()
    with pytest.raises(PlanDraftFailedError):
        _planner(client, retries=0).draft(inputs, trajectory_id="01U", on_attempt=lambda _: None)
    assert len(fake.jobs) == 1, "corrective_retries = 0 is one attempt and no retry"


def test_a_non_json_answer_is_an_issue_not_a_crash_and_is_never_repaired(
    fake: FakeLoadCoach, client: LoadCoachClient, inputs: PlanningInputs
) -> None:
    fenced = "```json\n" + _document(_step()) + "\n```"
    fake.script(ScriptedGeneration(text=fenced), ScriptedGeneration(text=_document(_step())))
    attempts: list[DraftAttempt] = []
    _planner(client).draft(inputs, trajectory_id="01T", on_attempt=attempts.append)
    assert attempts[0].issues[0].reason is PlanIssueReason.NOT_JSON
    assert attempts[0].raw_document == fenced, "the verbatim answer, fence and all"


def test_a_loadcoach_failure_propagates_and_a_cancel_stops_at_the_boundary(
    fake: FakeLoadCoach, client: LoadCoachClient, inputs: PlanningInputs
) -> None:
    fake.script(ScriptedError("PROVIDER_TIMEOUT"))
    with pytest.raises(LoadCoachError):
        _planner(client).draft(inputs, trajectory_id="01T", on_attempt=lambda _: None)
    with pytest.raises(PlanningCancelled):
        _planner(client).draft(
            inputs, trajectory_id="01T", on_attempt=lambda _: None, should_stop=lambda: True
        )


def test_each_drafting_session_uses_fresh_keys_so_a_redraft_never_replays(
    fake: FakeLoadCoach, client: LoadCoachClient, inputs: PlanningInputs
) -> None:
    fake.set_default(ScriptedGeneration(text=_document(_step())))
    planner = _planner(client)
    planner.draft(inputs, trajectory_id="01T", on_attempt=lambda _: None)
    planner.draft(inputs, trajectory_id="01T", on_attempt=lambda _: None)
    keys = {job.idempotency_key for job in fake.jobs.values()}
    assert len(keys) == 2 and all(key and key.startswith("plan:01T:") for key in keys)


def test_an_empty_first_answer_is_corrected_without_an_empty_assistant_turn(
    fake: FakeLoadCoach, client: LoadCoachClient, inputs: PlanningInputs
) -> None:
    """A reasoning model that spends its output budget thinking returns nothing (observed on
    the real stack); the corrective must not replay that nothing as an assistant turn, which a
    provider refuses, and the issue must say the document was empty."""
    fake.script(ScriptedGeneration(text=""), ScriptedGeneration(text=_document(_step())))
    attempts: list[DraftAttempt] = []
    plan = _planner(client).draft(inputs, trajectory_id="01T", on_attempt=attempts.append)
    assert plan.step_ids == ("s1",)
    assert "empty" in attempts[0].issues[0].message
    messages = fake.requests[-1]["body"]["messages"]
    assert [message["role"] for message in messages] == ["system", "user", "user"]
    assert "empty" in messages[-1]["content"]


def test_a_loadcoach_validation_failure_is_an_empty_attempt_within_the_budget(
    fake: FakeLoadCoach, client: LoadCoachClient, inputs: PlanningInputs
) -> None:
    """Spec §13: a validation failure on a planning call is recorded and the bounded corrective
    applies. Observed on the reference machine: LoadCoach's own corrective retry refuses its own
    request after an empty first answer and surfaces ``VALIDATION_ERROR``."""
    fake.script(ScriptedError("VALIDATION_ERROR"), ScriptedGeneration(text=_document(_step())))
    attempts: list[DraftAttempt] = []
    plan = _planner(client).draft(inputs, trajectory_id="01T", on_attempt=attempts.append)
    assert plan.step_ids == ("s1",)
    first, second = attempts
    assert not first.valid and first.raw_document == "" and first.job_id is None
    assert "VALIDATION_ERROR" in first.issues[0].message
    assert second.valid
    messages = fake.requests[-1]["body"]["messages"]
    assert [message["role"] for message in messages] == ["system", "user", "user"]

    fake.script(ScriptedError("VALIDATION_ERROR"), ScriptedError("VALIDATION_ERROR"))
    with pytest.raises(PlanDraftFailedError) as caught:
        _planner(client, retries=1).draft(inputs, trajectory_id="01U", on_attempt=lambda _: None)
    assert caught.value.details["attempt_count"] == 2

    fake.script(ScriptedError("PROVIDER_TIMEOUT"))
    with pytest.raises(LoadCoachError):
        _planner(client).draft(inputs, trajectory_id="01V", on_attempt=lambda _: None)
