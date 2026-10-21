"""The Quarterly Report's management view: activity by month, KPI cards, position, status distribution,
forecast by month, adoption and data coverage.

SQLite with only the tables these read, so they run without a database. The stockout measurement (which needs the
whole usage / stock / lead-time schema) is replaced by the counts it would return, since what is tested here is how
the report treats that measurement -- fresh, stale, or never built -- and not the stockout rule, which has its own tests.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.reporting import kpi_definitions
from app.initiatives.i7.reporting.period import resolve_quarter
from app.initiatives.i7.reporting.quarter_management import build_quarter_management
from app.initiatives.i7.reporting.source_activity import build_source_activity
from app.initiatives.i7.stockout_risk.counts import StockoutRiskCounts
from app.models.i7_features import MaterialFeature
from app.models.i7_forecast import ForecastBacktestPath, ForecastRun
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation, SapAdoptionResult
from app.models.i7_staging import StagedConsumption, StagedMaterial, StagedStock, StagingRun
from app.models.i7_stockout_risk import StockoutRiskInput
from app.schemas.i7.reports import AvailabilityStatus, MetricKind, PeriodStatus, QuarterlyReport, QuarterManagement

TABLES = (
    Recommendation, ApprovalLedgerEntry, SapAdoptionResult, StagedConsumption, StagedMaterial, StagedStock,
    StagingRun, StockoutRiskInput, MaterialFeature, ForecastRun, ForecastBacktestPath,
)

Q4_START, Q4_END = resolve_quarter("Q4 2026")
AFTER_Q4 = date(2027, 1, 15)  # Q4 2026 is complete
MID_Q4 = date(2026, 10, 21)  # Q4 2026 is in progress


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in TABLES:
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()  # the models declare some indexes twice; SQLite refuses that
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def at(year, month, day=10):
    return datetime(year, month, day, 9, 0, tzinfo=timezone.utc)


def rec(material, generated_at, *, plant="1300", status="READY_FOR_REVIEW", run=1, price=None, cur_rop=None, rec_rop=None,
        cur_max=None, rec_max=None):
    return Recommendation(
        recommendation_id=f"REC-{material}-{plant}-{run}", sap_material_number=material, sap_plant_code=plant,
        feature_run_id=run, forecast_run_id=run, inventory_run_id=run, oar_run_id=run, policy_id="p", policy_version=1,
        formula_version="f", status=status, impact_status="X", generated_at=generated_at, unit_price=price,
        current_rop=cur_rop, recommended_rop=rec_rop, current_max_stock=cur_max, recommended_max_stock=rec_max,
    )


def event(recommendation_id, action, when, *, version=1, actor="u1", new_status="PENDING_APPROVAL"):
    return ApprovalLedgerEntry(
        recommendation_id=recommendation_id, recommendation_version=version, actor_id=actor, actor_role="End User",
        action=action, previous_status="READY_FOR_REVIEW", new_status=new_status, comment=None, timestamp=when,
    )


def consumed(material, period, issues=2, plant="1300"):
    return StagedConsumption(
        sap_material_number=material, sap_plant_code=plant, period=period, quantity=Decimal("10"), unit_of_measure="EA",
        movement_count=issues, issue_count=issues, reversal_count=0, staging_run_id=1, source_table="n_mseg",
    )


def build(db, today, *, quarter="Q4 2026"):
    start, end = resolve_quarter(quarter)
    source = build_source_activity(db, start, end, recommendations_in_period=0)
    return build_quarter_management(db, start, end, today=today, source_activity=source)


def row(management, key):
    return next(r for r in management.monthly_activity.rows if r.key == key)


def card(management, key):
    return next(c for c in management.kpis if c.key == key)


@pytest.fixture(autouse=True)
def no_usage_build(monkeypatch):
    """By default the stockout usage has not been built."""
    monkeypatch.setattr(
        kpi_definitions, "stockout_risk_counts",
        lambda session, base, **kw: StockoutRiskCounts(at_risk=0, evaluated=0, insufficient_data=0, as_of=None),
    )


def usage_as_of(monkeypatch, as_of, *, at_risk=90, evaluated=800, insufficient=40):
    monkeypatch.setattr(
        kpi_definitions, "stockout_risk_counts",
        lambda session, base, **kw: StockoutRiskCounts(at_risk=at_risk, evaluated=evaluated, insufficient_data=insufficient, as_of=as_of),
    )


# --- the period ------------------------------------------------------------------------------


def test_a_closed_quarter_is_complete_and_a_running_one_is_in_progress(db):
    assert build(db, AFTER_Q4).period.status is PeriodStatus.COMPLETE
    running = build(db, MID_Q4)
    assert running.period.status is PeriodStatus.IN_PROGRESS
    assert running.period.cutoff == MID_Q4
    assert "in progress" in running.period.note


def test_the_three_months_of_q4_roll_over_the_year_correctly(db):
    months = [m.label for m in build(db, AFTER_Q4).period.months]
    assert months == ["Oct 2026", "Nov 2026", "Dec 2026"]
    assert [m.label for m in build(db, date(2027, 4, 5), quarter="Q1 2027").period.months] == ["Jan 2027", "Feb 2027", "Mar 2027"]


# --- monthly activity ------------------------------------------------------------------------


def test_recommendations_are_counted_in_the_month_they_were_generated_and_the_months_add_up(db):
    db.add_all([rec("A", at(2026, 10)), rec("B", at(2026, 10, 31)), rec("C", at(2026, 11)), rec("D", at(2026, 12, 31))])
    db.commit()
    generated = row(build(db, AFTER_Q4), "recommendations_generated")
    assert generated.months == [2, 1, 1]
    assert generated.quarter_total == 4
    assert generated.reconciles is True
    assert generated.kind is MetricKind.EVENT


def test_a_record_on_the_last_day_of_the_quarter_is_in_it_and_one_on_the_next_day_is_not(db):
    db.add_all([rec("A", datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)), rec("B", datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc))])
    db.commit()
    generated = row(build(db, AFTER_Q4), "recommendations_generated")
    assert generated.quarter_total == 1
    assert generated.months == [0, 0, 1]


def test_decisions_are_counted_from_dated_events_not_from_the_latest_status(db):
    """Approved in November and rejected in December is one approval in November and one rejection in December."""
    db.add(rec("A", at(2026, 10), status="REJECTED"))
    db.add_all([
        event("REC-A-1300-1", "SUBMIT", at(2026, 10, 20)),
        event("REC-A-1300-1", "APPROVE", at(2026, 11, 5), actor="u2"),
        event("REC-A-1300-1", "REJECT", at(2026, 12, 8), actor="u3", new_status="REJECTED"),
    ])
    db.commit()
    management = build(db, AFTER_Q4)
    assert row(management, "approval_decisions").months == [0, 1, 0]
    assert row(management, "rejections").months == [0, 0, 1]
    assert row(management, "submitted_for_approval").months == [1, 0, 0]
    assert row(management, "sent_back").quarter_total == 0


def test_a_retried_event_is_counted_once(db):
    db.add(rec("A", at(2026, 10)))
    db.add_all([event("REC-A-1300-1", "APPROVE", at(2026, 11, 5)) for _ in range(3)])  # the same event written three times
    db.add(event("REC-A-1300-1", "APPROVE", at(2026, 11, 5), actor="u9"))  # a different actor: a different decision
    db.commit()
    approvals = row(build(db, AFTER_Q4), "approval_decisions")
    assert approvals.months == [0, 2, 0]
    assert approvals.reconciles is True


def test_events_outside_the_quarter_are_not_counted(db):
    db.add_all([event("R1", "APPROVE", at(2026, 9, 30)), event("R2", "APPROVE", at(2027, 1, 2)), event("R3", "APPROVE", at(2026, 10, 1))])
    db.commit()
    assert row(build(db, AFTER_Q4), "approval_decisions").quarter_total == 1


def test_months_that_have_not_started_are_not_available_rather_than_zero(db):
    db.add(rec("A", at(2026, 10, 5)))
    db.commit()
    generated = row(build(db, MID_Q4), "recommendations_generated")
    assert generated.months == [1, None, None]
    assert generated.quarter_total == 1


def test_a_month_with_nothing_is_a_real_zero_but_an_untracked_series_is_not_available(db):
    management = build(db, AFTER_Q4)
    generated = row(management, "recommendations_generated")
    assert generated.months == [0, 0, 0]
    assert generated.quarter_total == 0
    adoption = row(management, "sap_adoptions_confirmed")
    assert adoption.months == [None, None, None]
    assert adoption.quarter_total is None
    assert adoption.status is AvailabilityStatus.UNKNOWN


def test_consumption_by_month_blanks_months_the_staged_data_does_not_cover(db):
    db.add_all([consumed("M1", date(2026, 10, 1), issues=5), consumed("M2", date(2026, 10, 1), issues=3)])
    db.add(consumed("M1", date(2026, 5, 1), issues=1))  # the staged data ends in October, so Nov and Dec are not covered
    db.add(consumed("M1", date(2026, 7, 1), issues=1))
    db.commit()
    consumption = row(build(db, AFTER_Q4), "sap_consumption_transactions")
    assert consumption.months == [8, None, None]
    assert consumption.quarter_total == 8
    assert consumption.reconciles is True


def test_a_covered_month_with_no_movements_is_zero_not_blank(db):
    db.add_all([consumed("M1", date(2026, 10, 1), issues=4), consumed("M1", date(2026, 12, 1), issues=2)])
    db.commit()
    consumption = row(build(db, AFTER_Q4), "sap_consumption_transactions")
    assert consumption.months == [4, 0, 2]  # November lies between covered months, so it is a confirmed zero


# --- forecast and actual demand --------------------------------------------------------------


def path(material, period, predicted, actual, *, run=1, step=1):
    return ForecastBacktestPath(
        forecast_run_id=run, sap_material_number=material, sap_plant_code="1300", model_name="SBA",
        origin_period=date(2026, 6, 1), horizon_step=step, forecast_period=period,
        predicted=Decimal(predicted), actual=Decimal(actual),
    )


def test_forecast_and_actual_are_compared_only_in_the_same_month_and_summed_per_month(db):
    db.add(ForecastRun(status="succeeded", policy_id="p", policy_version=1))
    db.add_all([
        path("A", date(2026, 10, 1), "10", "8"), path("B", date(2026, 10, 1), "5", "7"),
        path("A", date(2026, 11, 1), "12", "12"),
        path("A", date(2026, 9, 1), "99", "99"),  # a month outside the quarter
        path("A", date(2026, 10, 1), "77", "77", step=2),  # a later horizon predicts the same month again: not counted
    ])
    db.commit()
    forecast = build(db, AFTER_Q4).forecast_actual
    assert forecast.status is AvailabilityStatus.AVAILABLE
    assert [(m.forecast, m.actual, m.material_count) for m in forecast.months] == [
        (Decimal("15"), Decimal("15"), 2), (Decimal("12"), Decimal("12"), 1), (None, None, 0),
    ]
    assert forecast.forecast_total == Decimal("27")
    assert forecast.actual_total == Decimal("27")


def test_accuracy_is_taken_from_the_observations_not_from_monthly_percentages(db):
    db.add(ForecastRun(status="succeeded", policy_id="p", policy_version=1))
    db.add_all([path("A", date(2026, 10, 1), "10", "8"), path("B", date(2026, 10, 1), "5", "7"), path("A", date(2026, 11, 1), "12", "12")])
    db.commit()
    accuracy = build(db, AFTER_Q4).forecast_actual.accuracy
    assert accuracy.observations == 3
    assert accuracy.mean_absolute_error == Decimal("4") / Decimal("3")  # (2 + 2 + 0) / 3
    assert accuracy.bias_percentage == Decimal("0") / Decimal("27")  # (+2 - 2 + 0) over actual demand of 27


def test_no_forecast_run_is_not_available_not_zero(db):
    forecast = build(db, AFTER_Q4).forecast_actual
    assert forecast.status is AvailabilityStatus.NOT_AVAILABLE
    assert forecast.forecast_total is None and forecast.accuracy is None
    assert row(build(db, AFTER_Q4), "forecast_demand").months == [None, None, None]


# --- KPI cards -------------------------------------------------------------------------------


def test_materials_assessed_counts_distinct_material_plants_not_records(db):
    db.add_all([
        rec("A", at(2026, 10), run=1), rec("A", at(2026, 11), run=2),  # one material-plant, two records
        rec("B", at(2026, 10)), rec("C", at(2026, 10), status="NOT_EVALUABLE"),
    ])
    db.commit()
    management = build(db, AFTER_Q4)
    assessed = card(management, "materials_assessed")
    assert assessed.value == 2  # A and B; C was never computed
    assert assessed.denominator == 3
    assert card(management, "recommendations_generated").value == 4


def test_pending_approval_is_a_status_on_the_day_the_report_was_generated_not_at_the_quarters_end(db):
    db.add_all([rec("A", at(2026, 10), status="PENDING_APPROVAL"), rec("B", at(2026, 10), status="HELD"), rec("C", at(2026, 10)), rec("D", at(2026, 10), status="APPROVED")])
    db.commit()
    pending = card(build(db, AFTER_Q4), "pending_approval")
    assert pending.value == 2
    assert pending.kind is MetricKind.STATUS_AS_OF
    assert pending.measured_on == AFTER_Q4  # the generation date; the quarter ended on 31 Dec
    assert pending.period_label == "as of 15 Jan 2027"
    assert pending.denominator == 4


def test_working_capital_is_not_available_not_zero_when_nothing_has_a_price_and_both_reorder_points(db):
    db.add_all([rec("A", at(2026, 10), price=None, cur_rop=1, rec_rop=5), rec("B", at(2026, 10), price=Decimal("10"), cur_rop=None, rec_rop=5)])
    db.commit()
    working_capital = card(build(db, AFTER_Q4), "working_capital_impact")
    assert working_capital.status is AvailabilityStatus.NOT_AVAILABLE
    assert working_capital.value is None
    assert working_capital.unavailable_reason


def test_working_capital_is_on_the_reorder_point_over_priced_rows_only(db):
    db.add_all([
        rec("A", at(2026, 10), price=Decimal("10"), cur_rop=2, rec_rop=12),   # 10 x (2 - 12) = -100
        rec("B", at(2026, 10), price=Decimal("3"), cur_rop=9, rec_rop=4),     # 3 x (9 - 4) = +15
        rec("C", at(2026, 10), price=None, cur_rop=1, rec_rop=100),           # no price: left out, not zero
    ])
    db.commit()
    management = build(db, AFTER_Q4)
    working_capital = card(management, "working_capital_impact")
    assert working_capital.value == Decimal("-85")
    assert working_capital.denominator == 2
    assert management.position.priced_count == 2


def test_excess_opportunity_counts_only_where_current_max_is_above_the_recommended(db):
    db.add_all([
        rec("A", at(2026, 10), price=Decimal("2"), cur_max=100, rec_max=60),  # 2 x 40 = 80
        rec("B", at(2026, 10), price=Decimal("2"), cur_max=50, rec_max=60),   # not excess
        rec("C", at(2026, 10), price=Decimal("2"), cur_max=None, rec_max=10),  # no current: not excess
    ])
    db.commit()
    management = build(db, AFTER_Q4)
    assert management.position.excess_count == 1
    assert card(management, "excess_inventory_opportunity").value == Decimal("80")


def test_a_material_plant_regenerated_in_the_period_is_counted_once_in_the_position(db):
    db.add_all([
        rec("A", at(2026, 10), run=1, price=Decimal("10"), cur_max=100, rec_max=50),
        rec("A", at(2026, 11), run=2, price=Decimal("10"), cur_max=100, rec_max=50),
    ])
    db.commit()
    management = build(db, AFTER_Q4)
    assert management.position.population == 1
    assert management.position.excess_count == 1


def test_stockout_risk_is_not_available_when_the_usage_has_never_been_built(db):
    stockout = card(build(db, AFTER_Q4), "critical_stockout_risk")
    assert stockout.status is AvailabilityStatus.NOT_AVAILABLE
    assert stockout.value is None  # never 0 risk


def test_a_stale_stockout_measurement_is_not_shown_as_the_periods_position(db, monkeypatch):
    usage_as_of(monkeypatch, date(2026, 5, 31))
    management = build(db, AFTER_Q4)
    stockout = card(management, "critical_stockout_risk")
    assert stockout.status is AvailabilityStatus.NOT_AVAILABLE
    assert stockout.value is None
    assert "31 May 2026" in stockout.unavailable_reason
    assert management.position.stockout.stale is True
    assert management.position.stockout.at_risk == 90  # kept for the record, not presented
    assert any("31 May 2026" in w for w in management.data_coverage.warnings)


def test_a_fresh_stockout_measurement_is_available_with_its_date_and_denominator(db, monkeypatch):
    usage_as_of(monkeypatch, date(2026, 12, 31), at_risk=90, evaluated=800, insufficient=40)
    stockout = card(build(db, AFTER_Q4), "critical_stockout_risk")
    assert stockout.status is AvailabilityStatus.AVAILABLE
    assert stockout.value == 90
    assert stockout.measured_on == date(2026, 12, 31)
    assert stockout.denominator == 800
    assert stockout.kind is MetricKind.MEASUREMENT
    assert "40" in stockout.note


def test_a_measurement_within_a_month_of_the_cutoff_still_counts(db, monkeypatch):
    usage_as_of(monkeypatch, date(2026, 12, 5))  # the cutoff is 31 Dec: 26 days
    assert card(build(db, AFTER_Q4), "critical_stockout_risk").status is AvailabilityStatus.AVAILABLE


# --- status distribution ---------------------------------------------------------------------


def test_the_status_distribution_is_over_this_periods_recommendations_with_its_denominator(db):
    db.add_all([rec("A", at(2026, 10)), rec("B", at(2026, 10)), rec("C", at(2026, 11), status="APPROVED"), rec("D", at(2026, 12), status="REJECTED"), rec("E", at(2027, 1, 5))])
    db.commit()
    distribution = build(db, AFTER_Q4).status_distribution
    assert distribution.total == 4  # E is generated after the quarter
    assert {r.status: r.count for r in distribution.rows} == {"READY_FOR_REVIEW": 2, "APPROVED": 1, "REJECTED": 1}
    assert next(r for r in distribution.rows if r.status == "READY_FOR_REVIEW").percentage == Decimal("50.00")
    assert distribution.kind is MetricKind.STATUS_AS_OF
    assert distribution.measured_on == AFTER_Q4
    assert "current status" in distribution.basis.lower()


# --- SAP adoption ----------------------------------------------------------------------------


def test_adoption_is_not_measured_without_sap_evidence_and_an_approval_is_not_an_adoption(db):
    db.add_all([
        rec("A", at(2026, 10), status="APPROVED", cur_rop=1, rec_rop=5),
        rec("B", at(2026, 10), status="PENDING_APPROVAL", cur_rop=1, rec_rop=5),
    ])
    db.add(SapAdoptionResult(recommendation_id="REC-A-1300-1", status="UNKNOWN", evaluated_at=at(2026, 11)))
    db.commit()
    adoption = build(db, AFTER_Q4).adoption
    assert adoption.status is AvailabilityStatus.UNKNOWN
    assert adoption.confirmed_adoptions is None
    assert adoption.pending_approvals == 1
    assert "not an adoption" in adoption.reason
    # Nothing is adopted and nothing is "not adopted": every one with a change is simply pending.
    assert (adoption.tracked, adoption.adopted, adoption.not_adopted, adoption.pending) == (2, 0, 0, 2)


def test_adoption_counts_confirmed_results_for_this_periods_recommendations_only(db):
    db.add_all([
        rec("A", at(2026, 10), cur_rop=1, rec_rop=5), rec("B", at(2026, 10), cur_rop=1, rec_rop=5),
        rec("C", at(2026, 10), cur_rop=1, rec_rop=5), rec("OLD", at(2026, 6), cur_rop=1, rec_rop=5),
    ])
    db.add_all([
        SapAdoptionResult(recommendation_id="REC-A-1300-1", status="ADOPTED", evaluated_at=at(2026, 11)),
        SapAdoptionResult(recommendation_id="REC-B-1300-1", status="NOT_ADOPTED", evaluated_at=at(2026, 11)),
        SapAdoptionResult(recommendation_id="REC-OLD-1300-1", status="ADOPTED", evaluated_at=at(2026, 11)),
    ])
    db.commit()
    adoption = build(db, AFTER_Q4).adoption
    assert adoption.status is AvailabilityStatus.AVAILABLE
    assert adoption.confirmed_adoptions == 1
    assert (adoption.tracked, adoption.adopted, adoption.not_adopted, adoption.pending) == (3, 1, 1, 1)  # OLD is another period


def test_a_recommendation_with_nothing_to_adopt_is_not_counted_in_adoption(db):
    db.add_all([rec("SAME", at(2026, 10), cur_rop=5, rec_rop=5), rec("CHANGED", at(2026, 10), cur_rop=1, rec_rop=5)])
    db.commit()
    assert build(db, AFTER_Q4).adoption.tracked == 1


# --- data coverage ---------------------------------------------------------------------------


def test_coverage_names_the_months_the_staged_data_does_not_reach_and_never_calls_them_zero(db):
    db.add_all([consumed("M1", date(2026, 5, 1)), rec("A", at(2026, 10))])
    db.commit()
    coverage = build(db, AFTER_Q4).data_coverage
    assert coverage.latest_consumption_month == date(2026, 5, 1)
    assert any("May 2026" in w and "Oct 2026" in w and "never as zero" in w for w in coverage.warnings)
    assert coverage.eligible_recommendations == 1
    assert coverage.eligible_material_plants == 1
    assert coverage.basis == "current_calculation"
    assert [m.recommendations_generated for m in coverage.months] == [1, 0, 0]


def test_the_coverage_reads_the_dates_from_the_database_not_from_a_constant(db):
    db.add_all([consumed("M1", date(2026, 10, 1)), consumed("M1", date(2026, 12, 1))])
    db.commit()
    coverage = build(db, AFTER_Q4).data_coverage
    assert coverage.latest_consumption_month == date(2026, 12, 1)  # read from the staged data
    assert not any("no consumption data" in w for w in coverage.warnings)  # every month is covered, November as a real zero
    assert [m.consumption_status.value for m in coverage.months] == ["AVAILABLE", "NO_ACTIVITY", "AVAILABLE"]


# --- the report keeps loading ----------------------------------------------------------------


def test_the_management_view_survives_a_round_trip_through_json(db):
    management = build(db, AFTER_Q4)
    assert QuarterManagement.model_validate_json(management.model_dump_json()) == management


def test_a_report_stored_before_the_management_view_still_validates():
    assert QuarterlyReport.model_fields["management"].default is None


# --- the Overview's by-plant charts ----------------------------------------------------------


def test_the_position_is_given_for_every_plant_then_each_plant_the_overview_offers(db):
    db.add_all([
        rec("A", at(2026, 10), plant="1300", cur_max=100, rec_max=50, price=Decimal("2")),
        rec("B", at(2026, 10), plant="1300", cur_max=100, rec_max=50, price=Decimal("2")),
        rec("C", at(2026, 10), plant="1500", cur_max=100, rec_max=50, price=Decimal("2")),
        rec("D", at(2026, 10), plant="1500", cur_max=10, rec_max=50),
    ])
    db.commit()
    plants = build(db, AFTER_Q4).plant_positions
    assert [p.plant for p in plants] == [None, "1300", "1500"]
    assert [(p.figures.population, p.figures.excess_count) for p in plants] == [(4, 3), (2, 2), (2, 1)]


def test_the_plants_add_up_to_every_plant(db):
    db.add_all([rec(m, at(2026, 10), plant=p, cur_max=9, rec_max=1) for m, p in (("A", "1300"), ("B", "1500"), ("C", "1500"))])
    db.commit()
    all_plants, p1300, p1500 = build(db, AFTER_Q4).plant_positions
    assert p1300.figures.population + p1500.figures.population == all_plants.figures.population
    assert p1300.figures.excess_count + p1500.figures.excess_count == all_plants.figures.excess_count


def test_each_plants_stockout_is_asked_for_that_plant_only(db, monkeypatch):
    asked = []

    def counts(session, base, **kw):
        asked.append(kw.get("plant"))
        return StockoutRiskCounts(at_risk=1, evaluated=2, insufficient_data=0, as_of=date(2026, 12, 31))

    monkeypatch.setattr(kpi_definitions, "stockout_risk_counts", counts)
    build(db, AFTER_Q4)
    # All plants once -- the card and the chart read the same figures -- then each plant, never one plant's counts
    # standing in for another's.
    assert asked == [None, "1300", "1500"]
