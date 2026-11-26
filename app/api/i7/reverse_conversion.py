"""Min-Max -> OAR recommendations: list, CSV export and a material's consumption.

Read-only toward SAP. The recommendations are the ones the pipeline's ``reverse_conversion`` stage stored (the
newest run), as of the calculation month it recorded; every response says which month, which run and which
thresholds. Before the first run there is nothing to show and the list answers 409 ``REVERSE_NOT_BUILT``.

Independent of the OAR -> Min-Max conversion: this router shares no code with it and sits under its own
prefix, ``/reverse-conversion``, so no ``/recommendations/{id}`` route can read it as an id. The requests to
convert a material, and their approval, are in ``reverse_conversion_requests.py``.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.criticality import CriticalityTier
from app.core.db import get_db
from app.initiatives.i7.policy.reverse_conversion import current_reverse_conversion_policy
from app.initiatives.i7.reverse_conversion.export import rows_to_csv
from app.initiatives.i7.reverse_conversion.metrics import add_months
from app.initiatives.i7.reverse_conversion import repository
from app.initiatives.i7.reverse_conversion.snapshot import latest_request_briefs, load_latest_report
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
    paginate,
)
from app.schemas.i7.errors import ApiError, bad_request
from app.schemas.i7.reverse_conversion import (
    ReverseConsumptionMonth,
    ReverseConsumptionResponse,
    ReverseMinMaxAlternative,
    ReversePolicyInfo,
    ReverseRecommendation,
    ReverseRecommendationsResponse,
    ReverseRequestBrief,
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


def get_reverse_report(session: Annotated[Session, Depends(get_db)]) -> ReverseReport:
    """The newest stored run as a report. A dependency so tests can hand in a calculated one."""
    report = load_latest_report(session)
    if report is None:
        raise ApiError(
            409,
            "REVERSE_NOT_BUILT",
            "The Min-Max to OAR recommendation has not been built yet. It is built by the pipeline after the OAR step; "
            "if it has run, check that a calculation month is set (I7_REVERSE_ANCHOR_MONTH) and loaded.",
        )
    return report


BriefLookup = Callable[[list[tuple[str, str]]], dict[tuple[str, str], tuple[int, str, str | None]]]


def get_brief_lookup(session: Annotated[Session, Depends(get_db)]) -> BriefLookup:
    """How a list finds each material-plant's newest conversion request. A dependency so tests can hand in a fake."""
    return lambda keys: latest_request_briefs(session, keys)


AlternativeLookup = Callable[[list[tuple[str, str]]], dict[tuple[str, str], repository.MinMaxAlternative]]

#: Confidence levels that show the Stock item recommendation beside Convert to OAR. High is sure enough to stand alone.
ALTERNATIVE_SHOWN_FOR = frozenset({"Medium", "Low"})


def get_alternative_lookup(session: Annotated[Session, Depends(get_db)]) -> AlternativeLookup:
    """How a list finds the Stock item recommendation of a material-plant. A dependency so tests can hand in a fake."""
    return lambda keys: repository.fetch_alternatives(session, keys)


def _alternatives(lookup: AlternativeLookup, rows) -> dict[tuple[str, str], ReverseMinMaxAlternative]:
    """The Stock item recommendation beside each Medium or Low confidence row of the page, in one query."""
    wanted = [(row.material, row.plant) for row in rows if row.confidence is not None and row.confidence.value in ALTERNATIVE_SHOWN_FOR]
    return {
        key: ReverseMinMaxAlternative(
            current_rop=found.current_rop,
            recommended_rop=found.recommended_rop,
            current_max_stock=found.current_max_stock,
            recommended_max_stock=found.recommended_max_stock,
        )
        for key, found in lookup(wanted).items()
    }


def _briefs(lookup: BriefLookup, rows) -> dict[tuple[str, str], ReverseRequestBrief]:
    """The newest conversion request of each listed material-plant, in one query."""
    return {
        key: ReverseRequestBrief(id=found[0], status=found[1], pending_role=found[2])
        for key, found in lookup([(row.material, row.plant) for row in rows]).items()
    }


@router.get(
    "/recommendations",
    response_model=ReverseRecommendationsResponse,
    summary="Min-Max to OAR recommendations",
    description="Every in-scope Min-Max (VM/VB) material-plant of plants 1300 and 1500 with its category "
    "(Convert to OAR, Not Eligible), the reason and, for Convert to OAR, a confidence "
    "(High, Medium, Low); ranked inside its category by confidence then stock value. Read-only; returns the calculation anchor and the applied policy.",
)
def list_reverse_recommendations(
    report: Annotated[ReverseReport, Depends(get_reverse_report)],
    lookup: Annotated[BriefLookup, Depends(get_brief_lookup)],
    alternative_lookup: Annotated[AlternativeLookup, Depends(get_alternative_lookup)],
    category: CategoryCode | None = None,
    plant: str | None = None,
    reason: Annotated[str | None, Query(description="A reason code, e.g. `sparse_recurring_demand`.")] = None,
    confidence: ConfidenceCode | None = None,
    eligibility: Annotated[EligibilityCode | None, Query(description="`eligible` (Convert to OAR) or `not_eligible`.")] = None,
    criticality: Annotated[str | None, Query(description="A criticality tier, or `unknown`.")] = None,
    lead_time_bucket: LeadTimeBucket | None = None,
    search: Annotated[str | None, Query(max_length=100, description="Part of a material number or description.")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
) -> ReverseRecommendationsResponse:
    filters = _filters(category, plant, reason, criticality, lead_time_bucket, search, confidence, eligibility)
    matching = apply_filters(report.rows, filters)
    page_rows = paginate(matching, page, page_size)
    briefs = _briefs(lookup, page_rows)
    alternatives = _alternatives(alternative_lookup, page_rows)
    return ReverseRecommendationsResponse(
        anchor=build_anchor(report),
        policy=ReversePolicyInfo(version=report.policy.policy_version, parameters=report.policy.parameters()),
        summary=build_summary(report),
        reconciliation=build_reconciliation(report),
        items=[
            ReverseRecommendation.from_row(row, briefs.get((row.material, row.plant)), alternatives.get((row.material, row.plant)))
            for row in page_rows
        ],
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
    report: Annotated[ReverseReport, Depends(get_reverse_report)],
    source: Annotated[ReverseConversionSource, Depends(get_reverse_source)],
) -> ReverseConsumptionResponse:
    policy = current_reverse_conversion_policy()
    if plant not in policy.plants:
        raise bad_request("REVERSE_PLANT_INVALID", f"Plant must be one of {', '.join(policy.plants)}.", plant=plant)
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
    report: Annotated[ReverseReport, Depends(get_reverse_report)],
    category: CategoryCode | None = None,
    plant: str | None = None,
    reason: str | None = None,
    confidence: ConfidenceCode | None = None,
    eligibility: EligibilityCode | None = None,
    criticality: str | None = None,
    lead_time_bucket: LeadTimeBucket | None = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
) -> Response:
    filters = _filters(category, plant, reason, criticality, lead_time_bucket, search, confidence, eligibility)
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
