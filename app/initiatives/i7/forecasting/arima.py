"""Auto-ARIMA -- challenger for SMOOTH and ERRATIC demand.

The Solution Design names "Auto-ARIMA". This performs the automatic part -- the
order search -- directly over ``statsmodels`` fits rather than through
``pmdarima``, which is effectively unmaintained and does not build on Python
3.14. pmdarima's ``auto_arima`` is itself a loop over statsmodels fits scored by
an information criterion; that loop is below, in about thirty lines, and it is
reproducible to the digit.

**A successful fit is not an adoption.** The Formula Reference is explicit that
Auto-ARIMA is "evaluated only through rolling-origin backtesting against SES".
This module reports a forecast and a fitted order; the selection layer decides
whether either beats the baseline.
"""

import warnings
from decimal import Decimal

from app.initiatives.i7.forecasting.series import PreparedSeries
from app.initiatives.i7.forecasting.types import (
    MODEL_VERSIONS,
    ForecastResult,
    ModelName,
    ModelStatus,
)

MINIMUM_OBSERVATIONS = 6
"""An ARIMA fit needs enough points to estimate its parameters and still leave
residual degrees of freedom. Below six, statsmodels will usually converge on
something meaningless rather than refuse."""

CANDIDATE_ORDERS: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0),
    (1, 0, 0),
    (0, 0, 1),
    (1, 0, 1),
    (0, 1, 0),
    (1, 1, 0),
    (0, 1, 1),
    (1, 1, 1),
    (2, 0, 0),
    (0, 0, 2),
    (2, 1, 0),
    (0, 1, 2),
)
"""Orders searched, p and q up to 2 with and without differencing.

Bounded on purpose: series here hold at most 13 observations, and a richer grid
would select increasingly elaborate models on increasingly thin evidence. The
order is fixed so the search is deterministic.
"""


def _fit_order(values: list[float], order: tuple[int, int, int]):
    """Fit one order. Returns ``(aic, fitted)`` or ``None`` if it will not fit."""
    from statsmodels.tsa.arima.model import ARIMA

    try:
        with warnings.catch_warnings():
            # statsmodels is voluble about convergence on short series. The
            # outcome is what matters and it is scored below; the warnings would
            # otherwise flood a 471-material run.
            warnings.simplefilter("ignore")
            fitted = ARIMA(values, order=order).fit()
        aic = float(fitted.aic)
        if aic != aic:  # NaN
            return None
        return aic, fitted
    except Exception:
        # A failed order is a normal outcome of a search, not an error. The
        # caller reports MODEL_FIT_FAILURE only if *every* order fails.
        return None


def forecast(series: PreparedSeries, horizon_months: int) -> ForecastResult:
    """Search orders by AIC, then forecast a mean demand rate over the horizon.

    The rate is the mean of the ``horizon_months`` predicted values, so a model
    with trend or seasonality contributes its shape rather than just its final
    level -- which is where ARIMA can beat SES's flat forecast.
    """
    version = MODEL_VERSIONS[ModelName.AUTO_ARIMA]

    if series.length < MINIMUM_OBSERVATIONS:
        return ForecastResult(
            model=ModelName.AUTO_ARIMA,
            model_version=version,
            status=ModelStatus.INSUFFICIENT_HISTORY,
            detail=f"{series.length} observations, need {MINIMUM_OBSERVATIONS}",
        )

    values = [float(value) for value in series.values]

    best: tuple[float, object, tuple[int, int, int]] | None = None
    for order in CANDIDATE_ORDERS:
        result = _fit_order(values, order)
        if result is None:
            continue
        aic, fitted = result
        if best is None or aic < best[0]:
            best = (aic, fitted, order)

    if best is None:
        return ForecastResult(
            model=ModelName.AUTO_ARIMA,
            model_version=version,
            status=ModelStatus.MODEL_FIT_FAILURE,
            detail="no candidate order converged",
        )

    aic, fitted, order = best

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            predicted = fitted.forecast(steps=max(horizon_months, 1))
        mean_rate = sum(float(value) for value in predicted) / len(predicted)
    except Exception as exc:
        return ForecastResult(
            model=ModelName.AUTO_ARIMA,
            model_version=version,
            status=ModelStatus.MODEL_FIT_FAILURE,
            detail=f"forecast failed: {type(exc).__name__}",
        )

    if mean_rate != mean_rate:  # NaN
        return ForecastResult(
            model=ModelName.AUTO_ARIMA,
            model_version=version,
            status=ModelStatus.MODEL_FIT_FAILURE,
            detail="forecast produced NaN",
        )

    # ARIMA is unbounded and will happily predict negative demand. Demand is not
    # negative, so the rate is floored -- recorded here rather than hidden,
    # because a model that wanted to predict -3 units is saying something.
    rate = max(Decimal(str(round(mean_rate, 6))), Decimal(0))

    return ForecastResult(
        model=ModelName.AUTO_ARIMA,
        model_version=version,
        status=ModelStatus.SUCCESS,
        rate=rate,
        unit=series.unit,
        training_start=series.periods[0],
        training_end=series.periods[-1],
        horizon_months=horizon_months,
        parameters=(
            ("p", str(order[0])),
            ("d", str(order[1])),
            ("q", str(order[2])),
            ("aic", str(round(aic, 4))),
        ),
    )
