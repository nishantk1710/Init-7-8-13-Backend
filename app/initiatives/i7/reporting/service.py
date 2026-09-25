"""I07 Quarterly Deep-Dive Report -- aggregation service.

Pure read-side aggregation over tables that Phases 3-7 already wrote
(``i7_material_feature``, ``i7_forecast``, ``i7_recommendation``,
``i7_approval_ledger``, ``i7_sap_adoption``). Nothing here recomputes demand
classification, forecasting, safety stock, ROP, max stock or OAR similarity
-- every figure is a COUNT/AVG/GROUP BY over a column those pipelines already
persisted. See ``app/schemas/i7/reports.py`` for the response contract and
the confirmed-facts docstrings each section carries.

**Manual/scheduled trigger.** No scheduler infrastructure exists anywhere in
this repo (no celery, no apscheduler, no cron trigger, no Azure Functions
timer). This module is invoked two ways that share the exact same service
call, never two different code paths: an API route, and a CLI entry point
(``python -m app.reporting.generate_quarterly``, mirroring the existing
``python -m app.seed`` pattern) intended to be invoked on a quarterly cadence
by an external trigger -- an Azure Logic App, a WebJob, or a manual ops run.
No in-process scheduler is started by importing this module.

**Quarter scoping.** Each table is filtered to its own most meaningful
timestamp column for "generated/computed within this quarter":

- ``i7_material_feature``  -> ``computed_at``
- ``i7_forecast``          -> ``generated_at``
- ``i7_recommendation``    -> ``generated_at`` (also the row set Section 12's
  baseline comparison reads -- see ``_baseline_comparison_section``)
- ``i7_approval_ledger``   -> ``timestamp`` (the only timestamp the ledger
  model carries -- see ``ApprovalLedgerEntry.timestamp``)
- ``i7_sap_adoption``      -> quarter-independent: the table has 0 rows on
  this extract regardless of period, so no filter is applied; see
  ``_sap_adoption_section``.

**Section 12 -- Current/I11 Baseline vs I07 Recommendation.** By the
2026-09-21 product decision (see the schema module's docstring and
``app/initiatives/i7/reporting/baseline_comparison.py``), I07's own
already-persisted current-state columns on ``i7_recommendation`` stand in for
the I11 baseline in this report -- Safety Stock, ROP and Max Stock compare
``current_*`` against ``recommended_*``; Lead Time compares the persisted
``lead_time_days`` (MARC-PLIFZ) against itself, because
``app/initiatives/i7/inventory/lead_time.py`` confirms I07 now uses
MARC-PLIFZ unconditionally as its own lead-time input -- there is no second,
separately-calculated I07 lead time to compare against. This module does not
touch ``I11LeadTimeProvider`` or ``lead_time.py``; it only reads columns
those modules already wrote.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i7.reporting.baseline_comparison import compare_metric
from app.initiatives.i7.reporting.period import resolve_quarter
from app.models.i7_features import MaterialFeature
from app.models.i7_forecast import Forecast
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation
from app.schemas.i7.reports import (
    ApprovalSection,
    AvailabilityStatus,
    BaselineComparisonSection,
    ChampionChallengerCounts,
    ConversionEligibilityCount,
    CriticalityTierCount,
    DemandClassCount,
    DemandClassification,
    ExecutiveSummary,
    ForecastAccuracyMetric,
    ForecastingSection,
    HistoryStatusCount,
    LimitationsSection,
    ManagementSummary,
    MaterialCriticalitySection,
    MaxStockSection,
    OarSection,
    PopulationCount,
    QuarterlyReport,
    RecommendationsSection,
    RecommendationStatusCount,
    ReorderPointSection,
    ReportMetadata,
    SafetyStockSection,
    SapAdoptionSection,
    ScopeAndDataQuality,
    UndefinedManagementMetric,
)

logger = get_logger(__name__)

REPORT_VERSION = "1.0"


def _exclusive_end(period_end):
    """``period_end`` from :func:`resolve_quarter` is inclusive (the last
    calendar day) -- timestamp columns are ``DateTime``, so a bare
    ``<= period_end`` would silently exclude any row timestamped later than
    midnight on that day. One day is added here, once, so every filter below
    uses a correct half-open range."""
    return period_end + timedelta(days=1)


def _pct(numerator: int, denominator: int) -> Decimal | None:
    """``None`` when the denominator is 0 -- never a fabricated 0%."""
    if denominator == 0:
        return None
    return (Decimal(numerator) / Decimal(denominator) * Decimal(100)).quantize(Decimal("0.01"))


def _count(session: Session, statement) -> int:
    return session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()


def _population_count(session: Session, base, column) -> PopulationCount:
    total = _count(session, base)
    populated = _count(session, base.where(column.isnot(None)))
    return PopulationCount(
        populated_count=populated,
        missing_count=total - populated,
        total_count=total,
        percentage_populated=_pct(populated, total),
    )


def generate_quarterly_report(session: Session, quarter: str) -> QuarterlyReport:
    """Assemble the I07 Quarterly Deep-Dive Report for ``quarter`` (e.g.
    ``"Q3 2026"``) from current table state.

    Every aggregate is scoped to rows generated/computed within
    ``[period_start, period_end]`` on that table's own most meaningful
    timestamp column (see the module docstring), except where a section is
    genuinely quarter-independent (documented at that section's assembly
    point below).
    """
    period_start, period_end = resolve_quarter(quarter)
    exclusive_end = _exclusive_end(period_end)
    logger.info(
        "i7.reporting.quarterly.generate.start",
        extra={"quarter": quarter, "period_start": str(period_start), "period_end": str(period_end)},
    )

    feature_base = select(MaterialFeature).where(
        MaterialFeature.computed_at >= period_start,
        MaterialFeature.computed_at < exclusive_end,
    )
    forecast_base = select(Forecast).where(
        Forecast.generated_at >= period_start,
        Forecast.generated_at < exclusive_end,
    )
    recommendation_base = select(Recommendation).where(
        Recommendation.generated_at >= period_start,
        Recommendation.generated_at < exclusive_end,
    )
    ledger_base = select(ApprovalLedgerEntry).where(
        ApprovalLedgerEntry.timestamp >= period_start,
        ApprovalLedgerEntry.timestamp < exclusive_end,
    )

    scope_and_data_quality, demand_classification = _feature_sections(session, feature_base)
    forecasting = _forecasting_section(session, forecast_base)
    safety_stock = _safety_stock_section(session, recommendation_base)
    reorder_point = _reorder_point_section(session, recommendation_base)
    max_stock = _max_stock_section(session, recommendation_base)
    oar = _oar_section(session, recommendation_base)
    material_criticality = _material_criticality_section(session)
    recommendations = _recommendations_section(session, recommendation_base)
    approval = _approval_section(session, ledger_base)
    baseline_comparison = _baseline_comparison_section(session, recommendation_base)
    sap_adoption = _sap_adoption_section()

    total_recommendations = recommendations.total
    status_counts = {row.status: row.count for row in recommendations.by_status}
    ready_for_review_count = status_counts.get("READY_FOR_REVIEW", 0)
    pending_approval_count = status_counts.get("PENDING_APPROVAL", 0)
    not_evaluable_count = status_counts.get("NOT_EVALUABLE", 0)

    executive_summary = ExecutiveSummary(
        total_material_plants=scope_and_data_quality.total_records,
        classified_percentage=scope_and_data_quality.classified_percentage,
        total_recommendations=total_recommendations,
        ready_for_review_count=ready_for_review_count,
        pending_approval_count=pending_approval_count,
        pending_approval_percentage=_pct(pending_approval_count, total_recommendations),
        not_evaluable_count=not_evaluable_count,
        oar_count=oar.is_oar_true_count,
        approval_ledger_entries=approval.ledger_entry_count,
    )

    management_summary = _management_summary_section()

    metadata = ReportMetadata(
        quarter=quarter,
        period_start=period_start,
        period_end=period_end,
        generated_at=datetime.now(timezone.utc),
        report_version=REPORT_VERSION,
    )

    limitations = LimitationsSection(
        items=[
            "Service level policy is not configured in production; safety-stock "
            "deltas and any implied Z-factor are reported only where a value was "
            "actually resolved, never estimated.",
            "Max-stock strategy (review_period) is not configured in production; "
            "1,186 of 1,192 review_period-labeled rows are dev/test fixture "
            "artifacts with no resolved value, reported separately from the "
            "small number of genuine production results.",
            "current_safety_stock has zero populated rows on this extract (EISBE "
            "is absent from the MARC extract) -- the Safety Stock row of Section "
            "12 still appears, but with a baseline-available count of 0.",
            "Section 12 (Current/I11 Baseline vs I07 Recommendation) uses I07's "
            "own persisted current-state columns as the I11 baseline stand-in, "
            "per the 2026-09-21 product decision -- not a literal I11 system "
            "read, and not I11LeadTimeProvider (which remains unused here).",
            "The Lead Time row of Section 12 compares MARC-PLIFZ against itself: "
            "app/initiatives/i7/inventory/lead_time.py confirms I07 now uses "
            "MARC-PLIFZ unconditionally as its own lead-time input, so there is "
            "no separately-calculated I07 lead time to compare against.",
            "SAP adoption has never been measured: i7_sap_adoption has 0 rows "
            "and the CDHDR/CDPOS change-document tables it would read have "
            "never been staged. Reported as Unknown, never as 0% adoption.",
            "Forecast accuracy does not include MAPE: no MAPE column or "
            "computation exists anywhere in the forecasting pipeline.",
        ]
    )

    report = QuarterlyReport(
        metadata=metadata,
        executive_summary=executive_summary,
        management_summary=management_summary,
        scope_and_data_quality=scope_and_data_quality,
        demand_classification=demand_classification,
        forecasting=forecasting,
        safety_stock=safety_stock,
        reorder_point=reorder_point,
        max_stock=max_stock,
        material_criticality=material_criticality,
        oar=oar,
        recommendations=recommendations,
        approval=approval,
        baseline_comparison=baseline_comparison,
        sap_adoption=sap_adoption,
        limitations=limitations,
    )

    logger.info(
        "i7.reporting.quarterly.generate.done",
        extra={
            "quarter": quarter,
            "total_material_plants": scope_and_data_quality.total_records,
            "total_forecasts": forecasting.total_forecasts,
            "total_recommendations": total_recommendations,
        },
    )
    return report


# ---------------------------------------------------------------------------
# i7_material_feature -> Scope & Data Quality, Demand Classification
# ---------------------------------------------------------------------------


def _feature_sections(session: Session, feature_base):
    total = _count(session, feature_base)

    by_class_rows = session.execute(
        feature_base.with_only_columns(MaterialFeature.demand_class, func.count()).group_by(
            MaterialFeature.demand_class
        )
    ).all()
    class_counts = {cls: cnt for cls, cnt in by_class_rows}
    unclassified_count = class_counts.get("UNCLASSIFIED", 0)
    classified_count = total - unclassified_count

    by_history_rows = session.execute(
        feature_base.with_only_columns(MaterialFeature.history_status, func.count()).group_by(
            MaterialFeature.history_status
        )
    ).all()

    criticality_populated = _count(
        session, feature_base.where(MaterialFeature.criticality.isnot(None))
    )
    lead_time_populated = _count(
        session, feature_base.where(MaterialFeature.lead_time_days.isnot(None))
    )

    scope_and_data_quality = ScopeAndDataQuality(
        total_records=total,
        classified_count=classified_count,
        classified_percentage=_pct(classified_count, total),
        unclassified_count=unclassified_count,
        unclassified_percentage=_pct(unclassified_count, total),
        history_status_breakdown=[
            HistoryStatusCount(history_status=status, count=cnt) for status, cnt in by_history_rows
        ],
        criticality_populated_count=criticality_populated,
        criticality_populated_percentage=_pct(criticality_populated, total),
        lead_time_populated_count=lead_time_populated,
        lead_time_populated_percentage=_pct(lead_time_populated, total),
    )

    demand_classification = DemandClassification(
        total=total,
        by_class=[
            DemandClassCount(demand_class=cls, count=cnt, percentage=_pct(cnt, total))
            for cls, cnt in by_class_rows
        ],
    )

    return scope_and_data_quality, demand_classification


# ---------------------------------------------------------------------------
# i7_forecast -> Forecasting
# ---------------------------------------------------------------------------


def _accuracy_metric(session: Session, forecast_base, column, total: int) -> ForecastAccuracyMetric:
    populated = _count(session, forecast_base.where(column.isnot(None)))
    if populated == 0:
        return ForecastAccuracyMetric(
            status=AvailabilityStatus.NOT_AVAILABLE,
            value=None,
            populated_count=0,
            total_count=total,
        )
    value = session.execute(
        forecast_base.where(column.isnot(None)).with_only_columns(func.avg(column))
    ).scalar_one()
    return ForecastAccuracyMetric(
        status=AvailabilityStatus.AVAILABLE,
        value=value,
        populated_count=populated,
        total_count=total,
    )


def _forecasting_section(session: Session, forecast_base) -> ForecastingSection:
    total = _count(session, forecast_base)

    mean_absolute_error = _accuracy_metric(session, forecast_base, Forecast.mean_absolute_error, total)
    pinball_loss = _accuracy_metric(session, forecast_base, Forecast.pinball_loss, total)
    bias_percentage = _accuracy_metric(session, forecast_base, Forecast.bias_percentage, total)
    fill_rate = _accuracy_metric(session, forecast_base, Forecast.fill_rate, total)
    holding_cost = _accuracy_metric(session, forecast_base, Forecast.holding_cost, total)

    champion_count = _count(session, forecast_base.where(Forecast.is_champion.is_(True)))
    baseline_count = _count(session, forecast_base.where(Forecast.is_baseline.is_(True)))
    challenger_count = _count(
        session,
        forecast_base.where(Forecast.is_champion.is_(False), Forecast.is_baseline.is_(False)),
    )

    return ForecastingSection(
        total_forecasts=total,
        mean_absolute_error=mean_absolute_error,
        pinball_loss=pinball_loss,
        bias_percentage=bias_percentage,
        fill_rate=fill_rate,
        holding_cost=holding_cost,
        champion_challenger=ChampionChallengerCounts(
            champion_count=champion_count,
            challenger_count=challenger_count,
            baseline_count=baseline_count,
        ),
        # No MAPE column or computation exists anywhere in the forecasting
        # pipeline (confirmed by source-tree search) -- reported as an
        # explicit status, never a fabricated number.
        mape_status=AvailabilityStatus.NOT_AVAILABLE,
    )


# ---------------------------------------------------------------------------
# i7_recommendation -> Safety Stock / Reorder Point / Max Stock / OAR /
# Recommendations lifecycle
# ---------------------------------------------------------------------------


def _mean_delta(session: Session, base, current_col, recommended_col) -> Decimal | None:
    both = base.where(current_col.isnot(None), recommended_col.isnot(None))
    comparable = _count(session, both)
    if comparable == 0:
        return None
    return session.execute(
        both.with_only_columns(func.avg(recommended_col - current_col))
    ).scalar_one()


def _safety_stock_section(session: Session, recommendation_base) -> SafetyStockSection:
    current = _population_count(session, recommendation_base, Recommendation.current_safety_stock)
    recommended = _population_count(
        session, recommendation_base, Recommendation.recommended_safety_stock
    )
    both_base = recommendation_base.where(
        Recommendation.current_safety_stock.isnot(None),
        Recommendation.recommended_safety_stock.isnot(None),
    )
    both_available = _count(session, both_base)
    mean_delta = _mean_delta(
        session, recommendation_base, Recommendation.current_safety_stock,
        Recommendation.recommended_safety_stock,
    )

    return SafetyStockSection(
        current=current,
        recommended=recommended,
        both_available_count=both_available,
        mean_delta=mean_delta,
        # Service-level policy (ServiceLevelPolicy) is explicitly unresolved
        # in production -- a pending business-policy decision, never a
        # fabricated percentage or Z-factor.
        service_level_status=AvailabilityStatus.NOT_CONFIGURED,
    )


def _reorder_point_section(session: Session, recommendation_base) -> ReorderPointSection:
    current = _population_count(session, recommendation_base, Recommendation.current_rop)
    recommended = _population_count(session, recommendation_base, Recommendation.recommended_rop)
    both_base = recommendation_base.where(
        Recommendation.current_rop.isnot(None), Recommendation.recommended_rop.isnot(None)
    )
    both_available = _count(session, both_base)
    mean_delta = _mean_delta(
        session, recommendation_base, Recommendation.current_rop, Recommendation.recommended_rop
    )

    return ReorderPointSection(
        current=current,
        recommended=recommended,
        both_available_count=both_available,
        mean_delta=mean_delta,
    )


def _max_stock_section(session: Session, recommendation_base) -> MaxStockSection:
    current = _population_count(session, recommendation_base, Recommendation.current_max_stock)
    recommended = _population_count(session, recommendation_base, Recommendation.recommended_max_stock)
    both_base = recommendation_base.where(
        Recommendation.current_max_stock.isnot(None),
        Recommendation.recommended_max_stock.isnot(None),
    )
    both_available = _count(session, both_base)
    mean_delta = _mean_delta(
        session, recommendation_base, Recommendation.current_max_stock,
        Recommendation.recommended_max_stock,
    )

    strategy_labeled_base = recommendation_base.where(
        Recommendation.max_stock_strategy == "review_period"
    )
    strategy_labeled_count = _count(session, strategy_labeled_base)
    strategy_production_resolved_count = _count(
        session, strategy_labeled_base.where(Recommendation.recommended_max_stock.isnot(None))
    )
    strategy_unresolved_fixture_count = strategy_labeled_count - strategy_production_resolved_count

    return MaxStockSection(
        current=current,
        recommended=recommended,
        both_available_count=both_available,
        mean_delta=mean_delta,
        strategy_labeled_count=strategy_labeled_count,
        strategy_production_resolved_count=strategy_production_resolved_count,
        strategy_unresolved_fixture_count=strategy_unresolved_fixture_count,
        # MAX_STOCK_STRATEGY is explicitly unresolved in production.
        strategy_policy_status=AvailabilityStatus.NOT_CONFIGURED,
    )


def _material_criticality_section(session: Session) -> MaterialCriticalitySection:
    """Real distribution of ``Recommendation.criticality`` (the 5 ZMM065
    tiers, verbatim) -- never an invented A/B/C 3-bucket collapse (no such
    grouping is defined in policy or code).

    Deliberately portfolio-wide (all-time ``i7_recommendation`` -- the same
    table every other section already reads criticality from; deliberately
    NOT the raw ``raw_zmm065_bmm``/``raw_zmm065_gb`` SAP extract tables,
    which have no ORM model, no MATNR join to this table's identities, and
    are explicitly documented as an un-normalized layer nothing else in this
    report touches), unlike every other section in this module, which is
    quarter-scoped -- a 2026-09-23 product decision. Almost no rows in this
    extract carry a ``generated_at`` inside any specific quarter's window
    (most predate the reporting feature and were never regenerated within
    one), so a quarter-scoped count reads as zero across every tier even
    though the real, current distribution over this table is substantial
    (6,414 NORMAL / 228 IMPACT / 192 CRITICAL / 32 INSURANCE / 4 OBSOLETE /
    447,586 unpopulated, at the time of that decision) -- an empty report
    section here would misrepresent data that genuinely exists. Confirmed
    acceptable specifically for this section because criticality is a
    near-static material attribute (ZMM065 tier assignment does not
    meaningfully change quarter to quarter the way recommendation counts or
    approval activity do), so an all-time figure is still a materially
    accurate answer to "what does the current portfolio look like," not a
    stale one."""
    by_tier_rows = session.execute(
        select(Recommendation.criticality, func.count()).group_by(Recommendation.criticality)
    ).all()
    total = sum(cnt for _tier, cnt in by_tier_rows)
    populated = sum(cnt for tier, cnt in by_tier_rows if tier is not None)

    return MaterialCriticalitySection(
        total=total,
        by_tier=[CriticalityTierCount(criticality=tier, count=cnt) for tier, cnt in by_tier_rows],
        populated_count=populated,
        populated_percentage=_pct(populated, total),
    )


def _management_summary_section() -> ManagementSummary:
    """Every field here is confirmed, by full source-tree audit, to have no
    defined business rule or computation anywhere in I07 -- not merely
    unimplemented. Reported as an explicit NOT_CONFIGURED status with a
    factual reason so a management reader sees the gap, not a silently
    missing section or (worse) a fabricated number."""
    return ManagementSummary(
        critical_stockout_risk=UndefinedManagementMetric(
            reason=(
                "No stockout-risk severity classification (Critical/High/"
                "Medium/Low) exists anywhere in I07. This is not the same "
                "concept as demand-pattern classification (Smooth/Erratic/"
                "Intermittent/Lumpy, see Demand Classification above) and "
                "must not be approximated by it."
            )
        ),
        excess_inventory_candidates=UndefinedManagementMetric(
            reason=(
                "No 'excess inventory' business rule is defined anywhere in "
                "I07 -- there is no confirmed threshold (e.g. current stock "
                "vs. recommended stock by how much, over what period) for "
                "calling a material excess."
            )
        ),
        working_capital_impact=UndefinedManagementMetric(
            reason=(
                "No working-capital/currency-denominated impact figure is "
                "computed anywhere in the reporting layer. unit_price (from "
                "MBEW) carries no currency field in the extract, and the one "
                "impact-calculation path that takes unit_price as an input "
                "is blocked by an unconfigured holding_cost_rate policy -- "
                "reporting a currency value here would misrepresent both the "
                "missing currency and the unconfigured cost-of-holding rate."
            )
        ),
        stockout_risk_distribution=UndefinedManagementMetric(
            reason="Same gap as Critical Stockout Risk above -- no severity-tier classification exists."
        ),
        stockout_risk_trend=UndefinedManagementMetric(
            reason=(
                "No monthly historical time-series mechanism exists for any "
                "risk/stockout metric. i7_quarterly_report itself stores one "
                "row per quarter (upserted, not accumulated), so even a "
                "quarter-over-quarter trend would require a new, dedicated "
                "historical-snapshot capability that does not exist today."
            )
        ),
    )


def _oar_section(session: Session, recommendation_base) -> OarSection:
    is_oar_true = _count(session, recommendation_base.where(Recommendation.is_oar.is_(True)))
    is_oar_false = _count(session, recommendation_base.where(Recommendation.is_oar.is_(False)))
    is_oar_null = _count(session, recommendation_base.where(Recommendation.is_oar.is_(None)))

    by_eligibility_rows = session.execute(
        recommendation_base.with_only_columns(
            Recommendation.conversion_eligibility, func.count()
        ).group_by(Recommendation.conversion_eligibility)
    ).all()

    return OarSection(
        is_oar_true_count=is_oar_true,
        is_oar_false_count=is_oar_false,
        is_oar_null_count=is_oar_null,
        conversion_eligibility_breakdown=[
            ConversionEligibilityCount(conversion_eligibility=elig, count=cnt)
            for elig, cnt in by_eligibility_rows
        ],
    )


def _recommendations_section(session: Session, recommendation_base) -> RecommendationsSection:
    total = _count(session, recommendation_base)
    by_status_rows = session.execute(
        recommendation_base.with_only_columns(Recommendation.status, func.count()).group_by(
            Recommendation.status
        )
    ).all()
    return RecommendationsSection(
        total=total,
        by_status=[RecommendationStatusCount(status=status, count=cnt) for status, cnt in by_status_rows],
    )


# ---------------------------------------------------------------------------
# Section 12: Current/I11 Baseline vs I07 Recommendation
# ---------------------------------------------------------------------------


def _baseline_comparison_section(session: Session, recommendation_base) -> BaselineComparisonSection:
    """Four rows, always, via the reusable ``compare_metric`` (see
    ``app/initiatives/i7/reporting/baseline_comparison.py``) -- never four
    near-duplicate blocks of comparison logic inline here.

    Lead Time compares ``Recommendation.lead_time_days`` against itself:
    ``app/initiatives/i7/inventory/lead_time.py`` (``analyse()``) confirms
    I07 now uses SAP's MARC-PLIFZ planned delivery time unconditionally as
    its own lead-time input and persists that same figure into
    ``lead_time_days`` -- there is no second, separately-calculated I07 lead
    time. Passing the same column as both sides of ``compare_metric`` is
    deliberate, not a bug: every populated row's baseline and I07 value agree
    by construction, and the honest report of that fact is
    ``i07_lead_time_source`` below, not a fabricated distinct figure.
    """
    safety_stock_row = compare_metric(
        session,
        recommendation_base,
        metric="Safety Stock",
        baseline_column=Recommendation.current_safety_stock,
        recommendation_column=Recommendation.recommended_safety_stock,
    )
    rop_row = compare_metric(
        session,
        recommendation_base,
        metric="ROP",
        baseline_column=Recommendation.current_rop,
        recommendation_column=Recommendation.recommended_rop,
    )
    max_stock_row = compare_metric(
        session,
        recommendation_base,
        metric="Max Stock",
        baseline_column=Recommendation.current_max_stock,
        recommendation_column=Recommendation.recommended_max_stock,
    )
    lead_time_row = compare_metric(
        session,
        recommendation_base,
        metric="Lead Time",
        baseline_column=Recommendation.lead_time_days,
        recommendation_column=Recommendation.lead_time_days,
        self_referential=True,
    )

    # The lead_time_method I07 recorded for the populated lead-time rows, if
    # uniform across scope -- named explicitly rather than assumed. None when
    # no row in scope has a lead_time_days value, or when more than one
    # distinct method value is present (mixed sources are never collapsed
    # into a single misleading label).
    method_rows = session.execute(
        recommendation_base.where(Recommendation.lead_time_days.isnot(None))
        .with_only_columns(Recommendation.lead_time_method)
        .distinct()
    ).all()
    distinct_methods = {row[0] for row in method_rows}
    i07_lead_time_source = next(iter(distinct_methods)) if len(distinct_methods) == 1 else None

    return BaselineComparisonSection(
        baseline_lead_time_source="MARC-PLIFZ",
        i07_lead_time_source=i07_lead_time_source,
        rows=[safety_stock_row, rop_row, max_stock_row, lead_time_row],
    )


# ---------------------------------------------------------------------------
# i7_approval_ledger -> Approval
# ---------------------------------------------------------------------------


def _approval_section(session: Session, ledger_base) -> ApprovalSection:
    total = _count(session, ledger_base)
    distinct_recommendations = session.execute(
        ledger_base.with_only_columns(
            func.count(func.distinct(ApprovalLedgerEntry.recommendation_id))
        )
    ).scalar_one()

    # Ledger rows record an *action* (APPROVE/REJECT/...), not directly a
    # "pending" state -- pending-ness is approximated here from new_status on
    # the ledger entry itself, the ledger's own record of what the status
    # became as a result of that action.
    status_rows = session.execute(
        ledger_base.with_only_columns(ApprovalLedgerEntry.new_status, func.count()).group_by(
            ApprovalLedgerEntry.new_status
        )
    ).all()
    status_counts = {status: cnt for status, cnt in status_rows}
    pending_count = status_counts.get("PENDING_APPROVAL", 0) + status_counts.get("HELD", 0)
    approved_count = status_counts.get("APPROVED", 0)
    rejected_count = status_counts.get("REJECTED", 0) + status_counts.get("SENT_BACK", 0)

    return ApprovalSection(
        ledger_entry_count=total,
        distinct_recommendations_in_approval=distinct_recommendations,
        pending_count=pending_count,
        approved_count=approved_count,
        rejected_count=rejected_count,
    )


# ---------------------------------------------------------------------------
# i7_sap_adoption -> SAP Adoption
# ---------------------------------------------------------------------------


def _sap_adoption_section() -> SapAdoptionSection:
    """Quarter-independent by nature: ``i7_sap_adoption`` has 0 rows on this
    extract regardless of period (``NoSapStateAvailable.current_state``
    always returns ``None`` -- CDHDR/CDPOS change-document data has never
    been staged), so no quarter filter is meaningful here. Unrelated to
    Section 12's baseline comparison -- the two must never be conflated (see
    the schema module's docstring)."""
    return SapAdoptionSection(
        status=AvailabilityStatus.UNKNOWN,
        reason=(
            "i7_sap_adoption has 0 rows; CDHDR/CDPOS SAP change-document data "
            "has never been staged, so adoption has never actually been "
            "measured. This is distinct from a measured 0% adoption."
        ),
    )
