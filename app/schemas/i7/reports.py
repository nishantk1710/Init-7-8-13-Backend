"""I07 Quarterly Deep-Dive Report -- response schema.

This module defines only the *shape* of the report; no business logic lives
here. Every section mirrors a real, confirmed data source (``i7_material_feature``,
``i7_forecast``, ``i7_recommendation``, ``i7_approval_ledger``, ``i7_sap_adoption``)
-- nothing here invents a KPI the underlying tables cannot actually produce.

``AvailabilityStatus`` is the one recurring discipline: a metric that can be
genuinely unavailable (unconfigured policy, unstaged SAP adoption data, zero
populated rows) always carries an explicit status alongside its value, never
a bare ``0``/``False``/``None`` with no accompanying explanation. A ``None``
value paired with ``AVAILABLE`` is a real, correctly-computed absence (e.g. no
row in scope had all three fields needed for an aggregate); a ``None`` value
paired with anything else means the metric was never attempted or the
underlying policy/data does not exist yet -- the two must never be confused.

Section 12 (`Current/I11 Baseline vs I07 Recommendation`) implements a
corrected product decision (2026-09-21): I07's own currently-persisted values
-- ``current_safety_stock`` / ``current_rop`` / ``current_max_stock`` on
``i7_recommendation``, and MARC-PLIFZ lead time -- stand in for the I11
baseline for this report. This is a deliberate substitution, not a claim that
these columns originate from a system called "I11"; see
``BaselineComparisonRow`` below for the full rationale. The report must not
wait for ``I11LeadTimeProvider`` or a separate I11 dataset.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

# --------------------------------------------------------------------------
# API-facing request/response shapes (list/generate/status), added alongside
# the report body above. These carry no business logic of their own -- every
# field is read straight off ``QuarterlyReportRecord``
# (``app/models/i7_reporting.py``) or ``QuarterlyReportSummary``
# (``app/initiatives/i7/reporting/repository.py``).
# --------------------------------------------------------------------------


class GenerationStatus(StrEnum):
    """Mirrors ``QuarterlyReportRecord.status`` exactly -- the same four
    values, never relabeled. RUNNING is reserved for a future async
    implementation (see ``app/api/i7/reports.py``'s module docstring); the
    current synchronous generator only ever persists PENDING transiently
    in-memory before writing COMPLETED or FAILED."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class QuarterlyReportListItem(BaseModel):
    """One row of the quarterly-report list -- summary only, no
    ``report_json`` (mirrors ``QuarterlyReportSummary``, never the full
    report body)."""

    model_config = ConfigDict(frozen=True)

    report_id: int
    quarter: str
    status: GenerationStatus
    report_version: str
    generated_at: datetime
    period_start: date
    period_end: date

    @classmethod
    def from_summary(cls, summary) -> "QuarterlyReportListItem":
        return cls(
            report_id=summary.id,
            quarter=summary.quarter,
            status=GenerationStatus(summary.status),
            report_version=summary.report_version,
            generated_at=summary.generated_at,
            period_start=summary.period_start,
            period_end=summary.period_end,
        )


class QuarterlyReportListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[QuarterlyReportListItem]
    total: int


class GenerateReportRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    quarter: str
    """E.g. ``"Q3 2026"`` -- validated against ``resolve_quarter``'s format
    at the route, not here (keeps the parsing rule in one place)."""


class GenerationStatusResponse(BaseModel):
    """The generation lifecycle for one quarter -- for a frontend polling
    loop, even though generation is synchronous today (see
    ``app/api/i7/reports.py``'s module docstring: this shape stays stable if
    generation later becomes asynchronous)."""

    model_config = ConfigDict(frozen=True)

    quarter: str
    status: GenerationStatus
    report_id: int | None = None
    generated_at: datetime | None = None
    error: str | None = None
    """Populated only when ``status`` is FAILED -- mirrors
    ``QuarterlyReportRecord.error``."""


class AvailabilityStatus(StrEnum):
    """Attached to every metric that can genuinely be missing, alongside its
    value -- never collapsed into a bare 0/False/None. See module docstring."""

    AVAILABLE = "AVAILABLE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    """Data was looked for and there is none (e.g. zero populated rows for a
    column that exists), as distinct from a policy never having been set."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    """A business policy this metric depends on (service level, max-stock
    strategy) has not been decided -- a pending decision, not a data gap."""

    UNKNOWN = "UNKNOWN"
    """The underlying tracking mechanism has not been staged/wired up at all
    (e.g. SAP change-document adoption tracking -- 0 rows, ever, today)."""

    NOT_EVALUABLE = "NOT_EVALUABLE"
    """The record's own lifecycle status prevented any value from being
    computed in the first place -- distinct from "computed and null"."""


# --------------------------------------------------------------------------
# 1. Metadata
# --------------------------------------------------------------------------


class ReportMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    quarter: str
    """E.g. ``"Q3 2026"`` -- a human label, not used for any date math."""

    period_start: date
    period_end: date
    generated_at: datetime
    report_version: str

    feature_run_id: int | None = None
    forecast_run_id: int | None = None
    inventory_run_id: int | None = None
    oar_run_id: int | None = None
    """The specific pipeline run ids this report's figures were drawn from,
    when the report is tied to one build. ``None`` when the report aggregates
    across whatever rows exist in the period rather than a single named run
    -- never fabricated to look more precise than the query actually was."""


# --------------------------------------------------------------------------
# 2. Executive Summary
# --------------------------------------------------------------------------


class ExecutiveSummary(BaseModel):
    """Only metrics the audit confirmed are actually computable from real
    data -- no invented KPI (e.g. no "inventory turns" or "cost savings"
    figure with nothing backing it)."""

    model_config = ConfigDict(frozen=True)

    total_material_plants: int
    classified_percentage: Decimal | None
    """Share of ``i7_material_feature`` rows with a real (non-UNCLASSIFIED)
    demand_class. ``None`` only if ``total_material_plants`` is 0."""

    total_recommendations: int
    ready_for_review_count: int
    pending_approval_count: int
    not_evaluable_count: int
    oar_count: int
    approval_ledger_entries: int


# --------------------------------------------------------------------------
# 3. Scope & Data Quality
# --------------------------------------------------------------------------


class HistoryStatusCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    history_status: str
    count: int


class ScopeAndDataQuality(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_records: int
    classified_count: int
    classified_percentage: Decimal | None
    unclassified_count: int
    unclassified_percentage: Decimal | None
    history_status_breakdown: list[HistoryStatusCount]
    criticality_populated_count: int
    criticality_populated_percentage: Decimal | None
    lead_time_populated_count: int
    lead_time_populated_percentage: Decimal | None


# --------------------------------------------------------------------------
# 4. Demand Classification
# --------------------------------------------------------------------------


class DemandClassCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    demand_class: str
    count: int
    percentage: Decimal | None


class DemandClassification(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    by_class: list[DemandClassCount]
    """One row per ``SMOOTH``/``ERRATIC``/``INTERMITTENT``/``LUMPY``/
    ``UNCLASSIFIED`` value actually present in ``i7_material_feature``."""


# --------------------------------------------------------------------------
# 5. Forecasting
# --------------------------------------------------------------------------


class ForecastAccuracyMetric(BaseModel):
    """One aggregated accuracy/economics figure across ``i7_forecast`` rows
    that have it populated. Paired counts make the aggregate's coverage
    explicit -- a mean over 3 populated rows out of 16,005 reads very
    differently from a mean over all of them."""

    model_config = ConfigDict(frozen=True)

    status: AvailabilityStatus
    value: Decimal | None = None
    populated_count: int
    total_count: int


class ChampionChallengerCounts(BaseModel):
    model_config = ConfigDict(frozen=True)

    champion_count: int
    challenger_count: int
    baseline_count: int


class ForecastingSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_forecasts: int
    mean_absolute_error: ForecastAccuracyMetric
    pinball_loss: ForecastAccuracyMetric
    bias_percentage: ForecastAccuracyMetric
    fill_rate: ForecastAccuracyMetric
    holding_cost: ForecastAccuracyMetric
    champion_challenger: ChampionChallengerCounts
    mape_status: AvailabilityStatus
    """Always ``NOT_AVAILABLE`` -- no MAPE column or computation exists
    anywhere in the I07 pipeline (confirmed by source-tree search). Reported
    as an explicit status, never as a fabricated number or a silently
    omitted field."""


# --------------------------------------------------------------------------
# 6. Safety Stock
# --------------------------------------------------------------------------


class PopulationCount(BaseModel):
    """Populated/missing split for one column over a fixed denominator
    (typically all ``i7_recommendation`` rows in scope)."""

    model_config = ConfigDict(frozen=True)

    populated_count: int
    missing_count: int
    total_count: int
    percentage_populated: Decimal | None


class SafetyStockSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    current: PopulationCount
    recommended: PopulationCount
    both_available_count: int
    """Rows where both ``current_safety_stock`` and ``recommended_safety_stock``
    are populated -- the only rows a delta can be computed over."""

    mean_delta: Decimal | None
    """``recommended - current``, averaged over ``both_available_count`` rows
    only. ``None`` when that count is 0 (the confirmed current state: 0 rows
    have a populated ``current_safety_stock`` at all)."""

    service_level_status: AvailabilityStatus
    """Always ``NOT_CONFIGURED`` in production today -- the Criticality x
    Service Level matrix (``ServiceLevelPolicy``) is an explicitly unresolved
    business-policy decision, not a code defect. Never a fabricated
    percentage or Z-factor."""


# --------------------------------------------------------------------------
# 7. Reorder Point
# --------------------------------------------------------------------------


class ReorderPointSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    current: PopulationCount
    recommended: PopulationCount
    both_available_count: int
    mean_delta: Decimal | None
    """``recommended - current``, averaged over ``both_available_count`` rows
    only. ``None`` when that count is 0."""


# --------------------------------------------------------------------------
# 8. Max Stock
# --------------------------------------------------------------------------


class MaxStockSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    current: PopulationCount
    recommended: PopulationCount
    both_available_count: int
    mean_delta: Decimal | None

    strategy_labeled_count: int
    """Rows carrying ``max_stock_strategy = 'review_period'`` -- includes
    both the small number that actually resolved a value and the larger
    dev/test fixture set that did not (see the two fields below)."""

    strategy_production_resolved_count: int
    """Of the labeled rows, the number that genuinely resolved a max-stock
    value through the production path -- the real, small figure."""

    strategy_unresolved_fixture_count: int
    """Of the labeled rows, the number left unresolved by
    ``policy/unresolved.py`` -- dev/test fixture artifacts, not a production
    default. Kept separate so ``strategy_labeled_count`` is never read as
    "1,192 real production results" on its own."""

    strategy_policy_status: AvailabilityStatus
    """Always ``NOT_CONFIGURED`` in production today -- ``MAX_STOCK_STRATEGY``
    is an explicitly unresolved business-policy decision."""


# --------------------------------------------------------------------------
# 9. OAR / Min-Max
# --------------------------------------------------------------------------


class ConversionEligibilityCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    conversion_eligibility: str | None
    """``None`` groups rows where the field itself is NULL (distinct from the
    literal string ``"UNKNOWN"``, which is also a real, counted value)."""

    count: int


class OarSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    is_oar_true_count: int
    is_oar_false_count: int
    is_oar_null_count: int
    conversion_eligibility_breakdown: list[ConversionEligibilityCount]


# --------------------------------------------------------------------------
# 10. Recommendations
# --------------------------------------------------------------------------


class RecommendationStatusCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    """The exact backend ``LifecycleStatus`` value, verbatim -- e.g.
    ``NOT_EVALUABLE`` is never relabeled ``"rejected"`` or any other
    reinterpreted term."""

    count: int


class RecommendationsSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    by_status: list[RecommendationStatusCount]


# --------------------------------------------------------------------------
# 11. Approval
# --------------------------------------------------------------------------


class ApprovalSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    ledger_entry_count: int
    distinct_recommendations_in_approval: int
    pending_count: int
    approved_count: int
    rejected_count: int


# --------------------------------------------------------------------------
# 12. Current/I11 Baseline vs I07 Recommendation
# --------------------------------------------------------------------------


class BaselineComparisonRow(BaseModel):
    """One metric's baseline-vs-I07 comparison, computed by the reusable
    ``BaselineComparisonService`` (``app.initiatives.i7.reporting.baseline_comparison``)
    over ``i7_recommendation`` rows in scope. This same shape is used for all
    four rows of Section 12 (Safety Stock, ROP, Max Stock, Lead Time) --
    there is exactly one model here, not four near-duplicate ones.

    The "baseline" is, by explicit 2026-09-21 product decision, I07's own
    already-persisted current-state columns (``current_safety_stock`` /
    ``current_rop`` / ``current_max_stock``) and MARC-PLIFZ lead time --
    standing in for I11 because no separate I11 dataset or working
    ``I11LeadTimeProvider`` exists yet. It is *not* a literal I11 system
    output, and this substitution is confined to this report; it does not
    change what ``I11LeadTimeProvider`` returns or how any other part of the
    application treats I11.
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    """``"Safety Stock"`` / ``"ROP"`` / ``"Max Stock"`` / ``"Lead Time"``."""

    baseline_value: Decimal | None
    """Aggregate (mean) baseline value over rows where the baseline column is
    populated. ``None`` when zero rows have it populated -- never 0."""

    recommendation_value: Decimal | None
    """Aggregate (mean) I07 value over rows where the I07 column is
    populated. ``None`` when zero rows have it populated -- never 0."""

    delta: Decimal | None
    """``recommendation_value - baseline_value``, computed only over rows
    where BOTH sides are available for that same row (see
    ``both_available_count``) -- never derived from the two aggregates above,
    which may be computed over different, non-overlapping row sets. ``None``
    when ``both_available_count`` is 0."""

    delta_percentage: Decimal | None
    """``delta / baseline_value * 100`` for the same both-available rows,
    averaged, guarding divide-by-zero explicitly. ``None`` when
    ``both_available_count`` is 0 or every contributing baseline value is 0."""

    both_available_count: int
    """Rows where baseline AND I07 value are both populated -- the only rows
    a delta is computed over."""

    baseline_missing_count: int
    """Rows where the baseline side is missing (I07 side may or may not be
    present) -- counted separately from ``recommendation_missing_count`` per
    the reporting requirement, not collapsed into one "missing" bucket."""

    recommendation_missing_count: int
    """Rows where the I07 side is missing (baseline side may or may not be
    present)."""

    not_evaluable_count: int
    """Rows whose recommendation ``status`` is ``NOT_EVALUABLE`` -- the
    record's lifecycle prevented any value being computed at all, distinct
    from "computed but null". Counted once per row, not double-counted
    against the missing buckets above."""

    availability_status: AvailabilityStatus
    """``AVAILABLE`` when ``both_available_count`` > 0, ``NOT_AVAILABLE``
    when it is 0 -- e.g. the confirmed current Safety Stock state (0 rows
    have ``current_safety_stock`` populated), which still appears as a full
    row in this table rather than being omitted."""


class BaselineComparisonSection(BaseModel):
    """Section 12 of the report. Always exactly four rows, one per metric,
    even when a row's ``availability_status`` is ``NOT_AVAILABLE`` -- the
    row is never dropped for having no comparable data."""

    model_config = ConfigDict(frozen=True)

    baseline_lead_time_source: str = "MARC-PLIFZ"
    """Named explicitly per the product requirement -- the baseline lead
    time for this report is unconditionally MARC-PLIFZ, regardless of
    whether ``I11LeadTimeProvider`` becomes non-stub later."""

    i07_lead_time_source: str | None = None
    """The ``lead_time_method`` / ``lead_time_source`` value I07 itself
    recorded for the rows behind the Lead Time row below, when uniform
    across scope (e.g. ``"PLANNED_FALLBACK"``); ``None`` when no lead-time
    value was recorded for any row in scope, or when sources are mixed and
    no single value can be named without misrepresenting the data."""

    rows: list[BaselineComparisonRow]
    """Exactly four entries: Safety Stock, ROP, Max Stock, Lead Time --
    always in that order."""


# --------------------------------------------------------------------------
# 13. SAP Adoption
# --------------------------------------------------------------------------


class SapAdoptionSection(BaseModel):
    """FR-9 CDHDR/CDPOS-based SAP change-document adoption tracking -- an
    entirely separate concept from Section 12's baseline comparison; the two
    must never be conflated."""

    model_config = ConfigDict(frozen=True)

    status: AvailabilityStatus
    """Always ``UNKNOWN`` today -- ``i7_sap_adoption`` has 0 rows and
    ``NoSapStateAvailable.current_state`` always returns ``None``. Never
    reported as ``"0% adoption"``, which would falsely claim a measured
    result of zero rather than "never measured"."""

    reason: str
    """Human-readable explanation (CDHDR/CDPOS change documents are not yet
    staged), for a management reader."""


# --------------------------------------------------------------------------
# 14. Limitations / Dependencies
# --------------------------------------------------------------------------


class LimitationsSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[str]
    """Plain-language summaries for a management reader (e.g. "Safety stock
    service-level policy has not yet been set by the business" rather than
    "ServiceLevelPolicy is unresolved in policy/unresolved.py")."""


# --------------------------------------------------------------------------
# Top-level report
# --------------------------------------------------------------------------


class QuarterlyReport(BaseModel):
    """The I07 Quarterly Deep-Dive Report -- the complete response of the
    report-generation API and the payload the quarterly CLI writes.

    Composed of the fourteen sections above, each independently pure data --
    no cross-section computation happens at this level; every aggregate
    value was computed once, upstream, by the reporting service."""

    model_config = ConfigDict(frozen=True)

    metadata: ReportMetadata
    executive_summary: ExecutiveSummary
    scope_and_data_quality: ScopeAndDataQuality
    demand_classification: DemandClassification
    forecasting: ForecastingSection
    safety_stock: SafetyStockSection
    reorder_point: ReorderPointSection
    max_stock: MaxStockSection
    oar: OarSection
    recommendations: RecommendationsSection
    approval: ApprovalSection
    baseline_comparison: BaselineComparisonSection
    sap_adoption: SapAdoptionSection
    limitations: LimitationsSection

    @classmethod
    def from_report_json(cls, report_json: str) -> "QuarterlyReport":
        """Deserialize the ``Text`` column written by ``save_report``
        (``app/initiatives/i7/reporting/repository.py``) back into a
        validated model -- the one place that round-trip happens, so the API
        layer never hand-parses the JSON itself."""
        return cls.model_validate_json(report_json)
