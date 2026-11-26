"""Min-Max -> OAR recommendation API schemas.

Read-only. Nothing here is stored: a response is the report computed from the staged data and the
policy, so every response carries the anchor and the applied policy parameters it was produced under.

Stock quantities and money are ``Decimal`` (a JSON string, as every other I07 response; the frontend
parses them). Money is stated in ZAR by the rule the OAR and Stock item figures use: a price in ZAR as it is, in
NAD at 1:1; any other currency, or none stated, is left out (``currency`` and ``stock_value`` are then ``null``).
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.initiatives.i7.reverse_conversion.service import (
    CATEGORY_CODES,
    CONFIDENCE_CODES,
    conversion_eligibility,
    RecommendationRow,
    ReverseReport,
    reason_code,
)
from app.initiatives.i7.reverse_conversion.types import CONFIDENCE_ORDER, FINAL_CATEGORIES


class ReverseAnchor(BaseModel):
    model_config = ConfigDict(frozen=True)

    month: str
    """The latest complete month every consumption window is measured to, ``YYYY-MM``."""
    source: str
    """``configured`` (pinned) or ``data_load_timestamp`` (derived from when the data was loaded)."""
    latest_staged_month: str | None
    data_loaded_at: datetime | None
    warnings: list[str]
    generated_at: datetime | None = None
    """When the pipeline stage that stored this run finished -- the page's "last rebuilt"."""
    run_id: int | None = None


class ReversePolicyInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    parameters: dict[str, Any]
    """Every applied threshold, as the policy holds it."""


