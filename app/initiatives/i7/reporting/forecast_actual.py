"""Forecast vs Actual Demand for a report's period, from the champion backtests.

The same source as the Overview's chart (``GET /forecast-vs-actual``): the latest succeeded forecast run's
one-step-ahead paths, where each path carries the month it predicts (``forecast_period``) and what was actually
used in that month. A forecast is therefore compared only with the same month's actual demand -- the alignment is
in the data, not worked out afterwards.

**Accuracy is aggregated from the observations, not from monthly percentages.** Mean absolute error and bias as a
fraction of actual demand are the project's own definitions (``app.initiatives.i7.forecasting.metrics``); here they
are taken over every path observation in the period in one pass, so a month with few observations does not weigh
the same as one with many, and no percentage is averaged with another.

Quantities are summed in each material's own unit of measure, as the Overview's chart does.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.initiatives.i7.reporting.period import months_of
from app.models.i7_forecast import ForecastBacktestPath, ForecastRun
from app.schemas.i7.reports import (
    AvailabilityStatus,
    ForecastActualAccuracy,
    ForecastActualMonth,
    ForecastActualSection,
)

ALIGNMENT_NOTE = (
    "Each forecast is the champion model's one-month-ahead prediction for a month, compared with what was "
    "actually used in that same month. Quantities are summed in each material's own unit of measure. "
    "Material-plants with no backtest (cold start, and the declining-demand path) are not in the sums."
)


def _dec(value) -> Decimal:
    """A database number as an exact Decimal. ``str`` first so a float (SQLite) is not widened to its binary expansion."""
    return Decimal(str(value))


def build_forecast_actual(session: Session, period_start: date, period_end: date) -> ForecastActualSection:
    months = months_of(period_start, period_end)
    run_id = session.execute(select(func.max(ForecastRun.id)).where(ForecastRun.status == "succeeded")).scalar()
    if run_id is None:
        return ForecastActualSection(
            status=AvailabilityStatus.NOT_AVAILABLE,
            months=[ForecastActualMonth(month=m, forecast=None, actual=None, material_count=0) for m in months],
            forecast_total=None,
            actual_total=None,
            accuracy=None,
            forecast_run_id=None,
            note="No forecast run has succeeded, so there is no forecast to compare with actual demand. " + ALIGNMENT_NOTE,
        )

    in_period = (
        ForecastBacktestPath.forecast_run_id == run_id,
        ForecastBacktestPath.horizon_step == 1,
        ForecastBacktestPath.forecast_period >= period_start,
        ForecastBacktestPath.forecast_period <= period_end,
    )
    by_month = {
        row.forecast_period: row
        for row in session.execute(
            select(
                ForecastBacktestPath.forecast_period.label("forecast_period"),
                func.sum(ForecastBacktestPath.predicted).label("predicted"),
                func.sum(ForecastBacktestPath.actual).label("actual"),
                func.count(
                    func.distinct(
                        ForecastBacktestPath.sap_material_number + "|" + ForecastBacktestPath.sap_plant_code
                    )
                ).label("materials"),
            )
            .where(*in_period)
            .group_by(ForecastBacktestPath.forecast_period)
        ).all()
    }
    points = [
        ForecastActualMonth(
            month=m,
            forecast=_dec(by_month[m].predicted) if m in by_month else None,
            actual=_dec(by_month[m].actual) if m in by_month else None,
            material_count=int(by_month[m].materials) if m in by_month else 0,
        )
        for m in months
    ]
    present = [p for p in points if p.forecast is not None]
    if not present:
        return ForecastActualSection(
            status=AvailabilityStatus.NOT_AVAILABLE,
            months=points,
            forecast_total=None,
            actual_total=None,
            accuracy=None,
            forecast_run_id=run_id,
            note="The latest forecast run has no backtest observations for this period's months. " + ALIGNMENT_NOTE,
        )

    forecast_total = sum((p.forecast for p in present), Decimal(0))
    actual_total = sum((p.actual or Decimal(0) for p in present), Decimal(0))
    # Sums, divided here: an exact Decimal mean on every database, where AVG returns a float on some.
    observations, abs_error_total, error_total = session.execute(
        select(
            func.count(),
            func.sum(func.abs(ForecastBacktestPath.predicted - ForecastBacktestPath.actual)),
            func.sum(ForecastBacktestPath.predicted - ForecastBacktestPath.actual),
        ).where(*in_period)
    ).one()
    mean_abs_error = _dec(abs_error_total) / Decimal(observations) if observations and abs_error_total is not None else None
    # Bias as a fraction of actual demand: None, never 0, when there was no actual demand to be a fraction of.
    bias = _dec(error_total) / actual_total if actual_total != 0 and error_total is not None else None
    return ForecastActualSection(
        status=AvailabilityStatus.AVAILABLE,
        months=points,
        forecast_total=forecast_total,
        actual_total=actual_total,
        accuracy=ForecastActualAccuracy(
            observations=int(observations),
            mean_absolute_error=mean_abs_error,
            bias_percentage=bias,
        ),
        forecast_run_id=run_id,
        note=ALIGNMENT_NOTE,
    )
