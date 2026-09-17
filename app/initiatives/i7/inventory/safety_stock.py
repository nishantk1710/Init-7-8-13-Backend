"""Safety stock -- Path A (normal) and Path B (compound Poisson).

Formula Reference, Stage 5.

**Path A, SMOOTH / ERRATIC**::

    SS = Z * sqrt( LT_avg * sigma_D^2  +  D_avg^2 * sigma_LT^2 )

    term 1: demand uncertainty over the lead time
    term 2: lead-time uncertainty against average demand

**Path B, INTERMITTENT / LUMPY**::

    lambda   = LT_avg / p_final
    E[d^2]   = sigma_nz^2 + mu_nz^2
    Var[LTD] = lambda * E[d^2]
    SS       = Z * sqrt(Var[LTD])

The normal formula does not work for intermittent demand -- a series that is
mostly zeros has a mean and variance that describe nothing that actually
happens. The compound-Poisson form separates *how often* demand arrives from
*how large* it is when it does.

**Rounding happens once, at the end.** Intermediate values stay exact and are
kept in the trace; only the final quantity is rounded up. Rounding a term would
compound through the square root.
"""

import math
from decimal import ROUND_CEILING, Decimal

from app.initiatives.i7.inventory.types import CalculationStatus, SafetyStockResult


def _ceil_units(value: Decimal) -> int:
    """Round UP to a whole unit, per the Formula Reference."""
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _sqrt(value: Decimal) -> Decimal:
    return Decimal(str(math.sqrt(float(value))))


def normal(
    z: Decimal,
    lt_avg_months: Decimal,
    sigma_d: Decimal,
    d_avg: Decimal,
    sigma_lt_months: Decimal,
) -> SafetyStockResult:
    """Path A -- SMOOTH and ERRATIC demand."""
    if lt_avg_months < 0 or sigma_d < 0 or d_avg < 0 or sigma_lt_months < 0:
        return SafetyStockResult(
            status=CalculationStatus.CALCULATION_ERROR,
            method="normal",
            detail="negative input to the safety-stock formula",
        )

    term_1 = lt_avg_months * (sigma_d * sigma_d)
    term_2 = (d_avg * d_avg) * (sigma_lt_months * sigma_lt_months)
    variance = term_1 + term_2

    if variance < 0:
        return SafetyStockResult(
            status=CalculationStatus.CALCULATION_ERROR,
            method="normal",
            detail="negative variance",
        )

    raw = z * _sqrt(variance)

    if raw < 0:
        # Reachable only with a negative Z, i.e. a service level below 50%.
        # Surfaced rather than clamped: a negative safety stock means the inputs
        # are wrong, and max(x, 0) would hide that.
        return SafetyStockResult(
            status=CalculationStatus.CALCULATION_ERROR,
            method="normal",
            raw_safety_stock=raw,
            detail="negative safety stock; check the service level and Z factor",
        )

    return SafetyStockResult(
        status=CalculationStatus.SUCCESS,
        method="normal",
        raw_safety_stock=raw,
        safety_stock=_ceil_units(raw),
        trace=(
            ("z_factor", str(z)),
            ("lt_avg_months", str(lt_avg_months)),
            ("sigma_d", str(sigma_d)),
            ("d_avg", str(d_avg)),
            ("sigma_lt_months", str(sigma_lt_months)),
            ("term_1", str(term_1)),
            ("term_2", str(term_2)),
            ("raw_safety_stock", str(raw)),
            ("rounding_method", "CEILING"),
        ),
    )


def compound_poisson(
    z: Decimal,
    lt_avg_months: Decimal,
    p_final: Decimal,
    mu_nz: Decimal,
    sigma_nz: Decimal,
) -> SafetyStockResult:
    """Path B -- INTERMITTENT and LUMPY demand."""
    if p_final <= 0:
        return SafetyStockResult(
            status=CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND,
            method="compound_poisson",
            detail="smoothed demand interval is zero or negative, so lambda is undefined",
        )
    if lt_avg_months < 0 or mu_nz < 0 or sigma_nz < 0:
        return SafetyStockResult(
            status=CalculationStatus.CALCULATION_ERROR,
            method="compound_poisson",
            detail="negative input to the compound-Poisson formula",
        )

    lambda_events = lt_avg_months / p_final
    second_moment = (sigma_nz * sigma_nz) + (mu_nz * mu_nz)
    variance = lambda_events * second_moment

    if variance < 0:
        return SafetyStockResult(
            status=CalculationStatus.CALCULATION_ERROR,
            method="compound_poisson",
            detail="negative lead-time demand variance",
        )

    raw = z * _sqrt(variance)

    if raw < 0:
        return SafetyStockResult(
            status=CalculationStatus.CALCULATION_ERROR,
            method="compound_poisson",
            raw_safety_stock=raw,
            detail="negative safety stock; check the service level and Z factor",
        )

    return SafetyStockResult(
        status=CalculationStatus.SUCCESS,
        method="compound_poisson",
        raw_safety_stock=raw,
        safety_stock=_ceil_units(raw),
        trace=(
            ("z_factor", str(z)),
            ("lt_avg_months", str(lt_avg_months)),
            ("p_final", str(p_final)),
            ("mu_nz", str(mu_nz)),
            ("sigma_nz", str(sigma_nz)),
            ("lambda", str(lambda_events)),
            ("second_moment", str(second_moment)),
            ("variance_ltd", str(variance)),
            ("raw_safety_stock", str(raw)),
            ("rounding_method", "CEILING"),
        ),
    )
