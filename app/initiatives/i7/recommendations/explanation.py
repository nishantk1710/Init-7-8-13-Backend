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


#: How each demand class reads to someone who has never heard of ADI or CV-squared.
_PATTERN_PLAIN = {
    "SMOOTH": "steady and regular",
    "ERRATIC": "regular, but the quantities swing from one order to the next",
    "INTERMITTENT": "occasional, with long gaps between uses",
    "LUMPY": "occasional, and the quantities are uneven when it is used",
}

#: The forecasting method, described by what it is good at.
_MODEL_PLAIN = {
    "SES": "a method that follows recent usage",
    "SBA": "a method designed for materials used only now and then",
}

_CONFIDENCE_PLAIN = {
    "HIGH": "high, because many close matches were found",
    "MEDIUM": "medium",
    "LOW": "low, because only a few close matches were found",
}


def _units(value: Decimal) -> str:
    """A stock quantity as a reader would write it: no trailing zeros, no 16.000."""
    if abs(value) >= 10:
        return f"{value:,.0f}"
    return f"{value:,.1f}".rstrip("0").rstrip(".")


def _history_plain(history_status: str | None) -> str:
    if history_status == "NO_HISTORY":
        return "has no recorded usage"
    if history_status == "COLD_START":
        return "has been used too few times to show a reliable pattern"
    return "has too little usage history"


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
                label="Little usage history",
                detail=(
                    f"This material {_history_plain(history_status)}, so its stock "
                    f"levels cannot be worked out from its own past usage."
                ),
            )
        )
        # An unclassified material has no usage pattern to report; saying so
        # ("UNCLASSIFIED") would only be noise to a reader.
        if demand_class and demand_class.upper() in _PATTERN_PLAIN:
            factors.append(
                RecommendationFactor(
                    label="Usage pattern",
                    detail=f"Its usage is {_PATTERN_PLAIN[demand_class.upper()]}.",
                )
            )
        if oar_neighbour_count:
            plural = "s" if oar_neighbour_count != 1 else ""
            confidence = _CONFIDENCE_PLAIN.get((oar_confidence or "LOW").upper(), "low")
            factors.append(
                RecommendationFactor(
                    label="Based on similar materials",
                    detail=(
                        f"The suggested levels come from {oar_neighbour_count} "
                        f"similar material{plural}. Confidence in the estimate is "
                        f"{confidence}."
                    ),
                )
            )
        return tuple(factors)

    if demand_class:
        pattern = _PATTERN_PLAIN.get(demand_class.upper())
        factors.append(
            RecommendationFactor(
                label="Usage pattern",
                detail=(
                    f"Usage is {pattern}."
                    if pattern
                    else f"Usage pattern: {demand_class.lower()}."
                ),
            )
        )
    if baseline_model:
        method = _MODEL_PLAIN.get(baseline_model.upper(), baseline_model)
        factors.append(
            RecommendationFactor(
                label="How demand was projected",
                detail=f"Expected demand is projected with {method}.",
            )
        )
    if lead_time_detail:
        factors.append(
            RecommendationFactor(label="Delivery time", detail=lead_time_detail)
        )
    if not service_level_configured:
        factors.append(
            RecommendationFactor(
                label="Blocked",
                detail=(
                    "No recommendation can be made yet because the service-level "
                    "policy is not configured."
                ),
            )
        )
    elif recommended_rop is not None and current_rop is not None:
        if recommended_rop > current_rop:
            factors.append(
                RecommendationFactor(
                    label="Reorder point",
                    detail=(
                        f"The reorder point should rise from {_units(current_rop)} "
                        f"to {_units(recommended_rop)}, so orders are placed earlier "
                        f"and stock-outs become less likely."
                    ),
                )
            )
        elif recommended_rop < current_rop:
            factors.append(
                RecommendationFactor(
                    label="Reorder point",
                    detail=(
                        f"The reorder point can fall from {_units(current_rop)} "
                        f"to {_units(recommended_rop)}, so less stock is tied up."
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
