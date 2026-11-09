"""Assemble a recommendation from Phase 3/4/5/6 outputs.

Never recalculates a formula. Every value here is read from an upstream table
and carried forward; the only new work in this module is deciding whether the
required upstream values are actually present, and if not, which
``NOT_EVALUABLE`` reason to attach.

**A recommendation reaches READY_FOR_REVIEW only when its own path's mandatory
inputs are all present.** The normal path needs a Phase 5 SUCCESS; the OAR path
needs Phase 6 neighbours (the estimate itself may still be blocked on the
signed service level, which is reported but does not by itself prevent human
review of the similarity evidence).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.initiatives.i7.contracts import Criticality
from app.initiatives.i7.contracts.enums import ScopeDecision
from app.initiatives.i7.contracts.recommendation import RecommendationFactor
from app.initiatives.i7.inventory import service_level as service_level_module
from app.initiatives.i7.oar import demand_check
from app.initiatives.i7.recommendations import explanation
from app.initiatives.i7.recommendations.conversion import evaluate as evaluate_conversion
from app.initiatives.i7.recommendations.types import (
    RECOMMENDATION_FORMULA_VERSION,
    ConversionDecision,
    ConversionEligibility,
    ConversionTrigger,
    ExpectedImpact,
    ImpactStatus,
    LifecycleStatus,
)
from app.initiatives.i7.oar import blend as blend_module
from app.initiatives.i7.policy import PolicyDocument


NO_SIMILAR_MATERIALS_FOUND = "NO_SIMILAR_MATERIALS_FOUND"
"""``blocking_reason`` of a cold-start material whose similarity search completed but found no usable neighbour."""

MISSING_DATA_WINDOW = "MISSING_DATA"
"""``i7_material_feature.window_history_status`` when a month of the demand window is missing or incomplete."""

MISSING_DATA_REASON = (
    "MISSING_DATA: SAP consumption for one or more months of the 24-month demand window is missing or "
    "incomplete, so this recommendation is not built on a complete history. It cannot be submitted until "
    "those months are validated (I7_DEMAND_VALIDATED_MONTHS) or the extract is reloaded and the pipeline re-run."
)


@dataclass
class BuiltRecommendation:
    """Everything the persistence layer needs to write one row."""

    sap_material_number: str
    sap_plant_code: str
    status: LifecycleStatus
    is_oar: bool | None
    demand_class: str | None
    history_status: str | None
    criticality: str | None

    current_safety_stock: Decimal | None
    current_rop: Decimal | None
    current_max_stock: Decimal | None
    recommended_safety_stock: Decimal | None
    recommended_rop: Decimal | None
    recommended_max_stock: Decimal | None

    baseline_model: str | None
    forecast_rate: Decimal | None
    lead_time_method: str | None
    safety_stock_method: str | None
    max_stock_strategy: str | None
    confidence: str | None

    oar_neighbour_count: int | None
    oar_best_similarity: Decimal | None

    circuit: str | None
    unit_price: Decimal | None
    lead_time_days: Decimal | None
    lead_time_variance_days: Decimal | None
    service_level: Decimal | None
    z_factor: Decimal | None

    conversion: ConversionDecision | None
    blocking_reason: str | None
    factors: tuple
    calculation_trace: tuple
    impact: ExpectedImpact

    feature_run_id: int | None
    forecast_run_id: int | None
    inventory_run_id: int | None
    oar_run_id: int | None
    policy_id: str
    policy_version: int

    oar_similarity_status: str | None = None
    """``AVAILABLE`` when Phase 6 found >=1 eligible neighbour, else
    ``NOT_AVAILABLE``. ``None`` on the normal path, where similarity does not
    apply. Kept distinct from ``status`` on purpose: similarity evidence being
    available is not the same fact as the recommendation being reviewable --
    a human can see *why* a material is OAR and still have nothing to approve
    if no neighbour could lend a Phase 5 value."""

    oar_estimate_status: str | None = None
    """Phase 6's own ``EstimateStatus`` value, carried through unchanged
    (e.g. ``NOT_EVALUABLE_SERVICE_LEVEL_UNSET``, ``SUCCESS``). Preserved even
    when the recommendation itself is NOT_EVALUABLE, so the similarity work is
    never discarded merely because it could not be weighted into a number."""


_NORMAL_TRACE_FIELDS = (
    ("demand_class", "demand_class"),
    ("baseline_model", "baseline_model"),
    ("forecast_rate", "forecast_rate"),
    ("lead_time_method", "lead_time_method"),
    ("lt_avg_months", "lt_avg_months"),
    ("service_level", "service_level"),
    ("z_factor", "z_factor"),
    ("safety_stock_method", "safety_stock_method"),
    ("safety_stock", "safety_stock"),
    ("rop", "rop"),
    ("max_stock", "max_stock"),
)


_MAX_FLOOR_TRACE_KEYS = (
    "max_stock_weighted",
    "max_stock_floor",
    "max_stock_final",
    "max_stock_floor_applied",
    "max_stock_floor_reason",
)
"""The Max-consistency-floor entries of the OAR estimate's own trace -- what lets an approver tell
the similarity-weighted Max from the adjusted one."""


def _max_floor_entries(estimate_trace: str | None) -> list[tuple[str, str]]:
    """The floor's audit entries out of the stored ``key=value;key=value`` estimate trace.

    Empty for a result with no trace, or one stored before the floor existed (``oar-similarity-5``
    and earlier) -- nothing is invented for it.
    """
    if not estimate_trace:
        return []
    found: dict[str, str] = {}
    for part in str(estimate_trace).split(";"):
        key, separator, value = part.partition("=")
        if separator and key in _MAX_FLOOR_TRACE_KEYS:
            found[key] = value
    return [(key, found[key]) for key in _MAX_FLOOR_TRACE_KEYS if key in found]


_ESTIMATOR_TRACE_SUFFIXES = ("_mean", "_median", "_material_difference")
_ESTIMATOR_TRACE_FIELDS = ("safety_stock", "rop", "max_stock")


def _estimator_entries(estimate_trace: str | None) -> list[tuple[str, str]]:
    """The estimator's audit entries out of the stored estimate trace: which method produced the numbers,
    and the mean and median of each parameter side by side, so an approver can see what an outlier donor
    would have done. Empty for a result stored before they existed -- nothing is invented for it."""
    if not estimate_trace:
        return []
    found: dict[str, str] = {}
    for part in str(estimate_trace).split(";"):
        key, separator, value = part.partition("=")
        if separator:
            found[key] = value
    entries = [("estimator", found["estimator"])] if "estimator" in found else []
    for field in _ESTIMATOR_TRACE_FIELDS:
        for suffix in _ESTIMATOR_TRACE_SUFFIXES:
            key = f"{field}{suffix}"
            if key in found:
                entries.append((key, found[key]))
    return entries


def _trace_from_row(row: Any, fields: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    """Ordered (label, value) pairs, skipping fields the row does not carry."""
    entries = []
    for label, attribute in fields:
        value = getattr(row, attribute, None)
        if value is not None:
            entries.append((label, str(value)))
    return entries


def build_normal_recommendation(row: Any, policy: PolicyDocument) -> BuiltRecommendation:
    """A material-plant Phase 3 classified and routed through Phase 4/5.

    ``row`` is the joined Phase 3/5 record the repository provides -- see
    :func:`app.initiatives.i7.recommendations.repository.load_normal_inputs`.
    """
    # SUCCESS_FROM_CURRENT_SAP_VALUE (smooth/erratic materials where I11's
    # current MARC value stood in for I07's own calculation -- see
    # inventory/service.py's _apply_current_sap_baseline) is an equally valid,
    # equally reviewable input as SUCCESS: it means a real, present value
    # exists, just sourced from SAP's current planning baseline rather than
    # I07's own formula. It is a distinct status precisely so this file (and
    # the trace/API) can always say which one it was -- never so it is treated
    # as unavailable.
    _OK_STATUSES = ("SUCCESS", "SUCCESS_FROM_CURRENT_SAP_VALUE")
    safety_stock_ok = row.safety_stock_status in _OK_STATUSES
    rop_ok = row.rop_status in _OK_STATUSES
    max_ok = row.max_stock_status in _OK_STATUSES

    # A month of the demand window is missing or incomplete in the SAP extract
    # (features.consumption_window). Whatever the inventory stage produced, it
    # was not built on a complete history, so nothing is offered for review: the
    # recommendation is NOT_EVALUABLE -- which the workflow will not submit -- and
    # no recommended value is carried, until the months are validated or the
    # extract is reloaded and the pipeline re-run.
    window_missing_data = getattr(row, "window_history_status", None) == MISSING_DATA_WINDOW

    if window_missing_data:
        safety_stock_ok = rop_ok = max_ok = False
        status = LifecycleStatus.NOT_EVALUABLE
        blocking_reason = MISSING_DATA_REASON
        trace = [("blocked_status", MISSING_DATA_WINDOW), ("reason", blocking_reason)]
    elif safety_stock_ok and rop_ok:
        status = LifecycleStatus.READY_FOR_REVIEW
        blocking_reason = None
        trace = _trace_from_row(row, _NORMAL_TRACE_FIELDS)
    else:
        status = LifecycleStatus.NOT_EVALUABLE
        blocking_reason = (
            row.detail
            or f"safety stock status={row.safety_stock_status}, rop status={row.rop_status}"
        )
        trace = [
            ("blocked_status", row.safety_stock_status),
            ("reason", blocking_reason),
        ]

    # A material on the standard path can still be OAR (DISMM ND/PD/blank) --
    # history decides the path, MRP type decides OAR -- and the frequently used
    # ones are exactly the conversion candidates, so it is evaluated here too.
    conversion = conversion_for(row, policy)

    impact = explanation.expected_impact(
        current_ss=row.current_safety_stock,
        recommended_ss=row.safety_stock if safety_stock_ok else None,
        current_rop=row.current_reorder_point,
        recommended_rop=row.rop if rop_ok else None,
        current_max=row.current_maximum_stock,
        recommended_max=row.max_stock if max_ok else None,
        unit_price=row.unit_price,
        holding_cost_rate=policy.max_stock.holding_cost_rate,
    )

    factors = explanation.build_factors(
        demand_class=row.demand_class,
        baseline_model=row.baseline_model,
        forecast_model=getattr(row, "forecast_model", None),
        lead_time_detail=_lead_time_plain(row.lead_time_method, row.valid_po_count),
        # Part 30 -- whether *this persisted calculation* actually had a
        # configured service level, not whether the CURRENT
        # generate_recommendations() call's policy does. inventory/
        # service_level.resolve() only ever sets service_level/z_factor when
        # its own status is SUCCESS (every NOT_EVALUABLE_*/CALCULATION_ERROR
        # path leaves both None) -- so row.service_level is not None is an
        # exact, already-persisted, already-selected proxy for "the
        # inventory run this recommendation was built from had a signed
        # service level for this material," immune to whatever policy is
        # active when a later caller re-runs generation (e.g. DEV mock
        # toggled off, or a reused/idempotent recommendation).
        service_level_configured=row.service_level is not None,
        recommended_rop=row.rop if rop_ok else None,
        current_rop=row.current_reorder_point,
        is_oar=False,
        history_status=row.history_status,
        oar_neighbour_count=None,
        oar_confidence=None,
    )

    return BuiltRecommendation(
        sap_material_number=row.sap_material_number,
        sap_plant_code=row.sap_plant_code,
        status=status,
        is_oar=False,
        demand_class=row.demand_class,
        history_status=row.history_status,
        criticality=row.criticality,
        current_safety_stock=row.current_safety_stock,
        current_rop=row.current_reorder_point,
        current_max_stock=row.current_maximum_stock,
        recommended_safety_stock=row.safety_stock if safety_stock_ok else None,
        recommended_rop=row.rop if rop_ok else None,
        recommended_max_stock=row.max_stock if max_ok else None,
        baseline_model=row.baseline_model,
        forecast_rate=row.forecast_rate,
        lead_time_method=row.lead_time_method,
        safety_stock_method=row.safety_stock_method,
        max_stock_strategy=row.max_stock_strategy,
        confidence=None,
        oar_neighbour_count=None,
        oar_best_similarity=None,
        circuit=row.circuit,
        unit_price=row.unit_price,
        lead_time_days=row.lt_avg_days,
        lead_time_variance_days=row.sigma_lt_days,
        service_level=row.service_level,
        z_factor=row.z_factor,
        conversion=conversion,
        blocking_reason=blocking_reason,
        factors=factors,
        calculation_trace=tuple(trace),
        impact=impact,
        feature_run_id=row.feature_run_id,
        forecast_run_id=row.forecast_run_id,
        inventory_run_id=row.inventory_run_id,
        oar_run_id=None,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
    )


def _lead_time_plain(method: str | None, valid_po_count: int | None) -> str | None:
    """Where the delivery time came from, in words. ``None`` when there is no
    method to describe -- no sentence is written about a lead time nobody had."""
    if not method:
        return None
    if method == "PLANNED_FALLBACK":
        return "Delivery time is the planned delivery time kept for this material in SAP."
    if method == "PLANT_DEFAULT":
        return "Delivery time is not kept for this material; the plant default is assumed."
    if method == "PEER_WEIGHTED_MEAN":
        return "Delivery time is not kept for this material; the average of its similar materials is assumed."
    if valid_po_count:
        plural = "s" if valid_po_count != 1 else ""
        return f"Delivery time is taken from {valid_po_count} past purchase order{plural}."
    return "Delivery time is taken from past purchase orders."


def conversion_for(row: Any, policy: PolicyDocument) -> ConversionDecision | None:
    """The Min-Max conversion decision for one material-plant, on either path.

    ``None`` -- not evaluated at all -- when Stage 1 put the material-plant
    OUT_OF_SCOPE: only OAR materials are considered for conversion, and a
    "not eligible" on a material that was never OAR would read as "stays on
    OAR". Stage 1's verdict is read from the feature row, never re-derived.
    """
    if row.oar_scope == ScopeDecision.OUT_OF_SCOPE:
        return None
    return evaluate_conversion(
        consumption_count_12m=row.consumption_count_12m,
        criticality=row.criticality,
        policy=policy.conversion_triggers,
        oar_scope=row.oar_scope,
    )


def _criticality_tier(raw: str | None) -> Criticality | None:
    """The stored tier as the policy's enum, or None when absent or unrecognised.

    Unknown text is not guessed into a tier: ``resolve`` treats None as "no tier,
    so no service level".
    """
    if raw is None:
        return None
    try:
        return Criticality(str(raw).strip().upper())
    except ValueError:
        return None


def _oar_lead_time(row: Any) -> tuple[Decimal | None, str | None]:
    """The lead time of an OAR material-plant, and how it was obtained.

    The feature row already carries it (``lead_time_days``, resolved from the SAP planned delivery time, MARC-PLIFZ),
    even though an OAR material never reaches Phase 5 -- the lead time comes from the material master by business
    decision, not from Phase 5's inventory calculation. ``(None, None)`` when the material has none: nothing is
    invented for it.
    """
    days = getattr(row, "lead_time_days", None)
    if days is None:
        planned = getattr(row, "planned_delivery_time_days", None)
        days = Decimal(planned) if planned is not None else None
    if days is None:
        return None, None
    return Decimal(days), "PLANNED_FALLBACK"


BLEND_METHOD = "credibility_blend"
BLEND_MAX_STRATEGY = "rop_x_peer_max_ratio"
BLEND_ESTIMATION_METHOD = "CREDIBILITY_BLEND"

_BLEND_LEAD_TIME_METHOD = {
    "MATERIAL_MASTER": "PLANNED_FALLBACK",
    "PLANT_DEFAULT": "PLANT_DEFAULT",
    "PEER_WEIGHTED_MEAN": "PEER_WEIGHTED_MEAN",
}


def _blend_factors(
    blended: blend_module.BlendResult, lead_time_method: str | None
) -> tuple[RecommendationFactor, ...]:
    """The drivers of a blended OAR recommendation: the split in numbers, the lead time and its source, any check."""
    factors = [
        RecommendationFactor(label="Own usage and similar materials", detail=blend_module.explain(blended).split(" Lead time")[0].strip()),
    ]
    lead = _lead_time_plain(lead_time_method, None)
    if lead:
        factors.append(RecommendationFactor(label="Lead time", detail=lead))
    checks = [g for g in blended.guardrails_applied if g.startswith(("max_", "flag_", "no_usable"))]
    if checks:
        factors.append(RecommendationFactor(label="Checks", detail="; ".join(c.replace("_", " ") for c in checks) + "."))
    return tuple(factors)


def _blend_entries(row: Any, blended: blend_module.BlendResult) -> list[tuple[str, str]]:
    """The blend's own working, for the trace: the split, both estimates, the lead time and what was guarded."""

    def fmt(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"

    return [
        ("estimation_method", BLEND_ESTIMATION_METHOD),
        ("blend_status", blended.status.value),
        ("own_estimator", blended.estimator),
        ("n_issues", str(blended.n_events)),
        ("annual_consumption", fmt(blended.annual_consumption)),
        ("rop_own", fmt(blended.rop_own)),
        ("ss_own", fmt(blended.ss_own)),
        ("rop_peer", fmt(blended.rop_peer)),
        ("ss_peer", fmt(blended.ss_peer)),
        ("peer_quality_q", fmt(blended.peer_quality_q)),
        ("w_own", fmt(blended.w_own)),
        ("w_peer", fmt(blended.w_peer)),
        ("max_to_rop_ratio", fmt(blended.max_ratio)),
        ("lead_time_days", fmt(blended.lead_time_days)),
        ("lead_time_source", str(blended.lead_time_source)),
        ("lead_time_bucket_mode", blended.lead_time_bucket_mode),
        ("guardrails_applied", ",".join(blended.guardrails_applied) or "none"),
        ("guardrails_not_applied", ",".join(g.split(":")[0] for g in blended.guardrails_not_applied)),
        ("similarity_safety_stock", str(row.oar_safety_stock)),
        ("similarity_rop", str(row.oar_rop)),
        ("similarity_max_stock", str(row.oar_max_stock)),
        ("blend_summary", blend_module.explain(blended)),
    ]


def build_oar_recommendation(
    row: Any, policy: PolicyDocument, blend: blend_module.BlendResult | None = None
) -> BuiltRecommendation:
    """A cold-start material with Phase 6 similarity evidence.

    ``row`` is the joined Phase 3/6 record from
    :func:`app.initiatives.i7.recommendations.repository.load_oar_inputs`.
    """
    has_neighbours = row.neighbour_count and row.neighbour_count > 0
    estimate_ok = row.estimate_status == "SUCCESS"

    # The credibility-weighted blend (oar.blend) replaces the similarity numbers only when it could size the
    # material; otherwise -- no consumption, no lead time, a similarity estimate that failed -- the similarity
    # result stands exactly as before.
    blended = blend if (blend is not None and estimate_ok and blend.sized) else None
    if blended is not None:
        rec_ss, rec_rop, rec_max = Decimal(blended.ss), Decimal(blended.rop), Decimal(blended.max_stock)
    else:
        rec_ss, rec_rop, rec_max = row.oar_safety_stock, row.oar_rop, row.oar_max_stock

    # Similarity evidence being available is a different fact from the
    # recommendation being reviewable. A human can see *why* a material is
    # OAR, and even see its neighbours and their scores, and still have no
    # actual SS/ROP/Max to approve -- READY_FOR_REVIEW requires the latter,
    # never the former alone. The similarity result itself is preserved on
    # every path via oar_similarity_status/oar_estimate_status regardless of
    # whether the recommendation ends up reviewable.
    oar_similarity_status = "AVAILABLE" if has_neighbours else "NOT_AVAILABLE"
    oar_estimate_status = row.estimate_status

    if estimate_ok:
        status = LifecycleStatus.READY_FOR_REVIEW
        blocking_reason = None
    elif has_neighbours:
        status = LifecycleStatus.NOT_EVALUABLE
        # Name the estimate's own blocker. An unsigned service level is only the
        # reason when the estimate says so -- neighbours lend SAP values, so a
        # too-few-neighbours block must not be reported as a service-level one.
        blocking_reason = (
            "SERVICE_LEVEL_UNSET"
            if row.estimate_status == "NOT_EVALUABLE_SERVICE_LEVEL_UNSET"
            else row.estimate_status
        )
    else:
        status = LifecycleStatus.NOT_EVALUABLE
        # ``row.status`` is the similarity search's own outcome. SUCCESS there means the search ran to the
        # end and found no usable neighbour -- reported as the blocker it is, not as "SUCCESS".
        blocking_reason = (
            NO_SIMILAR_MATERIALS_FOUND
            if row.status == "SUCCESS"
            else row.status or "NO_ELIGIBLE_NEIGHBORS"
        )

    trace: list[tuple[str, str]] = [
        ("candidates_considered", str(row.candidates_considered)),
        ("eligible_candidates", str(row.eligible_candidates)),
        ("neighbour_count", str(row.neighbour_count or 0)),
    ]
    if row.best_similarity is not None:
        trace.append(("best_similarity", str(row.best_similarity)))
    trace.append(("estimate_status", row.estimate_status))
    if estimate_ok:
        trace.extend(
            [
                ("safety_stock", str(rec_ss)),
                ("rop", str(rec_rop)),
                ("max_stock", str(rec_max)),
            ]
        )
        if blended is not None:
            trace.extend(_blend_entries(row, blended))
        else:
            trace.extend(_max_floor_entries(getattr(row, "estimate_trace", None)))
            trace.extend(_estimator_entries(getattr(row, "estimate_trace", None)))
        # A diagnostic against the target's own consumption. It flags; it never changes the status or a
        # number, and it is not part of similarity or eligibility.
        trace.extend(
            demand_check.evaluate(
                demand_check.DemandInputs.from_row(row), rec_rop, policy.demand_sanity
            ).trace_entries()
        )

    # This path is chosen by cold-start history, not by MRP type, so a row here
    # is not necessarily OAR -- conversion_for reads Stage 1's verdict.
    conversion = conversion_for(row, policy)

    impact = explanation.expected_impact(
        current_ss=row.current_safety_stock,
        recommended_ss=rec_ss if estimate_ok else None,
        current_rop=row.current_reorder_point,
        recommended_rop=rec_rop if estimate_ok else None,
        current_max=row.current_maximum_stock,
        recommended_max=rec_max if estimate_ok else None,
        unit_price=row.unit_price,
        holding_cost_rate=policy.max_stock.holding_cost_rate,
    )

    lead_time_days, lead_time_method = _oar_lead_time(row)
    if blended is not None and blended.lead_time_days is not None:
        # the lead time the blend used -- it may be an assumed one, and the card says so
        lead_time_days = Decimal(str(round(blended.lead_time_days, 2)))
        lead_time_method = _BLEND_LEAD_TIME_METHOD.get(blended.lead_time_source, lead_time_method)

    factors = explanation.build_factors(
        demand_class=row.demand_class,
        baseline_model=None,
        lead_time_detail=_lead_time_plain(lead_time_method, None),
        service_level_configured=policy.service_level.is_configured,
        recommended_rop=rec_rop if estimate_ok else None,
        current_rop=row.current_reorder_point,
        is_oar=True,
        history_status=row.history_status,
        oar_neighbour_count=row.neighbour_count,
        oar_confidence=row.confidence,
    )

    if blended is not None:
        # The standard OAR wording says the levels cannot be worked out from the material's own usage -- the
        # opposite of what a blend does. Say what it did.
        factors = _blend_factors(blended, lead_time_method)

    tier_service = service_level_module.resolve(
        policy.service_level, _criticality_tier(row.criticality), None
    )

    return BuiltRecommendation(
        sap_material_number=row.sap_material_number,
        sap_plant_code=row.sap_plant_code,
        status=status,
        is_oar=True,
        demand_class=row.demand_class,
        history_status=row.history_status,
        criticality=row.criticality,
        current_safety_stock=row.current_safety_stock,
        current_rop=row.current_reorder_point,
        current_max_stock=row.current_maximum_stock,
        recommended_safety_stock=rec_ss if estimate_ok else None,
        recommended_rop=rec_rop if estimate_ok else None,
        recommended_max_stock=rec_max if estimate_ok else None,
        baseline_model=None,
        forecast_rate=None,
        lead_time_method=lead_time_method,
        safety_stock_method=(
            BLEND_METHOD if blended is not None else "similarity_weighted" if estimate_ok else None
        ),
        max_stock_strategy=BLEND_MAX_STRATEGY if blended is not None else None,
        confidence=row.confidence,
        oar_neighbour_count=row.neighbour_count,
        oar_best_similarity=row.best_similarity,
        # circuit is a Phase 5 output; an OAR/cold-start material never reaches Phase 5 (see
        # inventory/service.py's DEFERRED_TO_OAR), so it is genuinely unavailable here. The lead time is not: it is
        # the SAP planned delivery time, already on the feature row (see _oar_lead_time). unit_price is a
        # feature-store (Phase 3) field, available regardless of history status.
        #
        # The service level is different: it depends only on the criticality
        # tier, which an OAR material does have. It is looked up through the same
        # gate the standard path uses, so an unsigned matrix or a missing tier
        # still yields None -- never a default. It is the tier's *target*: the
        # OAR estimate is similarity-weighted and did not use it.
        circuit=None,
        unit_price=row.unit_price,
        lead_time_days=lead_time_days,
        lead_time_variance_days=None,
        service_level=tier_service.service_level,
        z_factor=tier_service.z_factor,
        conversion=conversion,
        blocking_reason=blocking_reason,
        factors=factors,
        calculation_trace=tuple(trace),
        impact=impact,
        feature_run_id=row.feature_run_id,
        forecast_run_id=None,
        inventory_run_id=None,
        oar_run_id=row.oar_run_id,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        oar_similarity_status=oar_similarity_status,
        oar_estimate_status=oar_estimate_status,
    )


def deferred_recommendation(row: Any, policy: PolicyDocument) -> BuiltRecommendation:
    """A cold-start material with no Phase 6 result at all yet (e.g. Phase 6
    has not been run since the latest feature build). NOT_EVALUABLE, not OAR
    success or failure -- there is simply no evidence yet."""
    return BuiltRecommendation(
        sap_material_number=row.sap_material_number,
        sap_plant_code=row.sap_plant_code,
        status=LifecycleStatus.NOT_EVALUABLE,
        is_oar=True,
        demand_class=None,
        history_status=row.history_status,
        criticality=row.criticality,
        current_safety_stock=None,
        current_rop=None,
        current_max_stock=None,
        recommended_safety_stock=None,
        recommended_rop=None,
        recommended_max_stock=None,
        baseline_model=None,
        forecast_rate=None,
        lead_time_method=None,
        safety_stock_method=None,
        max_stock_strategy=None,
        confidence=None,
        oar_neighbour_count=None,
        oar_best_similarity=None,
        circuit=None,
        unit_price=row.unit_price,
        lead_time_days=None,
        lead_time_variance_days=None,
        service_level=None,
        z_factor=None,
        conversion=None,
        blocking_reason="no OAR similarity run has evaluated this material yet",
        factors=(),
        calculation_trace=(("blocked_status", "NOT_EVALUABLE"), ("reason", "no OAR run")),
        impact=ExpectedImpact(status=ImpactStatus.NOT_EVALUABLE_MISSING_RECOMMENDED),
        feature_run_id=row.feature_run_id,
        forecast_run_id=None,
        inventory_run_id=None,
        oar_run_id=None,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
    )
