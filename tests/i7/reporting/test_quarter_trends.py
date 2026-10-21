"""The report's monthly trends and quarter-end position, from saved snapshots.

What is checked is the rules: a month with no snapshot is blank (not zero and not rebuilt), a stale stockout reading is not
a trend point, nothing is summed across months, and the quarter-end position is the last month's snapshot only when it is
valid -- an earlier month is never relabelled as the quarter-end.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.reporting import kpi_definitions, snapshots
from app.initiatives.i7.reporting.period import resolve_quarter
from app.initiatives.i7.reporting.quarter_management import build_quarter_management
from app.initiatives.i7.reporting.source_activity import build_source_activity
from app.initiatives.i7.stockout_risk.counts import StockoutRiskCounts
from app.models.i7_features import MaterialFeature
from app.models.i7_forecast import ForecastBacktestPath, ForecastRun
from app.models.i7_kpi_snapshot import KpiSnapshot, KpiSnapshotLog
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation, SapAdoptionResult
from app.models.i7_staging import StagedConsumption, StagedMaterial, StagedStock, StagingRun
from app.models.i7_stockout_risk import StockoutRiskInput
from app.schemas.i7.reports import AvailabilityStatus, QuarterManagement

TABLES = (
    Recommendation, ApprovalLedgerEntry, SapAdoptionResult, StagedConsumption, StagedMaterial, StagedStock, StagingRun,
    StockoutRiskInput, MaterialFeature, ForecastRun, ForecastBacktestPath, KpiSnapshot, KpiSnapshotLog,
)
OCT, NOV, DEC = date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)


def make_db(tables=TABLES):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in tables:
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    return Session(engine)


@pytest.fixture
def db():
    with make_db() as session:
        yield session


def at(year, month, day=10):
    return datetime(year, month, day, 9, 0, tzinfo=timezone.utc)


def rec(material, generated_at, *, run=1, price=Decimal("10"), cur_rop=2, rec_rop=12, cur_max=100, rec_max=60):
    return Recommendation(
        recommendation_id=f"REC-{material}-1300-{run}", sap_material_number=material, sap_plant_code="1300", feature_run_id=run,
        forecast_run_id=run, inventory_run_id=run, oar_run_id=run, policy_id="p", policy_version=1, formula_version="f",
        status="READY_FOR_REVIEW", impact_status="X", generated_at=generated_at, unit_price=price, current_rop=cur_rop,
        recommended_rop=rec_rop, current_max_stock=cur_max, recommended_max_stock=rec_max,
    )


def usage(monkeypatch, as_of, *, at_risk=5, evaluated=50, insufficient=2):
    monkeypatch.setattr(
        kpi_definitions, "stockout_risk_counts",
        lambda session, base, **kw: StockoutRiskCounts(at_risk=at_risk, evaluated=evaluated, insufficient_data=insufficient, as_of=as_of),
    )


@pytest.fixture(autouse=True)
def no_usage(monkeypatch):
    usage(monkeypatch, None, at_risk=0, evaluated=0, insufficient=0)


def build(session, today, quarter="Q4 2026") -> QuarterManagement:
    start, end = resolve_quarter(quarter)
    source = build_source_activity(session, start, end, recommendations_in_period=0)
    return build_quarter_management(session, start, end, today=today, source_activity=source)


def card(management, key):
    return next(c for c in management.kpis if c.key == key)


def capture_all(db, monkeypatch=None, *, oct_today=date(2026, 11, 3), nov_today=date(2026, 12, 3), dec_today=date(2027, 1, 4)):
    snapshots.capture_month(db, OCT, today=oct_today)
    if nov_today:
        snapshots.capture_month(db, NOV, today=nov_today)
    if dec_today:
        snapshots.capture_month(db, DEC, today=dec_today)


# --- nothing captured ------------------------------------------------------------------------


def test_with_no_snapshots_every_month_is_missing_not_zero_and_there_is_no_quarter_end(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    management = build(db, date(2027, 1, 15))
    assert [p.state for p in management.trends.months] == ["MISSING", "MISSING", "MISSING"]
    for point in management.trends.months:
        assert point.excess_count is None and point.net_rop_value_impact is None and point.stockout_at_risk is None
        assert "cannot be rebuilt" in point.note
    end = management.trends.quarter_end
    assert end.status is AvailabilityStatus.NOT_AVAILABLE
    assert "No snapshot was captured for Dec 2026" in end.reason
    assert end.latest_available_month is None
    assert any("No saved snapshot for Oct 2026, Nov 2026, Dec 2026" in n for n in management.trends.notes)


def test_without_a_quarter_end_snapshot_the_cards_say_they_are_a_current_calculation(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    management = build(db, date(2027, 1, 15))
    assert management.position_basis == "current_calculation"
    for key in ("excess_inventory_opportunity", "working_capital_impact"):
        assert card(management, key).basis == "current_calculation"
        assert "not a quarter-end measurement" in card(management, key).note
    assert management.data_coverage.basis == "current_calculation"


def test_report_generation_does_not_fail_when_the_snapshot_tables_do_not_exist_yet():
    with make_db(tuple(t for t in TABLES if t not in (KpiSnapshot, KpiSnapshotLog))) as session:
        management = build(session, date(2027, 1, 15))
        assert management.trends.quarter_end.status is AvailabilityStatus.NOT_AVAILABLE
        assert "migration has not been applied" in management.trends.quarter_end.reason
        assert management.kpis  # the rest of the report is intact


# --- trends ----------------------------------------------------------------------------------


def test_each_month_shows_what_was_measured_then(db, monkeypatch):
    usage(monkeypatch, date(2026, 11, 2), at_risk=5, evaluated=50)
    db.add(rec("A", at(2026, 10, 5)))
    db.commit()
    snapshots.capture_month(db, OCT, today=date(2026, 11, 3))
    db.add(rec("B", at(2026, 11, 8)))
    db.commit()
    usage(monkeypatch, date(2026, 12, 2), at_risk=9, evaluated=60)
    snapshots.capture_month(db, NOV, today=date(2026, 12, 3))
    october, november, december = build(db, date(2027, 1, 15)).trends.months
    assert (october.population, october.stockout_at_risk, october.state) == (1, 5, "FINAL")
    assert (november.population, november.stockout_at_risk) == (2, 9)
    assert december.state == "MISSING" and december.stockout_at_risk is None


def test_the_months_are_never_summed_into_a_quarter_figure(db, monkeypatch):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    # Each month is measured with the usage as it was built around that month's end.
    for month, built, taken in ((OCT, date(2026, 10, 30), date(2026, 11, 3)), (NOV, date(2026, 11, 29), date(2026, 12, 3)),
                                (DEC, date(2026, 12, 30), date(2027, 1, 4))):
        usage(monkeypatch, built, at_risk=7, evaluated=40)
        snapshots.capture_month(db, month, today=taken)
    management = build(db, date(2027, 1, 15))
    assert [p.stockout_at_risk for p in management.trends.months] == [7, 7, 7]
    # The quarter-end figure is one month's position, not 21.
    assert card(management, "critical_stockout_risk").value == 7


def test_a_stale_stockout_reading_is_not_a_trend_point_but_excess_still_is(db, monkeypatch):
    usage(monkeypatch, date(2026, 5, 31), at_risk=891, evaluated=9916)
    db.add(rec("A", at(2026, 10)))
    db.commit()
    snapshots.capture_month(db, OCT, today=date(2026, 11, 3))
    october = build(db, date(2027, 1, 15)).trends.months[0]
    assert october.stockout_valid is False
    assert october.stockout_at_risk is None and october.stockout_evaluated is None
    assert "31 May 2026" in october.note
    assert october.excess_count == 1  # not dependent on the usage build
    assert october.excess_opportunity == Decimal("400")


def test_an_open_month_is_a_rolling_reading_and_says_so(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    snapshots.capture_month(db, OCT, today=date(2026, 10, 21))
    october = build(db, date(2026, 10, 21)).trends.months[0]
    assert (october.state, october.month_end_valid) == ("OPEN", False)
    assert "rolling reading" in october.note


def test_the_working_capital_change_is_between_months_with_the_same_calculation_only(db):
    db.add(rec("A", at(2026, 10), cur_rop=2, rec_rop=12))  # 10 x (2 - 12) = -100
    db.commit()
    snapshots.capture_month(db, OCT, today=date(2026, 11, 3))
    db.add(rec("A", at(2026, 11, 5), run=2, cur_rop=2, rec_rop=32))  # 10 x (2 - 32) = -300
    db.commit()
    snapshots.capture_month(db, NOV, today=date(2026, 12, 3))
    october, november, _ = build(db, date(2027, 1, 15)).trends.months
    assert october.net_rop_value_impact == Decimal("-100") and october.net_rop_change_from_previous is None
    assert november.net_rop_value_impact == Decimal("-300")
    assert november.net_rop_change_from_previous == Decimal("-200")  # more cash tied up than last month

    row = db.execute(select(KpiSnapshot).where(KpiSnapshot.month == NOV)).scalar_one()
    row.calc_version = "2"  # made with a different calculation: not comparable
    db.commit()
    assert build(db, date(2027, 1, 15)).trends.months[1].net_rop_change_from_previous is None


def test_the_trend_never_calls_an_estimate_a_realised_saving(db):
    notes = build(db, date(2027, 1, 15)).trends.notes
    assert any("Realised savings are not measured" in n for n in notes)


# --- the quarter-end position ----------------------------------------------------------------


def test_the_last_months_valid_snapshot_is_the_quarter_end_and_the_cards_and_charts_read_it(db, monkeypatch):
    usage(monkeypatch, date(2026, 12, 30), at_risk=7, evaluated=40)
    db.add(rec("A", at(2026, 10), cur_max=100, rec_max=60))
    db.commit()
    capture_all(db)
    db.add(rec("A", at(2027, 1, 10), run=2, cur_max=10, rec_max=60))  # a later change must not move the quarter-end
    db.commit()
    management = build(db, date(2027, 1, 15))
    end = management.trends.quarter_end
    assert end.status is AvailabilityStatus.AVAILABLE and end.label == "Dec 2026"
    assert (end.snapshot_state, end.measured_on) == ("FINAL", date(2027, 1, 4))
    assert management.position_basis == "snapshot"
    assert management.position.excess_count == 1 and management.plant_positions[0].figures.excess_count == 1
    stockout = card(management, "critical_stockout_risk")
    assert (stockout.value, stockout.basis) == (7, "snapshot")
    excess = card(management, "excess_inventory_opportunity")
    assert (excess.basis, excess.period_label, excess.measured_on) == ("snapshot", "Dec 2026 month-end", date(2027, 1, 4))
    assert management.data_coverage.basis == "snapshot"
    assert any("saved Dec 2026 month-end snapshot" in w for w in management.data_coverage.warnings)
    assert {lvl.key: (lvl.aggregation, lvl.records) for lvl in end.stock_levels} == {
        "safety_stock": ("mean", 0), "reorder_point": ("mean", 1), "max_stock": ("mean", 1),
    }


def test_an_earlier_months_snapshot_is_never_relabelled_as_the_quarter_end(db, monkeypatch):
    usage(monkeypatch, date(2026, 11, 30), at_risk=7, evaluated=40)
    db.add(rec("A", at(2026, 10)))
    db.commit()
    capture_all(db, dec_today=None)  # October and November captured, December not
    management = build(db, date(2027, 1, 15))
    end = management.trends.quarter_end
    assert end.status is AvailabilityStatus.NOT_AVAILABLE
    assert "No snapshot was captured for Dec 2026" in end.reason
    assert (end.latest_available_label, end.latest_available_measured_on) == ("Nov 2026", date(2026, 12, 3))
    assert management.position_basis == "current_calculation"
    assert card(management, "critical_stockout_risk").basis == "current_calculation"


def test_a_final_month_snapshot_measured_long_after_the_month_is_not_a_quarter_end(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    snapshots.capture_month(db, DEC, today=date(2027, 3, 1))
    end = build(db, date(2027, 3, 2)).trends.quarter_end
    assert end.status is AvailabilityStatus.NOT_AVAILABLE
    assert "days after the month ended" in end.reason and "too late" in end.reason


def test_a_quarter_still_running_has_no_quarter_end_position(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    snapshots.capture_month(db, OCT, today=date(2026, 10, 21))
    management = build(db, date(2026, 10, 21))
    assert management.trends.quarter_end.status is AvailabilityStatus.NOT_AVAILABLE
    assert management.trends.months[0].state == "OPEN"
    assert management.position_basis == "current_calculation"


def test_a_stale_stockout_in_the_quarter_end_snapshot_leaves_that_card_unavailable_but_not_the_others(db, monkeypatch):
    usage(monkeypatch, date(2026, 5, 31), at_risk=891, evaluated=9916)
    db.add(rec("A", at(2026, 10)))
    db.commit()
    capture_all(db)
    management = build(db, date(2027, 1, 15))
    assert management.trends.quarter_end.status is AvailabilityStatus.AVAILABLE
    assert card(management, "critical_stockout_risk").status is AvailabilityStatus.NOT_AVAILABLE  # never shown as 0 risk
    assert card(management, "excess_inventory_opportunity").status is AvailabilityStatus.AVAILABLE


def test_the_management_view_with_trends_survives_a_round_trip_through_json(db, monkeypatch):
    usage(monkeypatch, date(2026, 12, 30))
    db.add(rec("A", at(2026, 10)))
    db.commit()
    capture_all(db)
    management = build(db, date(2027, 1, 15))
    assert QuarterManagement.model_validate_json(management.model_dump_json()) == management
