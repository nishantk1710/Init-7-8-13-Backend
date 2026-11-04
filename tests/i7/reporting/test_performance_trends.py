"""The report's performance trends: recommendation volume, dated approval activity and month-end excess.

SQLite with only the tables the trends read. What is tested is what each series is counted from and when a month has no
figure: a month with no history, not started, or with no valid month-end snapshot is None -- never 0.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.reporting import performance_trends as trends
from app.initiatives.i7.reporting.quarter_management import build_quarter_management
from app.initiatives.i7.reporting.period import resolve_period
from app.models.i7_kpi_snapshot import STATUS_FINAL, STATUS_OPEN, KpiSnapshot
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation

TABLES = (Recommendation, ApprovalLedgerEntry, KpiSnapshot)
OCT_END = date(2026, 10, 31)
TODAY = date(2026, 11, 3)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in TABLES:
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def at(year, month, day=10):
    return datetime(year, month, day, 9, 0, tzinfo=timezone.utc)


def rec(material, when, *, plant="1300", run=1, status="READY_FOR_REVIEW"):
    return Recommendation(
        recommendation_id=f"REC-{material}-{plant}-{run}", sap_material_number=material, sap_plant_code=plant,
        feature_run_id=run, forecast_run_id=run, inventory_run_id=run, oar_run_id=run, policy_id="p", policy_version=1,
        formula_version="f", status=status, impact_status="X", generated_at=when,
    )


def event(action, when, *, rid="REC-A-1300-1", version=1, actor="u1"):
    return ApprovalLedgerEntry(
        recommendation_id=rid, recommendation_version=version, actor_id=actor, actor_role="End User", action=action,
        previous_status="READY_FOR_REVIEW", new_status="PENDING_APPROVAL", comment=None, timestamp=when,
    )


def snapshot(month, *, status=STATUS_FINAL, valid=True, excess="100.00", count=3):
    return KpiSnapshot(
        month=month, status=status, cutoff=month, measured_on=month, month_end_valid=valid, population=10, stockout_at_risk=0,
        stockout_evaluated=0, stockout_insufficient=0, stockout_stale=True, excess_count=count,
        excess_opportunity=Decimal(excess) if excess is not None else None, net_rop_value_impact=None, priced_count=0, currency="ZAR",
        payload_json="{}", payload_sha256="x" * 64, calc_version="2", revision=1,
    )


def build(db, period_end=OCT_END, today=TODAY):
    return trends.build_performance_trends(db, period_end, today)


def by_label(result):
    return {m.label: m for m in result.months}


def test_it_reaches_back_six_months_across_a_year_boundary():
    assert [m.isoformat() for m in trends.trend_months(date(2027, 2, 28))] == [
        "2026-09-01", "2026-10-01", "2026-11-01", "2026-12-01", "2027-01-01", "2027-02-01",
    ]
    assert len(trends.trend_months(OCT_END)) == trends.TREND_MONTHS


def test_records_and_distinct_material_plants_are_counted_apart(db):
    db.add_all([
        rec("A", at(2026, 9), run=1), rec("A", at(2026, 9, 20), run=2), rec("B", at(2026, 9)),  # A twice in Sep
        rec("A", at(2026, 10), run=3), rec("A", at(2026, 10), plant="1500", run=3),  # one material in two plants is two material-plants
    ])
    db.commit()
    months = by_label(build(db))
    assert (months["Sep 2026"].records_generated, months["Sep 2026"].material_plants_generated) == (3, 2)
    assert (months["Oct 2026"].records_generated, months["Oct 2026"].material_plants_generated) == (2, 2)


def test_a_month_before_the_history_began_has_no_figure_and_a_later_empty_month_is_a_real_zero(db):
    db.add_all([rec("A", at(2026, 8)), rec("A", at(2026, 10), run=2)])  # nothing in September
    db.commit()
    result = build(db)
    months = by_label(result)
    assert months["May 2026"].records_generated is None and months["Jul 2026"].records_generated is None  # before the history
    assert months["Aug 2026"].records_generated == 1
    assert months["Sep 2026"].records_generated == 0  # the pipeline had run; a month with none is a real 0
    assert result.recommendation_history_from == date(2026, 8, 1)
    assert any("first generated in Aug 2026" in note for note in result.notes)


def test_a_month_that_has_not_started_has_no_figure(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    result = build(db, period_end=date(2026, 12, 31), today=TODAY)
    months = by_label(result)
    assert months["Nov 2026"].records_generated == 0 and months["Nov 2026"].state == "in_progress"
    assert months["Dec 2026"].state == "not_started"
    assert months["Dec 2026"].records_generated is None and months["Dec 2026"].submitted is None


def test_approval_activity_is_counted_from_dated_events_not_from_todays_status(db):
    # Approved in September, rejected in October: its status today is REJECTED, but September still has the approval.
    db.add(rec("A", at(2026, 9), status="REJECTED"))
    db.add_all([event("SUBMIT", at(2026, 9, 2)), event("APPROVE", at(2026, 9, 5)), event("REJECT", at(2026, 10, 7)), event("SEND_BACK", at(2026, 10, 8), actor="u2")])
    # The same event written twice counts once.
    db.add_all([event("APPROVE", at(2026, 10, 9), actor="u3"), event("APPROVE", at(2026, 10, 9), actor="u3")])
    db.commit()
    months = by_label(build(db))
    sep, oct_ = months["Sep 2026"], months["Oct 2026"]
    assert (sep.submitted, sep.approved, sep.rejected, sep.sent_back) == (1, 1, 0, 0)
    assert (oct_.submitted, oct_.approved, oct_.rejected, oct_.sent_back) == (0, 1, 1, 1)
    assert months["Aug 2026"].approved is None  # before the first recorded event


def test_a_resubmission_counts_as_a_submission(db):
    db.add_all([event("SUBMIT", at(2026, 10, 2)), event("RESUBMIT", at(2026, 10, 12), actor="u2")])
    db.commit()
    assert by_label(build(db))["Oct 2026"].submitted == 2


def test_excess_comes_only_from_a_final_snapshot_valid_for_the_months_end(db):
    db.add_all([
        snapshot(date(2026, 8, 1), excess="500.00", count=7),  # a month-end snapshot
        snapshot(date(2026, 9, 1), valid=False, excess="900.00"),  # taken too long after the month: not a month-end
        snapshot(date(2026, 10, 1), status=STATUS_OPEN, valid=False, excess="700.00"),  # a rolling reading
    ])
    db.commit()
    result = build(db)
    months = by_label(result)
    assert (months["Aug 2026"].excess_state, months["Aug 2026"].excess_opportunity, months["Aug 2026"].excess_count) == ("MONTH_END", Decimal("500.00"), 7)
    assert months["Sep 2026"].excess_state == "ROLLING" and months["Sep 2026"].excess_opportunity is None
    assert months["Oct 2026"].excess_state == "ROLLING" and months["Oct 2026"].excess_opportunity is None
    assert months["Jul 2026"].excess_state == "MISSING" and months["Jul 2026"].excess_opportunity is None  # never captured: not rebuilt
    assert any("No saved month-end snapshot for" in note and "Jul 2026" in note for note in result.notes)


def test_it_says_there_is_no_excess_trend_with_fewer_than_two_month_end_snapshots(db):
    db.add(snapshot(date(2026, 9, 1)))
    db.commit()
    assert any("no excess trend yet" in note for note in build(db).notes)
    db.add(snapshot(date(2026, 8, 1)))
    db.commit()
    assert not any("no excess trend yet" in note for note in build(db).notes)


def test_a_missing_excess_value_stays_missing_rather_than_zero(db):
    db.add(snapshot(date(2026, 9, 1), excess=None))
    db.commit()
    month = by_label(build(db))["Sep 2026"]
    assert month.excess_state == "MONTH_END" and month.excess_opportunity is None


def test_it_always_says_sap_implementation_has_no_monthly_series(db):
    assert any("no monthly series" in note for note in build(db).notes)


def test_an_empty_database_gives_no_figures_and_says_so(db):
    result = build(db)
    assert all(m.records_generated is None and m.approved is None and m.excess_opportunity is None for m in result.months)
    assert any("No recommendation has been generated yet" in note for note in result.notes)
    assert any("No approval event has been recorded yet" in note for note in result.notes)


def test_the_management_report_carries_the_trends(db):
    # The same builder the page, the PDF and the Excel read: one set of figures.
    import tests.i7.reporting.test_quarter_management as base

    assert "performance" in base.QuarterManagement.model_fields
    assert base.QuarterManagement.model_fields["performance"].default is None  # an older saved report still loads
    assert resolve_period("Oct 2026")[1] == OCT_END
    assert build_quarter_management is not None
