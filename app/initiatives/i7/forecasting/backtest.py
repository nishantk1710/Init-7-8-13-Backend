"""Rolling-origin backtesting.

The protocol, from the Solution Design::

    1. train on history through month T
    2. forecast T+1 .. T+LT
    3. compare against actual demand
    4. move T forward one month
    5. repeat

**No fabricated origins.** The number of origins is whatever the data supports.
A 13-month window with a 3-month minimum training period and a 1-month horizon
yields 10 origins -- never the 12 the documents require for production adoption.
That shortfall is reported, not closed by duplicating months or by lowering the
requirement.

**Leakage is structural, not disciplinary.** A model at origin ``i`` receives
``series.through(i)`` and nothing else. There is no argument by which a later
observation could reach it, so the guarantee does not depend on every model
author remembering it.
"""

from decimal import Decimal
from typing import Callable

from app.initiatives.i7.forecasting.series import PreparedSeries, add_months
from app.initiatives.i7.forecasting.types import (
    BacktestResult,
    BacktestStatus,
    ForecastResult,
    ModelName,
    ModelStatus,
    OriginForecast,
)

REQUIRED_ORIGINS = 12
"""The production acceptance bar. Fixed at the documented value: the current
extract cannot reach it, and lowering it would convert a data limitation into a
silent change of acceptance criteria."""

MINIMUM_TRAINING_MONTHS = 3
"""History a model needs before its first forecast. Three is the smallest window
in which SES has a level to update and SBA can see two demand events."""

ForecastFunction = Callable[[PreparedSeries, int], ForecastResult]
"""A model, as the harness sees it: training window and horizon in, forecast out."""


def count_available_origins(
    series_length: int, horizon_months: int, minimum_training: int = MINIMUM_TRAINING_MONTHS
) -> int:
    """Origins with at least one actual period to score against.

    Origins whose horizon runs past the end of the data are still counted when
    *some* of their horizon is observable -- the partial path is scored over the
    periods that exist. Discarding them would waste the most recent and most
    relevant history.
    """
    if series_length <= minimum_training:
        return 0
    return max(0, series_length - minimum_training)


def run(
    series: PreparedSeries,
    model: ModelName,
    model_version: str,
    forecast_function: ForecastFunction,
    horizon_months: int,
    *,
    required_origins: int = REQUIRED_ORIGINS,
    minimum_training: int = MINIMUM_TRAINING_MONTHS,
) -> BacktestResult:
    """Walk the origins forward, collecting every forecast/actual pair."""
    available = count_available_origins(series.length, horizon_months, minimum_training)

    if available == 0:
        return BacktestResult(
            model=model,
            model_version=model_version,
            status=BacktestStatus.NOT_EVALUABLE_INSUFFICIENT_HISTORY,
            required_origins=required_origins,
            available_origins=0,
            origins_evaluated=0,
            detail=(
                f"{series.length} months with a {minimum_training}-month minimum "
                "training window leaves no origin"
            ),
        )

    paths: list[OriginForecast] = []
    evaluated = 0

    for index in range(minimum_training, series.length):
        training = series.through(index)
        result = forecast_function(training, horizon_months)
        if not result.is_available:
            # A model that cannot fit at this origin contributes nothing. The
            # origin still counts as available -- the data was there.
            continue

        origin_period = training.periods[-1]
        scored = 0
        for step in range(1, horizon_months + 1):
            actual_index = index + step - 1
            if actual_index >= series.length:
                # The horizon runs past the data. The path is scored over the
                # periods that exist rather than padded with invented actuals.
                break
            paths.append(
                OriginForecast(
                    origin_period=origin_period,
                    horizon_step=step,
                    forecast_period=add_months(origin_period, step),
                    predicted=result.rate,
                    actual=series.points[actual_index].quantity,
                )
            )
            scored += 1

        if scored:
            evaluated += 1

    if evaluated == 0:
        return BacktestResult(
            model=model,
            model_version=model_version,
            status=BacktestStatus.NOT_EVALUABLE,
            required_origins=required_origins,
            available_origins=available,
            origins_evaluated=0,
            detail="no origin produced a usable forecast",
        )

    status = (
        BacktestStatus.COMPLETE
        if evaluated >= required_origins
        else BacktestStatus.PARTIAL_DEVELOPMENT_DATA
    )

    return BacktestResult(
        model=model,
        model_version=model_version,
        status=status,
        required_origins=required_origins,
        available_origins=available,
        origins_evaluated=evaluated,
        paths=tuple(paths),
        detail=(
            None
            if status is BacktestStatus.COMPLETE
            else f"{evaluated} of {required_origins} required origins available "
            "in the development extract"
        ),
    )


def not_evaluable(
    model: ModelName, model_version: str, status: BacktestStatus, detail: str
) -> BacktestResult:
    """A backtest that could not start -- no lead time, no model, no quantile."""
    return BacktestResult(
        model=model,
        model_version=model_version,
        status=status,
        required_origins=REQUIRED_ORIGINS,
        available_origins=0,
        origins_evaluated=0,
        detail=detail,
    )
