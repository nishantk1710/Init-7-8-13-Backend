"""I07 Quarterly Deep-Dive Report -- persistence repository.

Real Postgres or skip. Uses a throwaway quarter string
(``"Q1 2099"``, far outside any real data) so these tests never collide with
a real generated report and clean up their own row in a fixture teardown --
mirrors ``tests/i7/api/test_i7_api.py``'s throwaway-row pattern.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.reporting.repository import get_report, list_reports, save_report
from app.initiatives.i7.reporting.service import generate_quarterly_report
from app.models.i7_reporting import QuarterlyReportRecord

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")

TEST_QUARTER = "Q1 2099"


@pytest.fixture
def clean_test_quarter():
    with get_sessionmaker()() as session:
        session.execute(delete(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == TEST_QUARTER))
        session.commit()
    yield TEST_QUARTER
    with get_sessionmaker()() as session:
        session.execute(delete(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == TEST_QUARTER))
        session.commit()


@needs_db
def test_save_report_writes_a_row(clean_test_quarter):
    quarter = clean_test_quarter
    with get_sessionmaker()() as session:
        report = generate_quarterly_report(session, quarter)
        row = save_report(session, quarter, report, status="COMPLETED")
        session.commit()
        assert row.id is not None

    with get_sessionmaker()() as session:
        fetched = get_report(session, quarter)
        assert fetched is not None
        assert fetched.status == "COMPLETED"
        assert fetched.report_json is not None


@needs_db
def test_get_report_returns_none_for_a_quarter_never_generated():
    with get_sessionmaker()() as session:
        assert get_report(session, "Q2 2099") is None


@needs_db
def test_list_reports_returns_summaries_without_report_json(clean_test_quarter):
    quarter = clean_test_quarter
    with get_sessionmaker()() as session:
        report = generate_quarterly_report(session, quarter)
        save_report(session, quarter, report, status="COMPLETED")
        session.commit()

    with get_sessionmaker()() as session:
        summaries = list_reports(session, limit=50)
    matching = [s for s in summaries if s.quarter == quarter]
    assert len(matching) == 1
    assert not hasattr(matching[0], "report_json")


@needs_db
def test_regenerating_the_same_quarter_overwrites_rather_than_duplicates(clean_test_quarter):
    """Idempotency: calling generate+save twice for the same quarter must
    leave exactly one row for that quarter, not two."""
    quarter = clean_test_quarter
    with get_sessionmaker()() as session:
        report1 = generate_quarterly_report(session, quarter)
        row1 = save_report(session, quarter, report1, status="COMPLETED")
        session.commit()
        first_id = row1.id

    with get_sessionmaker()() as session:
        report2 = generate_quarterly_report(session, quarter)
        row2 = save_report(session, quarter, report2, status="COMPLETED")
        session.commit()
        second_id = row2.id

    assert first_id == second_id

    with get_sessionmaker()() as session:
        from sqlalchemy import func, select

        count = session.execute(
            select(func.count()).select_from(QuarterlyReportRecord).where(
                QuarterlyReportRecord.quarter == quarter
            )
        ).scalar_one()
    assert count == 1


@needs_db
def test_save_report_can_record_a_failed_status_with_error_detail(clean_test_quarter):
    quarter = clean_test_quarter
    with get_sessionmaker()() as session:
        report = generate_quarterly_report(session, quarter)
        row = save_report(session, quarter, report, status="FAILED", error="simulated failure")
        session.commit()
        assert row.status == "FAILED"
        assert row.error == "simulated failure"
