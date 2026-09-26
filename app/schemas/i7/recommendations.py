"""Recommendation API schemas.

Grouped to mirror ``app.models.i7_recommendation.Recommendation`` field for
field -- nothing here is invented. Where the conceptual shape in the Phase 8
brief names a field the model does not have, the group omits it rather than
fabricating a value, and ``docs/i07_api.md`` records the gap explicitly.

``circuit``/``unit_price``/lead-time days+variance/``service_level``/
``z_factor`` are Phase 5 values the recommendation builder already read (into
``calculation_trace`` as text) but never exposed as typed fields -- they are
now persisted columns, read verbatim, never recomputed. ADI and CV-squared
remain genuinely absent: they are Phase 3 feature-store fields with no
equivalent read anywhere in the recommendation pipeline. Working-capital
monetary impact and a "stockout risk" concept are also genuinely absent --
``ExpectedImpact.monetary_impact`` is hardcoded ``None`` because no I07
document or table supplies an annual holding-cost rate, and no risk model
exists in this codebase at all; inventing either would be exactly the kind of
unconfirmed business value this module refuses to guess.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.i7_recommendation import Recommendation as RecommendationModel


class StockParameters(BaseModel):
    model_config = ConfigDict(frozen=True)

    safety_stock: Decimal | None = None
    rop: Decimal | None = None
    max_stock: Decimal | None = None


class ConsumptionHistoryEntry(BaseModel):
    """One staged month of real consumption for this material-plant.

    Read straight from ``i7_staged_consumption`` -- the same table Phase 3's
    feature engineering reads to derive ADI/CV-squared/forecast_rate --
    never recomputed or densified with invented zero-months. Zero-demand
    months are not stored there (see StagedConsumption's own docstring), so a
    gap in ``period`` here is a genuine absence of a movement that month, not
    a missing row."""

    model_config = ConfigDict(frozen=True)

    period: date
    quantity: Decimal


class ForecastHistoryPoint(BaseModel):
    """One rolling-origin prediction from the CHAMPION model's own backtest,
    for the "Forecast vs Actual Demand" chart -- real model output, read from
    ``i7_forecast_backtest_path`` (added 2026-09-22), never a client-side
    approximation computed from raw consumption alone."""

    model_config = ConfigDict(frozen=True)

    forecast_period: date
    """The month this prediction was FOR -- what the chart's x-axis uses."""

    predicted: Decimal
    actual: Decimal


class ForecastHistoryResponse(BaseModel):
    """The champion model's predicted/actual history for one
    material-plant's most recent forecast run that has any persisted paths.

    ``points`` is empty, never fabricated, when no forecast run since
    2026-09-22 has produced paths for this material-plant (either it predates
    the table, or its backtest never produced a scoreable path -- see
    ForecastBacktestPath's own docstring on why history cannot be
    retroactively reconstructed)."""

    model_config = ConfigDict(frozen=True)

    sap_material_number: str
    sap_plant_code: str
    model_name: str | None
    """The champion model these points came from, or None when points is
    empty."""

    points: tuple[ForecastHistoryPoint, ...]


class ForecastHistoryAggregatePoint(BaseModel):
    """One period's SUMMED predicted/actual across every in-scope
    material-plant that has a persisted champion path for that period --
    real model output, aggregated, never recomputed or blended with the
    flat forecast_rate fallback."""

    model_config = ConfigDict(frozen=True)

    forecast_period: date
    predicted: Decimal
    actual: Decimal
    material_count: int
    """How many distinct material-plants contributed to this period's sum --
    varies period to period since not every material's history covers the
    same span."""


class ForecastHistoryAggregateResponse(BaseModel):
    """Portfolio-wide (optionally filtered, same filters as the list/summary
    endpoints) real backtest history, summed per period across every
    material-plant that has one -- for the Overview page's Forecast vs
    Actual chart, which shows many materials at once and cannot call the
    per-recommendation endpoint once per row.

    ``points`` is empty when NO in-scope material-plant has any persisted
    backtest path yet -- never fabricated as a flat line here; the caller
    (frontend) falls back to its own flat-line-of-forecast_rate display in
    that case, exactly as it already does per-material."""

    model_config = ConfigDict(frozen=True)

    materials_with_history_count: int
    """How many distinct material-plants (within the current filter scope)
    contributed at least one point -- always <= materials_in_scope_count."""

    materials_in_scope_count: int
    """Total material-plants matching the current filters, regardless of
    whether they have persisted history -- the denominator for an honest
    "N of M materials have real history" caption."""

    points: tuple[ForecastHistoryAggregatePoint, ...]


class DemandInfo(BaseModel):
    """Demand classification and forecast, as far as the recommendation row
    carries it. ADI and CV-squared are Phase 3 feature-store fields, not
    persisted on the recommendation -- see the trace endpoint, which reads
    them from the same upstream row the recommendation was built from."""

    model_config = ConfigDict(frozen=True)

    demand_class: str | None = None
    history_status: str | None = None
    model: str | None = None
    forecast_rate: Decimal | None = None
    consumption_history: tuple[ConsumptionHistoryEntry, ...] = ()
    """The staged monthly series behind this recommendation's forecast --
    populated only on the detail endpoint (a per-material query), empty on
    the list endpoint. Empty, never fabricated, for a material with no
    staged consumption rows at all (e.g. every OAR/cold-start material)."""


class LeadTimeInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    method: str | None = None
    """``PLANNED_FALLBACK`` today -- lead time comes from MARC-PLIFZ
    unconditionally by business decision, not from PO history. Older stored
    values may still read ``ACTUAL_STATISTICAL`` / ``ACTUAL_WARNING``, which
    is why both remain valid values here. ``None`` when no lead-time analysis
    was recorded for this recommendation."""

    days: Decimal | None = None
    """``i7_inventory_calculation.lt_avg_days`` -- Phase 5's own lead-time
    figure, read as-is, never recomputed here. ``None`` on the OAR/cold-start
    path, which never reaches Phase 5 (see ``inventory/service.py``'s
    ``DEFERRED_TO_OAR``)."""

    variance_days: Decimal | None = None
    """``i7_inventory_calculation.sigma_lt_days``. Same availability rule as
    ``days``."""


class CriticalityInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str | None = None
    """The ZMM065 tier, or ``None`` -- never defaulted to NORMAL."""


class ServiceLevelInfo(BaseModel):
    """The signed service level and its derived Z-factor, read verbatim from
    Phase 5's own ``inventory.service_level.resolve()`` result -- never
    recomputed here. Both are ``None`` while the Criticality x Service Level
    matrix is unsigned (``NOT_EVALUABLE_SERVICE_LEVEL_UNSET``), which is the
    honest current state on this extract, and also ``None`` on the OAR/
    cold-start path, which never reaches Phase 5 at all."""

    model_config = ConfigDict(frozen=True)

    service_level: Decimal | None = None
    z_factor: Decimal | None = None


class OarInfo(BaseModel):
    """OAR similarity and estimate status, kept distinct on purpose.

    ``similarity_status`` answers "did Phase 6 find neighbours?";
    ``estimate_status`` answers "could those neighbours' values be weighted
    into a number?". A recommendation can have similarity AVAILABLE and an
    estimate that is NOT_EVALUABLE_SERVICE_LEVEL_UNSET at the same time -- that
    combination is the majority state on the current extract, and it must
    never be read as a single generic "failed".

    ``demand_class`` is FR-2's demand pattern, carried here alongside the
    conversion fields per the FRS: "the FR-2 demand class as a confidence
    signal" on the OAR-to-Min-Max conversion suggestion, and "the ADI / CV
    squared class is shown as a supporting regularity and confidence signal,
    not the trigger." It is a display value only -- the same
    ``MaterialFeature.demand_class`` already carried on ``DemandInfo``, not a
    separate score, and it plays no part in ``conversion_eligibility`` or
    ``conversion_trigger``. Commonly ``"UNCLASSIFIED"`` on the OAR/cold-start
    path -- FR-2 classification only runs once ``history_status`` reaches
    SUFFICIENT (see ``features/builder.py``), which OAR materials by
    definition have not -- but this is the real, unmodified FR-2 result, not
    a fabricated placeholder; it is exposed as-is, never coerced to ``None``
    or to a real demand pattern.

    ``consumption_count_12m`` / ``consumption_count_threshold`` /
    ``production_impact`` / ``i13_hod_approved`` are the SOP 3.1.1 indicators'
    own structured evidence -- the same values ``conversion_detail`` already
    describes in prose, exposed as fields a client can read/filter/sort on
    without parsing free text. ``production_impact``/``i13_hod_approved`` are
    ``None`` when that indicator is unresolved (unconfigured tier set, no I13
    ledger), never a silent ``False``.
    """

    model_config = ConfigDict(frozen=True)

    is_oar: bool | None = None
    similarity_status: str | None = None
    estimate_status: str | None = None
    neighbour_count: int | None = None
    best_similarity: Decimal | None = None
    confidence: str | None = None
    conversion_eligibility: str | None = None
    conversion_trigger: str | None = None
    conversion_detail: str | None = None
    demand_class: str | None = None
    consumption_count_12m: int | None = None
    consumption_count_threshold: int | None = None
    production_impact: bool | None = None
    i13_hod_approved: bool | None = None


class RationaleInfo(BaseModel):
    """One paragraph explaining this recommendation, plus its provenance.

    ``text`` is never a source of truth for any calculated value -- it only
    explains SS/ROP/Max/OAR facts Phase 3/4/5/6 already computed. ``source``
    distinguishes a real model call (``AI_GENERATED``) from the deterministic
    template (``DETERMINISTIC_FALLBACK``, also used when
    ``LLM_PROVIDER=stub`` -- the stub always answers but never consulted a
    real model, so its output is never labelled AI_GENERATED). No API key,
    credential, or raw prompt reaches this schema.
    """

    model_config = ConfigDict(frozen=True)

    text: str | None = None
    source: str | None = None
    """``AI_GENERATED`` / ``DETERMINISTIC_FALLBACK`` / ``None`` (no
    recommendation has been generated with rationale support yet)."""


class ImpactInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    """``AVAILABLE`` / ``NOT_EVALUABLE_MISSING_CURRENT`` /
    ``NOT_EVALUABLE_MISSING_RECOMMENDED`` /
    ``NOT_EVALUABLE_COST_DATA_UNAVAILABLE``."""

    safety_stock_delta: Decimal | None = None
    rop_delta: Decimal | None = None
    max_stock_delta: Decimal | None = None


class GovernanceInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy_id: str
    policy_version: int
    formula_version: str
    feature_run_id: int | None = None
    forecast_run_id: int | None = None
    inventory_run_id: int | None = None
    oar_run_id: int | None = None


class RecommendationSummary(BaseModel):
    """One row of the list endpoint -- enough to render a table AND its
    value/ROP change columns without a second request per row. ``current``/
    ``recommended``/``impact`` add no query cost: the list endpoint already
    runs ``select(Recommendation)`` (the full ORM row) per app/api/i7/
    recommendations.py, so these are columns already in hand, read verbatim
    -- never a second lookup, never recomputed."""

    model_config = ConfigDict(frozen=True)

    recommendation_id: str
    material: str
    plant: str
    status: str
    is_oar: bool | None
    demand_class: str | None
    criticality: str | None
    confidence: str | None
    generated_at: datetime
    updated_at: datetime
    """When this row last changed -- a submit/hold/approve/reject action
    updates the existing row in place (see the Phase-7-cleanup note in
    docs/i07_recommendations.md), so this is the correct "waiting since"
    anchor for a pipeline/stuck-detection view, not ``generated_at`` (which
    only reflects when the recommendation was first calculated)."""
    chain_index: int
    """Index into ``route`` (below) of the role whose decision is next
    pending. Meaningless once status leaves PENDING_APPROVAL/SENT_BACK/HELD,
    exactly as WorkflowState.pending_role documents."""
    route: list[str]
    """The resolved approval route for this recommendation (routing.route_for
    -- OAR_APPROVAL_CHAIN for OAR conversions, otherwise a criticality-tier
    lookup through the configured ApprovalRoutingPolicy). Computed here, once,
    server-side -- a criticality-tier route is configuration, not a fixed
    constant, so deriving it a second time client-side from raw criticality
    would silently drift from whatever policy is actually active. Paired with
    chain_index this is enough for a pipeline table to show pending-role and
    step-N-of-M progress for every row in one list fetch, without the
    per-recommendation GET .../workflow-state call that exists for a single
    detail view, not a table of hundreds of rows."""
    current: StockParameters
    recommended: StockParameters
    impact: ImpactInfo
    unit_price: Decimal | None = None
    """Added so the list endpoint's own Value-change column can price
    ``impact.safety_stock_delta`` without a second (detail) request per row --
    the same reasoning as ``current``/``recommended``/``impact`` above."""

    @classmethod
    def from_model(cls, row: RecommendationModel, route: tuple[str, ...]) -> "RecommendationSummary":
        return cls(
            recommendation_id=row.recommendation_id,
            material=row.sap_material_number,
            plant=row.sap_plant_code,
            status=row.status,
            is_oar=row.is_oar,
            demand_class=row.demand_class,
            criticality=row.criticality,
            confidence=row.confidence,
            generated_at=row.generated_at,
            updated_at=row.updated_at,
            chain_index=row.chain_index,
            route=list(route),
            unit_price=row.unit_price,
            current=StockParameters(
                safety_stock=row.current_safety_stock,
                rop=row.current_rop,
                max_stock=row.current_max_stock,
            ),
            recommended=StockParameters(
                safety_stock=row.recommended_safety_stock,
                rop=row.recommended_rop,
                max_stock=row.recommended_max_stock,
            ),
            impact=ImpactInfo(
                status=row.impact_status,
                safety_stock_delta=row.safety_stock_delta,
                rop_delta=row.rop_delta,
                max_stock_delta=row.max_stock_delta,
            ),
        )


class RecommendationListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[RecommendationSummary]
    total: int
    page: int
    page_size: int


class StatusCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    count: int


class CriticalityCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    criticality: str | None
    """``None`` groups every row with no criticality recorded -- never
    collapsed into a real tier. See ``RecommendationSummary.from_model``: the
    same field, here counted rather than read per-row."""

    count: int


class CircuitCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    circuit: str | None
    """``None`` groups every row with no circuit recorded (see
    ``RecommendationDetail.circuit``'s own docstring: genuinely absent on the
    OAR/cold-start path, not merely unpopulated)."""

    count: int


class RiskCount(BaseModel):
    """Portfolio-wide count for one of the frontend's UI-only risk tiers --
    there is no "stockout risk" concept in the I07 domain itself (see
    RecommendationDetail's ImpactInfo docstring); this mirrors the frontend's
    own deriveRisk() (services/i7-api.ts) exactly, in SQL, over the whole
    (optionally filtered) set rather than one fetched page: a criticality tier
    only counts as elevated risk while the recommendation's status is still
    "open" (not yet finally decided), and CRITICAL/IMPACT/INSURANCE map to
    critical/high/medium respectively -- everything else (NORMAL/OBSOLETE/no
    criticality at all/not open) is "low", never fabricated as a middle
    tier."""

    model_config = ConfigDict(frozen=True)

    risk: str
    """``critical`` / ``high`` / ``medium`` / ``low`` -- lowercase, matching
    the frontend's own RiskLevel literal union."""

    count: int


class PlantCount(BaseModel):
    """Portfolio-wide count per SAP plant code.

    Exists so a plant filter can offer exactly the plants the data actually
    contains. The frontend's own PLANTS list (lib/shared-data/plants.ts) is
    app-side scenario master data keyed on invented ids (PLANT-GBG, PLANT-BMM,
    PLANT-SKZ) that no SAP field supplies -- a live plant filter built from it
    can never match a real row, whose plant is a SAP WERKS code (1300, 1500,
    3000, ...). This aggregate is the honest source for that dropdown."""

    model_config = ConfigDict(frozen=True)

    plant: str
    count: int


class RecommendationSummaryStats(BaseModel):
    """Portfolio-wide aggregates, computed in SQL over every recommendation
    matching the given filters -- never paginated, and never recomputed
    client-side. Exists because the dashboard/KPI screens need counts across
    the whole (filtered) set, not one page of it; every number here is a
    ``COUNT``/``SUM`` over columns the list/detail endpoints already expose,
    not a new business calculation.
    """

    model_config = ConfigDict(frozen=True)

    total: int
    by_status: list[StatusCount]
    by_criticality: list[CriticalityCount]
    by_circuit: list[CircuitCount]
    by_risk: list[RiskCount]
    by_plant: list[PlantCount]
    oar_count: int
    normal_count: int
    awaiting_approval_count: int
    """Rows whose status is PENDING_APPROVAL or HELD -- submitted and not yet
    finally decided. Matches the same statuses the workflow considers "in the
    chain" (see ``app.initiatives.i7.recommendations.workflow``)."""

    ready_for_review_count: int
    not_evaluable_count: int
    net_safety_stock_value_impact: Decimal | None
    """SUM(unit_price * (current_safety_stock - recommended_safety_stock))
    over rows where both stock values and unit_price are present -- positive
    means the portfolio's recommended changes would net RELEASE working
    capital (recommended safety stock is lower), negative means they would
    net TIE UP more. ``None`` when no row in scope has all three values
    available (this extract's actual state today, since unit_price and
    computed safety stock rarely coincide on the same row) -- never
    defaulted to 0, which would silently claim "no impact" instead of "not
    computable yet\"."""

    critical_stockout_risk_count: int
    """COUNT of material-plants where current on-hand stock
    (SUM(i7_staged_stock.unrestricted_use_stock) across storage locations)
    is below ``recommended_rop`` -- both values present and ``recommended_rop``
    not NULL. An I07-derived proxy, not a Vedanta-confirmed KPI definition:
    "current stock has already fallen below the newly-calculated reorder
    point." Rows with no staged stock record or no recommended ROP are
    excluded from the count, never counted as either at-risk or safe."""

    excess_inventory_candidates_count: int
    """COUNT of material-plants where ``current_max_stock >
    recommended_max_stock`` (both present, any positive gap, no minimum
    margin). An I07-derived proxy, not a Vedanta-confirmed KPI definition."""

    excess_inventory_opportunity: Decimal | None
    """SUM(unit_price * (current_max_stock - recommended_max_stock)) over
    rows in the excess-inventory count above where unit_price is also
    present -- the same "current vs recommended, priced" pattern as
    ``net_safety_stock_value_impact``, applied to Max Stock instead of
    Safety Stock. ``None`` when no in-scope row has all three values
    available, never defaulted to 0."""


class RecommendationDetail(BaseModel):
    """The complete recommendation, as persisted."""

    model_config = ConfigDict(frozen=True)

    recommendation_id: str
    material: str
    plant: str
    circuit: str | None = None
    """Phase 5's own circuit assignment. ``None`` on the OAR/cold-start path
    -- no SAP field currently supplies circuit at all (see
    ``oar/repository.py``'s hardcoded ``NULL AS circuit``), so this is
    genuinely absent there, not merely unpopulated."""
    unit_price: Decimal | None = None
    """Feature-store (Phase 3) field, available on both the normal and OAR
    path regardless of history status."""

    current: StockParameters
    recommended: StockParameters

    demand: DemandInfo
    lead_time: LeadTimeInfo
    criticality: CriticalityInfo
    service_level: ServiceLevelInfo
    oar: OarInfo
    impact: ImpactInfo
    rationale: RationaleInfo
    governance: GovernanceInfo

    status: str
    blocking_reason: str | None = None
    safety_stock_method: str | None = None
    max_stock_strategy: str | None = None

    chain_index: int
    adjustment_count: int
    current_version: int

    generated_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(
        cls,
        row: RecommendationModel,
        consumption_history: tuple[ConsumptionHistoryEntry, ...] = (),
    ) -> "RecommendationDetail":
        return cls(
            recommendation_id=row.recommendation_id,
            material=row.sap_material_number,
            plant=row.sap_plant_code,
            circuit=row.circuit,
            unit_price=row.unit_price,
            current=StockParameters(
                safety_stock=row.current_safety_stock,
                rop=row.current_rop,
                max_stock=row.current_max_stock,
            ),
            recommended=StockParameters(
                safety_stock=row.recommended_safety_stock,
                rop=row.recommended_rop,
                max_stock=row.recommended_max_stock,
            ),
            demand=DemandInfo(
                demand_class=row.demand_class,
                history_status=row.history_status,
                model=row.baseline_model,
                forecast_rate=row.forecast_rate,
                consumption_history=consumption_history,
            ),
            lead_time=LeadTimeInfo(
                method=row.lead_time_method,
                days=row.lead_time_days,
                variance_days=row.lead_time_variance_days,
            ),
            criticality=CriticalityInfo(value=row.criticality),
            service_level=ServiceLevelInfo(
                service_level=row.service_level,
                z_factor=row.z_factor,
            ),
            oar=OarInfo(
                is_oar=row.is_oar,
                similarity_status=row.oar_similarity_status,
                estimate_status=row.oar_estimate_status,
                neighbour_count=row.oar_neighbour_count,
                best_similarity=row.oar_best_similarity,
                confidence=row.confidence,
                conversion_eligibility=row.conversion_eligibility,
                conversion_trigger=row.conversion_trigger,
                conversion_detail=row.conversion_detail,
                demand_class=row.demand_class,
                consumption_count_12m=row.consumption_count_12m,
                consumption_count_threshold=row.consumption_count_threshold,
                production_impact=row.production_impact,
                i13_hod_approved=row.i13_hod_approved,
            ),
            impact=ImpactInfo(
                status=row.impact_status,
                safety_stock_delta=row.safety_stock_delta,
                rop_delta=row.rop_delta,
                max_stock_delta=row.max_stock_delta,
            ),
            rationale=RationaleInfo(
                text=row.rationale_text,
                source=row.rationale_source,
            ),
            governance=GovernanceInfo(
                policy_id=row.policy_id,
                policy_version=row.policy_version,
                formula_version=row.formula_version,
                feature_run_id=row.feature_run_id,
                forecast_run_id=row.forecast_run_id,
                inventory_run_id=row.inventory_run_id,
                oar_run_id=row.oar_run_id,
            ),
            status=row.status,
            blocking_reason=row.blocking_reason,
            safety_stock_method=row.safety_stock_method,
            max_stock_strategy=row.max_stock_strategy,
            chain_index=row.chain_index,
            adjustment_count=row.adjustment_count,
            current_version=row.current_version,
            generated_at=row.generated_at,
            updated_at=row.updated_at,
        )


class TraceEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    value: str


class RecommendationTrace(BaseModel):
    """The Phase 7 calculation trace, verbatim -- never recomputed here."""

    model_config = ConfigDict(frozen=True)

    recommendation_id: str
    status: str
    blocking_reason: str | None = None
    factors: list[str]
    entries: list[TraceEntry]

    @classmethod
    def from_model(cls, row: RecommendationModel) -> "RecommendationTrace":
        entries = _parse_trace_entries(row.calculation_trace)
        factors = row.factors_text.split("\n") if row.factors_text else []
        return cls(
            recommendation_id=row.recommendation_id,
            status=row.status,
            blocking_reason=row.blocking_reason,
            factors=factors,
            entries=entries,
        )


def _parse_trace_entries(raw: str | None) -> list[TraceEntry]:
    """Split Phase 7's ``key=value;key=value`` trace text into entries.

    A naive ``split(";")`` breaks when a value's own free text -- most often
    ``blocking_reason``, which is written into the trace verbatim -- contains a
    semicolon of its own; the tail of that value would silently be dropped
    into what looks like the next entry. Each segment is instead required to
    start a *new* recognisable ``key=`` before it is treated as a new entry;
    otherwise it is folded back into the previous entry's value, so free text
    within a value is never truncated.
    """
    if not raw:
        return []

    entries: list[TraceEntry] = []
    for chunk in raw.split(";"):
        if "=" in chunk and (not entries or _looks_like_key(chunk.split("=", 1)[0])):
            label, value = chunk.split("=", 1)
            entries.append(TraceEntry(label=label, value=value))
        elif entries:
            # Continuation of the previous entry's value -- the semicolon that
            # split it apart belonged to the value's own text, not the trace
            # format.
            previous = entries[-1]
            entries[-1] = TraceEntry(label=previous.label, value=f"{previous.value};{chunk}")
    return entries


def _looks_like_key(candidate: str) -> bool:
    """A Phase 7 trace key is a short identifier (``lambda``, ``rop_status``),
    never free text -- used to tell a genuine new entry from a value's own
    semicolon-separated clause."""
    return bool(candidate) and len(candidate) <= 40 and " " not in candidate
