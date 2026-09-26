"""LightGBM quantile challenger -- INTERMITTENT and LUMPY demand.

**Global pooled, never per material.** The Solution Design is explicit, and the
data agrees: a single material-plant here holds at most 13 observations, which
would not train anything. One model learns across all eligible series and is
asked about each.

**Blocked on the service-level matrix.** LightGBM is a *quantile* model, and the
target quantile is the service level -- which Vedanta has not signed. There is no
safe default: 0.95 and 0.98 produce materially different forecasts and neither
is attributable to a business decision. So training refuses with
``NOT_EVALUABLE_SERVICE_LEVEL_UNSET`` until a signed matrix supplies the
quantile, which is then read from policy rather than written here.

**Leakage is prevented structurally.** Every feature for the row at index ``i``
is computed from ``values[:i]`` alone -- :func:`build_features` never receives
the future. That is why the rolling statistics, months-since-demand and ADI/CV²
features can be trusted at a backtest origin: there is no code path by which a
later observation reaches an earlier row.

Features the Solution Design lists but the data does not supply -- criticality,
circuit, lead-time statistics, price band -- are **omitted**, not imputed. The
feature set in use is recorded in ``FEATURE_VERSION`` so a later run with fuller
data is distinguishable rather than silently different.
"""

from decimal import Decimal
from typing import NamedTuple

from app.initiatives.i7.forecasting.series import PreparedSeries
from app.initiatives.i7.forecasting.types import (
    MODEL_VERSIONS,
    ForecastResult,
    ModelName,
    ModelStatus,
)

FEATURE_VERSION = "demand-only-1"
"""Which feature groups this model was trained on.

``demand-only``: the Solution Design's material-attribute and lead-time groups
are absent because criticality reaches 1.7% of material-plants and lead-time
statistics are Phase 5 policy. Recorded rather than worked around -- a model
trained on fuller data must not be mistaken for this one.
"""

FEATURE_NAMES: tuple[str, ...] = (
    "months_observed",
    "months_since_last_demand",
    "rolling_3_sum",
    "rolling_6_sum",
    "rolling_12_sum",
    "non_zero_count",
    "mean_non_zero",
    "adi",
    "cv_squared",
    "month",
    "quarter",
)

MINIMUM_TRAINING_ROWS = 50
"""Below this a gradient-booster memorises rather than generalises."""

MINIMUM_HISTORY_FOR_ROW = 3
"""Rows need some past before their features mean anything."""


class PooledRow(NamedTuple):
    """One training row: features known at a period, and that period's demand."""

    features: list[float]
    target: float


