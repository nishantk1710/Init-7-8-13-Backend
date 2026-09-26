"""Deterministic reasons, calculation trace, and expected impact.

No LLM. Every reason is a template filled from a value that was actually
computed upstream -- there is no code path that produces a sentence about a
number nobody calculated. A reason that cannot be traced to a Phase 3/4/5/6
field is not written.
"""

from decimal import Decimal

from app.initiatives.i7.contracts.recommendation import CalculationTrace, RecommendationFactor
from app.initiatives.i7.recommendations.types import (
    ExpectedImpact,
    ImpactStatus,
    LifecycleStatus,
    ParameterDelta,
)


def _percent_change(current: Decimal | None, delta: Decimal | None) -> Decimal | None:
    if current is None or delta is None or current == 0:
        return None
    return (delta / current) * Decimal(100)


def parameter_delta(current: Decimal | None, recommended: Decimal | None) -> ParameterDelta:
    """Current vs recommended, plus percentage where the denominator is valid."""
    if current is None or recommended is None:
        return ParameterDelta(current, recommended, None, None)
    delta = recommended - current
    return ParameterDelta(current, recommended, delta, _percent_change(current, delta))


def expected_impact(
    current_ss: Decimal | None,
    recommended_ss: Decimal | None,
    current_rop: Decimal | None,
    recommended_rop: Decimal | None,
    current_max: Decimal | None,
    recommended_max: Decimal | None,
    unit_price: Decimal | None,
    holding_cost_rate: Decimal | None,
) -> ExpectedImpact:
    """Conservative current-vs-recommended deltas.

    Monetary impact requires a unit price and an annual holding rate. Neither
    exists in any I07 document or seeded table (Phase 5's finding, unchanged),
    so the monetary figure is always ``NOT_EVALUABLE_COST_DATA_UNAVAILABLE`` on
    the current configuration -- never estimated from a price alone, since a
    stock-level change without a holding rate has no defined cost.
    """
    if recommended_ss is None and recommended_rop is None and recommended_max is None:
        return ExpectedImpact(
            status=ImpactStatus.NOT_EVALUABLE_MISSING_RECOMMENDED,
            detail="no recommended value exists to compare against",
        )
    if current_ss is None and current_rop is None and current_max is None:
        return ExpectedImpact(
            status=ImpactStatus.NOT_EVALUABLE_MISSING_CURRENT,
            detail="no current SAP value exists to compare against",
        )

    # Monetary impact needs both a unit price and an annual holding rate; the
    # latter exists in no I07 document or seeded table (Phase 5's finding,
    # unchanged), so this is always unavailable on the current configuration.
    # No figure is estimated from price alone -- a stock-level change without a
    # holding rate has no defined cost.
    monetary_detail = None
    if unit_price is None or holding_cost_rate is None:
        monetary_detail = (
            "monetary impact requires a unit price and an annual holding rate; "
            "neither is available in the current configuration"
        )

    return ExpectedImpact(
        status=ImpactStatus.AVAILABLE,
        safety_stock=parameter_delta(current_ss, recommended_ss),
        reorder_point=parameter_delta(current_rop, recommended_rop),
        maximum_stock=parameter_delta(current_max, recommended_max),
        monetary_impact=None,
        detail=monetary_detail,
    )


def build_factors(
    *,
    demand_class: str | None,
    baseline_model: str | None,
    lead_time_detail: str | None,
    service_level_configured: bool,
    recommended_rop: Decimal | None,
    current_rop: Decimal | None,
    is_oar: bool | None,
    history_status: str | None,
    oar_neighbour_count: int | None,
    oar_confidence: str | None,
) -> tuple[RecommendationFactor, ...]:
    """Human-readable drivers, each traceable to an upstream fact.

    Built as a list of conditionally-included entries rather than a fixed six
    slots: a blocked recommendation has different true facts to report than a
    computed one, and forcing both through the same template would produce
    factors that describe fields the recommendation does not have.
    """
    factors: list[RecommendationFactor] = []

    if is_oar:
        factors.append(
            RecommendationFactor(
                label="OAR cold-start",
                detail=(
                    f"Material qualifies for the OAR cold-start path because "
                    f"consumption history is {history_status or 'insufficient'}."
                ),
            )
        )
        if demand_class:
            factors.append(
                RecommendationFactor(
                    label="Demand pattern (supporting signal)",
                    detail=(
                        f"FR-2 demand class is {demand_class} -- a regularity/"
                        f"confidence signal only, not part of OAR eligibility."
                    ),
                )
            )
        if oar_neighbour_count:
            factors.append(
                RecommendationFactor(
                    label="Similarity evidence",
                    detail=(
                        f"Estimate is based on similarity to {oar_neighbour_count} "
                        f"eligible neighbour(s), confidence {oar_confidence or 'LOW'}."
                    ),
                )
            )
        return tuple(factors)

    if demand_class:
        factors.append(
            RecommendationFactor(
                label="Demand pattern",
                detail=f"Demand class is {demand_class}.",
            )
        )
    if baseline_model:
        factors.append(
            RecommendationFactor(
                label="Forecasting model",
                detail=f"{baseline_model} was selected as the baseline model.",
            )
        )
    if lead_time_detail:
        factors.append(
            RecommendationFactor(label="Lead time", detail=lead_time_detail)
        )
    if not service_level_configured:
        factors.append(
            RecommendationFactor(
                label="Blocked",
                detail=(
                    "Recommendation is blocked because the service-level policy "
                    "is not configured."
                ),
            )
        )
    elif recommended_rop is not None and current_rop is not None:
        if recommended_rop > current_rop:
            factors.append(
                RecommendationFactor(
                    label="Reorder point",
                    detail=(
                        f"Recommended ROP ({recommended_rop}) is above current "
                        f"ROP ({current_rop})."
                    ),
                )
            )
        elif recommended_rop < current_rop:
            factors.append(
                RecommendationFactor(
                    label="Reorder point",
                    detail=(
                        f"Recommended ROP ({recommended_rop}) is below current "
                        f"ROP ({current_rop})."
                    ),
                )
            )

    return tuple(factors)


def build_trace(entries: list[tuple[str, str]]) -> CalculationTrace:
    """Wrap already-computed trace entries. Never recalculates -- the values
    come from Phase 4/5/6's own stored traces, verbatim."""
    return CalculationTrace(entries=tuple(entries))


def blocked_trace(status: LifecycleStatus, reason: str) -> CalculationTrace:
    """The trace for a blocked recommendation: what was checked, and where it
    stopped. Never a fabricated partial result."""
    return CalculationTrace(entries=(("blocked_status", status.value), ("reason", reason)))
