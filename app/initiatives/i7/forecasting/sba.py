"""Syntetos-Boylan Approximation -- baseline for INTERMITTENT and LUMPY demand.

Formula Reference, Stage 5 Path B, Step B1. After each non-zero demand at time
``t``::

    p_t = alpha * q_t + (1 - alpha) * p_(t-1)     smoothed interval
    z_t = alpha * d_t + (1 - alpha) * z_(t-1)     smoothed demand size

    y_SBA = (1 - alpha/2) * z_final / p_final

where ``q_t`` is the number of periods since the previous non-zero demand and
``d_t`` the quantity. The ``(1 - alpha/2)`` factor is Syntetos and Boylan's
correction for Croston's documented positive bias -- it is the whole point of the
method and is never omitted.

**Updates happen only on demand events.** Zero months are not skipped -- they
lengthen ``q_t``, which is how the inter-arrival interval grows for a material
that has gone quiet. Updating on every period instead would collapse the method
back toward a plain moving average and lose the intermittency signal entirely.

Approved alpha range is 0.05-0.20 (Formula Reference / Solution Design: "tune
via grid search on backtest"), which is what ``select_alpha`` does -- each
candidate is scored by its own rolling-origin backtest MAE, not by closeness
to the training window's own mean.
"""

from decimal import Decimal

from app.initiatives.i7.forecasting.series import PreparedSeries
from app.initiatives.i7.forecasting.types import (
    MODEL_VERSIONS,
    ForecastResult,
    ModelName,
    ModelStatus,
)

MINIMUM_DEMAND_EVENTS = 2
"""Two events. The first initialises ``z`` and ``p``; an interval cannot be
measured until a second arrives."""

ALPHA_GRID: tuple[Decimal, ...] = (
    Decimal("0.05"),
    Decimal("0.10"),
    Decimal("0.15"),
    Decimal("0.20"),
)
"""The documented 0.05-0.20 range. Deliberately not widened: the Formula
Reference states the range, and a broader grid would be an unapproved change to
a business-tuned parameter."""


def smooth(values: list[Decimal], alpha: Decimal) -> tuple[Decimal, Decimal, int] | None:
    """Run the SBA recurrence.

    Returns ``(z_final, p_final, event_count)``, or ``None`` when there are too
    few demand events to estimate an interval.
    """
    events: list[tuple[int, Decimal]] = []
    gap = 0
    for value in values:
        gap += 1
        if value > 0:
            events.append((gap, value))
            gap = 0

    if len(events) < MINIMUM_DEMAND_EVENTS:
        return None

    # Initialise from the first event: size is its quantity, interval its
    # position. Updating from the second event onward gives every subsequent
    # q_t a real predecessor to measure from.
    _, first_size = events[0]
    smoothed_size = first_size
    smoothed_interval = Decimal(events[0][0])

    for interval, size in events[1:]:
        smoothed_interval = alpha * Decimal(interval) + (Decimal(1) - alpha) * smoothed_interval
        smoothed_size = alpha * size + (Decimal(1) - alpha) * smoothed_size

    return smoothed_size, smoothed_interval, len(events)


def _in_sample_error(values: list[Decimal], alpha: Decimal) -> Decimal | None:
    """Absolute error of an alpha's per-period rate against observed demand.

    Retained only as ``select_alpha``'s fallback when no candidate can be
    backtested at all (see its docstring) -- no longer the primary selection
    method. SBA predicts a rate rather than a point, so the comparison is
    against mean demand per period over the same window -- the quantity the
    rate is meant to reproduce.
    """
    fitted = smooth(values, alpha)
    if fitted is None:
        return None
    size, interval, _ = fitted
    if interval == 0:
        return None
    rate = (Decimal(1) - alpha / Decimal(2)) * size / interval
    observed = sum(values, Decimal(0)) / Decimal(len(values))
    return abs(rate - observed)


