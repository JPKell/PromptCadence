"""Tests for promptcadence.observability.logging: correlation, formatting and content redaction."""

from __future__ import annotations

import json
import logging

import pytest

from promptcadence.observability.logging import (
    configure_logging,
    correlation,
    current_correlation,
)


def test_correlation_fields_are_scoped_to_the_block() -> None:
    assert current_correlation() == {}
    with correlation(request_id="abc123", trajectory_id="traj1"):
        assert current_correlation() == {"request_id": "abc123", "trajectory_id": "traj1"}
    assert current_correlation() == {}


def test_correlation_ignores_none_values() -> None:
    with correlation(request_id="abc", turn_id=None):
        assert current_correlation() == {"request_id": "abc"}


def test_correlation_restored_after_exception() -> None:
    with correlation(request_id="outer"):
        try:
            with correlation(request_id="inner"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert current_correlation() == {"request_id": "outer"}


def test_configure_logging_json_emits_parseable_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format="json")
    logger = logging.getLogger("promptcadence.test")
    with correlation(request_id="req-1", tier="local_fast"):
        logger.info("something happened")
    captured = capsys.readouterr()
    line = captured.err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "something happened"
    assert payload["request_id"] == "req-1"
    assert payload["tier"] == "local_fast"


def test_configure_logging_is_idempotent_no_duplicate_handlers() -> None:
    root = logging.getLogger()
    before = len([h for h in root.handlers if getattr(h, "_promptcadence", False)])
    configure_logging(level="INFO", log_format="text")
    configure_logging(level="INFO", log_format="text")
    after = len([h for h in root.handlers if getattr(h, "_promptcadence", False)])
    assert after == 1
    assert before <= 1


def test_content_fields_redacted_at_info_even_when_content_logging_requested(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", log_format="json")
    logger = logging.getLogger("promptcadence.test.content")
    logger.info("turn completed", extra={"prompt_text": "the user's private prompt"})
    captured = capsys.readouterr()
    line = captured.err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["prompt_text"] == "<redacted>"
