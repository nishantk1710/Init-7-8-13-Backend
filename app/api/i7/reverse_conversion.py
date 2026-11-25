"""Min-Max -> OAR recommendations: list and CSV export.

Read-only. Nothing is stored, approved, executed or written back to SAP: each request returns the
recommendations computed from the staged consumption and the reverse-conversion policy, as of the
calculation anchor (the latest complete month), and says which anchor and thresholds it used.

Independent of the OAR -> Min-Max conversion: this router shares no code with it and sits under its own
prefix, ``/reverse-conversion``, so no ``/recommendations/{id}`` route can read it as an id.

The ``anchor`` query parameter (``YYYY-MM``) re-runs the calculation as of an earlier complete month, for
reproducible results (a planner-review sample, a regression check). It is validated: a month with no
staged data, or one that had not ended when the data was loaded, is refused.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.criticality import CriticalityTier
from app.core.db import get_db
from app.initiatives.i7.policy.reverse_conversion import current_reverse_conversion_policy
from app.initiatives.i7.reverse_conversion.errors import AnchorError
from app.initiatives.i7.reverse_conversion.export import rows_to_csv
from app.initiatives.i7.reverse_conversion.metrics import add_months
from app.initiatives.i7.reverse_conversion.service import (
    CATEGORY_BY_CODE,
    CONFIDENCE_BY_CODE,
    CRITICALITY_UNKNOWN,
    REASON_BY_CODE,
    ListFilters,
    ReverseConversionSource,
    ReverseReport,
    SqlSource,
    apply_filters,
    consumption_series,
    get_report,
    paginate,
)
from app.schemas.i7.errors import ApiError, bad_request
from app.schemas.i7.reverse_conversion import (
    ReverseConsumptionMonth,
    ReverseConsumptionResponse,
    ReversePolicyInfo,
    ReverseRecommendation,
    ReverseRecommendationsResponse,
    build_anchor,
    build_reconciliation,
    build_summary,
)

router = APIRouter(prefix="/reverse-conversion", tags=["i7-reverse-conversion"])

MAX_PAGE_SIZE = 200
_KNOWN_TIERS = {tier.value for tier in CriticalityTier}

CategoryCode = Literal["recommended_for_oar", "not_eligible"]
ConfidenceCode = Literal["high", "medium", "low"]
EligibilityCode = Literal["eligible", "not_eligible"]
LeadTimeBucket = Literal["0-30d", "31-60d", "61-90d", ">90d", "unknown"]


def get_reverse_source(session: Annotated[Session, Depends(get_db)]) -> ReverseConversionSource:
    """The data source of a request. A dependency so tests can hand in a fake one."""
    return SqlSource(session, current_reverse_conversion_policy())


def _filters(
    category: str | None,
    plant: str | None,
    reason: str | None,
    criticality: str | None,
    lead_time_bucket: str | None,
    search: str | None,
    confidence: str | None = None,
    eligibility: str | None = None,
) -> ListFilters:
    policy = current_reverse_conversion_policy()
    if plant is not None and plant not in policy.plants:
        raise bad_request("REVERSE_PLANT_INVALID", f"Plant must be one of {', '.join(policy.plants)}.", plant=plant)
    if reason is not None and reason not in REASON_BY_CODE:
        raise bad_request("REVERSE_REASON_INVALID", "Unknown reason code.", reason=reason, allowed=sorted(REASON_BY_CODE))
    tier = criticality.strip().upper() if criticality else None
    if tier is not None and tier != CRITICALITY_UNKNOWN.upper() and tier not in _KNOWN_TIERS:
        raise bad_request(
            "REVERSE_CRITICALITY_INVALID",
            "Criticality must be a known tier or 'unknown'.",
            criticality=criticality,
            allowed=sorted(_KNOWN_TIERS) + [CRITICALITY_UNKNOWN],
        )
    return ListFilters(
        category=CATEGORY_BY_CODE[category] if category else None,
        plant=plant,
        reason=REASON_BY_CODE[reason] if reason else None,
        confidence=CONFIDENCE_BY_CODE[confidence] if confidence else None,
        eligibility=eligibility,
        criticality=tier,
        lead_time_bucket=lead_time_bucket,
        search=search,
    )


def _report(source: ReverseConversionSource, anchor: str | None) -> ReverseReport:
    settings = get_settings()
    try:
        return get_report(
            source,
            current_reverse_conversion_policy(),
            override_month=anchor,
            configured_month=settings.i7_reverse_anchor_month,
            anchor_source=settings.i7_reverse_anchor_source,
        )
    except AnchorError as exc:
        if anchor:
            raise bad_request("REVERSE_ANCHOR_INVALID", str(exc), anchor=anchor) from exc
        raise ApiError(409, "REVERSE_ANCHOR_UNAVAILABLE", str(exc)) from exc


@router.get(
    "/recommendations",
    response_model=ReverseRecommendationsResponse,
    summary="Min-Max to OAR recommendations",
    description="Every in-scope Min-Max (VM/VB) material-plant of plants 1300 and 1500 with its category "
    "(Convert to OAR, Not Eligible), the reason and, for Convert to OAR, a confidence "
    "(High, Medium, Low); ranked inside its category by confidence then stock value. Read-only; returns the calculation anchor and the applied policy.",
)
def list_reverse_recommendations(
    source: Annotated[ReverseConversionSource, Depends(get_reverse_source)],
    category: CategoryCode | None = None,
    plant: str | None = None,
    reason: Annotated[str | None, Query(description="A reason code, e.g. `sparse_recurring_demand`.")] = None,
    confidence: ConfidenceCode | None = None,
    eligibility: Annotated[EligibilityCode | None, Query(description="`eligible` (Convert to OAR) or `not_eligible`.")] = None,
    criticality: Annotated[str | None, Query(description="A criticality tier, or `unknown`.")] = None,
    lead_time_bucket: LeadTimeBucket | None = None,
    search: Annotated[str | None, Query(max_length=100, description="Part of a material number or description.")] = None,
    anchor: Annotated[str | None, Query(max_length=7, description="Calculate as of this complete month, `YYYY-MM`.")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
) -> ReverseRecommendationsResponse:
    filters = _filters(category, plant, reason, criticality, lead_time_bucket, search, confidence, eligibility)
    report = _report(source, anchor)
    matching = apply_filters(report.rows, filters)
    return ReverseRecommendationsResponse(
        anchor=build_anchor(report),
        policy=ReversePolicyInfo(version=report.policy.policy_version, parameters=report.policy.parameters()),
        summary=build_summary(report),
        reconciliation=build_reconciliation(report),
        items=[ReverseRecommendation.from_row(row) for row in paginate(matching, page, page_size)],
        total=len(matching),
        page=page,
        page_size=page_size,
    )


@router.get(
    "/recommendations/{material}/{plant}/consumption",
    response_model=ReverseConsumptionResponse,
    summary="Consumption of one Min-Max to OAR recommendation",
    description="The last 24 months up to the calculation anchor for one listed material-plant, month by "
    "month (net issued quantity and issue counts), for the detail chart. 404 when the material-plant is "
    "not in the recommendation. Read-only.",
)
def reverse_recommendation_consumption(
    material: str,
    plant: str,
    source: Annotated[ReverseConversionSource, Depends(get_reverse_source)],
    anchor: Annotated[str | None, Query(max_length=7, description="Calculate as of this complete month, `YYYY-MM`.")] = None,
) -> ReverseConsumptionResponse:
    policy = current_reverse_conversion_policy()
    if plant not in policy.plants:
        raise bad_request("REVERSE_PLANT_INVALID", f"Plant must be one of {', '.join(policy.plants)}.", plant=plant)
    report = _report(source, anchor)
    if not any(row.material == material and row.plant == plant for row in report.rows):
        raise ApiError(404, "REVERSE_ROW_NOT_FOUND", "That material-plant is not in the Min-Max to OAR recommendation.")
    points = consumption_series(source, material, plant, report.anchor.month)
    return ReverseConsumptionResponse(
        material=material,
        plant=plant,
        anchor_month=f"{report.anchor.month:%Y-%m}",
        latest_window_start=f"{add_months(report.anchor.month, -(policy.window_months - 1)):%Y-%m}",
        months=[
            ReverseConsumptionMonth(
                month=f"{p.month:%Y-%m}",
                net_issued_quantity=p.net_issued_quantity,
                issue_count=p.issue_count,
                reversal_count=p.reversal_count,
            )
            for p in points
        ],
    )


@router.get(
    "/recommendations/export",
    summary="Export Min-Max to OAR recommendations as CSV",
    description="The recommendations the same filters select, every page, as a UTF-8 CSV with the "
    "calculation anchor and policy version on every row.",
    response_class=Response,
)
def export_reverse_recommendations(
    source: Annotated[ReverseConversionSource, Depends(get_reverse_source)],
    category: CategoryCode | None = None,
    plant: str | None = None,
    reason: str | None = None,
    confidence: ConfidenceCode | None = None,
    eligibility: EligibilityCode | None = None,
    criticality: str | None = None,
    lead_time_bucket: LeadTimeBucket | None = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    anchor: Annotated[str | None, Query(max_length=7)] = None,
) -> Response:
    filters = _filters(category, plant, reason, criticality, lead_time_bucket, search, confidence, eligibility)
    report = _report(source, anchor)
    content = rows_to_csv(apply_filters(report.rows, filters), report.anchor, report.policy)
    filename = f"i7-min-max-to-oar-{report.anchor.month:%Y-%m}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Anchor-Month": f"{report.anchor.month:%Y-%m}",
            "X-Policy-Version": report.policy.policy_version,
        },
    )
