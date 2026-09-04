"""promptcadence.infrastructure.tool_calls — where ToolYard's records land in this application.

ToolYard owns no data (its spec §10): it defines :class:`toolyard.ToolCallRecord` and one method
to append one, and the application owns the table, the retention and the migration. This module is
that ownership, and it is deliberately the *only* place a ToolYard record becomes a row.

**Why the store collects rather than writes.** The executor appends its record from inside
:meth:`toolyard.ToolExecutor.execute`, and that call may be a ``run_command`` spending its whole
timeout inside a container. A store that wrote through to a session would hold a database
transaction open across a subprocess — on SQLite, a write lock — for as long as a model's command
chose to run. So :class:`CollectingToolCallStore` collects during execution and
:meth:`CollectingToolCallStore.flush` stages every collected record as a row on the session that
turn commits on. The ADR-0044 property survives: the record, the ``TOOL`` turn that carried its
result and the ``tool.call.completed`` event are one write, and a crash between them is impossible.

**Why a failed append must raise.** ToolYard turns a raising store into
:class:`toolyard.StoreFailure`, carrying the result and the record on the exception, precisely
because a side effect may already have happened and losing its audit trail is the worse failure.
Collection cannot fail, so the raise that matters here is the flush's, and it happens inside the
turn's transaction where a rollback loses the turn too — which is the correct outcome: a turn whose
tool calls could not be recorded is a turn that must not be reported as having run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from promptcadence.infrastructure.db import models

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session
    from toolyard import ToolCallRecord

__all__ = ["CollectingToolCallStore", "ToolCallLinks", "tool_call_row"]


@dataclass(frozen=True, slots=True)
class ToolCallLinks:
    """What a row needs and a ToolYard record cannot know.

    The record describes one call; these are the four facts that put it back into a trajectory and
    point at what it produced. They are supplied by the loop after the call ran, because the
    ``TOOL`` turn does not exist until the result is in hand and the artifact is not written until
    the output has been checked against its own digest.

    Attributes:
        tool_turn_id: The ``TOOL`` turn carrying the result back to the model, or ``None`` when the
            call produced no turn.
        artifact_ref: The digest the whole output was filed under, or ``None`` when it was small
            enough to keep in the turn — or when ToolYard truncated it, in which case there is no
            whole output to file.
        output_truncated: Whether the model saw a labelled prefix rather than everything.
        isolation_tier: The rung a command ran under, or ``None`` for a tool that runs no process.
    """

    tool_turn_id: str | None = None
    artifact_ref: str | None = None
    output_truncated: bool = False
    isolation_tier: str | None = None


def tool_call_row(
    record: ToolCallRecord,
    *,
    row_id: str,
    trajectory_id: str,
    turn_id: str,
    links: ToolCallLinks,
) -> models.ToolCallRecord:
    """Map one ToolYard record onto the ``tool_call_records`` row.

    Every field ToolYard produces is carried through unchanged — including ``args_json`` being
    ``None`` under ``redact_args``, and ``tool_name`` holding a name no tool has, because a refusal
    that does not say what was asked for cannot be diagnosed.

    Args:
        record: What the executor produced for one call.
        row_id: The row's identity, minted by the caller.
        trajectory_id: The trajectory the call belongs to.
        turn_id: The assistant turn whose ``tool_calls`` this answers.
        links: The turn, artifact and isolation facts the record cannot carry.

    Returns:
        The row, ready to be added to a session the caller owns.
    """
    return models.ToolCallRecord(
        id=row_id,
        trajectory_id=trajectory_id,
        turn_id=turn_id,
        tool_turn_id=links.tool_turn_id,
        invocation_id=record.invocation_id,
        tool_name=record.tool_name,
        args_json=record.args_json,
        args_sha256=record.args_sha256,
        status=record.status.value,
        reason=record.reason,
        reason_detail=record.reason_detail,
        result_summary=record.result_summary,
        result_sha256=record.result_sha256,
        artifact_ref=links.artifact_ref,
        output_truncated=links.output_truncated,
        duration_ms=record.duration_ms,
        risk_class=record.risk_class.value,
        egress=record.egress.value,
        isolation_tier=links.isolation_tier,
        started_at=record.started_at,
    )


class CollectingToolCallStore:
    """ToolYard's :class:`toolyard.ToolCallStore`, collecting for a later flush.

    One instance per call, because the loop writes each call's record, ``TOOL`` turn and event in
    one transaction and then moves on. Collecting more than one would only delay the write.
    """

    __slots__ = ("_records",)

    def __init__(self) -> None:
        """Create an empty store."""
        self._records: list[ToolCallRecord] = []

    def append(self, record: ToolCallRecord) -> None:
        """Collect one record.

        Args:
            record: The record for one call, whatever its outcome. Refusals and failures included:
                the table answers "what did this trajectory try", and a store that kept only
                successes would answer the wrong question.
        """
        self._records.append(record)

    @property
    def records(self) -> Sequence[ToolCallRecord]:
        """Return the collected records, in append order.

        Returns:
            A tuple snapshot, so a caller iterating it cannot be surprised by a later append.
        """
        return tuple(self._records)

    def flush(
        self,
        session: Session,
        *,
        trajectory_id: str,
        turn_id: str,
        links: ToolCallLinks,
        row_ids: Sequence[str],
    ) -> Sequence[models.ToolCallRecord]:
        """Stage every collected record as a row on the caller's session.

        Args:
            session: The session the loop's turn transaction owns.
            trajectory_id: The trajectory.
            turn_id: The assistant turn whose ``tool_calls`` these answer.
            links: The turn, artifact and isolation facts, applied to every collected record —
                one call produces one record, so in practice this is that call's.
            row_ids: One identifier per collected record, minted by the caller. Passed in rather
                than generated here so the loop's injected id source stays the only one.

        Returns:
            The rows staged, in order.

        Raises:
            ValueError: If ``row_ids`` does not have one entry per collected record. A caller bug,
                and one that would otherwise write a row with a duplicate or missing primary key.
        """
        if len(row_ids) != len(self._records):
            message = (
                f"flush needs one row id per collected record; got {len(row_ids)} for "
                f"{len(self._records)}"
            )
            raise ValueError(message)
        rows = [
            tool_call_row(
                record,
                row_id=row_id,
                trajectory_id=trajectory_id,
                turn_id=turn_id,
                links=links,
            )
            for record, row_id in zip(self._records, row_ids, strict=True)
        ]
        for row in rows:
            session.add(row)
        return tuple(rows)
