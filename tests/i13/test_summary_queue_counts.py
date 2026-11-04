"""The dashboard cards' Planned Overdue and Non-AI counts are the exception queue's.

They used to be the legacy queue recomputed per request, while the Exceptions
screen listed what detection last saved -- two numbers for one thing (on
non-prod, 4 Nov 2026: Planned Overdue 2 on the card, 1 on the screen).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.initiatives.i13.act.domain import ExceptionStatus, ExceptionType
from app.initiatives.i13.act_exception_store import SqlExceptionRepository
from app.initiatives.i13.summary import I13Summary, with_queue_exception_counts
from app.models.base import Base
from app.models.i13_act_exception import ActExceptionRecord
from tests.notifications.test_i13_shared import act_exception


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[ActExceptionRecord.__table__])
    with Session(engine) as session:
        yield session


def queue(db, *rows) -> None:
    repository = SqlExceptionRepository(db)
    for n, (exception_type, status, plant) in enumerate(rows):
        repository.upsert(act_exception(exception_id=f"ACT-{n}", exception_type=exception_type, status=status, plant=plant))
    db.flush()


def legacy_summary() -> I13Summary:
    return I13Summary(
        total_oar_positions=10, fast_moving_count=3, slow_moving_count=3, non_moving_count=4,
        gr_not_issued_30_day_count=7, plan_breach_count=99, no_plan_count=999,
        reclassification_candidate_count=2, valuation_is_mocked=True, reference_plan_count=0,
    )


def test_unresolved_exceptions_are_counted_per_type(db):
    queue(
        db,
        (ExceptionType.PLAN_BREACH, ExceptionStatus.AWAITING_REQUESTER, "1300"),
        (ExceptionType.PLAN_BREACH, ExceptionStatus.RESOLVED, "1300"),
        (ExceptionType.NO_PLAN, ExceptionStatus.OPEN, "1300"),
        (ExceptionType.NO_PLAN, ExceptionStatus.ESCALATED, "1500"),
        (ExceptionType.NO_PLAN, ExceptionStatus.CONFIRMED, "1300"),
        (ExceptionType.NO_PLAN_GRNI, ExceptionStatus.OPEN, "1300"),
    )

    counts = SqlExceptionRepository(db).active_counts_by_type()

    assert counts[ExceptionType.PLAN_BREACH] == 1  # the resolved one is not counted
    assert counts[ExceptionType.NO_PLAN] == 3
    assert counts[ExceptionType.NO_PLAN_GRNI] == 1
    assert counts[ExceptionType.QUANTITY_OVERRIDE] == 0  # every type is present, zero or not
    assert SqlExceptionRepository(db).active_counts_by_type(plant="1500")[ExceptionType.NO_PLAN] == 1


def test_the_cards_take_the_queue_counts_and_keep_everything_else(db):
    queue(
        db,
        (ExceptionType.PLAN_BREACH, ExceptionStatus.AWAITING_REQUESTER, "1300"),
        (ExceptionType.NO_PLAN, ExceptionStatus.OPEN, "1300"),
        (ExceptionType.NO_PLAN, ExceptionStatus.OPEN, "1500"),
    )

    summary = with_queue_exception_counts(legacy_summary(), db)

    assert (summary.plan_breach_count, summary.no_plan_count) == (1, 2)
    assert summary.gr_not_issued_30_day_count == 7  # not an exception-queue type; untouched
    assert summary.total_oar_positions == 10
    assert with_queue_exception_counts(legacy_summary(), db, plant="1500").no_plan_count == 1


def test_an_empty_queue_reads_zero_not_the_recomputed_number(db):
    summary = with_queue_exception_counts(legacy_summary(), db)
    assert (summary.plan_breach_count, summary.no_plan_count) == (0, 0)