class ReverseCategoryTotals(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str
    label: str
    count: int
    with_stock: int
    with_stock_missing_price: int
    stock_value: Decimal
    """Stock x unit price, in ZAR, over rows whose price can be counted in rand."""
    currency: str | None = "ZAR"


class ReversePlantCategoryCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    plant: str
    category: str
    count: int


class ReverseReasonCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str
    reason: str
    reason_label: str
    count: int


class ReverseConfidenceCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    confidence: str
    label: str
    count: int


class ReversePlantReconciliation(BaseModel):
    model_config = ConfigDict(frozen=True)

    plant: str
    source_population: int
    categorised: int


class ReverseReconciliation(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_population: int
    excluded: int
    categorised: int
    duplicate_keys: int
    reconciled: bool
    by_plant: list[ReversePlantReconciliation]


class ReverseSummary(BaseModel):
    """The whole population, whatever filters the list uses."""

    model_config = ConfigDict(frozen=True)

    by_category: list[ReverseCategoryTotals]
    by_plant_category: list[ReversePlantCategoryCount]
    by_reason: list[ReverseReasonCount]
    by_confidence: list[ReverseConfidenceCount]
    """Convert to OAR rows by confidence (High, Medium, Low)."""


class ReverseExplanationPoint(BaseModel):
    """One labelled line of a reason: ``What we saw`` / ``What it means`` / ``What to do``."""

    model_config = ConfigDict(frozen=True)

    label: str
    text: str


class ReverseRequestBrief(BaseModel):
    """The newest conversion request of a material-plant, as a list row shows it."""

    model_config = ConfigDict(frozen=True)

    id: int
    status: str
    pending_role: str | None


class ReverseMinMaxAlternative(BaseModel):
    """The Stock item recommendation for the same material-plant: what it would be on Min-Max."""

    model_config = ConfigDict(frozen=True)

    current_rop: Decimal | None
    recommended_rop: Decimal | None
    current_max_stock: Decimal | None
    recommended_max_stock: Decimal | None


class ReverseRecommendation(BaseModel):
    model_config = ConfigDict(frozen=True)

    material: str
    description: str | None
    plant: str
    mrp_type: str | None
    criticality: str | None
    category: str
    category_label: str
    conversion_eligibility: str
    """``eligible`` (Convert to OAR) or ``not_eligible`` (Not Eligible)."""
    reason: str
    reason_label: str
    confidence: str | None
    """``high`` / ``medium`` / ``low`` for a Convert to OAR row; null for every other category."""
    confidence_label: str | None
    explanation: str
    explanation_points: list[ReverseExplanationPoint]
    """The same reason as three short labelled lines, for a person to scan. ``explanation`` is it as one paragraph."""
    decision_rule: int

    issues_latest_12m: int
    months_with_consumption_12m: int
    issues_prior_12m: int
    first_valid_movement: str | None
    last_valid_movement: str | None
    months_since_last_movement: int | None
    history_span_months: int | None
    lifetime_months_with_use: int

    lead_time_days: Decimal | None
    lead_time_bucket: str
    current_stock: Decimal | None
    minbe: Decimal | None
    mabst: Decimal | None
    unit_price: Decimal | None
    stock_value: Decimal | None
    """In ZAR; null when the stock or a countable price is missing."""
    currency: str | None = None
    """``ZAR`` when the price is counted in rand (ZAR, or NAD at 1:1); null when it is left out."""
    price_currency: str | None = None
    """The currency SAP stated for the price, as it is; null when none was stated."""
    conversion_request: ReverseRequestBrief | None = None
    """The newest request to convert this material-plant to OAR, when there is one."""
    min_max_alternative: ReverseMinMaxAlternative | None = None
    """Medium and Low confidence only: the Stock item recommendation (ROP and max stock) if it stays on Min-Max, shown
    for comparison. Convert to OAR is still the recommended action. Null for High confidence and for Not eligible."""

    @classmethod
    def from_row(
        cls,
        row: RecommendationRow,
        request: ReverseRequestBrief | None = None,
        alternative: ReverseMinMaxAlternative | None = None,
    ) -> "ReverseRecommendation":
        m = row.metrics
        return cls(
            material=row.material,
            description=row.description,
            plant=row.plant,
            mrp_type=row.mrp_type,
            criticality=row.criticality,
            category=CATEGORY_CODES[row.category],
            category_label=row.category.value,
            conversion_eligibility=conversion_eligibility(row.category),
            reason=reason_code(row.reason),
            reason_label=row.reason.value,
            confidence=CONFIDENCE_CODES[row.confidence] if row.confidence else None,
            confidence_label=row.confidence.value if row.confidence else None,
            explanation=row.detail,
            explanation_points=[ReverseExplanationPoint(label=label, text=text) for label, text in row.points],
            decision_rule=row.rule,
            issues_latest_12m=m.issues_latest_window,
            months_with_consumption_12m=m.months_with_use_latest_window,
            issues_prior_12m=m.issues_prior_window,
            first_valid_movement=f"{m.first_valid_month:%Y-%m}" if m.first_valid_month else None,
            last_valid_movement=f"{m.last_valid_month:%Y-%m}" if m.last_valid_month else None,
            months_since_last_movement=m.months_since_last_movement,
            history_span_months=m.history_span_months,
            lifetime_months_with_use=m.lifetime_months_with_use,
            lead_time_days=row.lead_time_days,
            lead_time_bucket=row.lead_time_bucket,
            current_stock=row.current_stock,
            minbe=row.minbe,
            mabst=row.mabst,
            unit_price=row.unit_price,
            stock_value=row.stock_value,
            currency=row.value_currency,
            price_currency=row.price_currency,
            conversion_request=request,
            min_max_alternative=alternative,
        )


class ReverseConsumptionMonth(BaseModel):
    model_config = ConfigDict(frozen=True)

    month: str
    """``YYYY-MM``."""
    net_issued_quantity: Decimal
    """Issues minus reversals, in the base unit. A month counts as used only above zero."""
    issue_count: int
    reversal_count: int


class ReverseConsumptionResponse(BaseModel):
    """One material-plant's last 24 months up to the anchor, for the detail chart."""

    model_config = ConfigDict(frozen=True)

    material: str
    plant: str
    anchor_month: str
    latest_window_start: str
    """First month of the latest 12-month window the rule counts; earlier months are the prior window."""
    months: list[ReverseConsumptionMonth]


class ReverseRecommendationsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    anchor: ReverseAnchor
    policy: ReversePolicyInfo
    summary: ReverseSummary
    reconciliation: ReverseReconciliation
    items: list[ReverseRecommendation]
    total: int
    """Rows matching the filters (before paging)."""
    page: int
    page_size: int


def build_anchor(report: ReverseReport) -> ReverseAnchor:
    anchor = report.anchor
    return ReverseAnchor(
        month=f"{anchor.month:%Y-%m}",
        source=anchor.source,
        latest_staged_month=f"{anchor.latest_staged_month:%Y-%m}" if anchor.latest_staged_month else None,
        data_loaded_at=anchor.data_loaded_at,
        warnings=list(anchor.warnings),
        generated_at=report.generated_at,
        run_id=report.run_id,
    )


def build_summary(report: ReverseReport) -> ReverseSummary:
    return ReverseSummary(
        by_category=[
            ReverseCategoryTotals(
                category=CATEGORY_CODES[category],
                label=category.value,
                count=report.totals_by_category[category].count,
                with_stock=report.totals_by_category[category].with_stock,
                with_stock_missing_price=report.totals_by_category[category].with_stock_missing_price,
                stock_value=report.totals_by_category[category].stock_value,
            )
            for category in FINAL_CATEGORIES
        ],
        by_plant_category=[
            ReversePlantCategoryCount(plant=plant, category=CATEGORY_CODES[category], count=count)
            for (plant, category), count in sorted(
                report.count_by_plant_category.items(), key=lambda item: (item[0][0], FINAL_CATEGORIES.index(item[0][1]))
            )
        ],
        by_reason=[
            ReverseReasonCount(category=CATEGORY_CODES[category], reason=reason_code(reason), reason_label=reason.value, count=count)
            for (category, reason), count in sorted(
                report.count_by_category_reason.items(),
                key=lambda item: (FINAL_CATEGORIES.index(item[0][0]), -item[1], item[0][1].value),
            )
        ],
        by_confidence=[
            ReverseConfidenceCount(confidence=CONFIDENCE_CODES[level], label=level.value, count=report.count_by_confidence[level])
            for level in CONFIDENCE_ORDER
        ],
    )


def build_reconciliation(report: ReverseReport) -> ReverseReconciliation:
    r = report.reconciliation
    return ReverseReconciliation(
        source_population=r.source_population,
        excluded=r.excluded,
        categorised=r.categorised,
        duplicate_keys=r.duplicate_keys,
        reconciled=r.reconciled,
        by_plant=[
            ReversePlantReconciliation(plant=plant, source_population=population, categorised=categorised)
            for plant, (population, categorised) in r.by_plant.items()
        ],
    )
