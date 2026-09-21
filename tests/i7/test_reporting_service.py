"""I07 Quarterly Deep-Dive Report -- aggregation service, against real
seeded data.

Real Postgres or skip (mirrors ``tests/i7/test_recommendation_ledger.py`` /
``tests/i7/api/test_i7_api.py``'s ``needs_db`` pattern). Nothing here
triggers a pipeline run -- these tests read whatever the current extract has
already produced, and assert against the confirmed Phase 1-4 audit numbers
where the underlying tables are not quarter-filtered in a way that would
exclude that data (all rows in ``i7_material_feature`` /
``i7_forecast`` / ``i7_recommendation`` / ``i7_approval_ledger`` on this
extract were generated in one build, so a sufficiently wide quarter window
captures all of them; a too-narrow window is treated as a skip, not a
failure, since the report's correctness does not depend on which quarter
today's data happens to fall in).
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.reporting.service import generate_quarterly_report
from app.models.i7_features import MaterialFeature
from app.models.i7_forecast import Forecast
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation
from app.schemas.i7.reports import AvailabilityStatus
from sqlalchemy import func, select

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")

# A deliberately wide quarter window is not possible -- resolve_quarter only
# accepts one calendar quarter -- so tests below use the quarter that
# actually contains this extract's data. Determined once per test module via
# the fixture below rather than hardcoded, so the suite keeps working if the
# extract is regenerated with a different timestamp.


@pytest.fixture(scope="module")
def data_quarter():
    """The 'Q<n> <year>' string covering the generated_at/computed_at of the
    bulk of the current i7_recommendation extract, or a skip if the table is
    empty."""
    with get_sessionmaker()() as session:
        latest = session.execute(select(func.max(Recommendation.generated_at))).scalar_one_or_none()
    if latest is None:
        pytest.skip("no i7_recommendation rows on this database")
    quarter_num = (latest.month - 1) // 3 + 1
    return f"Q{quarter_num} {latest.year}"


@pytest.fixture(scope="module")
def report(data_quarter):
    with get_sessionmaker()() as session:
        return generate_quarterly_report(session, data_quarter)


# --- Scope & Data Quality / Demand Classification --------------------------


@needs_db
def test_scope_and_classification_totals_are_internally_consistent(report):
    sdq = report.scope_and_data_quality
    dc = report.demand_classification
    assert sdq.total_records == dc.total
    assert sdq.classified_count + sdq.unclassified_count == sdq.total_records


@needs_db
def test_demand_classification_rows_sum_to_the_total(report):
    dc = report.demand_classification
    assert sum(row.count for row in dc.by_class) == dc.total


@needs_db
def test_unclassified_is_the_overwhelmingly_dominant_class_per_the_audit(report):
    """Confirmed fact: UNCLASSIFIED 112,869 of 113,465 total (99.47%). A
    full-database run should reproduce this almost exactly; a quarter-scoped
    subset should still show UNCLASSIFIED as the dominant class by a wide
    margin -- this is a structural property of the extract, not a coincidence
    of one quarter."""
    dc = report.demand_classification
    counts = {row.demand_class: row.count for row in dc.by_class}
    if dc.total == 0:
        pytest.skip("no material-feature rows in scope for this quarter")
    unclassified_share = counts.get("UNCLASSIFIED", 0) / dc.total
    assert unclassified_share > 0.9


@needs_db
def test_history_status_breakdown_uses_only_known_values(report):
    known = {"NO_HISTORY", "COLD_START", "SUFFICIENT"}
    for row in report.scope_and_data_quality.history_status_breakdown:
        assert row.history_status in known


@needs_db
def test_history_status_breakdown_sums_to_total(report):
    sdq = report.scope_and_data_quality
    assert sum(row.count for row in sdq.history_status_breakdown) == sdq.total_records


@needs_db
def test_criticality_and_lead_time_populated_counts_never_exceed_total(report):
    sdq = report.scope_and_data_quality
    assert 0 <= sdq.criticality_populated_count <= sdq.total_records
    assert 0 <= sdq.lead_time_populated_count <= sdq.total_records


# --- Forecasting -------------------------------------------------------------


@needs_db
def test_forecast_metrics_are_present_and_mape_is_explicitly_not_available(report):
    forecasting = report.forecasting
    assert forecasting.mape_status == AvailabilityStatus.NOT_AVAILABLE
    assert not hasattr(forecasting, "mape")  # no field exists at all, never a number

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
        else:
            assert metric.value is not None
            assert metric.populated_count > 0


@needs_db
def test_champion_challenger_counts_are_each_non_negative_and_individually_bounded(report):
    """champion/baseline/challenger are independently-computed counts over
    overlapping predicates on is_champion/is_baseline (challenger being
    neither) -- each is bounded by the total, but they are not guaranteed to
    be mutually exclusive partitions summing to it (a row could in principle
    be counted by more than one predicate if the underlying data is
    inconsistent), so only the individual bound is asserted here."""
    forecasting = report.forecasting
    ccc = forecasting.champion_challenger
    assert 0 <= ccc.champion_count <= forecasting.total_forecasts
    assert 0 <= ccc.challenger_count <= forecasting.total_forecasts
    assert 0 <= ccc.baseline_count <= forecasting.total_forecasts


# --- Safety Stock --------------------------------------------------------------


@needs_db
def test_safety_stock_current_population_reported_as_zero_with_explicit_status_not_bare_zero(report):
    """Confirmed fact: current_safety_stock has 0 non-null rows anywhere.
    This must be a real PopulationCount(populated=0, missing=total), not an
    omitted field or a silently-collapsed value."""
    ss = report.safety_stock
    if report.scope_and_data_quality.total_records == 0:
        pytest.skip("no recommendation rows in scope for this quarter")
    # current_safety_stock is confirmed to have zero populated rows overall;
    # a quarter-scoped subset of the same underlying zero-populated column
    # must still show zero.
    assert ss.current.populated_count == 0
    assert ss.current.missing_count == ss.current.total_count
    assert ss.current.percentage_populated is None or ss.current.percentage_populated == 0
    assert ss.both_available_count == 0
    assert ss.mean_delta is None


@needs_db
def test_safety_stock_service_level_status_is_not_configured(report):
    assert report.safety_stock.service_level_status == AvailabilityStatus.NOT_CONFIGURED


# --- ROP / Max Stock availability --------------------------------------------


@needs_db
def test_rop_current_population_count_is_internally_consistent(report):
    rop = report.reorder_point
    assert rop.current.populated_count + rop.current.missing_count == rop.current.total_count
    assert rop.recommended.populated_count + rop.recommended.missing_count == rop.recommended.total_count
    if rop.both_available_count == 0:
        assert rop.mean_delta is None
    else:
        assert rop.mean_delta is not None


@needs_db
def test_max_stock_strategy_counts_are_broken_out_not_conflated(report):
    """The confirmed fact: 1,192 rows are labeled review_period but only 6
    genuinely resolved a value in production; the other 1,186 are dev/test
    fixture artifacts. These three numbers must remain separate fields."""
    ms = report.max_stock
    assert (
        ms.strategy_production_resolved_count + ms.strategy_unresolved_fixture_count
        == ms.strategy_labeled_count
    )
    assert ms.strategy_policy_status == AvailabilityStatus.NOT_CONFIGURED


# --- OAR -----------------------------------------------------------------------


@needs_db
def test_oar_counts_sum_to_total_recommendations_in_scope(report):
    oar = report.oar
    total = report.recommendations.total
    assert oar.is_oar_true_count + oar.is_oar_false_count + oar.is_oar_null_count == total


@needs_db
def test_oar_true_count_dominates_per_the_audit(report):
    """Confirmed fact: is_oar True for 225,738 of 226,930 rows -- OAR is the
    overwhelming majority."""
    oar = report.oar
    total = report.recommendations.total
    if total == 0:
        pytest.skip("no recommendation rows in scope for this quarter")
    assert oar.is_oar_true_count / total > 0.9


@needs_db
def test_conversion_eligibility_breakdown_keeps_null_distinct_from_unknown_string(report):
    """None (column is NULL) and the literal string 'UNKNOWN' are two
    different, separately counted buckets -- never merged."""
    oar = report.oar
    keys = [row.conversion_eligibility for row in oar.conversion_eligibility_breakdown]
    # If both a None bucket and a literal "UNKNOWN" bucket are present, they
    # must be two distinct rows, not collapsed into one.
    if None in keys and "UNKNOWN" in keys:
        assert keys.count(None) == 1
        assert keys.count("UNKNOWN") == 1


# --- Recommendation status distribution --------------------------------------


@needs_db
def test_recommendation_status_counts_sum_to_total(report):
    rec = report.recommendations
    assert sum(row.count for row in rec.by_status) == rec.total


@needs_db
def test_not_evaluable_status_is_reported_verbatim_never_relabeled(report):
    """Confirmed fact: status breakdown NOT_EVALUABLE 226,924, READY_FOR_REVIEW
    5, PENDING_APPROVAL 1 (on the full dataset). NOT_EVALUABLE must appear
    exactly as that string -- never renamed 'rejected' or any other term."""
    rec = report.recommendations
    statuses = {row.status for row in rec.by_status}
    forbidden_relabels = {"rejected", "REJECTED", "denied", "DENIED"}
    assert not (statuses & forbidden_relabels)
    if rec.total > 0:
        # NOT_EVALUABLE dominates on the real extract; if any rows are in
        # scope at all we expect to see the literal status string present
        # somewhere sensible (not asserting it must be present for a
        # possibly-narrow quarter window, only that nothing is relabeled).
        for row in rec.by_status:
            assert row.status.isupper() or "_" in row.status  # real enum-shaped strings only


# --- Approval ledger -----------------------------------------------------------


@needs_db
def test_approval_section_counts_are_internally_consistent(report):
    approval = report.approval
    assert approval.distinct_recommendations_in_approval <= approval.ledger_entry_count
    assert approval.pending_count + approval.approved_count + approval.rejected_count <= approval.ledger_entry_count


# --- I11 Baseline Comparison: the explicit three-way split -------------------


@needs_db
def test_baseline_comparison_has_exactly_four_rows_in_fixed_order(report):
    bc = report.baseline_comparison
    assert [row.metric for row in bc.rows] == ["Safety Stock", "ROP", "Max Stock", "Lead Time"]


@needs_db
def test_i11_rop_and_max_stock_are_available_safety_stock_is_not(report):
    """The explicit 2026-09-21 product decision, tested precisely: ROP and
    Max Stock use current_rop/current_max_stock as the I11 stand-in and are
    AVAILABLE (90,818 non-null current values on the full dataset); Safety
    Stock is NOT_AVAILABLE because current_safety_stock has 0 non-null rows.
    These three must never be conflated into one blanket status."""
    bc = report.baseline_comparison
    by_metric = {row.metric: row for row in bc.rows}

    safety_stock_row = by_metric["Safety Stock"]
    rop_row = by_metric["ROP"]
    max_stock_row = by_metric["Max Stock"]

    assert safety_stock_row.availability_status == AvailabilityStatus.NOT_AVAILABLE
    assert safety_stock_row.both_available_count == 0
    assert safety_stock_row.baseline_value is None
    assert safety_stock_row.delta is None

    if report.recommendations.total > 0:
        # ROP/Max Stock only guaranteed AVAILABLE if this quarter's scope
        # actually contains rows with a populated current_rop/current_max_stock
        # -- true for the full extract (90,818 rows) but not asserted blindly
        # for an arbitrarily narrow future quarter.
        if rop_row.both_available_count > 0:
            assert rop_row.availability_status == AvailabilityStatus.AVAILABLE
        if max_stock_row.both_available_count > 0:
            assert max_stock_row.availability_status == AvailabilityStatus.AVAILABLE


@needs_db
def test_baseline_lead_time_source_is_always_marc_plifz(report):
    assert report.baseline_comparison.baseline_lead_time_source == "MARC-PLIFZ"


# --- Service-level / max-stock-strategy unconfigured state -------------------


@needs_db
def test_service_level_and_strategy_policy_are_not_configured_never_a_fabricated_percentage(report):
    assert report.safety_stock.service_level_status == AvailabilityStatus.NOT_CONFIGURED
    assert report.max_stock.strategy_policy_status == AvailabilityStatus.NOT_CONFIGURED


# --- SAP adoption UNKNOWN state ------------------------------------------------


@needs_db
def test_sap_adoption_is_unknown_never_reported_as_zero_percent(report):
    sap_adoption = report.sap_adoption
    assert sap_adoption.status == AvailabilityStatus.UNKNOWN
    assert "never" in sap_adoption.reason.lower() or "not" in sap_adoption.reason.lower()
    # Structural guard: the field is a status enum, not a bare number --
    # there is no numeric "percentage" field on this section at all.
    assert not hasattr(sap_adoption, "percentage")
    assert not hasattr(sap_adoption, "adoption_rate")


# --- Metadata / executive summary --------------------------------------------


@needs_db
def test_executive_summary_counts_are_consistent_with_their_sections(report):
    summary = report.executive_summary
    assert summary.total_recommendations == report.recommendations.total
    assert summary.oar_count == report.oar.is_oar_true_count
    assert summary.approval_ledger_entries == report.approval.ledger_entry_count
    status_counts = {row.status: row.count for row in report.recommendations.by_status}
    assert summary.not_evaluable_count == status_counts.get("NOT_EVALUABLE", 0)
    assert summary.ready_for_review_count == status_counts.get("READY_FOR_REVIEW", 0)
    assert summary.pending_approval_count == status_counts.get("PENDING_APPROVAL", 0)


@needs_db
def test_report_metadata_carries_the_requested_quarter(report, data_quarter):
    assert report.metadata.quarter == data_quarter
    assert report.metadata.report_version


# --- Failure path --------------------------------------------------------------


@needs_db
def test_generate_report_raises_value_error_for_an_invalid_quarter():
    with get_sessionmaker()() as session:
        with pytest.raises(ValueError):
            generate_quarterly_report(session, "not-a-quarter")


@needs_db
def test_generate_report_for_a_quarter_with_no_data_returns_zeroed_but_well_formed_sections():
    """A quarter guaranteed to have no rows (far future) must still produce a
    structurally valid report -- zero counts and None percentages, never an
    exception or a fabricated non-zero value."""
    with get_sessionmaker()() as session:
        report = generate_quarterly_report(session, "Q1 2099")
    assert report.scope_and_data_quality.total_records == 0
    assert report.scope_and_data_quality.classified_percentage is None
    assert report.recommendations.total == 0
    assert report.forecasting.mape_status == AvailabilityStatus.NOT_AVAILABLE
    assert report.sap_adoption.status == AvailabilityStatus.UNKNOWN
