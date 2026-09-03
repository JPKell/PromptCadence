"""Tests for promptcadence.domain.events: the closed vocabulary and the no-content rule.

The second of those is enforced over **every** body in the package, discovered by walking the
domain modules, so a body added by a later phase is covered without anyone remembering to add it
here.
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
from datetime import datetime
from typing import Any

import pytest

import promptcadence.domain as domain_package
from promptcadence.domain.events import EventBody, EventType

_SPEC_17_TYPES = {
    "trajectory.created",
    "trajectory.claimed",
    "plan.drafted",
    "plan.approved",
    "plan.rejected",
    "approval.requested",
    "approval.granted",
    "approval.denied",
    "intent.minted",
    "step.started",
    "turn.started",
    "turn.completed",
    "tool.call.started",
    "tool.call.completed",
    "context.compacted",
    "budget.debited",
    "budget.window_wait",
    "egress.evaluated",
    "deviation.detected",
    "step.completed",
    "trajectory.completed",
    "trajectory.resumed",
    "trajectory.halted",
    "trajectory.failed",
    "trajectory.cancelled",
    "trajectory.recovered",
}
_CONTENT_FIELD_NAMES = {
    "content",
    "text",
    "prompt",
    "prompt_text",
    "output",
    "response",
    "task",
    "description",
    "arguments",
    "args",
    "result",
    "transcript",
    "summary",
}


def _event_bodies() -> list[Any]:
    """Every event body defined anywhere under ``promptcadence.domain``.

    Typed loosely on purpose: these are dataclass *types* that also satisfy
    :class:`~promptcadence.domain.events.EventBody`, and no single static type expresses both to
    ``dataclasses.fields`` and to the protocol at once.
    """
    found: list[Any] = []
    for module_info in pkgutil.iter_modules(domain_package.__path__):
        module = importlib.import_module(f"{domain_package.__name__}.{module_info.name}")
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and dataclasses.is_dataclass(value)
                and isinstance(getattr(value, "event_type", None), EventType)
            ):
                found.append(value)
    return sorted(set(found), key=lambda cls: cls.__name__)


def test_the_event_vocabulary_is_exactly_spec_seventeens() -> None:
    """An event this application can produce that is not a member is a spec defect, not a string."""
    assert {member.value for member in EventType} == _SPEC_17_TYPES


def test_every_event_type_value_is_dotted_and_unique() -> None:
    values = [member.value for member in EventType]
    assert len(values) == len(set(values))
    assert all("." in value for value in values)


def test_phase_two_defines_a_body_for_every_event_it_can_emit() -> None:
    """The transitions this phase implements all have a shape to write."""
    defined = {cls.event_type for cls in _event_bodies()}
    assert {
        EventType.TRAJECTORY_CREATED,
        EventType.TRAJECTORY_CLAIMED,
        EventType.TRAJECTORY_COMPLETED,
        EventType.TRAJECTORY_HALTED,
        EventType.TRAJECTORY_FAILED,
        EventType.TRAJECTORY_CANCELLED,
        EventType.TRAJECTORY_RESUMED,
        EventType.TRAJECTORY_RECOVERED,
        EventType.BUDGET_WINDOW_WAIT,
        EventType.PLAN_APPROVED,
        EventType.PLAN_REJECTED,
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_GRANTED,
        EventType.APPROVAL_DENIED,
        EventType.INTENT_MINTED,
        EventType.DEVIATION_DETECTED,
    } <= defined


def test_at_least_one_body_exists_and_they_are_all_discovered() -> None:
    assert len(_event_bodies()) >= 16


@pytest.mark.parametrize("body_type", _event_bodies(), ids=lambda cls: cls.__name__)
def test_no_event_body_carries_prompt_text_or_model_output(body_type: Any) -> None:
    """The rule this module exists to state: ids, categories, numbers — never content.

    Events are replayed over SSE, written to logs and rendered in a browser. A body carrying the
    transcript would put a confidential trajectory's content on every one of those surfaces, for
    the trajectory the operator was most careful about.
    """
    names = {field.name for field in dataclasses.fields(body_type)}
    offending = names & _CONTENT_FIELD_NAMES
    assert not offending, f"{body_type.__name__} carries content field(s) {sorted(offending)}"


@pytest.mark.parametrize("body_type", _event_bodies(), ids=lambda cls: cls.__name__)
def test_every_event_body_is_frozen_and_declares_its_type(body_type: Any) -> None:
    assert body_type.__dataclass_params__.frozen
    assert isinstance(body_type.event_type, EventType)
    assert hasattr(body_type, "as_canonical")


@pytest.mark.parametrize("body_type", _event_bodies(), ids=lambda cls: cls.__name__)
def test_every_event_body_names_a_trajectory(body_type: Any) -> None:
    """Every event belongs to one trajectory; SSE replay and the explanation both key on it."""
    names = {field.name for field in dataclasses.fields(body_type)}
    assert "trajectory_id" in names


def test_a_body_satisfies_the_event_body_protocol() -> None:
    from promptcadence.domain.trajectory import TrajectoryCompleted

    body: EventBody = TrajectoryCompleted(trajectory_id="tr1", step_count=1, turn_count=1)
    assert isinstance(body, EventBody)
    assert body.as_canonical()["turn_count"] == 1


@pytest.mark.parametrize("body_type", _event_bodies(), ids=lambda cls: cls.__name__)
def test_no_event_body_field_is_a_raw_datetime_in_its_canonical_form(body_type: Any) -> None:
    """``as_canonical`` is JSON-serialized into ``events.data_json``; a datetime is not JSON."""
    annotations = {field.name: field.type for field in dataclasses.fields(body_type)}
    for name, annotation in annotations.items():
        if "datetime" in str(annotation):
            assert name.endswith("_at"), (
                f"{body_type.__name__}.{name} holds an instant but is not named *_at"
            )


def test_canonical_forms_are_json_serializable() -> None:
    import json

    from promptcadence.domain.trajectory import BudgetWindowWait, TrajectoryState

    body = BudgetWindowWait(
        trajectory_id="tr1",
        parked_from=TrajectoryState.EXECUTING,
        next_edge_at=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
        days_waited=0,
        window_wait_max_days=3,
    )
    payload: dict[str, Any] = body.as_canonical()
    assert json.loads(json.dumps(payload)) == payload
