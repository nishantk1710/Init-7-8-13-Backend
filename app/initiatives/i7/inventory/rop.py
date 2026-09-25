"""Reorder point.

Formula Reference, Stage 6. The same structure for every demand class::

    E[LTD] = forecast_rate * LT_avg_months
    ROP    = E[LTD] + SS

**The forecast rate, not the historical average.** The selected Phase 4 model's
output is what feeds E[LTD]; substituting ``D_avg`` would quietly discard the
forecasting layer and make the champion/challenger work pointless. They differ
in general -- SES weights recent months more heavily than a flat mean, and SBA's
rate is deliberately below the naive average.

Rounded UP once, at the end.
"""

from decimal import ROUND_CEILING, Decimal

from app.initiatives.i7.inventory.types import CalculationStatus, RopResult


def calculate(
    forecast_rate: Decimal, lt_avg_months: Decimal, safety_stock: int
) -> RopResult:
    """``ROP = forecast_rate * LT_avg + SS``."""
    if forecast_rate < 0:
        return RopResult(
            status=CalculationStatus.NOT_EVALUABLE_INVALID_FORECAST,
            detail=f"negative forecast rate {forecast_rate}",
        )
    if lt_avg_months < 0:
        return RopResult(
            status=CalculationStatus.NOT_EVALUABLE_LEAD_TIME,
            detail=f"negative lead time {lt_avg_months}",
        )
    if safety_stock < 0:
        return RopResult(
            status=CalculationStatus.CALCULATION_ERROR,
            detail=f"negative safety stock {safety_stock}",
        )

    expected = forecast_rate * lt_avg_months
    raw = expected + Decimal(safety_stock)

    return RopResult(
        status=CalculationStatus.SUCCESS,
        expected_lead_time_demand=expected,
        raw_rop=raw,
        rop=int(raw.to_integral_value(rounding=ROUND_CEILING)),
        trace=(
            ("forecast_rate", str(forecast_rate)),
            ("lt_avg_months", str(lt_avg_months)),
            ("expected_lead_time_demand", str(expected)),
            ("safety_stock", str(safety_stock)),
            ("raw_rop", str(raw)),
            ("rounding_method", "CEILING"),
        ),
    )
