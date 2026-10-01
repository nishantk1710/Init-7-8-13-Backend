"""CLI entry point (`python -m app.reporting.generate_quarterly`) -- argument
parsing and the latest-closed-quarter fallback, against the real database
(matches this package's other reporting tests' `needs_db` pattern)."""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.reporting.period import latest_closed_quarter
from app.models.i7_reporting import QuarterlyReportRecord
from app.reporting.generate_quarterly import _parser, main

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


def test_parser_no_longer_requires_quarter():
    # Previously `required=True` -- parse_args([]) raised SystemExit(2).
    args = _parser().parse_args([])
    assert args.quarter is None


def test_parser_still_accepts_explicit_quarter():
    args = _parser().parse_args(["--quarter", "Q3 2026"])
    assert args.quarter == "Q3 2026"


@needs_db
def test_main_with_no_quarter_generates_the_latest_closed_quarter():
    expected_quarter = latest_closed_quarter()
    factory = get_sessionmaker()
    with factory() as session:
        session.execute(delete(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == expected_quarter))
        session.commit()

    try:
        exit_code = main([])
        assert exit_code == 0

        with factory() as session:
            row = session.execute(
                delete(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == expected_quarter)
            )
            session.commit()
            # delete() returns a CursorResult; rowcount tells us a row existed
            assert row.rowcount == 1
    finally:
        with factory() as session:
            session.execute(delete(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == expected_quarter))
            session.commit()


@needs_db
def test_main_still_accepts_explicit_quarter_unchanged():
    """Non-regression: the documented SSH command
    `python -m app.reporting.generate_quarterly --quarter "Q3 2026"` form must
    keep working exactly as before."""
    quarter = "Q1 2098"
    factory = get_sessionmaker()
    with factory() as session:
        session.execute(delete(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == quarter))
        session.commit()

    try:
        exit_code = main(["--quarter", quarter])
        assert exit_code == 0

        with factory() as session:
            row = session.execute(
                delete(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == quarter)
            )
            session.commit()
            assert row.rowcount == 1
    finally:
        with factory() as session:
            session.execute(delete(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == quarter))
            session.commit()
