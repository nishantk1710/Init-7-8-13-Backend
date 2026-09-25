"""``decide_intermittent`` (the SBA-vs-LightGBM adoption gate) evaluates all
three documented conditions -- rolling-origin evidence, pinball improvement,
bias deterioration -- every time, and reports every one that failed, instead
of stopping at the first.

Root cause: the gate used three sequential early-return checks (origins,
then improvement, then bias). A challenger that failed more than one bar at
once -- the common case on this extract, where short series both starve the
backtest of origins and let a quantile-trained challenger's bias run wild --
only ever had its FIRST failure surfaced. Fixing the origins shortfall alone
(e.g. once more history is staged) would then make the bias failure appear
for the first time, as if it were new, when it was there the whole time.

No adoption rule changed: the three thresholds
(``MINIMUM_PINBALL_IMPROVEMENT``, ``MAXIMUM_BIAS_DETERIORATION``,
``BacktestResult.required_origins``), their boundary comparisons, and the
final ``adoption_status`` precedence (evidence before merit -- an
insufficient-origins challenger is always
``NOT_ELIGIBLE_INSUFFICIENT_ORIGINS``, even if it also fails on bias) are all
unchanged. Only ``decision_reason`` now names every failed bar.
"""

from decimal import Decimal

from app.initiatives.i7.forecasting.selection import decide_intermittent
from app.initiatives.i7.forecasting.types import (
    AdoptionStatus,
    BacktestMetrics,
    BacktestResult,
    BacktestStatus,
    MetricStatus,
    ModelName,
)


def _result(model: ModelName, pinball, bias, origins: int, required: int = 12) -> BacktestResult:
    return BacktestResult(
        model=model,
        model_version="test-1",
        status=BacktestStatus.COMPLETE if origins >= required else BacktestStatus.PARTIAL_DEVELOPMENT_DATA,
        required_origins=required,
        available_origins=origins,
        origins_evaluated=origins,
        metrics=BacktestMetrics(
            pinball_loss=Decimal(str(pinball)) if pinball is not None else None,
            pinball_status=MetricStatus.AVAILABLE if pinball is not None else MetricStatus.NOT_EVALUABLE,
            mean_error=None,
            bias_percentage=Decimal(str(bias)) if bias is not None else None,
            fill_rate=None,
            fill_rate_status=MetricStatus.NOT_EVALUABLE,
            holding_cost=None,
            holding_cost_status=MetricStatus.NOT_EVALUABLE,
            mean_absolute_error=None,
        ),
    )


# --- The actual production case: all three conditions fail together ---------


def test_reports_all_three_failed_conditions_together():
    """Reproduces the 3 audited materials' real backtest shape: only 10 of 12
    origins, no pinball improvement (the challenger is actually worse), and a
    large bias deterioration -- all three fail at once."""
    baseline = _result(ModelName.SBA, pinball=0.2908, bias=1.4815, origins=10)
    challenger = _result(ModelName.LIGHTGBM, pinball=0.4766, bias=6.3552, origins=10)

    decision = decide_intermittent("5000092261/1300", baseline, challenger)

    assert decision.adoption_status is AdoptionStatus.NOT_ELIGIBLE_INSUFFICIENT_ORIGINS
    assert "10 of 12" in decision.decision_reason
    assert "pinball improvement" in decision.decision_reason
    assert "bias worsened" in decision.decision_reason
    # All three named, in the fixed order: origins, improvement, bias.
    origins_pos = decision.decision_reason.index("10 of 12")
    improvement_pos = decision.decision_reason.index("pinball improvement")
    bias_pos = decision.decision_reason.index("bias worsened")
    assert origins_pos < improvement_pos < bias_pos


def test_two_of_three_conditions_reported_together_origins_and_bias_only():
    """Sufficient origins is NOT the failure here -- only improvement and
    bias fail. Both must appear; origins must not."""
    baseline = _result(ModelName.SBA, pinball=1.0, bias=0.10, origins=12)
    challenger = _result(ModelName.LIGHTGBM, pinball=0.98, bias=0.30, origins=12)
    # improvement = (1.0-0.98)/1.0 = 0.02, below the 0.05 bar
    # bias_change = |0.30| - |0.10| = 0.20, above the 0.05 bar

    decision = decide_intermittent("MAT-X/1300", baseline, challenger)

    assert decision.adoption_status is AdoptionStatus.BASELINE_RETAINED
    assert "10 of 12" not in decision.decision_reason
    assert "pinball improvement" in decision.decision_reason
    assert "bias worsened" in decision.decision_reason


# --- Single-failure cases: unchanged behaviour -------------------------------


