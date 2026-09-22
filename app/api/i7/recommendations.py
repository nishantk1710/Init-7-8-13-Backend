"""Recommendation list, detail and calculation-trace endpoints.

Read-only. Every value returned is read from ``i7_recommendation`` exactly as
Phase 7 wrote it -- no ADI, CV-squared, forecast or safety-stock formula is
recalculated here. Filtering and pagination happen in SQL; the full table is
never loaded into Python to be filtered or sliced in memory.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session, load_latest_recommendation
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import routing
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedConsumption
from app.schemas.i7.errors import bad_request
from app.schemas.i7.recommendations import (
    CircuitCount,
    ConsumptionHistoryEntry,
    CriticalityCount,
    PlantCount,
    RecommendationDetail,
    RecommendationListResponse,
    RecommendationSummary,
    RecommendationSummaryStats,
    RecommendationTrace,
    RiskCount,
    StatusCount,
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
    generated_from: datetime | None,
    generated_to: datetime | None,
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
    if generated_from is not None:
        statement = statement.where(Recommendation.generated_at >= generated_from)
    if generated_to is not None:
        statement = statement.where(Recommendation.generated_at < generated_to)
    return statement


def _latest_only(statement: Select) -> Select:
    """Restrict to the newest ``Recommendation`` row per material-plant.

    A material-plant can have more than one row -- the pipeline regenerating
    under new upstream run ids legitimately produces a second row for the
    same material_number/plant_code (see app/models/i7_recommendation.py's
    uq_i7_recommendation_inputs). Every list/aggregate endpoint here reports
    one figure per material-plant, not one per pipeline run, so counting or
    listing without this filter overstates totals by however many times a
    material-plant has been recomputed -- the same "newest wins" rule
    app/api/i7/deps.py::load_latest_recommendation already applies to the
    single-item endpoint.
    """
    row_rank = (
        func.row_number()
        .over(
            partition_by=(Recommendation.sap_material_number, Recommendation.sap_plant_code),
            order_by=(Recommendation.generated_at.desc(), Recommendation.id.desc()),
        )
        .label("row_rank")
    )
    # Window functions cannot appear directly in a WHERE clause, so rank in a
    # subquery first and filter on that.
    ranked = select(Recommendation.id, row_rank).subquery()
    latest_ids = select(ranked.c.id).where(ranked.c.row_rank == 1)
    return statement.where(Recommendation.id.in_(latest_ids))


@router.get(
    "/recommendations",
    response_model=RecommendationListResponse,
    summary="List I07 recommendations",
    description="Database-paginated, database-filtered. Supports the "
    "persisted fields status, plant, material, demand_class, is_oar, "
    "confidence, criticality and a generated_at date range; a field with no "
    "backing column is not offered as a filter (see docs/i07_api.md).",
)
def list_recommendations(
    session: Annotated[Session, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    sort: Annotated[str, Query()] = "generated_at",
    sort_desc: Annotated[
        bool,
        Query(description="Descending order on `sort` (e.g. newest-first on generated_at)."),
    ] = False,
    status: str | None = None,
    plant: str | None = None,
    material: str | None = None,
    demand_class: str | None = None,
    is_oar: bool | None = None,
    confidence: str | None = None,
    criticality: str | None = None,
    generated_from: Annotated[
        datetime | None,
        Query(description="Inclusive lower bound on generated_at -- e.g. a report period's start."),
    ] = None,
    generated_to: Annotated[
        datetime | None,
        Query(description="Exclusive upper bound on generated_at -- e.g. a report period's end, plus one day."),
    ] = None,
) -> RecommendationListResponse:
    if sort not in SORT_FIELDS:
        raise bad_request(
            "INVALID_SORT_FIELD",
            f"'{sort}' is not a sortable field.",
            allowed=list(SORT_FIELDS),
        )

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
            generated_from=generated_from,
            generated_to=generated_to,
        )
    )

    total = session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()

    # A secondary, always-present tiebreaker column keeps ordering
    # deterministic when the sort field has duplicate values -- otherwise two
    # rows with the same status could swap position between page 1 and page 2.
    # The tiebreaker follows the same direction as the primary sort so a
    # descending page and an ascending page never interleave the same tied
    # group differently.
    sort_column = SORT_FIELDS[sort]
    order = (sort_column.desc(), Recommendation.id.desc()) if sort_desc else (sort_column, Recommendation.id)
    statement = (
        base.order_by(*order)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = session.execute(statement).scalars().all()

    # One shared PolicyDocument for the whole page, not one per row -- the
    # policy lookup routing.route_for() does is pure/in-memory (no DB call),
    # so this stays zero extra query cost per the module's own docstring.
    policy = PolicyDocument()

    return RecommendationListResponse(
        items=[
            RecommendationSummary.from_model(
                row, tuple(role.value for role in routing.route_for(row.is_oar, row.criticality, policy))
            )
            for row in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/recommendations/summary",
    response_model=RecommendationSummaryStats,
    summary="Portfolio-wide recommendation aggregates",
    description="COUNT/SUM aggregates over every recommendation matching the "
    "same filters as the list endpoint (status, plant, material, "
    "demand_class, is_oar, confidence, criticality) -- for dashboards/KPI "
    "cards that need a whole-portfolio figure, not one page of rows. "
    "Registered before /{recommendation_id} so 'summary' is never read as an "
    "id.",
)
def get_recommendation_summary(
    session: Annotated[Session, Depends(get_session)],
    status: str | None = None,
    plant: str | None = None,
    material: str | None = None,
    demand_class: str | None = None,
    is_oar: bool | None = None,
    confidence: str | None = None,
    criticality: str | None = None,
) -> RecommendationSummaryStats:
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
            generated_from=None,
            generated_to=None,
        )
    )

    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()

    by_status = session.execute(
        base.with_only_columns(Recommendation.status, func.count())
        .group_by(Recommendation.status)
    ).all()

    by_criticality = session.execute(
        base.with_only_columns(Recommendation.criticality, func.count())
        .group_by(Recommendation.criticality)
    ).all()

    by_circuit = session.execute(
        base.with_only_columns(Recommendation.circuit, func.count())
        .group_by(Recommendation.circuit)
    ).all()

    # Mirrors the frontend's own deriveRisk() (services/i7-api.ts) exactly, in
    # SQL, over the whole filtered set rather than one fetched page -- see
    # RiskCount's docstring. OPEN_STATUSES here must stay the exact backend-
    # status set whose STATUS_MAP display reduction is "Pending Review" /
    # "In Approval" / "Returned" on the frontend.
    open_statuses = ("NOT_EVALUABLE", "READY_FOR_REVIEW", "PENDING_APPROVAL", "HELD", "SENT_BACK", "ADJUSTED")
    risk_case = case(
        (Recommendation.status.not_in(open_statuses), "low"),
        (Recommendation.criticality.is_(None), "low"),
        (Recommendation.criticality == "CRITICAL", "critical"),
        (Recommendation.criticality == "IMPACT", "high"),
        (Recommendation.criticality == "INSURANCE", "medium"),
        else_="low",
    )
    by_risk = session.execute(
        base.with_only_columns(risk_case.label("risk"), func.count()).group_by(risk_case)
    ).all()

    # Ordered by plant code so a filter dropdown built from this is stable
    # between requests rather than reordering as counts shift.
    by_plant = session.execute(
        base.with_only_columns(Recommendation.sap_plant_code, func.count())
        .group_by(Recommendation.sap_plant_code)
        .order_by(Recommendation.sap_plant_code)
    ).all()

    oar_count = session.execute(
        select(func.count()).select_from(
            base.where(Recommendation.is_oar.is_(True)).subquery()
        )
    ).scalar_one()
    normal_count = session.execute(
        select(func.count()).select_from(
            base.where(Recommendation.is_oar.is_(False)).subquery()
        )
    ).scalar_one()

    status_counts = {row[0]: row[1] for row in by_status}
    awaiting_approval_count = status_counts.get("PENDING_APPROVAL", 0) + status_counts.get(
        "HELD", 0
    )
    ready_for_review_count = status_counts.get("READY_FOR_REVIEW", 0)
    not_evaluable_count = status_counts.get("NOT_EVALUABLE", 0)

    net_value_row = session.execute(
        base.with_only_columns(
            func.sum(
                Recommendation.unit_price
                * (Recommendation.current_safety_stock - Recommendation.recommended_safety_stock)
            )
        ).where(
            Recommendation.unit_price.isnot(None),
            Recommendation.current_safety_stock.isnot(None),
            Recommendation.recommended_safety_stock.isnot(None),
        )
    ).scalar()

    return RecommendationSummaryStats(
        total=total,
        by_status=[StatusCount(status=s, count=c) for s, c in by_status],
        by_criticality=[CriticalityCount(criticality=cr, count=c) for cr, c in by_criticality],
        by_circuit=[CircuitCount(circuit=ci, count=c) for ci, c in by_circuit],
        by_risk=[RiskCount(risk=r, count=c) for r, c in by_risk],
        by_plant=[PlantCount(plant=p, count=c) for p, c in by_plant],
        oar_count=oar_count,
        normal_count=normal_count,
        awaiting_approval_count=awaiting_approval_count,
        ready_for_review_count=ready_for_review_count,
        not_evaluable_count=not_evaluable_count,
        net_safety_stock_value_impact=net_value_row,
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
    consumption_rows = session.execute(
        select(StagedConsumption.period, StagedConsumption.quantity)
        .where(
            StagedConsumption.sap_material_number == row.sap_material_number,
            StagedConsumption.sap_plant_code == row.sap_plant_code,
        )
        .order_by(StagedConsumption.period)
    ).all()
    consumption_history = tuple(
        ConsumptionHistoryEntry(period=period, quantity=quantity) for period, quantity in consumption_rows
    )
    return RecommendationDetail.from_model(row, consumption_history=consumption_history)


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
