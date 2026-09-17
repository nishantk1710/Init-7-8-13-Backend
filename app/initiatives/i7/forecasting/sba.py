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

Approved alpha range is 0.05-0.20 (Formula Reference), tuned by backtesting.
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

    SBA predicts a rate rather than a point, so the comparison is against mean
    demand per period over the same window -- the quantity the rate is meant to
    reproduce.
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


def select_alpha(values: list[Decimal], grid: tuple[Decimal, ...] = ALPHA_GRID) -> Decimal:
    """Choose alpha within the approved range. Ties break toward the smaller."""
    best_alpha = grid[0]
    best_error: Decimal | None = None
    for alpha in grid:
        error = _in_sample_error(values, alpha)
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
    chosen = alpha if alpha is not None else select_alpha(values)
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