def test_origins_alone_failing_reports_only_origins():
    """Good metrics, insufficient evidence -- the ONLY named failure is
    origins; improvement/bias, which would have passed, are not mentioned as
    failures."""
    baseline = _result(ModelName.SBA, pinball=1.0, bias=0.05, origins=8)
    challenger = _result(ModelName.LIGHTGBM, pinball=0.5, bias=0.05, origins=8)

    decision = decide_intermittent("MAT-Y/1300", baseline, challenger)

    assert decision.adoption_status is AdoptionStatus.NOT_ELIGIBLE_INSUFFICIENT_ORIGINS
    assert "8 of 12" in decision.decision_reason
    assert "pinball improvement" not in decision.decision_reason
    assert "bias worsened" not in decision.decision_reason


def test_improvement_alone_failing_reports_only_improvement():
    """Sufficient origins, bias fine, but improvement does not clear the bar
    -- the only named failure is improvement."""
    baseline = _result(ModelName.SBA, pinball=1.0, bias=0.10, origins=12)
    challenger = _result(ModelName.LIGHTGBM, pinball=0.98, bias=0.10, origins=12)
    # improvement = 0.02, below 0.05; bias_change = 0, well within bound

    decision = decide_intermittent("MAT-Z/1300", baseline, challenger)

    assert decision.adoption_status is AdoptionStatus.BASELINE_RETAINED
    assert "10 of 12" not in decision.decision_reason
    assert "12 of 12" not in decision.decision_reason
    assert "pinball improvement" in decision.decision_reason
    assert "bias worsened" not in decision.decision_reason


def test_bias_alone_failing_reports_only_bias():
    """Sufficient origins, improvement clears the bar, but bias deteriorates
    too much -- the only named failure is bias."""
    baseline = _result(ModelName.SBA, pinball=1.0, bias=0.10, origins=12)
    challenger = _result(ModelName.LIGHTGBM, pinball=0.80, bias=0.20, origins=12)
    # improvement = 0.20, clears 0.05; bias_change = 0.10, above 0.05

    decision = decide_intermittent("MAT-W/1300", baseline, challenger)

    assert decision.adoption_status is AdoptionStatus.BASELINE_RETAINED
    assert "pinball improvement" not in decision.decision_reason
    assert "bias worsened" in decision.decision_reason


def test_all_three_conditions_pass_yields_challenger_eligible_unchanged():
    """The success path is unaffected: when nothing fails, the reason keeps
    its original positive-report shape, not a failure list."""
    baseline = _result(ModelName.SBA, pinball=1.0, bias=0.10, origins=12)
    challenger = _result(ModelName.LIGHTGBM, pinball=0.80, bias=0.10, origins=12)

    decision = decide_intermittent("MAT-V/1300", baseline, challenger)

    assert decision.adoption_status is AdoptionStatus.CHALLENGER_ELIGIBLE
    assert "pinball improved" in decision.decision_reason
    assert "over 12 origins" in decision.decision_reason


# --- Precedence: adoption_status is unchanged even with multiple failures ---


def test_insufficient_origins_status_wins_even_when_bias_also_fails():
    """The FINAL STATUS still follows evidence-before-merit precedence --
    origins insufficiency is reported as NOT_ELIGIBLE_INSUFFICIENT_ORIGINS,
    never BASELINE_RETAINED, regardless of what else also failed. Only the
    reason text is new; the rule that decides the status is not."""
    baseline = _result(ModelName.SBA, pinball=1.0, bias=0.10, origins=10)
    challenger = _result(ModelName.LIGHTGBM, pinball=0.90, bias=0.50, origins=10)

    decision = decide_intermittent("MAT-U/1300", baseline, challenger)

    assert decision.adoption_status is AdoptionStatus.NOT_ELIGIBLE_INSUFFICIENT_ORIGINS


def test_boundary_values_are_unchanged_exactly_005_still_fails_improvement():
    """Regression pin for the boundary: improvement of EXACTLY the threshold
    is still a failure (strict >, not >=) -- the fix must not have loosened
    or tightened any comparison."""
    baseline = _result(ModelName.SBA, pinball=Decimal("1.00"), bias=0.0, origins=12)
    challenger = _result(ModelName.LIGHTGBM, pinball=Decimal("0.95"), bias=0.0, origins=12)
    # improvement = (1.00 - 0.95) / 1.00 = 0.05 exactly

    decision = decide_intermittent("MAT-T/1300", baseline, challenger)

    assert decision.adoption_status is AdoptionStatus.BASELINE_RETAINED
    assert "pinball improvement" in decision.decision_reason


def test_boundary_values_are_unchanged_exactly_005_bias_change_still_passes():
    """Bias deterioration of EXACTLY the threshold is still within bound
    (<=, not <) -- unchanged from before the fix."""
    baseline = _result(ModelName.SBA, pinball=1.0, bias=0.10, origins=12)
    challenger = _result(ModelName.LIGHTGBM, pinball=0.80, bias=0.15, origins=12)
    # bias_change = |0.15| - |0.10| = 0.05 exactly -- at the bound, not over it

    decision = decide_intermittent("MAT-S/1300", baseline, challenger)

    assert decision.adoption_status is AdoptionStatus.CHALLENGER_ELIGIBLE
    assert "bias worsened" not in decision.decision_reason
