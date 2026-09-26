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
from app.models.i7_forecast import ForecastBacktestPath
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedConsumption, StagedStock
from app.schemas.i7.errors import bad_request
from app.schemas.i7.recommendations import (
    CircuitCount,
    ConsumptionHistoryEntry,
    CriticalityCount,
    ForecastHistoryPoint,
    ForecastHistoryResponse,
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
    # Performance (2026-09-22): _latest_only(base) wraps a row_number() OVER
    # (...) window function computed across the WHOLE table. Reusing the
    # `base` Select object across ~12 separate aggregate queries below does
    # NOT share that computation between them -- each session.execute() call
    # re-plans and re-runs the window function from scratch, independently.
    # Measured on the real 113k-row table: ~2.2s per re-run x 12+ queries =
    # this endpoint took over 60s.
    #
    # Fix: GROUP BY sap_material_number, sap_plant_code, MAX(id) is the same
    # "latest row per material-plant" answer as the row_number() rank (id is
    # autoincrement, so MAX(id) agrees with _latest_only's
    # generated_at DESC, id DESC ordering), computed as ONE subquery kept in
    # SQL and reused across every aggregate below via a JOIN -- never
    # materialised into a Python list (an id.in_([...]) over 113k ids was
    # tried first and hit Postgres's 65535-bind-parameter limit outright).
    # Measured: ~0.3s once, join cost is then part of each aggregate's own
    # (already fast) query plan -- roughly a 10x+ reduction, and no
    # parameter-count ceiling to hit as the table grows.
    latest_per_material_plant = (
        select(func.max(Recommendation.id).label("id"))
        .group_by(Recommendation.sap_material_number, Recommendation.sap_plant_code)
        .subquery()
    )

    def _scope(statement: Select) -> Select:
        return _apply_filters(
            statement.join(
                latest_per_material_plant,
                latest_per_material_plant.c.id == Recommendation.id,
            ),
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

    base = _scope(select(Recommendation))

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
    # Grouped over a subquery column, not the CASE itself: SQL Server binds the
    # CASE's literals as fresh parameters in GROUP BY and then refuses to treat
    # the two copies as the same expression.
    risk_rows = base.with_only_columns(risk_case.label("risk")).subquery()
    by_risk = session.execute(
        select(risk_rows.c.risk, func.count()).group_by(risk_rows.c.risk)
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
            base.where(Recommendation.is_oar).subquery()
        )
    ).scalar_one()
    normal_count = session.execute(
        select(func.count()).select_from(
            base.where(~Recommendation.is_oar).subquery()
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

    # --- Critical Stockout Risk: current on-hand stock < recommended ROP ---
    # I07-derived proxy (2026-09-22), not a Vedanta-confirmed severity tier --
    # see RecommendationSummaryStats.critical_stockout_risk_count's docstring.
    # i7_staged_stock is material-plant-storage-location grain; aggregate to
    # material-plant before comparing, same as Phase 3's own assumption
    # (unrestricted_use_stock only -- see StagedStock's class docstring).
    on_hand_stock = (
        select(
            StagedStock.sap_material_number,
            StagedStock.sap_plant_code,
            func.sum(StagedStock.unrestricted_use_stock).label("on_hand"),
        )
        .group_by(StagedStock.sap_material_number, StagedStock.sap_plant_code)
        .subquery()
    )
    stockout_risk_base = base.join(
        on_hand_stock,
        (on_hand_stock.c.sap_material_number == Recommendation.sap_material_number)
        & (on_hand_stock.c.sap_plant_code == Recommendation.sap_plant_code),
    ).where(
        Recommendation.recommended_rop.isnot(None),
        on_hand_stock.c.on_hand < Recommendation.recommended_rop,
    )
    stockout_risk_count = session.execute(
        select(func.count()).select_from(stockout_risk_base.subquery())
    ).scalar_one()

    # --- Excess Inventory Candidates: current Max Stock > recommended ------
    # I07-derived proxy (2026-09-22): any positive gap counts, no minimum
    # margin -- see RecommendationSummaryStats.excess_inventory_candidates_count.
    excess_base = base.where(
        Recommendation.current_max_stock.isnot(None),
        Recommendation.recommended_max_stock.isnot(None),
        Recommendation.current_max_stock > Recommendation.recommended_max_stock,
    )
    excess_inventory_count = session.execute(
        select(func.count()).select_from(excess_base.subquery())
    ).scalar_one()
    excess_opportunity_row = session.execute(
        excess_base.with_only_columns(
            func.sum(
                Recommendation.unit_price
                * (Recommendation.current_max_stock - Recommendation.recommended_max_stock)
            )
        ).where(Recommendation.unit_price.isnot(None))
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
        critical_stockout_risk_count=stockout_risk_count,
        excess_inventory_candidates_count=excess_inventory_count,
        excess_inventory_opportunity=excess_opportunity_row,
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


@router.get(
    "/recommendations/{recommendation_id}/forecast-history",
    response_model=ForecastHistoryResponse,
    summary="Champion model's predicted-vs-actual history",
    description="Real rolling-origin predictions from the CHAMPION model's "
    "own backtest (i7_forecast_backtest_path, added 2026-09-22), for the "
    "Forecast vs Actual Demand chart -- never a client-side approximation. "
    "points is empty, never fabricated, when no forecast run since the "
    "table shipped has produced paths for this material-plant.",
    responses={404: {"description": "Recommendation not found"}},
)
def get_recommendation_forecast_history(
    recommendation_id: str, session: Annotated[Session, Depends(get_session)]
) -> ForecastHistoryResponse:
    row = load_latest_recommendation(session, recommendation_id)

    latest_run_with_paths = session.execute(
        select(ForecastBacktestPath.forecast_run_id)
        .where(
            ForecastBacktestPath.sap_material_number == row.sap_material_number,
            ForecastBacktestPath.sap_plant_code == row.sap_plant_code,
        )
        .order_by(ForecastBacktestPath.forecast_run_id.desc())
        .limit(1)
    ).scalar_one_or_none()

    if latest_run_with_paths is None:
        return ForecastHistoryResponse(
            sap_material_number=row.sap_material_number,
            sap_plant_code=row.sap_plant_code,
            model_name=None,
            points=(),
        )

    path_rows = session.execute(
        select(
            ForecastBacktestPath.model_name,
            ForecastBacktestPath.forecast_period,
            ForecastBacktestPath.predicted,
            ForecastBacktestPath.actual,
        )
        .where(
            ForecastBacktestPath.sap_material_number == row.sap_material_number,
            ForecastBacktestPath.sap_plant_code == row.sap_plant_code,
            ForecastBacktestPath.forecast_run_id == latest_run_with_paths,
        )
        .order_by(ForecastBacktestPath.forecast_period)
    ).all()

    return ForecastHistoryResponse(
        sap_material_number=row.sap_material_number,
        sap_plant_code=row.sap_plant_code,
        model_name=path_rows[0].model_name if path_rows else None,
        points=tuple(
            ForecastHistoryPoint(
                forecast_period=r.forecast_period, predicted=r.predicted, actual=r.actual
            )
            for r in path_rows
        ),
    )
