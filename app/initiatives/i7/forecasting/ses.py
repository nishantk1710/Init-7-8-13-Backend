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
backtesting -- a different criterion that would have had to be overridden
anyway, and is what ``select_alpha`` now actually does (see its own docstring).

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

    Retained only as ``select_alpha``'s fallback when no candidate can be
    backtested at all (see its docstring) -- no longer the primary selection
    method. Only training data is touched -- the caller passes the window, and
    this walks it forward predicting each point from its predecessors, but it
    never re-fits from a growing window at independent origins the way
    :mod:`app.initiatives.i7.forecasting.backtest` does, so it is an in-sample
    fit, not a rolling-origin backtest.
    """
    level = values[0]
    total = Decimal(0)
    for value in values[1:]:
        total += abs(value - level)
        level = alpha * value + (Decimal(1) - alpha) * level
    return total


def select_alpha(
    series: PreparedSeries, horizon_months: int, grid: tuple[Decimal, ...] = ALPHA_GRID
) -> Decimal:
    """Choose alpha by rolling-origin backtest.

    The Formula Reference is explicit: "The parameter alpha is selected using
    rolling-origin backtesting" -- the same protocol
    (:mod:`app.initiatives.i7.forecasting.backtest`) the champion/challenger
    decision already runs, not a single in-sample pass over the training
    window. Each candidate is backtested independently over its own origins
    and scored by mean absolute error -- quantile-free, for the same reason as
    SBA's alpha selection: SES is a point forecast, and its own tuning must
    not depend on the (possibly unsigned) service-level quantile that belongs
    to the quantile challengers, not the baseline.

    Every candidate forecast call below passes ``alpha=`` explicitly so it
    never re-enters this function -- SES's 19-value grid makes this the same
    recursion guard SBA's 4-value grid needs, just with more candidates to
    pin down.

    Falls back to the in-sample fit only when no candidate could be
    backtested at all (fewer than the 3-month minimum training window plus
    one scorable origin -- reachable here because ``MINIMUM_OBSERVATIONS`` (2)
    is below the backtest's own minimum training window (3), so ``forecast``
    can still be called on a series too short for any origin). Ties break
    toward the smaller alpha, both here and in the fallback, so the result
    leans on more history rather than less when the choice is otherwise
    indifferent.
    """
    from app.initiatives.i7.forecasting import backtest as backtest_engine
    from app.initiatives.i7.forecasting import metrics as metric_functions
    from app.initiatives.i7.forecasting.types import ModelName

    best_alpha = grid[0]
    best_score: Decimal | None = None
    for alpha in grid:
        result = backtest_engine.run(
            series,
            ModelName.SES,
            MODEL_VERSIONS[ModelName.SES],
            lambda s, h, a=alpha: forecast(s, h, alpha=a),
            horizon_months,
        )
        if not result.paths:
            continue
        score = metric_functions.mean_absolute_error(list(result.paths))
        if score is None:
            continue
        if best_score is None or score < best_score:
            best_alpha, best_score = alpha, score

    if best_score is not None:
        return best_alpha

    # No candidate produced a single scorable origin -- fall back to the
    # in-sample fit rather than leaving alpha unselectable.
    best_alpha = grid[0]
    best_error: Decimal | None = None
    for alpha in grid:
        error = _in_sample_error(series.values, alpha)
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
    chosen = alpha if alpha is not None else select_alpha(series, horizon_months)
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
