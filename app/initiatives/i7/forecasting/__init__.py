"""I07 demand forecasting and rolling-origin backtesting.

    feature store -> routing -> models -> backtest -> segment decision -> forecast

Models predict **demand only**, as a rate in units per month. Nothing here
produces safety stock, a reorder point or a maximum -- those stay deterministic
Phase 5 calculations over the forecast, which is what keeps them auditable.

| Module | Responsibility |
| --- | --- |
| ``types`` | statuses, model names, result contracts |
| ``series`` | validated demand series, training windows |
| ``ses`` | SES baseline (SMOOTH / ERRATIC) |
| ``arima`` | Auto-ARIMA challenger (SMOOTH / ERRATIC) |
| ``sba`` | SBA baseline (INTERMITTENT / LUMPY) |
| ``lightgbm_model`` | global pooled quantile challenger |
| ``tsb`` | obsolescence-aware candidate |
| ``metrics`` | pinball loss, bias, fill rate, holding cost |
| ``backtest`` | rolling-origin engine |
| ``selection`` | champion/challenger decision, per segment |
| ``service`` | orchestration and persistence |

Three statuses travel with every result and are not interchangeable: whether the
model ran, how much backtest evidence exists, and whether production adoption is
warranted. On the current extract a model can succeed, be backtested over 8
origins, and still be ineligible -- the documents require 12.

The module is named ``lightgbm_model`` rather than ``lightgbm`` so that
``import lightgbm`` inside it reaches the installed library rather than itself.
"""

from app.initiatives.i7.forecasting.service import ForecastRunResult, run_forecasting
from app.initiatives.i7.forecasting.types import (
    MODEL_VERSIONS,
    AdoptionStatus,
    BacktestMetrics,
    BacktestResult,
    BacktestStatus,
    DemandPoint,
    ForecastResult,
    MetricStatus,
    ModelName,
    ModelStatus,
    OriginForecast,
    SegmentDecision,
)

__all__ = [
    "MODEL_VERSIONS",
    "AdoptionStatus",
    "BacktestMetrics",
    "BacktestResult",
    "BacktestStatus",
    "DemandPoint",
    "ForecastResult",
    "ForecastRunResult",
    "MetricStatus",
    "ModelName",
    "ModelStatus",
    "OriginForecast",
    "SegmentDecision",
    "run_forecasting",
]
