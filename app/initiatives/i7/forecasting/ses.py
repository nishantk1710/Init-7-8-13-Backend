"""Simple Exponential Smoothing -- baseline for SMOOTH and ERRATIC demand.

Formula Reference, Stage 2A::

    l_t      = alpha * y_t + (1 - alpha) * l_(t-1)
    y_(t+h)  = l_t

A level-only forecast: the smoothed level *is* the prediction for every horizon
step, so a multi-step forecast is flat. That is the documented behaviour, not a
simplification.

Written directly rather than via ``statsmodels.SimpleExpSmoothing``. The
recurrence is three lines, and implementing it here means the code matches the
Formula Reference line for line, initialises where the document says to, and
stays deterministic. The library's optimiser also chooses alpha by maximum
likelihood, whereas the source requires alpha to be selected by rolling-origin
backtesting -- a different criterion that would have had to be overridden anyway.

Zero-demand months are ordinary observations here: ``y_t = 0`` pulls the level
down, which is exactly right for a series that has genuinely stopped moving.
"""

from decimal import Decimal

from app.initiatives.i7.forecasting.series import PreparedSeries
from app.initiatives.i7.forecasting.types import (
    MODEL_VERSIONS,
    ForecastResult,
    ModelName,
    ModelStatus,
)

MINIMUM_OBSERVATIONS = 2
"""Two points: one to initialise the level, one to update it."""

ALPHA_GRID: tuple[Decimal, ...] = tuple(
    Decimal(str(round(0.05 + 0.05 * step, 2))) for step in range(19)
)
"""Candidate alphas from 0.05 to 0.95 in steps of 0.05.

A grid rather than a continuous optimiser: the search space is one bounded
parameter, the grid is reproducible to the digit, and with at most 13
observations a finer search would be fitting noise.
"""


def fit_level(values: list[Decimal], alpha: Decimal) -> Decimal:
    """Run the SES recurrence and return the final level.

    Initialised at the first observation, the standard choice for a level-only
    model and the one that makes the first update well defined.
    """
    level = values[0]
    for value in values[1:]:
        level = alpha * value + (Decimal(1) - alpha) * level
    return level


def _in_sample_error(values: list[Decimal], alpha: Decimal) -> Decimal:
    """One-step-ahead absolute error of an alpha over the training window.

    Only training data is touched -- the caller passes the window, and this walks
    it forward predicting each point from its predecessors.
    """
    level = values[0]
    total = Decimal(0)
    for value in values[1:]:
        total += abs(value - level)
        level = alpha * value + (Decimal(1) - alpha) * level
    return total


def select_alpha(values: list[Decimal], grid: tuple[Decimal, ...] = ALPHA_GRID) -> Decimal:
    """Choose alpha by one-step-ahead error over the training window.

    Ties break toward the smaller alpha, so the result is deterministic and
    leans on more history rather than less.
    """
    best_alpha = grid[0]
    best_error: Decimal | None = None
    for alpha in grid:
        error = _in_sample_error(values, alpha)
        if best_error is None or error < best_error:
            best_alpha, best_error = alpha, error
    return best_alpha


def forecast(
    series: PreparedSeries, horizon_months: int, alpha: Decimal | None = None
) -> ForecastResult:
    """Forecast a demand rate in units per month.

    ``alpha`` may be supplied by the backtest harness (which selects it per
    origin); otherwise it is chosen from the series itself.
    """
    version = MODEL_VERSIONS[ModelName.SES]

    if series.length < MINIMUM_OBSERVATIONS:
        return ForecastResult(
            model=ModelName.SES,
            model_version=version,
            status=ModelStatus.INSUFFICIENT_HISTORY,
            detail=f"{series.length} observations, need {MINIMUM_OBSERVATIONS}",
        )

    values = series.values
    chosen = alpha if alpha is not None else select_alpha(values)
    level = fit_level(values, chosen)

    # A level can drift marginally below zero only through rounding; demand
    # cannot be negative, so floor it.
    rate = max(level, Decimal(0))

    return ForecastResult(
        model=ModelName.SES,
        model_version=version,
        status=ModelStatus.SUCCESS,
        rate=rate,
        unit=series.unit,
        training_start=series.periods[0],
        training_end=series.periods[-1],
        horizon_months=horizon_months,
        parameters=(("alpha", str(chosen)),),
    )
