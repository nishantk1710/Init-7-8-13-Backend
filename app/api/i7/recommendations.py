"""Recommendation list, detail and calculation-trace endpoints.

Read-only. Every value returned is read from ``i7_recommendation`` exactly as
Phase 7 wrote it -- no ADI, CV-squared, forecast or safety-stock formula is
recalculated here. Filtering and pagination happen in SQL; the full table is
never loaded into Python to be filtered or sliced in memory.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, and_, case, exists, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session, load_latest_recommendation
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import routing
from app.initiatives.i7.smooth_demand import SMOOTH_DEMAND_CLASS
from app.initiatives.i7.stockout_risk.counts import stockout_risk_counts
from app.initiatives.i7.recommendations.consumption_history import monthly_consumption_history
from app.models.i7_features import DecliningDemandEvaluation
from app.models.i7_forecast import Forecast, ForecastBacktestPath, ForecastRun
from app.models.i7_oar import OarNeighbour
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant
from app.schemas.i7.errors import bad_request
from app.schemas.i7.recommendations import (
    CircuitCount,
    ConsumptionHistoryEntry,
    CriticalityCount,
    ForecastHistoryAggregatePoint,
    ForecastHistoryAggregateResponse,
    ForecastHistoryPoint,
    ForecastHistoryResponse,
    PlantCount,
    RecommendationDetail,
    RecommendationListResponse,
    RecommendationSummary,
    RecommendationSummaryStats,
    RecommendationTrace,
    RiskCount,
    SimilarMaterial,
    SimilarMaterialsResponse,
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


# The backend statuses whose display reduction on the frontend (services/i7-api.ts STATUS_MAP) is
# "Pending Review", "In Approval" or "Returned" -- the states where a recommendation still needs
# a decision, and so where its criticality counts as a stockout risk.
OPEN_STATUSES = ("NOT_EVALUABLE", "READY_FOR_REVIEW", "PENDING_APPROVAL", "HELD", "SENT_BACK", "ADJUSTED")

# The display status the table shows -> the backend statuses behind it. Mirrors STATUS_MAP on the
# frontend, so a filter on "In Approval" finds exactly the rows the table labels "In Approval".
STATUS_GROUPS: dict[str, tuple[str, ...]] = {
    "Pending Review": ("NOT_EVALUABLE", "READY_FOR_REVIEW"),
    "In Approval": ("PENDING_APPROVAL", "HELD", "ADJUSTED"),
    "Returned": ("SENT_BACK",),
    "Approved": ("APPROVED", "SAP_EXECUTION_PENDING"),
    "Rejected": ("REJECTED",),
    "Implemented": ("SAP_EXECUTED", "ADOPTED", "PARTIALLY_ADOPTED", "NOT_ADOPTED"),
}

# The criticality tier -> the display level, mirroring CRITICALITY_MAP on the frontend. NORMAL,
# OBSOLETE, an unrecognised tier and no tier at all are all "Low".
CRITICALITY_TIERS: dict[str, str] = {"Critical": "CRITICAL", "High": "IMPACT", "Medium": "INSURANCE"}


def _risk_case():
    """Stockout risk, derived the way the frontend's deriveRisk() does: only a recommendation still
    awaiting a decision can be at risk, and then only by its criticality tier."""
    return case(
        (Recommendation.status.not_in(OPEN_STATUSES), "low"),
        (Recommendation.criticality.is_(None), "low"),
        (Recommendation.criticality == "CRITICAL", "critical"),
        (Recommendation.criticality == "IMPACT", "high"),
        (Recommendation.criticality == "INSURANCE", "medium"),
        else_="low",
    )


def _actionable():
    """A recommendation with something to adopt in SAP.

    * **OAR** -- only when it is eligible for the Min-Max conversion (and a value was calculated):
      the conversion is the one change an OAR recommendation proposes, so a not-eligible or
      undecided one has nothing to adopt.
    * **Stock item** (anything not OAR) -- only when at least one of reorder point, safety stock
      and maximum stock is recommended and differs from SAP's current value. A recommended value
      against a current one SAP leaves blank is a change. Three equal values are not.

    One definition for the Recommendations list (``actionable=true``) and for every SAP adoption
    count, so the two always count the same material-plants.
    """

    def changes(recommended, current):
        return and_(recommended.is_not(None), or_(current.is_(None), recommended != current))

    has_value = or_(
        Recommendation.recommended_rop.is_not(None),
        Recommendation.recommended_safety_stock.is_not(None),
        Recommendation.recommended_max_stock.is_not(None),
    )
    not_oar = or_(~Recommendation.is_oar, Recommendation.is_oar.is_(None))
    return or_(
        and_(Recommendation.is_oar, Recommendation.conversion_eligibility == "ELIGIBLE", has_value),
        and_(
            not_oar,
            or_(
                changes(Recommendation.recommended_rop, Recommendation.current_rop),
                changes(Recommendation.recommended_safety_stock, Recommendation.current_safety_stock),
                changes(Recommendation.recommended_max_stock, Recommendation.current_max_stock),
            ),
        ),
    )


def _not_smooth():
    """A SMOOTH material-plant is not recommended -- its current SAP values stand (see
    app.initiatives.i7.smooth_demand) -- so a SMOOTH row written before that rule is not listed
    or counted. Kept out of ``_apply_filters``, which adoption tracking shares: a SMOOTH
    recommendation already approved or adopted stays visible there."""
    # An OAR recommendation is never one of those: OAR materials are sized from similar
    # materials whatever their demand class, so a SMOOTH OAR row is a real recommendation.
    return or_(
        Recommendation.is_oar,
        Recommendation.demand_class.is_(None),
        Recommendation.demand_class != SMOOTH_DEMAND_CLASS,
    )


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
    demand_type: Literal["oar", "declining", "normal", "stock_item"] | None = None,
    search: str | None = None,
    criticality_level: str | None = None,
    status_group: str | None = None,
    risk: str | None = None,
    conversion_eligibility: str | None = None,
    consumption_above_threshold: bool | None = None,
    actionable: bool | None = None,
) -> Select:
    """Every filter here maps to a real, persisted column or a keyed lookup.
    Nothing that would require loading rows into Python to evaluate.

    ``demand_type`` splits the catalogue three ways with no overlap: ``oar``
    (sized from similar materials), ``declining`` (not OAR, and flagged by the
    declining-demand rule in the feature run this recommendation was built
    from) and ``normal`` (everything else). OAR is decided first, so a
    material is never in two.

    ``stock_item`` is ``declining`` and ``normal`` together: everything that is not OAR.
    """
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
    if conversion_eligibility is not None:
        statement = statement.where(Recommendation.conversion_eligibility == conversion_eligibility)
    if actionable:
        statement = statement.where(_actionable())
    if consumption_above_threshold:
        # Issued more than the policy's own count in the trailing 12 months -- the threshold
        # stored on the row, not a literal, so it follows the policy. A row with no count (never
        # evaluated) cannot be shown to pass, so the comparison to NULL leaves it out; zero
        # consumption fails for any non-negative threshold.
        statement = statement.where(
            Recommendation.consumption_count_12m > Recommendation.consumption_count_threshold
        )
    if criticality is not None:
        statement = statement.where(Recommendation.criticality == criticality)
    if generated_from is not None:
        statement = statement.where(Recommendation.generated_at >= generated_from)
    if generated_to is not None:
        statement = statement.where(Recommendation.generated_at < generated_to)
    if search is not None and search.strip():
        # Free text over the whole catalogue: part of the material number (stored with its
        # leading zeros, so "8000005029" is found inside it) or of its description.
        text = search.strip()
        by_description = select(StagedMaterial.sap_material_number).where(
            StagedMaterial.description.icontains(text, autoescape=True)
        )
        statement = statement.where(
            or_(
                Recommendation.sap_material_number.icontains(text, autoescape=True),
                Recommendation.sap_material_number.in_(by_description),
            )
        )
    if criticality_level is not None:
        if criticality_level == "Low":
            statement = statement.where(
                or_(
                    Recommendation.criticality.is_(None),
                    Recommendation.criticality.not_in(tuple(CRITICALITY_TIERS.values())),
                )
            )
        else:
            statement = statement.where(Recommendation.criticality == CRITICALITY_TIERS[criticality_level])
    if status_group is not None:
        if status_group == "Pending Review":
            # The frontend shows any status it does not recognise as Pending Review too.
            others = tuple(raw for name, raws in STATUS_GROUPS.items() if name != status_group for raw in raws)
            statement = statement.where(Recommendation.status.not_in(others))
        else:
            statement = statement.where(Recommendation.status.in_(STATUS_GROUPS[status_group]))
    if risk is not None:
        # The same CASE the summary groups by. "high" is the merged tier the pages show --
        # critical and high together -- so a High filter agrees with the High badge on every row;
        # "critical" still asks for the CRITICAL tier alone.
        risk_case = _risk_case()
        statement = statement.where(risk_case.in_(("critical", "high")) if risk == "high" else risk_case == risk)
    if demand_type is not None:
        # The boolean column itself: SQL Server has no "IS 1" (see tests/i7/test_overview_real_counts.py).
        not_oar = or_(~Recommendation.is_oar, Recommendation.is_oar.is_(None))
        if demand_type == "oar":
            statement = statement.where(Recommendation.is_oar)
        elif demand_type == "stock_item":
            statement = statement.where(not_oar)
        elif demand_type == "declining":
            statement = statement.where(not_oar, _flagged_declining())
        else:
            statement = statement.where(not_oar, ~_flagged_declining())
    return statement


def _flagged_declining():
    """This recommendation's material-plant was flagged DECLINING_DEMAND in the
    feature run the recommendation was built from -- read from the evaluation the
    feature build stored, never re-derived here."""
    return exists().where(
        DecliningDemandEvaluation.feature_run_id == Recommendation.feature_run_id,
        DecliningDemandEvaluation.sap_material_number == Recommendation.sap_material_number,
        DecliningDemandEvaluation.sap_plant_code == Recommendation.sap_plant_code,
        DecliningDemandEvaluation.status == "DECLINING_DEMAND",
    )


def _declining_keys_for(session: Session, rows) -> set[tuple[str, str, int]]:
    """``(material, plant, feature_run_id)`` of the page's rows that were flagged.

    One query for the whole page. A database that does not have the evaluation
    table yet (the migration is applied on purpose) simply has no flags, rather
    than failing the list."""
    if not rows:
        return set()
    try:
        found = session.execute(
            select(
                DecliningDemandEvaluation.sap_material_number,
                DecliningDemandEvaluation.sap_plant_code,
                DecliningDemandEvaluation.feature_run_id,
            ).where(
                DecliningDemandEvaluation.status == "DECLINING_DEMAND",
                DecliningDemandEvaluation.sap_material_number.in_({row.sap_material_number for row in rows}),
                DecliningDemandEvaluation.feature_run_id.in_({row.feature_run_id for row in rows}),
            )
        ).all()
    except SQLAlchemyError:
        session.rollback()
        return set()
    return {(material, plant, run) for material, plant, run in found}


def _demand_type(row, declining_keys: set[tuple[str, str, int]]) -> str:
    """OAR, DECLINING or NORMAL -- the same split the ``type`` filter makes."""
    if row.is_oar:
        return "OAR"
    if (row.sap_material_number, row.sap_plant_code, row.feature_run_id) in declining_keys:
        return "DECLINING"
    return "NORMAL"


def _display_fields_for(session: Session, materials: set[str]) -> dict[str, tuple[str | None, str | None]]:
    """``material -> (description, currency)`` from staging, for one page of rows.

    Neither value is stored on the recommendation row. The description is
    display-only and feeds no calculation, so it is read from
    ``i7_staged_material`` (staged from MAKT, English preferred) rather than
    snapshotted onto every recommendation -- that would be right for
    ``unit_price``, where the value in force at generation time is itself the
    fact being recorded, but a description has no such property. The currency
    travels with the price SAP gave (WAERS, staged alongside the unit price on
    ``i7_staged_material_plant``), so it is read from the same place rather than
    stored a second time. A price is a plant's, so a material's currency is
    shown only when every plant that states one agrees.

    Two ``IN`` queries over the page's materials, never a lookup per row and
    never one query per field. A 200-row page costs two extra queries rather
    than 200. A material absent from staging, or carrying no value for a field,
    maps to ``None`` -- never to a placeholder derived from the material number.
    """
    if not materials:
        return {}
    rows = session.execute(
        select(StagedMaterial.sap_material_number, StagedMaterial.description).where(
            StagedMaterial.sap_material_number.in_(materials)
        )
    ).all()
    # The currency belongs to the plant's price (StagedMaterialPlant), not the material: it is
    # shown only when every plant that states one agrees, never the first one found.
    currencies: dict[str, set[str]] = {}
    for material, currency in session.execute(
        select(StagedMaterialPlant.sap_material_number, StagedMaterialPlant.currency)
        .where(
            StagedMaterialPlant.sap_material_number.in_(materials),
            StagedMaterialPlant.currency.is_not(None),
            StagedMaterialPlant.unit_price.is_not(None),
        )
        .distinct()
    ).all():
        currencies.setdefault(material, set()).add(currency)
    return {
        material: (
            description,
            next(iter(currencies[material])) if len(currencies.get(material, ())) == 1 else None,
        )
        for material, description in rows
    }


def _current_sap_stock_for(
    session: Session, keys: set[tuple[str, str]]
) -> dict[tuple[str, str], tuple[Decimal | None, Decimal | None, Decimal | None]]:
    """``(material, plant) -> (reorder point, safety stock, max stock)`` as SAP holds them now.

    Read from ``i7_staged_material_plant`` (MARC), not from the neighbour's own I07
    calculation: the similar-materials list shows what is set in SAP today. A neighbour with
    no staged row, or with a value SAP leaves blank, maps to ``None`` -- never to 0.
    """
    if not keys:
        return {}
    rows = session.execute(
        select(
            StagedMaterialPlant.sap_material_number,
            StagedMaterialPlant.sap_plant_code,
            StagedMaterialPlant.current_reorder_point,
            StagedMaterialPlant.current_safety_stock,
            StagedMaterialPlant.current_maximum_stock,
        ).where(StagedMaterialPlant.sap_material_number.in_({material for material, _ in keys}))
    ).all()
    return {
        (material, plant): (rop, safety, maximum)
        for material, plant, rop, safety, maximum in rows
        if (material, plant) in keys
    }


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
    # The highest id per material-plant. ``id`` is an auto-increment, so a later run's row
    # always has a higher id -- the same answer as ranking by ``generated_at DESC, id DESC``
    # (what this used to do with row_number(), which the database planned badly: the
    # unfiltered list took ~55 s and the OAR / Normal filters did not finish in 150 s on
    # the real table). The summary endpoint below has used this form since 2026-09-22.
    latest_ids = select(func.max(Recommendation.id)).group_by(
        Recommendation.sap_material_number, Recommendation.sap_plant_code
    )
    return statement.where(Recommendation.id.in_(latest_ids))


def _portfolio_currency(session: Session) -> str | None:
    """The one currency every priced material is in, or ``None``.

    A portfolio total in mixed currencies has no single true symbol, so it
    reports none rather than the first one found.
    """
    found = session.execute(
        select(StagedMaterialPlant.currency)
        .where(StagedMaterialPlant.currency.is_not(None), StagedMaterialPlant.unit_price.is_not(None))
        .distinct()
        .limit(2)
    ).scalars().all()
    return found[0] if len(found) == 1 else None


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
    search: Annotated[
        str | None,
        Query(description="Part of a material number or description, matched across every recommendation."),
    ] = None,
    criticality_level: Annotated[
        Literal["Critical", "High", "Medium", "Low"] | None,
        Query(description="The display level: CRITICAL / IMPACT / INSURANCE; Low is NORMAL, OBSOLETE or no tier."),
    ] = None,
    status_group: Annotated[
        Literal["Pending Review", "In Approval", "Returned", "Approved", "Rejected", "Implemented"] | None,
        Query(description="The status as the table shows it; several backend statuses make up one."),
    ] = None,
    risk: Annotated[
        Literal["critical", "high", "medium", "low"] | None,
        Query(
            description="Stockout risk as the table shows it, derived from status and criticality tier. "
            "'high' includes the critical tier (the pages show them as one High tier); "
            "'critical' selects the critical tier alone."
        ),
    ] = None,
    demand_type: Annotated[
        Literal["oar", "declining", "normal", "stock_item"] | None,
        Query(
            alias="type",
            description="oar: sized from similar materials. declining: not OAR and flagged by the declining-demand rule. normal: everything else. The three never overlap. stock_item: declining and normal together, i.e. everything that is not OAR.",
        ),
    ] = None,
    conversion_eligibility: Annotated[
        Literal["ELIGIBLE", "NOT_ELIGIBLE", "UNKNOWN"] | None,
        Query(description="The OAR -> Min-Max conversion decision. A recommendation with no decision (not OAR) matches none."),
    ] = None,
    consumption_above_threshold: Annotated[
        bool | None,
        Query(
            description="true: only recommendations issued more than the policy's consumption-count "
            "threshold in the last 12 months (this also drops zero-consumption materials). A "
            "recommendation with no count yet is left out."
        ),
    ] = None,
    actionable: Annotated[
        bool | None,
        Query(
            description="true: only recommendations with something to adopt in SAP -- an OAR one "
            "eligible for the Min-Max conversion, or a stock item whose reorder point, safety stock "
            "or maximum stock differs from SAP's current value. The same set the SAP adoption "
            "counts cover."
        ),
    ] = None,
) -> RecommendationListResponse:
    if sort not in SORT_FIELDS:
        raise bad_request(
            "INVALID_SORT_FIELD",
            f"'{sort}' is not a sortable field.",
            allowed=list(SORT_FIELDS),
        )

    # Narrow first: filter, count, sort and page on the id alone. A recommendation row carries
    # long text columns (rationale, factors, trace); counting and sorting thousands of those
    # wide rows to return one page of 50 is what made this endpoint take minutes. Only the
    # page's own rows are loaded in full, below.
    base = _latest_only(
        _apply_filters(
            select(Recommendation.id),
            status=status,
            plant=plant,
            material=material,
            demand_class=demand_class,
            is_oar=is_oar,
            confidence=confidence,
            criticality=criticality,
            generated_from=generated_from,
            generated_to=generated_to,
            demand_type=demand_type,
            search=search,
            criticality_level=criticality_level,
            status_group=status_group,
            risk=risk,
            conversion_eligibility=conversion_eligibility,
            consumption_above_threshold=consumption_above_threshold,
            actionable=actionable,
        ).where(_not_smooth())
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
    page_ids = session.execute(
        base.order_by(*order).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    loaded = (
        {row.id: row for row in session.execute(select(Recommendation).where(Recommendation.id.in_(page_ids))).scalars()}
        if page_ids
        else {}
    )
    rows = [loaded[row_id] for row_id in page_ids if row_id in loaded]

    # One shared PolicyDocument for the whole page, not one per row -- the
    # policy lookup routing.route_for() does is pure/in-memory (no DB call),
    # so this stays zero extra query cost per the module's own docstring.
    policy = PolicyDocument()

    # One batched staging lookup for the whole page -- see _display_fields_for.
    display_fields = _display_fields_for(session, {row.sap_material_number for row in rows})
    declining_keys = _declining_keys_for(session, rows)

    return RecommendationListResponse(
        items=[
            RecommendationSummary.from_model(
                row,
                tuple(role.value for role in routing.route_for(row.is_oar, row.criticality, policy)),
                description=display_fields.get(row.sap_material_number, (None, None))[0],
                currency=display_fields.get(row.sap_material_number, (None, None))[1],
                demand_type=_demand_type(row, declining_keys),
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
        ).where(_not_smooth())

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
    risk_case = _risk_case()
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

    # The Overview's Working Capital Impact: the reorder point, not the safety stock. Same sign and
    # the same "priced, current and recommended all present" rule as above, so a row missing any of
    # the three is left out of the sum rather than read as zero.
    net_rop_value_row = session.execute(
        base.with_only_columns(
            func.sum(Recommendation.unit_price * (Recommendation.current_rop - Recommendation.recommended_rop))
        ).where(
            Recommendation.unit_price.isnot(None),
            Recommendation.current_rop.isnot(None),
            Recommendation.recommended_rop.isnot(None),
        )
    ).scalar()

    # --- High Stockout Risk: stock on hand < average daily consumption x lead time --
    # Every Fast and Slow moving material-plant in the usage table, recommended or not, with the
    # usage read as of the configured anchor date -- see app.initiatives.i7.stockout_risk for the
    # rule and for how a missing or invalid figure is Insufficient Data, not safe.
    stockout_risk = stockout_risk_counts(
        session,
        base,
        plant=plant,
        material=material,
        demand_class=demand_class,
        criticality=criticality,
        status=status,
        is_oar=is_oar,
        confidence=confidence,
    )

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
        currency=_portfolio_currency(session),
        net_safety_stock_value_impact=net_value_row,
        net_rop_value_impact=net_rop_value_row,
        critical_stockout_risk_count=stockout_risk.at_risk,
        stockout_risk_evaluated_count=stockout_risk.evaluated,
        stockout_risk_insufficient_data_count=stockout_risk.insufficient_data,
        stockout_risk_as_of=stockout_risk.as_of,
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
    # Complete, netted and floored monthly series (see the service for the rules); the
    # staged rows read directly gave only the months with movements.
    consumption_history = tuple(
        ConsumptionHistoryEntry(period=period, quantity=quantity)
        for period, quantity in monthly_consumption_history(session, row.sap_material_number, row.sap_plant_code)
    )
    display_fields = _display_fields_for(session, {row.sap_material_number}).get(
        row.sap_material_number, (None, None)
    )
    return RecommendationDetail.from_model(
        row,
        consumption_history=consumption_history,
        description=display_fields[0],
        currency=display_fields[1],
    )


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


SIMILAR_MATERIALS_LIMIT = 10
"""How many neighbours the detail shows -- the engine's usual top_k."""


