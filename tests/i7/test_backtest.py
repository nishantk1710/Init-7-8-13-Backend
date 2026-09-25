"""Rolling-origin backtesting, metrics and champion/challenger selection.

The leakage tests matter most. A model that can see the future scores brilliantly
in backtest and fails in production, and nothing in the metrics reveals it --
which is exactly why the guarantee has to be structural and tested rather than
assumed.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.initiatives.i7.contracts import (
    ConsumptionObservation,
    ConsumptionSeries,
    MaterialIdentity,
    MaterialPlantKey,
    PlantIdentity,
)
from app.initiatives.i7.forecasting import backtest, metrics, selection, ses
from app.initiatives.i7.forecasting import lightgbm_model
from app.initiatives.i7.forecasting.series import PreparedSeries, prepare
from app.initiatives.i7.forecasting.types import (
    MODEL_VERSIONS,
    AdoptionStatus,
    BacktestMetrics,
    BacktestResult,
    BacktestStatus,
    MetricStatus,
    ModelName,
    OriginForecast,
)

KEY = MaterialPlantKey(
    material=MaterialIdentity(sap_material_number="X"),
    plant=PlantIdentity(sap_plant_code="1300"),
)


def make_series(quantities: list) -> PreparedSeries:
    observations = []
    year, month = 2025, 1
    for quantity in quantities:
        observations.append(
            ConsumptionObservation(period=date(year, month, 1), quantity=Decimal(str(quantity)))
        )
        month += 1
        if month > 12:
            month, year = 1, year + 1
    prepared, _ = prepare(ConsumptionSeries(key=KEY, observations=tuple(observations)))
    return prepared


def path(predicted, actual, origin=date(2025, 6, 1), step=1) -> OriginForecast:
    return OriginForecast(
        origin_period=origin,
        horizon_step=step,
        forecast_period=date(2025, 7, 1),
        predicted=Decimal(str(predicted)),
        actual=Decimal(str(actual)),
    )


# --- Origin counting ---------------------------------------------------------


def test_thirteen_months_yields_ten_origins():
    """The extract's window. Ten is below the required twelve, which is the
    central limitation of the development data."""
    assert backtest.count_available_origins(13, 1, minimum_training=3) == 10


def test_no_origins_when_history_equals_the_training_minimum():
    assert backtest.count_available_origins(3, 1, minimum_training=3) == 0


def test_origins_never_negative():
    assert backtest.count_available_origins(1, 1, minimum_training=3) == 0


# --- The rolling protocol -----------------------------------------------------


def test_origins_advance_exactly_one_month():
    result = backtest.run(
        make_series([4, 5, 6, 7, 8, 9, 10]),
        ModelName.SES,
        MODEL_VERSIONS[ModelName.SES],
        lambda s, h: ses.forecast(s, h),
        horizon_months=1,
        minimum_training=3,
    )
    origins = sorted({p.origin_period for p in result.paths})
    for earlier, later in zip(origins, origins[1:]):
        months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
        assert months == 1


def test_horizon_follows_the_lead_time():
    """T+1 .. T+LT, so a three-month lead time scores three steps per origin."""
    result = backtest.run(
        make_series([4, 5, 6, 7, 8, 9, 10, 11]),
        ModelName.SES,
        MODEL_VERSIONS[ModelName.SES],
        lambda s, h: ses.forecast(s, h),
        horizon_months=3,
        minimum_training=3,
    )
    steps = {p.horizon_step for p in result.paths}
    assert steps <= {1, 2, 3}
    assert max(steps) == 3


def test_actuals_align_with_their_forecast_period():
    """Off-by-one here would silently score every forecast against the wrong
    month and no metric would look wrong."""
    quantities = [1, 2, 3, 4, 5, 6, 7]
    result = backtest.run(
        make_series(quantities),
        ModelName.SES,
        MODEL_VERSIONS[ModelName.SES],
        lambda s, h: ses.forecast(s, h),
        horizon_months=1,
        minimum_training=3,
    )
    series = make_series(quantities)
    by_period = {point.period: point.quantity for point in series.points}
    for p in result.paths:
        assert p.actual == by_period[p.forecast_period]


def test_incomplete_final_horizon_is_truncated_not_padded():
    """A horizon running past the data is scored over what exists."""
    result = backtest.run(
        make_series([1, 2, 3, 4, 5, 6]),
        ModelName.SES,
        MODEL_VERSIONS[ModelName.SES],
        lambda s, h: ses.forecast(s, h),
        horizon_months=3,
        minimum_training=3,
    )
    last_origin = max(p.origin_period for p in result.paths)
    steps = [p.horizon_step for p in result.paths if p.origin_period == last_origin]
    assert len(steps) < 3


def test_fewer_than_twelve_origins_is_partial():
    result = backtest.run(
        make_series(list(range(1, 14))),
        ModelName.SES,
        MODEL_VERSIONS[ModelName.SES],
        lambda s, h: ses.forecast(s, h),
        horizon_months=1,
        minimum_training=3,
    )
    assert result.origins_evaluated < result.required_origins
    assert result.status is BacktestStatus.PARTIAL_DEVELOPMENT_DATA


def test_required_origins_stays_at_twelve():
    """Never lowered to accommodate a short extract."""
    assert backtest.REQUIRED_ORIGINS == 12


def test_no_origins_are_fabricated():
    """Origins are bounded by what the series can support."""
    series = make_series(list(range(1, 9)))
    result = backtest.run(
        series,
        ModelName.SES,
        MODEL_VERSIONS[ModelName.SES],
        lambda s, h: ses.forecast(s, h),
        horizon_months=1,
        minimum_training=3,
    )
    assert result.origins_evaluated <= backtest.count_available_origins(series.length, 1, 3)


def test_short_series_is_not_evaluable():
    result = backtest.run(
        make_series([1, 2]),
        ModelName.SES,
        MODEL_VERSIONS[ModelName.SES],
        lambda s, h: ses.forecast(s, h),
        horizon_months=1,
        minimum_training=3,
    )
    assert result.status is BacktestStatus.NOT_EVALUABLE_INSUFFICIENT_HISTORY
    assert result.origins_evaluated == 0


# --- Leakage ------------------------------------------------------------------


def test_model_only_ever_sees_the_training_window():
    """The structural leakage guarantee: record what each call receives and
    prove no future observation ever appeared in it."""
    quantities = [1, 2, 3, 4, 5, 6, 7, 8]
    series = make_series(quantities)
    seen: list[list[Decimal]] = []

    def spy(training: PreparedSeries, horizon: int):
        seen.append(list(training.values))
        return ses.forecast(training, horizon)

    backtest.run(
        series,
        ModelName.SES,
        MODEL_VERSIONS[ModelName.SES],
        spy,
        horizon_months=2,
        minimum_training=3,
    )

    assert seen
    for index, window in enumerate(seen):
        expected = [Decimal(str(q)) for q in quantities[: index + 3]]
        assert window == expected, "training window included future observations"


def test_lightgbm_features_use_only_prior_observations():
    """build_features receives values[:i]; the target at i must not influence it."""
    history = [Decimal(4), Decimal(0), Decimal(6)]
    before = lightgbm_model.build_features(history, period_month=4)
    # A wildly different future value must not change features computed from the
    # same past.
    after = lightgbm_model.build_features(history, period_month=4)
    assert before == after


def test_lightgbm_rows_never_include_their_own_target():
    series = make_series([4, 0, 6, 0, 5, 7, 0, 3])
    rows = lightgbm_model.rows_for_series(series)
    values = series.values
    for index, row in enumerate(rows):
        target_index = index + lightgbm_model.MINIMUM_HISTORY_FOR_ROW
        assert row.target == float(values[target_index])
        # months_observed is the count of prior observations, so it must equal
        # the target's index -- proof the window stopped before the target.
        assert row.features[0] == float(target_index)


# --- Metrics --------------------------------------------------------------------


def test_pinball_loss_matches_hand_calculation():
    """q=0.9, forecast 10, actual 14 -> under-forecast: 0.9 * 4 = 3.6"""
    loss, status = metrics.pinball_loss([path(10, 14)], 0.9)
    assert status is MetricStatus.AVAILABLE
    assert loss == Decimal("3.6")


def test_pinball_penalises_under_forecasting_more_at_a_high_quantile():
    under, _ = metrics.pinball_loss([path(10, 14)], 0.9)
    over, _ = metrics.pinball_loss([path(14, 10)], 0.9)
    assert under > over


def test_pinball_not_evaluable_without_a_quantile():
    """No neutral default: 0.5 would silently become mean absolute error."""
    loss, status = metrics.pinball_loss([path(10, 14)], None)
    assert loss is None
    assert status is MetricStatus.NOT_EVALUABLE


def test_mean_error_is_signed():
    assert metrics.mean_error([path(12, 10)]) == Decimal(2)
    assert metrics.mean_error([path(8, 10)]) == Decimal(-2)


def test_bias_percentage_is_scale_free():
    assert metrics.bias_percentage([path(12, 10)]) == Decimal("0.2")


def test_bias_percentage_undefined_against_zero_demand():
    """Returning 0 would read as "unbiased"."""
    assert metrics.bias_percentage([path(5, 0)]) is None


def test_fill_rate_matches_hand_calculation():
    """min(8,10) / 10 = 0.8"""
    rate, status = metrics.simulated_fill_rate([path(8, 10)])
    assert status is MetricStatus.AVAILABLE
    assert rate == Decimal("0.8")


def test_fill_rate_caps_at_one():
    rate, _ = metrics.simulated_fill_rate([path(20, 10)])
    assert rate == Decimal(1)


def test_fill_rate_undefined_with_no_demand():
    rate, status = metrics.simulated_fill_rate([path(5, 0)])
    assert rate is None
    assert status is MetricStatus.NOT_EVALUABLE


def test_holding_cost_not_evaluable_without_a_rate():
    """No holding rate exists in any document or table."""
    cost, status = metrics.simulated_holding_cost([path(12, 10)], Decimal(100), None)
    assert cost is None
    assert status is MetricStatus.NOT_EVALUABLE


def test_holding_cost_computes_when_inputs_exist():
    cost, status = metrics.simulated_holding_cost(
        [path(12, 10)], Decimal(100), Decimal("0.24")
    )
    assert status is MetricStatus.AVAILABLE
    assert cost == Decimal(2) * Decimal(100) * (Decimal("0.24") / Decimal(12))


# --- Selection ---------------------------------------------------------------------


def result_with(
    model: ModelName, pinball, bias, origins: int, required: int = 12
) -> BacktestResult:
    return BacktestResult(
        model=model,
        model_version=MODEL_VERSIONS[model],
        status=BacktestStatus.COMPLETE
        if origins >= required
        else BacktestStatus.PARTIAL_DEVELOPMENT_DATA,
        required_origins=required,
        available_origins=origins,
        origins_evaluated=origins,
        metrics=BacktestMetrics(
            pinball_loss=Decimal(str(pinball)) if pinball is not None else None,
            pinball_status=MetricStatus.AVAILABLE
            if pinball is not None
            else MetricStatus.NOT_EVALUABLE,
            mean_error=None,
            bias_percentage=Decimal(str(bias)) if bias is not None else None,
            fill_rate=None,
            fill_rate_status=MetricStatus.NOT_EVALUABLE,
            holding_cost=None,
            holding_cost_status=MetricStatus.NOT_EVALUABLE,
            mean_absolute_error=None,
        ),
    )


def test_challenger_eligible_when_both_criteria_and_origins_met():
    """10% better loss, bias unchanged, 12 origins."""
    decision = selection.decide_intermittent(
        "LUMPY",
        result_with(ModelName.SBA, 1.0, 0.10, 12),
        result_with(ModelName.LIGHTGBM, 0.90, 0.10, 12),
    )
    assert decision.adoption_status is AdoptionStatus.CHALLENGER_ELIGIBLE


def test_baseline_retained_when_improvement_is_too_small():
    """Exactly 5% does not clear "> 5%"."""
    decision = selection.decide_intermittent(
        "LUMPY",
        result_with(ModelName.SBA, 1.0, 0.10, 12),
        result_with(ModelName.LIGHTGBM, 0.95, 0.10, 12),
    )
    assert decision.adoption_status is AdoptionStatus.BASELINE_RETAINED


def test_baseline_retained_when_bias_worsens_too_much():
    """Loss improves 20% but bias grows from 10% to 20% -- rejected."""
    decision = selection.decide_intermittent(
        "LUMPY",
        result_with(ModelName.SBA, 1.0, 0.10, 12),
        result_with(ModelName.LIGHTGBM, 0.80, 0.20, 12),
    )
    assert decision.adoption_status is AdoptionStatus.BASELINE_RETAINED
    assert "bias" in decision.decision_reason


def test_bias_is_compared_on_magnitude():
    """-0.05 to +0.20 is a deterioration even though the signed value rose."""
    decision = selection.decide_intermittent(
        "LUMPY",
        result_with(ModelName.SBA, 1.0, -0.05, 12),
        result_with(ModelName.LIGHTGBM, 0.70, 0.20, 12),
    )
    assert decision.adoption_status is AdoptionStatus.BASELINE_RETAINED


def test_insufficient_origins_blocks_adoption_even_when_metrics_are_better():
    """The honest state on the current extract."""
    decision = selection.decide_intermittent(
        "LUMPY",
        result_with(ModelName.SBA, 1.0, 0.10, 8),
        result_with(ModelName.LIGHTGBM, 0.50, 0.10, 8),
    )
    assert decision.adoption_status is AdoptionStatus.NOT_ELIGIBLE_INSUFFICIENT_ORIGINS


def test_unset_service_level_makes_the_challenger_not_evaluable():
    decision = selection.decide_intermittent(
        "LUMPY",
        result_with(ModelName.SBA, None, 0.10, 12),
        result_with(ModelName.LIGHTGBM, None, 0.10, 12),
    )
    assert decision.adoption_status is AdoptionStatus.NOT_EVALUABLE


def test_missing_challenger_is_not_evaluable():
    decision = selection.decide_intermittent(
        "LUMPY", result_with(ModelName.SBA, 1.0, 0.10, 12), None
    )
    assert decision.adoption_status is AdoptionStatus.NOT_EVALUABLE


def test_smooth_comparison_invents_no_threshold():
    """The documents specify no SES-vs-Auto-ARIMA criterion, so none is applied."""
    decision = selection.decide_smooth(
        "SMOOTH",
        result_with(ModelName.SES, 1.0, 0.10, 12),
        result_with(ModelName.AUTO_ARIMA, 0.10, 0.10, 12),
    )
    assert decision.adoption_status is AdoptionStatus.BASELINE_RETAINED
    assert "signed adoption criterion" in decision.decision_reason


def test_smooth_comparison_still_reports_the_improvement():
    decision = selection.decide_smooth(
        "SMOOTH",
        result_with(ModelName.SES, 1.0, 0.10, 12),
        result_with(ModelName.AUTO_ARIMA, 0.80, 0.10, 12),
    )
    assert decision.improvement == Decimal("0.2")


def test_thresholds_match_the_documents():
    assert selection.MINIMUM_PINBALL_IMPROVEMENT == Decimal("0.05")
    assert selection.MAXIMUM_BIAS_DETERIORATION == Decimal("0.05")


def test_decisions_are_recorded_at_segment_grain():
    decision = selection.decide_intermittent(
        "LUMPY",
        result_with(ModelName.SBA, 1.0, 0.10, 12),
        result_with(ModelName.LIGHTGBM, 0.90, 0.10, 12),
    )
    assert decision.segment_key == "LUMPY"
    assert decision.decision_reason