def select_alpha(
    series: PreparedSeries, horizon_months: int, grid: tuple[Decimal, ...] = ALPHA_GRID
) -> Decimal:
    """Choose alpha within the approved range by rolling-origin backtest.

    The Solution Design is explicit: alpha is "0.05 to 0.20, tune via grid
    search on backtest" -- the same rolling-origin protocol
    (:mod:`app.initiatives.i7.forecasting.backtest`) the champion/challenger
    decision already runs, not a fit against the training window's own mean.
    Each candidate is backtested independently and scored by mean absolute
    error over its own origins -- quantile-free on purpose: SBA is a point
    forecast, and scoring it by pinball loss would tie its own tuning to the
    service-level quantile that is LightGBM's business, not SBA's, and would
    make alpha unselectable whenever that policy is unsigned.

    Every candidate forecast call below passes ``alpha=`` explicitly so it
    never re-enters this function -- the recursion guard that keeps a
    12-origin backtest from itself running four nested 12-origin backtests
    per origin.

    Falls back to the in-sample fit only when no candidate could be
    backtested at all (fewer than the 3-month minimum training window plus
    one scorable origin) -- the same condition ``forecast`` already handles by
    reporting ``INSUFFICIENT_NON_ZERO_OBSERVATIONS``/short-series statuses
    downstream. Ties break toward the smaller alpha, both here and in the
    fallback.
    """
    from app.initiatives.i7.forecasting import backtest as backtest_engine
    from app.initiatives.i7.forecasting import metrics as metric_functions
    from app.initiatives.i7.forecasting.types import ModelName

    best_alpha = grid[0]
    best_score: Decimal | None = None
    for alpha in grid:
        result = backtest_engine.run(
            series,
            ModelName.SBA,
            MODEL_VERSIONS[ModelName.SBA],
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

    # No candidate produced a single scorable origin (a short series, or one
    # whose demand events are too sparse for any alpha to fit within the
    # available training window) -- fall back to the in-sample fit rather
    # than reporting no forecast at all.
    best_alpha = grid[0]
    best_error: Decimal | None = None
    for alpha in grid:
        error = _in_sample_error(series.values, alpha)
        if error is None:
            continue
        if best_error is None or error < best_error:
            best_alpha, best_error = alpha, error
    return best_alpha


def forecast(
    series: PreparedSeries, horizon_months: int, alpha: Decimal | None = None
) -> ForecastResult:
    """Forecast a demand rate in units per month."""
    version = MODEL_VERSIONS[ModelName.SBA]

    if series.non_zero_count == 0:
        return ForecastResult(
            model=ModelName.SBA,
            model_version=version,
            status=ModelStatus.NO_NON_ZERO_DEMAND,
            detail="no demand events in the series",
        )

    values = series.values
    chosen = alpha if alpha is not None else select_alpha(series, horizon_months)
    fitted = smooth(values, chosen)

    if fitted is None:
        return ForecastResult(
            model=ModelName.SBA,
            model_version=version,
            status=ModelStatus.INSUFFICIENT_NON_ZERO_OBSERVATIONS,
            detail=f"{series.non_zero_count} demand event(s), need {MINIMUM_DEMAND_EVENTS}",
        )

    size, interval, events = fitted
    if interval <= 0:
        # Unreachable: an interval is at least one period. Guarded because the
        # division below would otherwise be the failure point.
        return ForecastResult(
            model=ModelName.SBA,
            model_version=version,
            status=ModelStatus.MODEL_FIT_FAILURE,
            detail="smoothed interval collapsed to zero",
        )

    rate = (Decimal(1) - chosen / Decimal(2)) * size / interval

    return ForecastResult(
        model=ModelName.SBA,
        model_version=version,
        status=ModelStatus.SUCCESS,
        rate=max(rate, Decimal(0)),
        unit=series.unit,
        training_start=series.periods[0],
        training_end=series.periods[-1],
        horizon_months=horizon_months,
        parameters=(
            ("alpha", str(chosen)),
            ("p_final", str(interval)),
            ("z_final", str(size)),
            ("demand_events", str(events)),
        ),
    )
