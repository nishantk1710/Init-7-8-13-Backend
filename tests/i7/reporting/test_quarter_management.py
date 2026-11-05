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
from app.models.i7_staging import StagedConsumption, StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun
from app.models.i7_stockout_risk import StockoutRiskInput
from app.schemas.i7.reports import AvailabilityStatus, MetricKind, PeriodStatus, QuarterlyReport, QuarterManagement

TABLES = (
    Recommendation, ApprovalLedgerEntry, SapAdoptionResult, StagedConsumption, StagedMaterial, StagedMaterialPlant, StagedStock,
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


def test_approved_is_a_status_on_the_day_the_report_was_generated_not_at_the_quarters_end(db):
    db.add_all([
        rec("A", at(2026, 10), status="PENDING_APPROVAL"), rec("B", at(2026, 10), status="HELD"),
        rec("C", at(2026, 10)), rec("D", at(2026, 10), status="APPROVED"), rec("E", at(2026, 11), status="ADOPTED"),
    ])
    db.commit()
    management = build(db, AFTER_Q4)
    approved = card(management, "approved")
    assert approved.value == 2  # D approved, E taken on in SAP; pending and held ones are not
    assert approved.kind is MetricKind.STATUS_AS_OF
    assert approved.measured_on == AFTER_Q4  # the generation date; the quarter ended on 31 Dec
    assert approved.period_label == "as of 15 Jan 2027"
    assert approved.denominator == 5
    # What is waiting for a decision is not a top card: it is in the Approval status section, with every other stage.
    assert all(c.key != "pending_approval" for c in management.kpis)
    assert management.adoption.stages.awaiting_approval == 2  # A pending, B held
    assert management.adoption.pending_approvals == 2
    assert "SAP" in approved.explanation


def test_the_three_counts_are_records_then_distinct_material_plants_then_those_with_a_computed_recommendation(db):
    db.add_all([
        rec("A", at(2026, 10), run=1), rec("A", at(2026, 10, 20), run=2),  # A regenerated: two records, one material-plant
        rec("B", at(2026, 10)), rec("C", at(2026, 10), status="NOT_EVALUABLE"),  # C was looked at, nothing computed
    ])
    db.commit()
    management = build(db, AFTER_Q4)
    computed, records = card(management, "materials_assessed"), card(management, "recommendations_generated")
    assert records.value == 4 and records.unit == "recommendation records"
    assert computed.value == 2  # A and B
    assert computed.denominator == 3  # distinct material-plants looked at: A, B, C
    assert "distinct" in computed.denominator_label
    assert computed.title == "Material-plants with a computed recommendation"
    assert "1 of the 3" in computed.note  # C
    assert "4 records over 3 distinct material-plants" in records.note and "1 are repeat runs" in records.note


def test_stages_separate_approval_from_sap_and_add_up_to_the_records_generated(db):
    statuses = ["READY_FOR_REVIEW", "NOT_EVALUABLE", "PENDING_APPROVAL", "HELD", "ADJUSTED", "APPROVED", "SAP_EXECUTION_PENDING",
                "SAP_EXECUTED", "ADOPTED", "REJECTED", "SENT_BACK"]
    db.add_all([rec(f"M{i}", at(2026, 10), status=s) for i, s in enumerate(statuses)])
    db.commit()
    management = build(db, AFTER_Q4)
    stages = management.adoption.stages
    assert stages.total == 11 and stages.other == 0
    assert (stages.not_submitted, stages.awaiting_approval, stages.adjusted_in_chain) == (2, 2, 1)
    assert (stages.approved_awaiting_sap, stages.implemented_in_sap, stages.rejected_or_sent_back) == (2, 2, 2)
    assert sum([stages.not_submitted, stages.awaiting_approval, stages.adjusted_in_chain, stages.approved_awaiting_sap,
                stages.implemented_in_sap, stages.rejected_or_sent_back, stages.other]) == stages.total
    # The cards read the same sets as the stages.
    assert stages.awaiting_approval == management.adoption.pending_approvals == 2  # PENDING_APPROVAL and HELD
    assert card(management, "approved").value == stages.approved_awaiting_sap + stages.implemented_in_sap


def test_the_approval_card_sets_and_the_stage_sets_are_one_definition():
    approved = kpi_definitions.STAGE_STATUSES["approved_awaiting_sap"] + kpi_definitions.STAGE_STATUSES["implemented_in_sap"]
    assert set(approved) == set(kpi_definitions.APPROVED_STATUSES)
    assert kpi_definitions.STAGE_STATUSES["awaiting_approval"] == kpi_definitions.AWAITING_DECISION
    assert kpi_definitions.stage_counts({"SOMETHING_NEW": 3, "APPROVED": 1})["other"] == 3


def test_working_capital_is_zero_when_nothing_has_been_adopted_in_sap(db):
    # Priced, with both reorder points, and approved -- but SAP has not adopted it, so it has not taken effect: 0, not an estimate.
    db.add_all([rec("A", at(2026, 10), price=Decimal("10"), cur_rop=2, rec_rop=12, status="APPROVED")])
    db.commit()
    management = build(db, AFTER_Q4)
    for key in ("working_capital_impact", "working_capital_stock", "working_capital_oar"):
        working_capital = card(management, key)
        assert working_capital.status is AvailabilityStatus.AVAILABLE and working_capital.value == 0
        assert working_capital.denominator == 0


def test_working_capital_is_on_the_reorder_point_over_priced_rows_only(db):
    db.add_all([
        rec("A", at(2026, 10), price=Decimal("10"), cur_rop=2, rec_rop=12),   # 10 x (2 - 12) = -100
        rec("B", at(2026, 10), price=Decimal("3"), cur_rop=9, rec_rop=4),     # 3 x (9 - 4) = +15
        rec("C", at(2026, 10), price=None, cur_rop=1, rec_rop=100),           # no price: left out, not zero
        rec("D", at(2026, 10), price=Decimal("50"), cur_rop=1, rec_rop=2),    # priced in USD: left out, not converted
        rec("E", at(2026, 10), price=Decimal("7"), cur_rop=1, rec_rop=100),   # priced, but SAP has not adopted it: left out
        *[adoption(m, "ADOPTED") for m in ("A", "B", "C", "D")],
        # The currency each price is stated in (MBEW WAERS): only ZAR and NAD count.
        *[
            StagedMaterialPlant(sap_material_number=m, sap_plant_code="1300", unit_price=p, currency=c,
                                staging_run_id=1, source_table="n_mbew")
            for m, p, c in (("A", Decimal("10"), "ZAR"), ("B", Decimal("3"), "NAD"), ("D", Decimal("50"), "USD"))
        ],
    ])
    db.commit()
    management = build(db, AFTER_Q4)
    working_capital = card(management, "working_capital_impact")
    assert working_capital.value == Decimal("-85")
    assert working_capital.unit == "ZAR"
    assert working_capital.denominator == 2
    assert management.position.priced_count == 2
    assert management.position.working_capital_excluded_count == 2  # C (no price) and D (USD)


def test_working_capital_is_also_shown_apart_for_stock_items_and_oar_and_they_add_up(db):
    stock = rec("A", at(2026, 10), price=Decimal("10"), cur_rop=2, rec_rop=12)   # stock item: 10 x (2 - 12) = -100
    eligible = rec("B", at(2026, 10), price=Decimal("3"), cur_rop=9, rec_rop=4)  # OAR, eligible: +15
    unrequested = rec("C", at(2026, 10), price=Decimal("5"), cur_rop=9, rec_rop=4)  # OAR, not eligible, no request: left out
    not_adopted = rec("E", at(2026, 10), price=Decimal("4"), cur_rop=9, rec_rop=1)  # stock item SAP has not adopted: left out
    for row, verdict in ((eligible, "ELIGIBLE"), (unrequested, "NOT_ELIGIBLE")):
        row.is_oar, row.conversion_eligibility, row.manually_converted = True, verdict, False
    db.add_all([
        stock, eligible, unrequested, not_adopted, *[adoption(m, "ADOPTED") for m in ("A", "B", "C")],
        *[StagedMaterialPlant(sap_material_number=m, sap_plant_code="1300", unit_price=Decimal(p), currency="ZAR",
                              staging_run_id=1, source_table="n_mbew") for m, p in (("A", "10"), ("B", "3"), ("C", "5"), ("E", "4"))],
    ])
    db.commit()
    management = build(db, AFTER_Q4)
    assert card(management, "working_capital_stock").value == Decimal("-100")
    assert card(management, "working_capital_oar").value == Decimal("15")  # C is not eligible and has no request
    assert card(management, "working_capital_impact").value == Decimal("-85")  # the two add up to the total
    # If every eligible material-plant were adopted: A (-100), B (+15) and E (4 x (9 - 1) = +32); C is not eligible.
    assert card(management, "working_capital_potential").value == Decimal("-53")


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
    assert adoption.adoption_rate is None  # no evidence: not a measured 0%
    # SAP "pending" (2) covers an approved and a pending-approval recommendation alike; approval stages are separate.
    assert (adoption.stages.approved_awaiting_sap, adoption.stages.awaiting_approval) == (1, 1)


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
    assert adoption.adoption_rate == Decimal("0.5")  # 1 adopted of the 2 SAP has evidence for (the Overview's formula); C is pending, not a miss


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


# --- data coverage statuses -------------------------------------------------------------------


def coverage_items(management):
    return {item.key: item for item in management.data_coverage.items}


def test_each_source_gets_a_status_and_a_reason_saved_with_the_report(db, monkeypatch):
    monkeypatch.setattr(
        kpi_definitions, "stockout_risk_counts",
        lambda session, base, **kw: StockoutRiskCounts(at_risk=4, evaluated=40, insufficient_data=1, as_of=date(2026, 5, 31)),
    )
    db.add(rec("A", at(2026, 10)))
    db.commit()
    items = coverage_items(build(db, AFTER_Q4))
    assert items["stockout"].status == "Stale" and "31 May 2026" in items["stockout"].detail
    assert items["stockout"].value == "31 May 2026"
    assert items["snapshot"].status == "Not available" and "snapshot" in items["snapshot"].detail
    assert items["forecast"].status == "Not available"
    assert items["recommendation_date"].status == "Available" and items["recommendation_date"].value == "10 Oct 2026"
    assert items["recommendation_counts"].value == "1 · 1"
    assert items["currency"].status == "Available" and items["currency"].value == "ZAR"


def test_an_unbuilt_stockout_usage_and_no_stock_load_are_not_available_and_not_zero_risk(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    items = coverage_items(build(db, AFTER_Q4))
    assert items["stockout"].status == "Not available" and "not zero risk" in items["stockout"].detail
    assert items["stock_load"].status == "Not available"
    assert items["goods_issue"].status == "Not available"


def test_a_month_without_staged_consumption_is_not_covered_never_zero(db):
    db.add_all([rec("A", at(2026, 10)), consumed("A", date(2026, 9, 1))])
    db.commit()
    item = coverage_items(build(db, AFTER_Q4))["consumption_month"]
    assert item.status == "Not covered" and "never as zero" in item.detail


# --- OAR records and Min-Max conversion ---------------------------------------------------------


def oar(material, when, *, verdict="ELIGIBLE", manual=False, run=1, plant="1300", is_oar=True, valued=True, status="READY_FOR_REVIEW"):
    """An OAR recommendation. ``valued=False`` is one with no recommended value at all, as an eligible material the system could not size."""
    row = rec(material, when, run=run, plant=plant, rec_rop=5 if valued else None, status=status)
    row.is_oar, row.conversion_eligibility, row.manually_converted = is_oar, verdict, manual
    return row


def test_oar_card_counts_material_plants_once_not_records(db):
    db.add_all([
        oar("A", at(2026, 10), run=1), oar("A", at(2026, 10, 20), run=2),  # one OAR material-plant, two runs
        oar("B", at(2026, 10)), oar("N", at(2026, 10), is_oar=False, verdict=None),  # a stock item is not OAR
        oar("OLD", at(2026, 6)),  # another period
    ])
    db.commit()
    card_ = card(build(db, AFTER_Q4), "oar_recommendations")
    assert card_.value == 2 and card_.unit == "material-plants"  # A (once, on its newest run) and B
    assert card_.denominator == 2  # the OAR material-plants of the period
    assert "counts once" in card_.explanation


def test_oar_card_counts_eligible_only_until_a_not_eligible_one_is_submitted_for_approval(db):
    db.add_all([
        oar("E", at(2026, 10)),  # eligible: counts
        oar("ER", at(2026, 10), status="REJECTED"),  # eligible but rejected: not counted
        oar("N0", at(2026, 10), verdict="NOT_ELIGIBLE"),  # not eligible, no request: not counted
        oar("N1", at(2026, 10), verdict="NOT_ELIGIBLE", manual=True, status="PENDING_APPROVAL"),  # submitted: counts
        oar("N2", at(2026, 10), verdict="NOT_ELIGIBLE", manual=True, status="REJECTED"),  # request turned down: not counted
        oar("NV", at(2026, 10), verdict=None),  # no verdict: not counted
    ])
    db.commit()
    assert card(build(db, AFTER_Q4), "oar_recommendations").value == 2


def adoption(material, status, *, run=1, plant="1300"):
    return SapAdoptionResult(recommendation_id=f"REC-{material}-{plant}-{run}", status=status, evaluated_at=at(2026, 11))


def test_min_max_conversion_is_zero_when_nothing_is_converted_and_says_why(db):
    db.add_all([oar("E1", at(2026, 10)), oar("E2", at(2026, 10), plant="1500"), oar("NO", at(2026, 10), verdict="NOT_ELIGIBLE")])
    db.commit()
    c = card(build(db, AFTER_Q4), "min_max_conversion")
    assert c.value == 0 and c.status is AvailabilityStatus.AVAILABLE  # a material-plant counts only when SAP shows it converted
    assert c.unavailable_reason is None
    assert c.denominator == 2 and c.denominator_label == "eligible to convert, with recommended values"  # E1 and E2 may convert
    assert "converted is what SAP shows" in c.note
    assert "0 means none is confirmed converted" in c.note  # no SAP change evidence yet: the 0 is explained, not hidden


def test_min_max_conversion_counts_the_eligible_material_plants_sap_now_matches(db):
    db.add_all([
        oar("E1", at(2026, 10)), oar("E2", at(2026, 10), plant="1500"), oar("E3", at(2026, 10)), oar("NO", at(2026, 10), verdict="NOT_ELIGIBLE"),
        oar("ASKED", at(2026, 10), verdict="NOT_ELIGIBLE", manual=True), oar("STOCK", at(2026, 10), is_oar=False, verdict=None),
        oar("OLD", at(2026, 6)),  # generated in another period
    ])
    db.add_all([
        adoption("E1", "ADOPTED"), adoption("E2", "NOT_ADOPTED", plant="1500"),  # E3 has no result yet: pending, not converted
        adoption("ASKED", "ADOPTED"),  # converted, but the rule did not make it eligible: not counted here
        adoption("OLD", "ADOPTED"),  # another period
    ])
    db.commit()
    c = card(build(db, AFTER_Q4), "min_max_conversion")
    assert c.value == 1  # E1 only
    assert c.status is AvailabilityStatus.AVAILABLE and c.unavailable_reason is None
    assert "0 means none is confirmed converted" not in c.note  # SAP has evidence for some, so the note does not say that
    assert c.denominator == 3  # E1, E2, E3 are eligible
    assert c.kind is MetricKind.STATUS_AS_OF and c.period_label == "as of 15 Jan 2027"
    assert "1 more were not eligible" in c.note  # ASKED, apart from the counts
    counts = kpi_definitions.min_max_conversion(db, Q4_START, date(2027, 1, 1))
    assert (counts.eligible, counts.converted, counts.with_evidence, counts.oar_material_plants, counts.manually_requested) == (3, 1, 2, 5, 1)


def test_an_adoption_result_on_a_superseded_run_does_not_count_as_converted(db):
    db.add_all([oar("RERUN", at(2026, 10), run=1), oar("RERUN", at(2026, 10, 25), run=2)])
    db.add(adoption("RERUN", "ADOPTED", run=1))  # SAP matched the older run; the newest run has no result yet
    db.commit()
    counts = kpi_definitions.min_max_conversion(db, Q4_START, date(2027, 1, 1))
    assert (counts.eligible, counts.converted, counts.with_evidence) == (1, 0, 0)
    assert card(build(db, AFTER_Q4), "min_max_conversion").value == 0


def test_the_min_max_card_and_the_lists_behind_it_agree(db):
    """The counts use the Recommendations list's own filters, so each click-through list holds exactly the counted material-plants."""
    from app.api.i7 import recommendations as api

    db.add_all([
        oar("E1", at(2026, 10)), oar("E2", at(2026, 11), plant="1500"), oar("E3", at(2026, 10)), oar("NO", at(2026, 10), verdict="NOT_ELIGIBLE"),
        oar("EARLY", at(2026, 9)),  # before the period
    ])
    db.add_all([adoption("E1", "ADOPTED"), adoption("E2", "ADOPTED", plant="1500"), adoption("NO", "ADOPTED")])
    db.commit()

    def listed(**more):
        return api.list_recommendations(
            session=db, page=1, page_size=50, sort="generated_at", sort_desc=True, status=None, plant=None, material=None, demand_class=None,
            is_oar=None, confidence=None, criticality=None, generated_from=datetime(2026, 10, 1), generated_to=datetime(2027, 1, 1),
            demand_type="oar", search=None, criticality_level=None, status_group=None, risk=None, conversion_eligibility="ELIGIBLE", has_recommended=True, **more,
        )

    management = build(db, AFTER_Q4)
    converted, eligible = listed(adopted_in_sap=True), listed()
    c = card(management, "min_max_conversion")
    assert c.value == converted.total == 2 and c.denominator == eligible.total == 3
    assert sorted(item.material for item in converted.items) == ["E1", "E2"]  # NO was adopted but is not eligible
    assert sorted(item.material for item in eligible.items) == ["E1", "E2", "E3"]


def test_an_eligible_material_with_no_recommended_value_has_nothing_to_move_and_is_counted_apart(db):
    # The rule allows all four, but only two have a value worked out: the others cannot be moved to Min-Max yet.
    db.add_all([
        oar("HAS1", at(2026, 10)), oar("HAS2", at(2026, 10), plant="1500"),
        oar("NOVAL1", at(2026, 10), valued=False), oar("NOVAL2", at(2026, 10), valued=False, plant="1500"),
    ])
    db.add(adoption("HAS1", "ADOPTED"))
    db.commit()
    c = card(build(db, AFTER_Q4), "min_max_conversion")
    assert c.denominator == 2 and c.value == 1  # HAS1 and HAS2 can move; HAS1 has moved
    assert "2 more are eligible by the rule but have no recommended value yet" in c.note
    counts = kpi_definitions.min_max_conversion(db, Q4_START, date(2027, 1, 1))
    assert (counts.eligible, counts.without_values, counts.converted, counts.oar_material_plants) == (2, 2, 1, 4)


def test_without_any_eligible_value_the_note_stays_quiet_about_it(db):
    db.add_all([oar("A", at(2026, 10)), oar("B", at(2026, 10), plant="1500")])
    db.commit()
    assert "no recommended value yet" not in card(build(db, AFTER_Q4), "min_max_conversion").note
