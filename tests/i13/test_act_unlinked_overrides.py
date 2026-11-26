"""The one-off that removes Quantity overrides raised before their reservation existed (25-Nov-2026)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.initiatives.i13.act_unlinked_overrides import remove, unlinked_overrides
from app.models.base import Base
from app.models.i13_act_exception import (
    ActConfirmationRecord,
    ActExceptionEventRecord,
    ActExceptionRecord,
    ActNotificationRecord,
)
from app.models.i13_session_link import SessionReservationLink

WHEN = datetime(2026, 11, 20, tzinfo=timezone.utc)


def exception(exception_id, *, exception_type="QUANTITY_OVERRIDE", session_id=None, reservation=None):
    return ActExceptionRecord(
        exception_id=exception_id,
        exception_type=exception_type,
        status="CONFIRMED",
        material="MAT1",
        plant="1300",
        reservation_number=reservation,
        reservation_item="0001" if reservation else None,
        session_id=session_id,
        detected_at=WHEN,
        reason="r",
        evidence_json="{}",
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[
            t.__table__
            for t in (
                ActExceptionRecord,
                ActExceptionEventRecord,
                ActConfirmationRecord,
                ActNotificationRecord,
                SessionReservationLink,
            )
        ],
    )
    with sessionmaker(bind=engine)() as session:
        session.add_all(
            [
                exception("ACT-QUANTITY_OVERRIDE-SESSION-S1", session_id="S1"),  # never linked
                exception("ACT-QUANTITY_OVERRIDE-SESSION-S2", session_id="S2"),  # linked since
                exception("ACT-QUANTITY_OVERRIDE-1000000001-0001", session_id="S3", reservation="1000000001"),
                exception("ACT-NO_PLAN-1000000002-0001", exception_type="NO_PLAN"),
                SessionReservationLink(
                    session_id="S2", reservation_number="1000000003", reservation_item="0001",
                    material="MAT1", plant="1300", source="SGTXT",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                ActExceptionEventRecord(
                    exception_id="ACT-QUANTITY_OVERRIDE-SESSION-S1", event_type="DETECTED",
                    actor_type="SYSTEM", timestamp=WHEN, metadata_json="{}",
                ),
                ActConfirmationRecord(
                    exception_id="ACT-QUANTITY_OVERRIDE-SESSION-S1", reason_category="SHUTDOWN",
                    free_text="x", actor_id="REQ1", submitted_at=WHEN,
                ),
                ActExceptionEventRecord(
                    exception_id="ACT-QUANTITY_OVERRIDE-SESSION-S2", event_type="DETECTED",
                    actor_type="SYSTEM", timestamp=WHEN, metadata_json="{}",
                ),
            ]
        )
        session.commit()
        yield session


def test_only_the_override_with_no_linked_reservation_is_listed(db) -> None:
    assert [r.exception_id for r in unlinked_overrides(db)] == ["ACT-QUANTITY_OVERRIDE-SESSION-S1"]


def test_removing_takes_its_history_and_leaves_everything_else(db) -> None:
    remove(db, [r.exception_id for r in unlinked_overrides(db)])
    db.commit()

    assert set(db.execute(select(ActExceptionRecord.exception_id)).scalars()) == {
        "ACT-QUANTITY_OVERRIDE-SESSION-S2",
        "ACT-QUANTITY_OVERRIDE-1000000001-0001",
        "ACT-NO_PLAN-1000000002-0001",
    }
    assert db.scalar(select(func.count()).select_from(ActConfirmationRecord)) == 0
    assert list(db.execute(select(ActExceptionEventRecord.exception_id)).scalars()) == [
        "ACT-QUANTITY_OVERRIDE-SESSION-S2"
    ]