@router.get(
    "/recommendations/{recommendation_id}/similar-materials",
    response_model=SimilarMaterialsResponse,
    summary="The most similar materials an OAR recommendation was sized from",
    description="The neighbours the OAR similarity engine stored for this "
    "recommendation's own run (i7_oar_neighbour), best first, up to ten -- never "
    "re-ranked or recomputed. neighbours is empty, never fabricated, when the "
    "recommendation has no OAR run or the run kept no neighbours.",
    responses={404: {"description": "Recommendation not found"}},
)
def get_recommendation_similar_materials(
    recommendation_id: str, session: Annotated[Session, Depends(get_session)]
) -> SimilarMaterialsResponse:
    row = load_latest_recommendation(session, recommendation_id)

    neighbours: list[OarNeighbour] = []
    if row.oar_run_id is not None:
        neighbours = list(
            session.execute(
                select(OarNeighbour)
                .where(
                    OarNeighbour.oar_run_id == row.oar_run_id,
                    OarNeighbour.sap_material_number == row.sap_material_number,
                    OarNeighbour.sap_plant_code == row.sap_plant_code,
                )
                .order_by(OarNeighbour.rank)
                .limit(SIMILAR_MATERIALS_LIMIT)
            ).scalars()
        )

    descriptions = _display_fields_for(session, {n.neighbour_material for n in neighbours})
    current_stock = _current_sap_stock_for(session, {(n.neighbour_material, n.neighbour_plant) for n in neighbours})
    no_stock = (None, None, None)
    return SimilarMaterialsResponse(
        sap_material_number=row.sap_material_number,
        sap_plant_code=row.sap_plant_code,
        neighbours=tuple(
            SimilarMaterial(
                rank=n.rank,
                material=n.neighbour_material,
                plant=n.neighbour_plant,
                description=descriptions.get(n.neighbour_material, (None, None))[0],
                similarity=n.combined_similarity,
                criticality=n.criticality,
                history_months=n.history_months,
                safety_stock=n.safety_stock,
                rop=n.rop,
                max_stock=n.max_stock,
                current_rop=current_stock.get((n.neighbour_material, n.neighbour_plant), no_stock)[0],
                current_safety_stock=current_stock.get((n.neighbour_material, n.neighbour_plant), no_stock)[1],
                current_max_stock=current_stock.get((n.neighbour_material, n.neighbour_plant), no_stock)[2],
                contributed=n.inventory_eligible,
            )
            for n in neighbours
        ),
    )


