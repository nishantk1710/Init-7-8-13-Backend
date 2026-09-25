"""Champion/challenger selection.

Two rules are in play and they are not the same.

**Intermittent/Lumpy, SBA vs LightGBM.** The Formula Reference states the bar::

    pinball loss improves by > 5%
    AND bias does not worsen by > 5%
    over >= 12 rolling origins

All three must hold. A challenger that wins on loss while drifting badly on bias
is not adopted, and neither is one whose evidence is thin -- which is every
challenger on the current extract.

**Every applicable condition is checked, not just the first one that fails.**
A challenger can fail more than one bar at once -- insufficient origins AND a
bias blowout is the common case on this extract's short series -- and
``decide_intermittent`` reports all of them in ``decision_reason``, in a fixed
order (origins, improvement, bias). The final ``adoption_status`` still
follows the documented precedence (evidence before merit: an insufficient-
origins challenger is always ``NOT_ELIGIBLE_INSUFFICIENT_ORIGINS``, regardless
of what else it also failed) -- only the reporting stopped short-circuiting.

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

    # All three documented conditions are evaluated independently, every time
    # -- never short-circuited on the first one that fails. A challenger can
    # fail more than one bar at once (thin evidence AND a bias blowout is the
    # common case on this extract), and reporting only whichever check ran
    # first would hide the others: a later fix to the origins shortfall alone
    # would then surface the bias failure as if it were new, when it was
    # there the whole time. The final status and precedence are unchanged --
    # only the reason now names every bar this material actually failed.
    origins_ok = challenger.origins_evaluated >= challenger.required_origins
    improvement_ok = improvement > minimum_improvement
    bias_ok = bias_change is None or bias_change <= maximum_bias_deterioration

    failures: list[str] = []
    if not origins_ok:
        failures.append(
            f"{challenger.origins_evaluated} of {challenger.required_origins} "
            "required rolling origins; development data cannot support production "
            "adoption evidence"
        )
    if not improvement_ok:
        failures.append(
            f"pinball improvement {improvement:.4f} does not exceed "
            f"{minimum_improvement}"
        )
    if not bias_ok:
        failures.append(f"bias worsened by {bias_change:.4f}, above {maximum_bias_deterioration}")

    if failures:
        # Evidence outranks merit for the STATUS (an insufficient-origins
        # challenger is NOT_ELIGIBLE_INSUFFICIENT_ORIGINS even if it also
        # failed on bias, exactly as before) -- but the reason lists every
        # failed bar, in the same fixed order (origins, improvement, bias),
        # regardless of which one decided the status.
        status = (
            AdoptionStatus.NOT_ELIGIBLE_INSUFFICIENT_ORIGINS
            if not origins_ok
            else AdoptionStatus.BASELINE_RETAINED
        )
        return build(status, "; ".join(failures), improvement, bias_change)

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
