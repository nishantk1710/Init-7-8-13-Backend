"""Recommendation list, detail and calculation-trace endpoints.

Read-only. Every value returned is read from ``i7_recommendation`` exactly as
Phase 7 wrote it -- no ADI, CV-squared, forecast or safety-stock formula is
recalculated here. Filtering and pagination happen in SQL; the full table is
never loaded into Python to be filtered or sliced in memory.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session, load_latest_recommendation
from app.models.i7_recommendation import Recommendation
from app.schemas.i7.errors import bad_request
from app.schemas.i7.recommendations import (
    RecommendationDetail,
    RecommendationListResponse,
    RecommendationSummary,
    RecommendationTrace,
)

router = APIRouter(tags=["i7-recommendations"])

MAX_PAGE_SIZE = 200
"""A safe upper bound: the frontend renders a table, not a bulk export. A
client that genuinely needs the full 45k+ rows should page through them."""

# Whitelisted API sort field -> ORM column. Never accept a raw column name
# from the query string -- that is how a sort parameter becomes a SQL
# injection vector or an accidental information leak through error messages.
SORT_FIELDS: dict[str, "object"] = {
    "generated_at": Recommendation.generated_at,
    "status": Recommendation.status,
    "material": Recommendation.sap_material_number,
    "plant": Recommendation.sap_plant_code,
    "demand_class": Recommendation.demand_class,
    "confidence": Recommendation.confidence,
}


def _apply_filters(
    statement: Select,
    *,
    status: str | None,
    plant: str | None,
    material: str | None,
    demand_class: str | None,
    is_oar: bool | None,
    confidence: str | None,
    criticality: str | None,
) -> Select:
    """Every filter here maps to a real, indexed, persisted column. Nothing
    that would require loading rows into Python to evaluate."""
    if status is not None:
        statement = statement.where(Recommendation.status == status)
    if plant is not None:
        statement = statement.where(Recommendation.sap_plant_code == plant)
    if material is not None:
        statement = statement.where(Recommendation.sap_material_number == material)
    if demand_class is not None:
        statement = statement.where(Recommendation.demand_class == demand_class)
    if is_oar is not None:
        statement = statement.where(Recommendation.is_oar == is_oar)
    if confidence is not None:
        statement = statement.where(Recommendation.confidence == confidence)
    if criticality is not None:
        statement = statement.where(Recommendation.criticality == criticality)
    return statement


@router.get(
    "/recommendations",
    response_model=RecommendationListResponse,
    summary="List I07 recommendations",
    description="Database-paginated, database-filtered. Supports the "
    "persisted fields status, plant, material, demand_class, is_oar, "
    "confidence and criticality; a field with no backing column is not "
    "offered as a filter (see docs/i07_api.md).",
)
def list_recommendations(
    session: Annotated[Session, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    sort: Annotated[str, Query()] = "generated_at",
    status: str | None = None,
    plant: str | None = None,
    material: str | None = None,
    demand_class: str | None = None,
    is_oar: bool | None = None,
    confidence: str | None = None,
    criticality: str | None = None,
) -> RecommendationListResponse:
    if sort not in SORT_FIELDS:
        raise bad_request(
            "INVALID_SORT_FIELD",
            f"'{sort}' is not a sortable field.",
            allowed=list(SORT_FIELDS),
        )

    base = _apply_filters(
        select(Recommendation),
        status=status,
        plant=plant,
        material=material,
        demand_class=demand_class,
        is_oar=is_oar,
        confidence=confidence,
        criticality=criticality,
    )

    total = session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()

    # A secondary, always-present tiebreaker column keeps ordering
    # deterministic when the sort field has duplicate values -- otherwise two
    # rows with the same status could swap position between page 1 and page 2.
    statement = (
        base.order_by(SORT_FIELDS[sort], Recommendation.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = session.execute(statement).scalars().all()

    return RecommendationListResponse(
        items=[RecommendationSummary.from_model(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/recommendations/{recommendation_id}",
    response_model=RecommendationDetail,
    summary="Get one recommendation",
    responses={404: {"description": "Recommendation not found"}},
)
def get_recommendation(
    recommendation_id: str, session: Annotated[Session, Depends(get_session)]
) -> RecommendationDetail:
    row = load_latest_recommendation(session, recommendation_id)
    return RecommendationDetail.from_model(row)


@router.get(
    "/recommendations/{recommendation_id}/trace",
    response_model=RecommendationTrace,
    summary="Get the calculation trace",
    description="The Phase 7 explanation trace exactly as generated -- never "
    "recomputed. For a blocked recommendation this shows where and why "
    "calculation stopped; for an OAR recommendation with similarity evidence "
    "but no weighted estimate, the neighbour count, confidence and estimate "
    "blocking reason are preserved rather than discarded.",
    responses={404: {"description": "Recommendation not found"}},
)
def get_recommendation_trace(
    recommendation_id: str, session: Annotated[Session, Depends(get_session)]
) -> RecommendationTrace:
    row = load_latest_recommendation(session, recommendation_id)
    return RecommendationTrace.from_model(row)
