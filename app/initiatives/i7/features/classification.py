"""History gate, ADI/CV-squared classification, and model routing.

Every threshold comes from the Phase 1 policy. No cutoff is written here -- a
literal 1.32 in this module would make recalibration a code change, which is
exactly what the configuration boundary exists to prevent.

Three decisions, in order:

1. **History gate** -- is there enough evidence to classify at all?
2. **Classification** -- which Syntetos-Boylan quadrant?
3. **Routing** -- which baseline and challenger models follow from that?

Routing names models; it does not run or select them. A challenger becomes
champion only after Phase 4 backtesting, so nothing here claims one has won.
"""

from decimal import Decimal
from enum import StrEnum
from typing import NamedTuple

from app.initiatives.i7.contracts import DemandPattern
from app.initiatives.i7.features.statistics import DemandStatistics
from app.initiatives.i7.policy import ClassificationPolicy, ConfidencePolicy, HistoryGatePolicy


class HistoryStatus(StrEnum):
    """Whether a material-plant has enough history to classify.

    COLD_START is a routing decision, not a failure: the material goes to the
    OAR similarity path instead of being discarded.
    """

    SUFFICIENT = "SUFFICIENT"
    COLD_START = "COLD_START"
    NO_HISTORY = "NO_HISTORY"


class DataSufficiency(StrEnum):
    """How the available history compares with what confidence grading wants.

    Separate from :class:`HistoryStatus`, which asks a different question. The
    gate asks "can this be classified?" (6 months); sufficiency asks "how much
    trust does the result deserve?" (24 months for HIGH). The extract tops out
    near 13 months, so almost everything here is LIMITED -- a fact about the
    data, recorded rather than papered over.
    """

    FULL = "FULL"
    LIMITED = "LIMITED"
    INSUFFICIENT = "INSUFFICIENT"


class BaselineModel(StrEnum):
    """Models a demand class routes to. Named, not implemented, in Phase 3."""

    SES = "SES"
    SBA = "SBA"


class ChallengerModel(StrEnum):
    AUTO_ARIMA = "AUTO_ARIMA"
    LIGHTGBM = "LIGHTGBM"


class HistoryAssessment(NamedTuple):
    status: HistoryStatus
    sufficiency: DataSufficiency
    required_months: int
    reason: str


class RoutingDecision(NamedTuple):
    """Which models will compete for this material-plant."""

    baseline: BaselineModel | None
    challenger: ChallengerModel | None
    reason: str


def assess_history(
    statistics: DemandStatistics,
    gate: HistoryGatePolicy,
    confidence: ConfidencePolicy,
) -> HistoryAssessment:
    """Apply the documented history gate.

    ``non_zero < minimum OR total_months < minimum -> cold start``. Both
    thresholds are policy; neither appears as a literal here.
    """
    if statistics.total_periods == 0:
        return HistoryAssessment(
            HistoryStatus.NO_HISTORY,
            DataSufficiency.INSUFFICIENT,
            confidence.high_minimum_history_months,
            "no consumption history",
        )

    too_few_non_zero = statistics.non_zero_periods < gate.minimum_non_zero_periods
    too_short = statistics.total_periods < gate.minimum_history_months

    if too_few_non_zero or too_short:
        reasons = []
        if too_few_non_zero:
            reasons.append(
                f"{statistics.non_zero_periods} non-zero periods "
                f"(< {gate.minimum_non_zero_periods})"
            )
        if too_short:
            reasons.append(
                f"{statistics.total_periods} months of history "
                f"(< {gate.minimum_history_months})"
            )
        status = HistoryStatus.COLD_START
        reason = "; ".join(reasons)
    else:
        status = HistoryStatus.SUFFICIENT
        reason = (
            f"{statistics.total_periods} months, "
            f"{statistics.non_zero_periods} with demand"
        )

    # Sufficiency is graded against the confidence thresholds, independently of
    # whether the gate passed.
    if statistics.total_periods >= confidence.high_minimum_history_months:
        sufficiency = DataSufficiency.FULL
    elif statistics.total_periods >= confidence.medium_minimum_history_months:
        sufficiency = DataSufficiency.LIMITED
    else:
        sufficiency = DataSufficiency.INSUFFICIENT

    return HistoryAssessment(
        status, sufficiency, confidence.high_minimum_history_months, reason
    )


def classify_demand(
    adi: Decimal | None,
    cv_squared: Decimal | None,
    policy: ClassificationPolicy,
) -> DemandPattern:
    """The Syntetos-Boylan matrix.

    ::

                      CV2 <= cutoff        CV2 > cutoff
        ADI <= cutoff    SMOOTH              ERRATIC
        ADI >  cutoff    INTERMITTENT        LUMPY

    The comparison is ``<=`` on both axes, so a value exactly on a cutoff falls
    in the lower class -- ADI 1.32 is SMOOTH or ERRATIC, 1.320001 is not.

    Either statistic missing yields UNCLASSIFIED. Substituting a value would
    invent a class from absent evidence, and the cheapest substitution (zero)
    happens to produce SMOOTH, the class carrying the least safety stock.
    """
    if adi is None or cv_squared is None:
        return DemandPattern.UNCLASSIFIED

    frequent = adi <= Decimal(str(policy.adi_cutoff))
    stable = cv_squared <= Decimal(str(policy.cv_squared_cutoff))

    if frequent:
        return DemandPattern.SMOOTH if stable else DemandPattern.ERRATIC
    return DemandPattern.INTERMITTENT if stable else DemandPattern.LUMPY


def route_models(pattern: DemandPattern) -> RoutingDecision:
    """Which baseline and challenger a demand class routes to.

    Records the pairing only. Phase 4 runs the backtest that decides which one
    becomes champion -- nothing here asserts a winner.
    """
    match pattern:
        case DemandPattern.SMOOTH | DemandPattern.ERRATIC:
            return RoutingDecision(
                BaselineModel.SES,
                ChallengerModel.AUTO_ARIMA,
                f"{pattern.value}: regular demand suits exponential smoothing",
            )
        case DemandPattern.INTERMITTENT | DemandPattern.LUMPY:
            return RoutingDecision(
                BaselineModel.SBA,
                ChallengerModel.LIGHTGBM,
                f"{pattern.value}: intermittent demand needs a Croston-family method",
            )
        case _:
            # Unclassified reaches the OAR similarity engine, which is Phase 6.
            return RoutingDecision(None, None, "unclassified: no forecasting route")
