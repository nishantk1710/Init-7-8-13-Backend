"""SAP adoption reconciliation, read-only.

Calls the existing Phase 7 evaluators directly -- no SAP client is imported
here, and none exists to import. ``RawChangeDocumentProvider`` reads the raw
``raw_cdhdr``/``raw_cdpos`` extract tables (real data, no SAP call); on the
current extract every result is still UNKNOWN, because that extract's CDPOS
table contains no MATERIAL/MARC change rows at all -- see
``sap_change_documents.py`` for the verification.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session, load_latest_recommendation
from app.api.i7.recommendations import _apply_filters, _latest_only
from app.initiatives.i7.recommendations.adoption import (
    evaluate_conversion_adoption,
    evaluate_parameter_adoption,
)
from app.initiatives.i7.recommendations.sap_change_documents import RawChangeDocumentProvider
from app.initiatives.i7.recommendations.types import AdoptionResult
from app.models.i7_recommendation import Recommendation
from app.schemas.i7.adoption import AdoptionListItem, AdoptionListResponse, AdoptionResponse

router = APIRouter(tags=["i7-adoption"])

MAX_PAGE_SIZE = 200
"""Same bound as the recommendations list endpoint -- this is a table, not a
bulk export."""


def _evaluate_row(row: Recommendation, provider: RawChangeDocumentProvider) -> tuple[AdoptionResult, bool]:
    """The same reconciliation the single-record endpoint runs, factored out
    so the list endpoint computes it identically per row -- one evaluator
    call per recommendation, still read-only, still no SAP call."""
    is_conversion_adoption = bool(row.is_oar and row.conversion_eligibility == "ELIGIBLE")
    if is_conversion_adoption:
        result = evaluate_conversion_adoption(
            row.sap_material_number, row.sap_plant_code, expected_mrp_type="VB",
            provider=provider,
        )
    else:
        result = evaluate_parameter_adoption(
            row.sap_material_number,
            row.sap_plant_code,
            approved_safety_stock=(
                int(row.recommended_safety_stock) if row.recommended_safety_stock else None
            ),
            approved_rop=int(row.recommended_rop) if row.recommended_rop else None,
            approved_max_stock=(
                int(row.recommended_max_stock) if row.recommended_max_stock else None
            ),
            provider=provider,
        )
    return result, is_conversion_adoption


@router.get(
    "/recommendations/{recommendation_id}/adoption",
    response_model=AdoptionResponse,
    summary="Get SAP adoption reconciliation status",
    description="ADOPTED / PARTIALLY_ADOPTED / NOT_ADOPTED / UNKNOWN, "
    "computed read-only through the existing Phase 7 adoption evaluators. "
    "UNKNOWN means no SAP evidence is available -- it is never reported as "
    "NOT_ADOPTED. No SAP call is made; the current extract has no staged "
    "CDHDR/CDPOS, so every result on this data is UNKNOWN today.",
    responses={404: {"description": "Recommendation not found"}},
)
def get_adoption(
    recommendation_id: str, session: Annotated[Session, Depends(get_session)]
) -> AdoptionResponse:
    row: Recommendation = load_latest_recommendation(session, recommendation_id)
    provider = RawChangeDocumentProvider(session)
    result, is_conversion_adoption = _evaluate_row(row, provider)

    detail = result.detail
    if is_conversion_adoption and result.status.value == "UNKNOWN":
        # The literal, business-facing phrase for this specific case: no
        # ND/PD -> VB transition has been observed for this material-plant.
        # Never claimed as ADOPTED/NOT_ADOPTED from planning-field evidence
        # alone -- conversion adoption is a distinct check (ND/PD -> VB +
        # MINBE + MABST), not inferred from any other field changing.
        detail = "Awaiting SAP test change: " + detail

    return AdoptionResponse(
        recommendation_id=recommendation_id,
        status=result.status.value,
        expected=dict(result.expected),
        observed=dict(result.observed),
        matched_fields=list(result.matched_fields),
        mismatched_fields=list(result.mismatched_fields),
        detail=detail,
        is_conversion_adoption=is_conversion_adoption,
    )


@router.get(
    "/recommendations/adoption",
    response_model=AdoptionListResponse,
    summary="Portfolio-wide SAP adoption reconciliation",
    description="The same read-only ADOPTED / PARTIALLY_ADOPTED / NOT_ADOPTED "
    "/ UNKNOWN reconciliation as the per-recommendation endpoint, computed for "
    "a filtered, paginated page of recommendations -- for a dedicated "
    "adoption-tracking table rather than one recommendation at a time. Only "
    "material-plants with an actual calculated recommendation (a non-null "
    "recommended reorder point, safety stock or max stock) are listed -- "
    "there is nothing to reconcile adoption of for the far larger set that "
    "never reached a calculated value (see docs on the upstream data gaps). "
    "Supports the same filters as GET /recommendations. Registered before "
    "/{recommendation_id} so 'adoption' is never read as one.",
)
def list_adoption(
    session: Annotated[Session, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    status: str | None = None,
    plant: str | None = None,
    material: str | None = None,
    demand_class: str | None = None,
    is_oar: bool | None = None,
    confidence: str | None = None,
    criticality: str | None = None,
) -> AdoptionListResponse:
    # Adoption tracking is a per-material-plant view, not a per-run history --
    # see _latest_only's docstring for why a material-plant can have more
    # than one Recommendation row and why only the newest is eligible here.
    base = _latest_only(
        _apply_filters(
            select(Recommendation),
            status=status,
            plant=plant,
            material=material,
            demand_class=demand_class,
            is_oar=is_oar,
            confidence=confidence,
            criticality=criticality,
        )
    ).where(
        # Only list material-plants a value was actually calculated for --
        # the vast majority of the catalogue never reaches this point (see
        # the upstream criticality/history/lead-time data gaps), and there is
        # nothing for an adoption check to reconcile against for them.
        or_(
            Recommendation.recommended_rop.is_not(None),
            Recommendation.recommended_safety_stock.is_not(None),
            Recommendation.recommended_max_stock.is_not(None),
        ),
    )

    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()

    statement = (
        base.order_by(Recommendation.generated_at.desc(), Recommendation.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = session.execute(statement).scalars().all()

    provider = RawChangeDocumentProvider(session)
    items = []
    for row in rows:
        result, is_conversion_adoption = _evaluate_row(row, provider)
        items.append(
            AdoptionListItem(
                recommendation_id=row.recommendation_id,
                sap_material_number=row.sap_material_number,
                sap_plant_code=row.sap_plant_code,
                status=result.status.value,
                is_conversion_adoption=is_conversion_adoption,
                detail=result.detail,
            )
        )

    return AdoptionListResponse(items=items, total=total, page=page, page_size=page_size)
