"""Tests for promptcadence.infrastructure.threads: the generic turn mapped onto the host's row.

Integration by nature — it needs a migrated database — but it runs against SQLite in-process, so
it stays in the default suite (spec §18: the full suite passes with no LoadCoach, no GPU and no
network).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from baseaicore import (
    UNSUPPORTED,
    ConflictError,
    DataClassification,
    NotFoundError,
    TokenUsage,
)
from sqlalchemy.orm import Session, sessionmaker
from weightsdb import MigrationRunner, session_factory
from weightsdb.testing import temporary_sqlite

from promptcadence.domain.intent import ExecutionIntent, TurnProvenance, mint_bypass_default
from promptcadence.domain.policy import ApprovalPolicy
from promptcadence.domain.threads import FinishReason, Thread, Turn, TurnRole
from promptcadence.domain.tiers import TierPolicy
from promptcadence.domain.trajectory import TrajectoryDeclaration
from promptcadence.infrastructure.db import models
from promptcadence.infrastructure.threads import SqlThreadStore
from promptcadence.services.database import MIGRATIONS_LOCATION

_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def sessions(declaration: TrajectoryDeclaration) -> Iterator[sessionmaker[Session]]:
    """A migrated in-memory database holding the fixture trajectory."""
    with temporary_sqlite() as engine:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        factory = session_factory(engine)
        with factory.begin() as session:
            session.add(
                models.Trajectory(
                    id=declaration.trajectory_id,
                    task="summarize the notes",
                    data_classification=declaration.classification.value,
                )
            )
        yield factory


@pytest.fixture
def store(sessions: sessionmaker[Session]) -> SqlThreadStore:
    """The store under test, bound to the temporary database."""
    return SqlThreadStore(sessions)


@pytest.fixture
def intent(
    declaration: TrajectoryDeclaration,
    tier_policy: TierPolicy,
    approval_policy: ApprovalPolicy,
    minted_at: datetime,
) -> ExecutionIntent:
    """The envelope every turn in these tests runs under."""
    return mint_bypass_default(
        intent_id="01INTENT00000000000000000A",
        declaration=declaration,
        tier_policy=tier_policy,
        policy=approval_policy,
        minted_at=minted_at,
    )


def _turn(
    intent: ExecutionIntent,
    sequence: int,
    *,
    thread_id: str = "01THREAD0000000000000000A0",
    content: str | None = None,
    finish_reason: FinishReason | None = None,
    usage: TokenUsage | None = None,
) -> Turn[TurnProvenance]:
    """A governed turn, which can only be built from an intent."""
    provenance = intent.provenance(trajectory_id=intent.trajectory_id, tier="local_fast")
    return Turn(
        f"01TURN{sequence:020d}",
        thread_id,
        sequence,
        TurnRole.ASSISTANT,
        provenance,
        content=content,
        finish_reason=finish_reason,
        usage=usage,
    )


def _thread(declaration: TrajectoryDeclaration) -> Thread:
    """One thread owned by the fixture trajectory."""
    return Thread(
        thread_id="01THREAD0000000000000000A0",
        owner_id=declaration.trajectory_id,
        created_at=_NOW,
    )


def test_a_thread_round_trips(store: SqlThreadStore, declaration: TrajectoryDeclaration) -> None:
    thread = _thread(declaration)
    store.create_thread(thread)
    assert store.get_thread(thread.thread_id) == thread
    assert store.get_thread("01ABSENT000000000000000000") is None


def test_creating_the_same_thread_twice_conflicts(
    store: SqlThreadStore, declaration: TrajectoryDeclaration
) -> None:
    store.create_thread(_thread(declaration))
    with pytest.raises(ConflictError):
        store.create_thread(_thread(declaration))


def test_a_turn_round_trips_with_the_envelope_it_ran_under(
    store: SqlThreadStore, declaration: TrajectoryDeclaration, intent: ExecutionIntent
) -> None:
    """Contract 1 at the storage boundary: the row records ``(intent_id, revision)``."""
    store.create_thread(_thread(declaration))
    store.append_turn(
        _turn(
            intent,
            1,
            content="the notes say...",
            finish_reason=FinishReason.STOP,
            usage=TokenUsage(input_tokens=120, output_tokens=45),
        )
    )
    (stored,) = store.turns("01THREAD0000000000000000A0")
    assert stored.provenance.intent_id == intent.intent_id
    assert stored.provenance.intent_revision == intent.revision
    assert stored.provenance.tier == "local_fast"
    assert stored.content == "the notes say..."
    assert stored.finish_reason is FinishReason.STOP
    assert stored.usage is not None
    assert stored.usage.input_tokens == 120


def test_an_unreported_token_class_is_stored_null_and_read_back_unsupported(
    store: SqlThreadStore, declaration: TrajectoryDeclaration, intent: ExecutionIntent
) -> None:
    """ADR-0016 through the round trip: "not reported" must never become "used none"."""
    store.create_thread(_thread(declaration))
    store.append_turn(_turn(intent, 1, usage=TokenUsage(input_tokens=10, output_tokens=2)))
    (stored,) = store.turns("01THREAD0000000000000000A0")
    assert stored.usage is not None
    assert stored.usage.cache_write_tokens is UNSUPPORTED
    assert stored.usage.cache_read_tokens is UNSUPPORTED


def test_all_four_token_classes_survive_the_round_trip(
    store: SqlThreadStore, declaration: TrajectoryDeclaration, intent: ExecutionIntent
) -> None:
    """ADR-0070 decision 7: a turn row that cannot hold four classes throws two away."""
    store.create_thread(_thread(declaration))
    store.append_turn(
        _turn(
            intent,
            1,
            usage=TokenUsage(
                input_tokens=10, output_tokens=2, cache_write_tokens=7, cache_read_tokens=3
            ),
        )
    )
    (stored,) = store.turns("01THREAD0000000000000000A0")
    assert stored.usage is not None
    assert stored.usage.cache_write_tokens == 7
    assert stored.usage.cache_read_tokens == 3


def test_sequences_are_dense_and_a_duplicate_conflicts(
    store: SqlThreadStore, declaration: TrajectoryDeclaration, intent: ExecutionIntent
) -> None:
    store.create_thread(_thread(declaration))
    assert store.next_sequence("01THREAD0000000000000000A0") == 1
    store.append_turn(_turn(intent, 1))
    assert store.next_sequence("01THREAD0000000000000000A0") == 2
    store.append_turn(_turn(intent, 2))
    with pytest.raises(ConflictError, match="sequence"):
        store.append_turn(
            Turn(
                "01TURNOTHER000000000000000",
                "01THREAD0000000000000000A0",
                2,
                TurnRole.ASSISTANT,
                intent.provenance(trajectory_id=intent.trajectory_id, tier="local_fast"),
            )
        )


def test_appending_the_same_turn_twice_conflicts(
    store: SqlThreadStore, declaration: TrajectoryDeclaration, intent: ExecutionIntent
) -> None:
    store.create_thread(_thread(declaration))
    store.append_turn(_turn(intent, 1))
    with pytest.raises(ConflictError, match="already exists"):
        store.append_turn(_turn(intent, 1))


def test_appending_to_a_missing_thread_is_refused(
    store: SqlThreadStore, intent: ExecutionIntent
) -> None:
    with pytest.raises(NotFoundError):
        store.append_turn(_turn(intent, 1))


def test_a_snapshot_is_the_thread_in_order(
    store: SqlThreadStore, declaration: TrajectoryDeclaration, intent: ExecutionIntent
) -> None:
    store.create_thread(_thread(declaration))
    for sequence in (1, 2, 3):
        store.append_turn(_turn(intent, sequence))
    snapshot = store.snapshot("01THREAD0000000000000000A0", taken_at=_NOW)
    assert [turn.sequence for turn in snapshot.turns] == [1, 2, 3]
    assert snapshot.taken_at == _NOW


def test_the_store_takes_the_snapshot_instant_rather_than_reading_a_clock(
    store: SqlThreadStore, declaration: TrajectoryDeclaration
) -> None:
    """A snapshot appears in the record; one that stamped its own time would depend on the read."""
    store.create_thread(_thread(declaration))
    earlier = datetime(2020, 1, 1, tzinfo=UTC)
    assert store.snapshot("01THREAD0000000000000000A0", taken_at=earlier).taken_at == earlier


def test_a_turn_row_with_no_envelope_reference_is_refused_on_read(
    store: SqlThreadStore,
    sessions: sessionmaker[Session],
    declaration: TrajectoryDeclaration,
) -> None:
    """A row written by some future path that skipped minting is a defect, surfaced on read.

    Nothing in this package can produce one — the domain constructor requires an intent — so this
    inserts the row directly to prove the store does not quietly hand back a turn that never had
    an envelope.
    """
    store.create_thread(_thread(declaration))
    with sessions.begin() as session:
        session.add(
            models.Turn(
                id="01TURNUNGOVERNED0000000000",
                thread_id="01THREAD0000000000000000A0",
                trajectory_id=declaration.trajectory_id,
                sequence=1,
                role="assistant",
                tier="local_fast",
            )
        )
    with pytest.raises(NotFoundError, match="envelope it ran under"):
        store.turns("01THREAD0000000000000000A0")


def test_the_turns_table_may_carry_promptcadence_columns_the_domain_type_may_not(
    store: SqlThreadStore, declaration: TrajectoryDeclaration, intent: ExecutionIntent
) -> None:
    """Spec §10's split: the row is the host's, the type must stay extractable."""
    store.create_thread(_thread(declaration))
    store.append_turn(_turn(intent, 1))
    columns = set(models.Turn.__table__.columns.keys())
    assert {"trajectory_id", "tier", "intent_id", "intent_revision"} <= columns
    assert DataClassification.INTERNAL is declaration.classification
