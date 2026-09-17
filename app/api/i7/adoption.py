"""SAP adoption reconciliation, read-only.

Calls the existing Phase 7 evaluators directly -- no SAP client is imported
here, and none exists to import. ``NoSapStateAvailable`` (the only
``SapStateProvider`` implementation today) makes every call resolve to
UNKNOWN, which is the honest answer on an extract with no staged CDHDR/CDPOS.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session, load_latest_recommendation
from app.initiatives.i7.recommendations.adoption import (
    evaluate_conversion_adoption,
    evaluate_parameter_adoption,
)
from app.models.i7_recommendation import Recommendation
from app.schemas.i7.adoption import AdoptionResponse

router = APIRouter(tags=["i7-adoption"])


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

    if row.is_oar and row.conversion_eligibility == "ELIGIBLE":
        result = evaluate_conversion_adoption(
            row.sap_material_number, row.sap_plant_code, expected_mrp_type="VB"
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
        )

    return AdoptionResponse(
        recommendation_id=recommendation_id,
        status=result.status.value,
        expected=dict(result.expected),
        observed=dict(result.observed),
        matched_fields=list(result.matched_fields),
        mismatched_fields=list(result.mismatched_fields),
        detail=result.detail,
    )
