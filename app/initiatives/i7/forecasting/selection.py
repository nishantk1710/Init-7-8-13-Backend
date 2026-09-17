"""Champion/challenger selection.

Two rules are in play and they are not the same.

**Intermittent/Lumpy, SBA vs LightGBM.** The Formula Reference states the bar::

    pinball loss improves by > 5%
    AND bias does not worsen by > 5%
    over >= 12 rolling origins

All three must hold. A challenger that wins on loss while drifting badly on bias
is not adopted, and neither is one whose evidence is thin -- which is every
challenger on the current extract.

**Smooth/Erratic, SES vs Auto-ARIMA.** The documents require backtesting but
state no numeric threshold. So none is invented: the comparison is computed and
reported, and the outcome is
``NOT_ELIGIBLE_INSUFFICIENT_ORIGINS`` or ``BASELINE_RETAINED`` pending a signed
criterion. Reusing LightGBM's 5% here would be fabricating a business rule.

**Decisions are per segment, not per SKU.** A material here has at most 13
observations -- far too few to justify swapping its model on its own evidence.
The Solution Design pools by segment ("lumpy pump seals") and adopts there.
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.initiatives.i7.forecasting.types import (
    AdoptionStatus,
    BacktestResult,
    BacktestStatus,
    ModelName,
    SegmentDecision,
)

MINIMUM_PINBALL_IMPROVEMENT = Decimal("0.05")
"""> 5% improvement in pinball loss (Formula Reference, Step B2)."""

MAXIMUM_BIAS_DETERIORATION = Decimal("0.05")
"""Bias may not worsen by more than 5%."""


def _relative_improvement(baseline: Decimal | None, challenger: Decimal | None) -> Decimal | None:
    """Fractional reduction in loss, positive meaning the challenger is better."""
    if baseline is None or challenger is None or baseline == 0:
        return None
    return (baseline - challenger) / baseline


def _bias_change(baseline: Decimal | None, challenger: Decimal | None) -> Decimal | None:
    """Increase in the *magnitude* of bias.

    Compared on absolute value: a swing from -8% to +9% is a deterioration even
    though the signed number rose. Positive means worse.
    """
    if baseline is None or challenger is None:
        return None
    return abs(challenger) - abs(baseline)


def decide_intermittent(
    segment_key: str,
    baseline: BacktestResult,
    challenger: BacktestResult | None,
    *,
    minimum_improvement: Decimal = MINIMUM_PINBALL_IMPROVEMENT,
    maximum_bias_deterioration: Decimal = MAXIMUM_BIAS_DETERIORATION,
) -> SegmentDecision:
    """Apply the documented SBA-vs-LightGBM adoption rule."""
    now = datetime.now(timezone.utc)

    def build(
        status: AdoptionStatus,
        reason: str,
        improvement: Decimal | None = None,
        bias_change: Decimal | None = None,
    ) -> SegmentDecision:
        return SegmentDecision(
            segment_key=segment_key,
            baseline_model=baseline.model,
            challenger_model=challenger.model if challenger else None,
            baseline_pinball=baseline.metrics.pinball_loss if baseline.metrics else None,
            challenger_pinball=(
                challenger.metrics.pinball_loss
                if challenger and challenger.metrics
                else None
            ),
            improvement=improvement,
            baseline_bias=baseline.metrics.bias_percentage if baseline.metrics else None,
            challenger_bias=(
                challenger.metrics.bias_percentage
                if challenger and challenger.metrics
                else None
            ),
            bias_change=bias_change,
            origins_available=baseline.available_origins,
            origins_evaluated=baseline.origins_evaluated,
            required_origins=baseline.required_origins,
            adoption_status=status,
            decision_reason=reason,
            generated_at=now,
        )

    if challenger is None or challenger.metrics is None:
        detail = challenger.detail if challenger else "challenger not run"
        return build(AdoptionStatus.NOT_EVALUABLE, f"challenger not evaluable: {detail}")

    if baseline.metrics is None:
        return build(AdoptionStatus.NOT_EVALUABLE, "baseline produced no metrics")

    improvement = _relative_improvement(
        baseline.metrics.pinball_loss, challenger.metrics.pinball_loss
    )
    bias_change = _bias_change(
        baseline.metrics.bias_percentage, challenger.metrics.bias_percentage
    )

    if improvement is None:
        return build(
            AdoptionStatus.NOT_EVALUABLE,
            "pinball loss unavailable -- the target quantile requires the signed "
            "service-level matrix",
            improvement,
            bias_change,
        )

    # Evidence is checked before merit. A challenger that looks better over 8
    # origins has not met the standard, and reporting it as adopted-but-for-the-
    # origins would invite exactly the wrong reading.
    if challenger.origins_evaluated < challenger.required_origins:
        return build(
            AdoptionStatus.NOT_ELIGIBLE_INSUFFICIENT_ORIGINS,
            f"{challenger.origins_evaluated} of {challenger.required_origins} "
            "required rolling origins; development data cannot support production "
            "adoption evidence",
            improvement,
            bias_change,
        )

    if improvement <= minimum_improvement:
        return build(
            AdoptionStatus.BASELINE_RETAINED,
            f"pinball improvement {improvement:.4f} does not exceed "
            f"{minimum_improvement}",
            improvement,
            bias_change,
        )

    if bias_change is not None and bias_change > maximum_bias_deterioration:
        return build(
            AdoptionStatus.BASELINE_RETAINED,
            f"bias worsened by {bias_change:.4f}, above {maximum_bias_deterioration}",
            improvement,
            bias_change,
        )

    return build(
        AdoptionStatus.CHALLENGER_ELIGIBLE,
        f"pinball improved {improvement:.4f} with bias change "
        f"{bias_change if bias_change is None else format(bias_change, '.4f')} "
        f"over {challenger.origins_evaluated} origins",
        improvement,
        bias_change,
    )


def decide_smooth(
    segment_key: str, baseline: BacktestResult, challenger: BacktestResult | None
) -> SegmentDecision:
    """Compare SES with Auto-ARIMA and report, without inventing a threshold.

    The documents require the comparison but specify no adoption criterion for
    this pair. The metrics and the relative improvement are produced; the
    verdict stays BASELINE_RETAINED until a signed criterion exists, so nothing
    is adopted on a number this code chose.
    """
    now = datetime.now(timezone.utc)

    improvement = (
        _relative_improvement(
            baseline.metrics.pinball_loss if baseline.metrics else None,
            challenger.metrics.pinball_loss if challenger and challenger.metrics else None,
        )
        if challenger
        else None
    )
    bias_change = (
        _bias_change(
            baseline.metrics.bias_percentage if baseline.metrics else None,
            challenger.metrics.bias_percentage if challenger and challenger.metrics else None,
        )
        if challenger
        else None
    )

    if challenger is None or challenger.metrics is None:
        status = AdoptionStatus.NOT_EVALUABLE
        reason = (
            f"challenger not evaluable: {challenger.detail}"
            if challenger
            else "challenger not run"
        )
    elif challenger.origins_evaluated < challenger.required_origins:
        status = AdoptionStatus.NOT_ELIGIBLE_INSUFFICIENT_ORIGINS
        reason = (
            f"{challenger.origins_evaluated} of {challenger.required_origins} "
            "required rolling origins"
        )
    else:
        status = AdoptionStatus.BASELINE_RETAINED
        reason = (
            "comparison recorded; no signed adoption criterion exists for "
            "SES vs Auto-ARIMA, so the baseline is retained pending business sign-off"
        )

    return SegmentDecision(
        segment_key=segment_key,
        baseline_model=baseline.model,
        challenger_model=challenger.model if challenger else ModelName.AUTO_ARIMA,
        baseline_pinball=baseline.metrics.pinball_loss if baseline.metrics else None,
        challenger_pinball=(
            challenger.metrics.pinball_loss if challenger and challenger.metrics else None
        ),
        improvement=improvement,
        baseline_bias=baseline.metrics.bias_percentage if baseline.metrics else None,
        challenger_bias=(
            challenger.metrics.bias_percentage if challenger and challenger.metrics else None
        ),
        bias_change=bias_change,
        origins_available=baseline.available_origins,
        origins_evaluated=baseline.origins_evaluated,
        required_origins=baseline.required_origins,
        adoption_status=status,
        decision_reason=reason,
        generated_at=now,
    )


def aggregate_paths(results: list[BacktestResult]) -> list:
    """Every origin path across a segment's material-plants, pooled.

    Segment-level adoption needs segment-level metrics: one material's 10
    origins prove nothing, while 250 materials' paths together are evidence
    about the segment -- which is the grain the Solution Design adopts at.
    """
    paths = []
    for result in results:
        if result.status in (
            BacktestStatus.COMPLETE,
            BacktestStatus.PARTIAL_DEVELOPMENT_DATA,
        ):
            paths.extend(result.paths)
    return paths
