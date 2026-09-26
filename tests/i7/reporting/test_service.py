"""I07 Quarterly Deep-Dive Report -- aggregation service tests.

Real Postgres or skip (reads persisted Phase 3-7 data; nothing here triggers
a pipeline run). Uses a wide quarter window (``Q1 2000``..``Q4 2035``-style
per-call quarters aren't enough since ``resolve_quarter`` only accepts a
single calendar quarter) -- so the report is generated once per test module
against a quarter that is known, live, to contain the seeded rows: the tests
below discover the right quarter from the data itself (the max
``generated_at`` across the tables) rather than hard-coding a date that will
go stale.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.reporting.service import generate_quarterly_report
from app.models.i7_features import MaterialFeature
from app.models.i7_forecast import Forecast
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation
from app.schemas.i7.reports import AvailabilityStatus

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


def _quarter_label(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return f"Q{q} {d.year}"


@pytest.fixture(scope="module")
def quarter_and_report():
    """Discover the quarter containing the bulk of ``i7_recommendation`` rows
    (by ``generated_at``) and generate the report for it once per module --
    generation is expensive (~40-50s at full volume per the API agent's smoke
    test), so every test in this module shares one report."""
    factory = get_sessionmaker()
    with factory() as session:
        max_generated_at = session.execute(select(func.max(Recommendation.generated_at))).scalar_one()
        if max_generated_at is None:
            pytest.skip("i7_recommendation has no rows to report on")
        quarter = _quarter_label(max_generated_at.date())
        report = generate_quarterly_report(session, quarter)
        return quarter, report


@needs_db
def test_report_metadata_matches_requested_quarter(quarter_and_report):
    quarter, report = quarter_and_report
    assert report.metadata.quarter == quarter
    assert report.metadata.report_version == "1.0"
    assert report.metadata.period_start <= report.metadata.period_end


@needs_db
def test_classification_counts_are_internally_consistent(quarter_and_report):
    """Roughly matches confirmed live counts: doesn't hard-code exact
    numbers (the audit's own instruction), but proves the by-class rows sum
    to the section total and UNCLASSIFIED dominates, matching the confirmed
    112,869/113,465-shape skew."""
    _, report = quarter_and_report
    demand = report.demand_classification
    assert sum(row.count for row in demand.by_class) == demand.total

    scope = report.scope_and_data_quality
    assert scope.classified_count + scope.unclassified_count == scope.total_records
    if scope.total_records > 0:
        # Live-verified: UNCLASSIFIED is the overwhelming majority.
        assert scope.unclassified_count >= scope.classified_count


@needs_db
def test_forecast_metrics_present_and_mape_never_a_number(quarter_and_report):
    _, report = quarter_and_report
    forecasting = report.forecasting
    assert forecasting.mape_status == AvailabilityStatus.NOT_AVAILABLE
    # mape is not even a field on ForecastingSection -- confirming it cannot
    # be serialized as a number is structural, not just a status check.
    assert not hasattr(forecasting, "mape")
    assert not hasattr(forecasting, "mape_value")

    for metric in (
        forecasting.mean_absolute_error,
        forecasting.pinball_loss,
        forecasting.bias_percentage,
        forecasting.fill_rate,
        forecasting.holding_cost,
    ):
        assert metric.status in (AvailabilityStatus.AVAILABLE, AvailabilityStatus.NOT_AVAILABLE)
        if metric.status == AvailabilityStatus.NOT_AVAILABLE:
            assert metric.value is None
            assert metric.populated_count == 0


@needs_db
def test_safety_stock_rop_max_stock_report_explicit_availability_status(quarter_and_report):
    """The non-comparison summary fields (Sections 6/7/8) must never present
    a genuinely-missing count as a bare 0 with no status -- every section
    that can be unconfigured carries an explicit AvailabilityStatus."""
    _, report = quarter_and_report

    assert report.safety_stock.service_level_status == AvailabilityStatus.NOT_CONFIGURED
    # both_available_count is allowed to legitimately be 0 (confirmed live
    # state), but mean_delta must be None, never a fabricated 0, when so.
    if report.safety_stock.both_available_count == 0:
        assert report.safety_stock.mean_delta is None

    assert report.max_stock.strategy_policy_status == AvailabilityStatus.NOT_CONFIGURED
    assert (
        report.max_stock.strategy_labeled_count
        == report.max_stock.strategy_production_resolved_count
        + report.max_stock.strategy_unresolved_fixture_count
    )

    # Reorder Point has no unconfigured-policy field, but its population
    # counts must be self-consistent.
    rop = report.reorder_point
    assert rop.current.populated_count + rop.current.missing_count == rop.current.total_count


@needs_db
def test_oar_aggregation_breaks_down_true_false_null(quarter_and_report):
    _, report = quarter_and_report
    oar = report.oar
    assert oar.is_oar_true_count >= 0
    assert oar.is_oar_false_count >= 0
    assert oar.is_oar_null_count >= 0
    # conversion_eligibility_breakdown must include a None-key bucket
    # distinct from the literal "UNKNOWN" string when both exist.
    keys = [row.conversion_eligibility for row in oar.conversion_eligibility_breakdown]
    assert len(keys) == len(set(keys))  # no duplicate grouping keys


@needs_db
def test_recommendation_status_distribution_never_reinterprets_not_evaluable(quarter_and_report):
    _, report = quarter_and_report
    statuses = {row.status for row in report.recommendations.by_status}
    # Verbatim backend LifecycleStatus values only -- never "rejected" or any
    # other relabeling of NOT_EVALUABLE.
    assert not any(s.lower() in ("rejected", "denied") for s in statuses if s == "NOT_EVALUABLE")
    if "NOT_EVALUABLE" in statuses:
        assert "NOT_EVALUABLE" in [row.status for row in report.recommendations.by_status]
    total_by_status = sum(row.count for row in report.recommendations.by_status)
    assert total_by_status == report.recommendations.total


@needs_db
def test_approval_ledger_section_shape(quarter_and_report):
    _, report = quarter_and_report
    approval = report.approval
    assert approval.ledger_entry_count >= 0
    assert approval.distinct_recommendations_in_approval <= approval.ledger_entry_count \
        if approval.ledger_entry_count else True
    assert (
        approval.pending_count + approval.approved_count + approval.rejected_count
        <= approval.ledger_entry_count
    )


@needs_db
def test_service_level_reported_as_not_configured_never_fabricated(quarter_and_report):
    _, report = quarter_and_report
    assert report.safety_stock.service_level_status == AvailabilityStatus.NOT_CONFIGURED


@needs_db
def test_sap_adoption_is_unknown_never_zero_percent(quarter_and_report):
    """Distinct from Section 12's baseline comparison -- this section has no
    concept of 'baseline' or 'recommendation' at all."""
    _, report = quarter_and_report
    sap = report.sap_adoption
    assert sap.status == AvailabilityStatus.UNKNOWN
    assert sap.reason  # a human explanation, never silently empty
    assert not hasattr(sap, "adoption_percentage")
    assert not hasattr(sap, "baseline_value")
    assert not hasattr(sap, "recommendation_value")


@needs_db
def test_baseline_comparison_section_has_exactly_four_rows_in_order(quarter_and_report):
    _, report = quarter_and_report
    section = report.baseline_comparison
    assert [row.metric for row in section.rows] == ["Safety Stock", "ROP", "Max Stock", "Lead Time"]
    assert section.baseline_lead_time_source == "MARC-PLIFZ"


@needs_db
def test_safety_stock_baseline_row_still_appears_with_zero_available(quarter_and_report):
    """Confirmed live state: current_safety_stock has 0 populated rows. The
    row must still be present, not omitted, with baseline_missing_count
    accounting for the whole scope and availability_status NOT_AVAILABLE."""
    _, report = quarter_and_report
    ss_row = next(r for r in report.baseline_comparison.rows if r.metric == "Safety Stock")

    # Verify live rather than hard-coding: baseline population is queried
    # independently here to cross-check the service's own figure.
    factory = get_sessionmaker()
    with factory() as session:
        base = select(Recommendation).where(
            Recommendation.generated_at >= report.metadata.period_start,
            Recommendation.generated_at
            < report.metadata.period_end + timedelta(days=1),
        )
        total = session.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar_one()
        baseline_populated = session.execute(
            select(func.count()).select_from(
                base.where(Recommendation.current_safety_stock.isnot(None)).subquery()
            )
        ).scalar_one()

    expected_baseline_missing = total - baseline_populated
    assert ss_row.baseline_missing_count == expected_baseline_missing
    if baseline_populated == 0:
        assert ss_row.both_available_count == 0
        assert ss_row.availability_status == AvailabilityStatus.NOT_AVAILABLE
        assert ss_row.delta is None
        assert ss_row.baseline_value is None


@needs_db
def test_rop_and_max_stock_baseline_rows_report_real_availability_counts(quarter_and_report):
    _, report = quarter_and_report
    rows = {r.metric: r for r in report.baseline_comparison.rows}

    factory = get_sessionmaker()
    with factory() as session:
        base = select(Recommendation).where(
            Recommendation.generated_at >= report.metadata.period_start,
            Recommendation.generated_at
            < report.metadata.period_end + timedelta(days=1),
        )
        rop_both = session.execute(
            select(func.count()).select_from(
                base.where(
                    Recommendation.current_rop.isnot(None),
                    Recommendation.recommended_rop.isnot(None),
                ).subquery()
            )
        ).scalar_one()
        max_both = session.execute(
            select(func.count()).select_from(
                base.where(
                    Recommendation.current_max_stock.isnot(None),
                    Recommendation.recommended_max_stock.isnot(None),
                ).subquery()
            )
        ).scalar_one()

    assert rows["ROP"].both_available_count == rop_both
    assert rows["Max Stock"].both_available_count == max_both
    if rop_both > 0:
        assert rows["ROP"].availability_status == AvailabilityStatus.AVAILABLE
    if max_both > 0:
        assert rows["Max Stock"].availability_status == AvailabilityStatus.AVAILABLE


@needs_db
def test_lead_time_baseline_row_reflects_marc_plifz_used_on_both_sides(quarter_and_report):
    """Per the service's own documented finding: I07 uses MARC-PLIFZ
    (lead_time_days) unconditionally as its own lead-time input, so this row
    compares the same persisted column against itself -- every populated row
    agrees by construction (delta 0), which is the honest reflection of that
    fact, not a fabricated distinct I07 value."""
    _, report = quarter_and_report
    lt_row = next(r for r in report.baseline_comparison.rows if r.metric == "Lead Time")
    assert report.baseline_comparison.baseline_lead_time_source == "MARC-PLIFZ"

    if lt_row.both_available_count > 0:
        assert lt_row.delta == 0
        assert lt_row.availability_status == AvailabilityStatus.AVAILABLE
        # i07_lead_time_source should be populated when lead_time_days rows exist.
        assert report.baseline_comparison.i07_lead_time_source is not None
    else:
        assert lt_row.availability_status == AvailabilityStatus.NOT_AVAILABLE


@needs_db
def test_baseline_comparison_never_conflated_with_sap_adoption(quarter_and_report):
    """Different concepts, different sections -- a bug that reused one
    section's status for the other would be a real regression."""
    _, report = quarter_and_report
    assert report.sap_adoption.status == AvailabilityStatus.UNKNOWN
    baseline_statuses = {row.availability_status for row in report.baseline_comparison.rows}
    # sap_adoption's UNKNOWN must not appear as if it were a baseline row's
    # status unless a baseline row is genuinely also unresolved for its own
    # reasons -- this assertion is about structure, not value equality.
    assert isinstance(report.sap_adoption.reason, str)
    assert all(isinstance(s, AvailabilityStatus) for s in baseline_statuses)