@router.get(
    "/forecast-vs-actual",
    response_model=ForecastHistoryAggregateResponse,
    summary="Portfolio Forecast vs Actual Demand, from the champion backtests",
    description="For the Overview's Forecast vs Actual Demand chart: per month, the "
    "SUM of the champion models' one-step-ahead predictions and of what was "
    "actually used, across every material-plant that has a persisted backtest "
    "path in the latest succeeded forecast run (optionally one plant). Real model "
    "output, never a client-side approximation. Material-plants with no backtest "
    "-- cold start, and those on the declining-demand TSB path, which is not "
    "backtested by design -- are not in the sums; materials_in_scope_count against "
    "materials_with_history_count says how many. Quantities are summed in each "
    "material's own unit of measure.",
)
def get_forecast_vs_actual(
    session: Annotated[Session, Depends(get_session)],
    plant: Annotated[str | None, Query(description="One SAP plant code; omit for every plant.")] = None,
    months: Annotated[int, Query(ge=1, le=60, description="How many of the latest months to return.")] = 12,
) -> ForecastHistoryAggregateResponse:
    run_id = session.execute(
        select(func.max(ForecastRun.id)).where(ForecastRun.status == "succeeded")
    ).scalar()
    if run_id is None:
        return ForecastHistoryAggregateResponse(
            materials_with_history_count=0, materials_in_scope_count=0, points=()
        )

    plant_path = (ForecastBacktestPath.sap_plant_code == plant,) if plant else ()
    plant_forecast = (Forecast.sap_plant_code == plant,) if plant else ()

    # One-step-ahead only: each material-plant then has one path row per month, so
    # a month's sum adds each material once. Later horizon steps predict the same
    # month again and would count it several times.
    path_filter = (
        ForecastBacktestPath.forecast_run_id == run_id,
        ForecastBacktestPath.horizon_step == 1,
        *plant_path,
    )

    latest_period = session.execute(
        select(func.max(ForecastBacktestPath.forecast_period)).where(*path_filter)
    ).scalar()

    points: tuple[ForecastHistoryAggregatePoint, ...] = ()
    if latest_period is not None:
        first_month = (latest_period.year * 12 + latest_period.month - 1) - (months - 1)
        cutoff = date(first_month // 12, first_month % 12 + 1, 1)
        rows = session.execute(
            select(
                ForecastBacktestPath.forecast_period,
                func.sum(ForecastBacktestPath.predicted),
                func.sum(ForecastBacktestPath.actual),
                func.count(),
            )
            .where(*path_filter, ForecastBacktestPath.forecast_period >= cutoff)
            .group_by(ForecastBacktestPath.forecast_period)
            .order_by(ForecastBacktestPath.forecast_period)
        ).all()
        points = tuple(
            ForecastHistoryAggregatePoint(
                forecast_period=period, predicted=predicted, actual=actual, material_count=count
            )
            for period, predicted, actual, count in rows
        )

    with_history = session.execute(
        select(func.count()).select_from(
            select(ForecastBacktestPath.sap_material_number, ForecastBacktestPath.sap_plant_code)
            .where(*path_filter)
            .distinct()
            .subquery()
        )
    ).scalar_one()

    in_scope = session.execute(
        select(func.count()).where(
            Forecast.forecast_run_id == run_id,
            Forecast.is_champion,  # the column itself: SQL Server has no `IS 1`
            Forecast.forecast_status == "SUCCESS",
            *plant_forecast,
        )
    ).scalar_one()

    return ForecastHistoryAggregateResponse(
        materials_with_history_count=with_history,
        materials_in_scope_count=in_scope,
        points=points,
    )