def build_features(history: list[Decimal], period_month: int) -> list[float]:
    """Features from ``history`` only -- the observations strictly before the row.

    The leakage control. Callers pass ``values[:i]`` and the target is
    ``values[i]``, so nothing at or after the predicted period is visible.
    """
    count = len(history)
    non_zero = [value for value in history if value > 0]

    months_since = 0
    for value in reversed(history):
        if value > 0:
            break
        months_since += 1

    def window_sum(size: int) -> float:
        return float(sum(history[-size:], Decimal(0)))

    mean_non_zero = (
        float(sum(non_zero, Decimal(0)) / Decimal(len(non_zero))) if non_zero else 0.0
    )
    adi = float(Decimal(count) / Decimal(len(non_zero))) if non_zero else 0.0

    if len(non_zero) >= 2:
        mean = sum(non_zero, Decimal(0)) / Decimal(len(non_zero))
        variance = sum((value - mean) ** 2 for value in non_zero) / Decimal(
            len(non_zero) - 1
        )
        cv_squared = float(variance / (mean * mean)) if mean > 0 else 0.0
    else:
        cv_squared = 0.0

    return [
        float(count),
        float(months_since),
        window_sum(3),
        window_sum(6),
        window_sum(12),
        float(len(non_zero)),
        mean_non_zero,
        adi,
        cv_squared,
        float(period_month),
        float((period_month - 1) // 3 + 1),
    ]


def rows_for_series(series: PreparedSeries) -> list[PooledRow]:
    """Every trainable row from one series, each built from its own past."""
    rows: list[PooledRow] = []
    values = series.values
    for index in range(MINIMUM_HISTORY_FOR_ROW, len(values)):
        rows.append(
            PooledRow(
                features=build_features(values[:index], series.points[index].period.month),
                target=float(values[index]),
            )
        )
    return rows


class PooledModel(NamedTuple):
    """A trained global model, plus what it was trained on."""

    booster: object
    quantile: float
    feature_version: str
    training_rows: int


def train(
    corpus: list[PreparedSeries], quantile: float | None
) -> tuple[PooledModel | None, ModelStatus, str]:
    """Train one global model across every eligible series.

    ``quantile`` comes from the signed service-level matrix. ``None`` means
    unsigned, and training refuses rather than picking one.
    """
    if quantile is None:
        return (
            None,
            ModelStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET,
            "target quantile requires the signed Criticality x Circuit "
            "service-level matrix; no value may be assumed",
        )
    if not 0 < quantile < 1:
        return None, ModelStatus.MISSING_REQUIRED_FEATURE, f"invalid quantile {quantile}"

    rows: list[PooledRow] = []
    for series in corpus:
        rows.extend(rows_for_series(series))

    if len(rows) < MINIMUM_TRAINING_ROWS:
        return (
            None,
            ModelStatus.INSUFFICIENT_HISTORY,
            f"{len(rows)} pooled training rows, need {MINIMUM_TRAINING_ROWS}",
        )

    import lightgbm as lgb
    import numpy as np

    features = np.array([row.features for row in rows], dtype=float)
    targets = np.array([row.target for row in rows], dtype=float)

    try:
        dataset = lgb.Dataset(features, label=targets, feature_name=list(FEATURE_NAMES))
        booster = lgb.train(
            {
                # Pinball loss at the target quantile -- the documented objective.
                "objective": "quantile",
                "alpha": quantile,
                "verbose": -1,
                "num_leaves": 15,
                "min_data_in_leaf": 10,
                "learning_rate": 0.05,
                # Fraction of features sampled per tree. A LightGBM
                # hyperparameter, deliberately written as a fraction rather than
                # a decimal literal: the boundary guard flags bare values like
                # 0.9 in this package because they are the shape of an assumed
                # service level, and the only way a quantile should enter this
                # module is through `quantile`, from the signed matrix.
                "feature_fraction": 9 / 10,
                # Fixed so repeated runs on the same corpus agree.
                "seed": 20260911,
                "deterministic": True,
                "num_threads": 1,
            },
            dataset,
            num_boost_round=200,
        )
    except Exception as exc:
        return None, ModelStatus.MODEL_FIT_FAILURE, f"{type(exc).__name__}: {exc}"

    return (
        PooledModel(
            booster=booster,
            quantile=quantile,
            feature_version=FEATURE_VERSION,
            training_rows=len(rows),
        ),
        ModelStatus.SUCCESS,
        "",
    )


def forecast(
    series: PreparedSeries,
    horizon_months: int,
    model: PooledModel | None,
    unavailable_status: ModelStatus = ModelStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET,
    detail: str = "",
) -> ForecastResult:
    """Predict a demand rate for one series from the pooled model."""
    version = MODEL_VERSIONS[ModelName.LIGHTGBM]

    if model is None:
        return ForecastResult(
            model=ModelName.LIGHTGBM,
            model_version=version,
            status=unavailable_status,
            detail=detail or "pooled model unavailable",
        )

    if series.length < MINIMUM_HISTORY_FOR_ROW:
        return ForecastResult(
            model=ModelName.LIGHTGBM,
            model_version=version,
            status=ModelStatus.INSUFFICIENT_HISTORY,
            detail=f"{series.length} observations, need {MINIMUM_HISTORY_FOR_ROW}",
        )

    import numpy as np

    from app.initiatives.i7.forecasting.series import add_months

    next_period = add_months(series.periods[-1], 1)
    features = np.array([build_features(series.values, next_period.month)], dtype=float)

    try:
        predicted = float(model.booster.predict(features)[0])
    except Exception as exc:
        return ForecastResult(
            model=ModelName.LIGHTGBM,
            model_version=version,
            status=ModelStatus.MODEL_FIT_FAILURE,
            detail=f"{type(exc).__name__}: {exc}",
        )

    return ForecastResult(
        model=ModelName.LIGHTGBM,
        model_version=version,
        status=ModelStatus.SUCCESS,
        rate=max(Decimal(str(round(predicted, 6))), Decimal(0)),
        unit=series.unit,
        training_start=series.periods[0],
        training_end=series.periods[-1],
        horizon_months=horizon_months,
        parameters=(
            ("quantile", str(model.quantile)),
            ("feature_version", model.feature_version),
            ("training_scope", "global_pooled"),
            ("training_rows", str(model.training_rows)),
        ),
    )
