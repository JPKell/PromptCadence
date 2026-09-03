"""promptcadence.observability.logging — structured logging with the suite's correlation fields.

Observability standards (spec §17): every record carries whatever of ``request_id``,
``trajectory_id``, ``step_id``, ``turn_id``, ``tier`` and ``model_canonical_id`` the call site
knew. Contributed by a context variable rather than threaded through every signature, because a
service function five frames below a route has no business taking a request ID as a parameter.

Transcript content never reaches a log record at INFO or above (spec §14). ``include_content`` is
off by default, and the redaction filter drops the content-carrying fields even when a caller sets
them by mistake.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any, Final

__all__ = ["configure_logging", "correlation", "current_correlation"]

# A frozen empty mapping as the default: a mutable default on a ContextVar is shared across every
# context that never set one, so a single stray mutation would leak fields between requests.
_EMPTY: Final[Mapping[str, str]] = MappingProxyType({})
_CORRELATION: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "promptcadence_correlation", default=_EMPTY
)

CORRELATION_FIELDS: Final[tuple[str, ...]] = (
    "request_id",
    "trajectory_id",
    "step_id",
    "turn_id",
    "tier",
    "model_canonical_id",
)

# Fields that can hold transcript text. Dropped from every record at INFO or above regardless of
# `include_content` — a DEBUG session is a deliberate act, an INFO log is not (spec §14).
_CONTENT_FIELDS: Final[frozenset[str]] = frozenset({"prompt_text", "response_text", "content"})

_RESERVED: Final[frozenset[str]] = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


def current_correlation() -> dict[str, str]:
    """Return the correlation fields in scope, as a copy."""
    return dict(_CORRELATION.get())


@contextmanager
def correlation(**fields: str | int | None) -> Iterator[None]:
    """Add correlation fields to every log record emitted inside the block.

    Args:
        **fields: Any of :data:`CORRELATION_FIELDS`. A ``None`` value is ignored rather than
            logged as ``"None"``, so a caller that has no turn yet can pass ``turn_id=None``.

    Yields:
        Nothing. The fields are removed when the block exits, including on an exception.
    """
    merged = dict(_CORRELATION.get())
    merged.update({key: str(value) for key, value in fields.items() if value is not None})
    token = _CORRELATION.set(MappingProxyType(merged))
    try:
        yield
    finally:
        _CORRELATION.reset(token)


class _CorrelationFilter(logging.Filter):
    """Attach the in-scope correlation fields, and drop content fields from every record."""

    def __init__(self, *, include_content: bool) -> None:
        super().__init__()
        self._include_content = include_content

    def filter(self, record: logging.LogRecord) -> bool:
        """Always keeps the record; mutates it to carry correlation and to shed content."""
        for key, value in _CORRELATION.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        drop_content = not self._include_content or record.levelno >= logging.INFO
        if drop_content:
            for field in _CONTENT_FIELDS:
                if hasattr(record, field):
                    setattr(record, field, "<redacted>")
        return True


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, with the correlation fields as top-level keys."""

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as a single-line JSON object."""
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


class _TextFormatter(logging.Formatter):
    """Human-readable, with correlation appended as ``key=value`` pairs."""

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as a line, with any correlation fields appended."""
        base = super().format(record)
        extras = " ".join(
            f"{key}={record.__dict__[key]}" for key in CORRELATION_FIELDS if key in record.__dict__
        )
        return f"{base} {extras}" if extras else base


def configure_logging(*, level: str = "INFO", log_format: str = "auto") -> None:
    """Install the root handler for this process.

    Args:
        level: Threshold name (``DEBUG`` … ``CRITICAL``).
        log_format: ``"text"``, ``"json"``, or ``"auto"`` (text on a TTY, json otherwise).

    Refuses nothing; called once from the composition root and idempotent, so a CLI command that
    configures logging and then builds an app does not stack handlers.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_promptcadence", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler._promptcadence = True  # type: ignore[attr-defined]  # marks ours, for idempotence
    resolved_format = log_format
    if resolved_format == "auto":
        resolved_format = "text" if sys.stderr.isatty() else "json"
    formatter: logging.Formatter = (
        _JsonFormatter()
        if resolved_format == "json"
        else _TextFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    handler.setFormatter(formatter)
    # include_content governs what reaches a record at all (services never attach it without
    # the setting); the filter's own job here is only the INFO-and-above blanket redaction.
    handler.addFilter(_CorrelationFilter(include_content=True))
    root.addHandler(handler)
    root.setLevel(level.upper())
