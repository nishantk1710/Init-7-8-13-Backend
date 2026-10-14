"""The declining-demand rule (v1): has this material-plant's demand been falling?

One implementation, used by the feature build and nowhere else. Forecasting
reads the stored result; it never recomputes the rule. Pure: no database, no
clock, no settings -- the thresholds arrive as a
:class:`~app.initiatives.i7.policy.thresholds.DecliningDemandPolicy`.

THE RULE

Take 12 consecutive complete calendar months and split them into an earlier half
(months 1-6) and a recent half (months 7-12). A material-plant is flagged
``DECLINING_DEMAND`` only when ALL of these hold:

* at least 4 of the 12 months have demand;
* at least 3 of the earlier 6 months have demand (recurring demand before the
  decline);
* ``decline = 1 - recent_rate / early_rate`` is at least 0.50, where a rate is
  the share of months in its half that had demand;
* the latest 3 months all have zero demand.

Checked in that order, so the reason a material was not flagged is the first
condition it failed -- deterministic, and stored with the result.

Worked example ``10,0,8,0,6,0 | 0,5,0,0,0,0``: 4 months with demand, 3 in the
earlier half (rate 3/6), 1 in the recent half (rate 1/6), decline 66.7 %, the
latest 3 months empty -> DECLINING_DEMAND.

WHAT "MISSING" MEANS

Inside the extract's observation window a month with no consumption movement IS
zero demand -- staging stores only months that had movements, and the whole of
I07 (ADI, the history gate, forecasting) densifies the gaps to explicit zeros.
"Missing" means months OUTSIDE that window: history nobody delivered. They are
never invented as zeros: fewer than ``window_months`` complete months is
``INSUFFICIENT_HISTORY``, and a month whose quantity is unknown (``None``) or a
gap in the sequence is ``DATA_NOT_EVALUABLE``.

THE LATEST MONTH IS NOT COMPLETE

Nothing in the extract says whether its last month was fully delivered, and
staging records no data cut-off date. So the latest staged month is treated as
incomplete and the window ends one month earlier
(:func:`evaluate_dense_series`). With a 12-month window that means the extract
must cover at least 13 months. A cut-off date recorded at staging would let a
complete last month count; until then this errs on the side of never using a
month that may be partial.

A flag is a statement about demand, not about the material: it does not mean
obsolete, and it never changes the OAR or obsolete-material handling.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import NamedTuple, Sequence

from app.initiatives.i7.policy.thresholds import DecliningDemandPolicy


class DecliningStatus(StrEnum):
    DECLINING_DEMAND = "DECLINING_DEMAND"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    """Fewer than the window's complete months, or too few months with demand."""

    INSUFFICIENT_EARLY_DEMAND = "INSUFFICIENT_EARLY_DEMAND"
    """Not enough demand in the earlier half to call a later silence a decline."""

    DECLINE_THRESHOLD_NOT_MET = "DECLINE_THRESHOLD_NOT_MET"
    RECENT_DEMAND_PRESENT = "RECENT_DEMAND_PRESENT"
    DATA_NOT_EVALUABLE = "DATA_NOT_EVALUABLE"
    """The months themselves cannot be trusted: a gap, an unknown quantity, or no
    observation window at all."""


class DecliningEvaluation(NamedTuple):
    status: DecliningStatus
    reason: str
    rule_version: str
    window_start: date | None = None
    window_end: date | None = None
    months_evaluated: int = 0
    nonzero_months: int | None = None
    early_nonzero: int | None = None
    recent_nonzero: int | None = None
    early_rate: Decimal | None = None
    recent_rate: Decimal | None = None
    decline_pct: Decimal | None = None
    latest_months_zero: bool | None = None

    @property
    def flagged(self) -> bool:
        return self.status is DecliningStatus.DECLINING_DEMAND


def _next_month(period: date) -> date:
    return date(period.year + 1, 1, 1) if period.month == 12 else date(period.year, period.month + 1, 1)


def evaluate(
    months: Sequence[tuple[date, Decimal | None]], policy: DecliningDemandPolicy
) -> DecliningEvaluation:
    """Apply the rule to complete months, oldest first.

    ``months`` is ``(first-of-month, quantity)`` for months known to be complete.
    A quantity of ``None`` means the amount is unknown, which is not zero.
    """
    version = policy.rule_version

    if not months:
        return DecliningEvaluation(
            DecliningStatus.DATA_NOT_EVALUABLE, "no monthly demand was available", version
        )
    if any(quantity is None for _, quantity in months):
        return DecliningEvaluation(
            DecliningStatus.DATA_NOT_EVALUABLE,
            "a month's demand is unknown, and an unknown month is not counted as zero",
            version,
        )
    for (previous, _), (current, _) in zip(months, months[1:]):
        if current != _next_month(previous):
            return DecliningEvaluation(
                DecliningStatus.DATA_NOT_EVALUABLE,
                f"the months are not consecutive ({previous:%Y-%m} is followed by {current:%Y-%m})",
                version,
            )

    window_months = policy.window_months
    if len(months) < window_months:
        return DecliningEvaluation(
            DecliningStatus.INSUFFICIENT_HISTORY,
            f"{len(months)} complete months of history; {window_months} are needed",
            version,
            months_evaluated=len(months),
        )

    window = list(months[-window_months:])
    half = window_months // 2
    has_demand = [quantity is not None and quantity > 0 for _, quantity in window]
    window_start, window_end = window[0][0], window[-1][0]

    nonzero_total = sum(has_demand)
    early_nonzero = sum(has_demand[:half])
    recent_nonzero = sum(has_demand[half:])
    latest_zero = not any(has_demand[-policy.zero_latest_months :])

    def result(status, reason, **extra):
        return DecliningEvaluation(
            status,
            reason,
            version,
            window_start=window_start,
            window_end=window_end,
            months_evaluated=window_months,
            nonzero_months=nonzero_total,
            early_nonzero=early_nonzero,
            recent_nonzero=recent_nonzero,
            latest_months_zero=latest_zero,
            **extra,
        )

    if nonzero_total < policy.minimum_nonzero_months:
        return result(
            DecliningStatus.INSUFFICIENT_HISTORY,
            f"demand in {nonzero_total} of {window_months} months; at least "
            f"{policy.minimum_nonzero_months} are needed",
        )

    early_rate = Decimal(early_nonzero) / Decimal(half)
    recent_rate = Decimal(recent_nonzero) / Decimal(half)

    if early_nonzero < policy.minimum_early_nonzero_months or early_rate == 0:
        return result(
            DecliningStatus.INSUFFICIENT_EARLY_DEMAND,
            f"demand in {early_nonzero} of the earlier {half} months; at least "
            f"{policy.minimum_early_nonzero_months} are needed",
            early_rate=early_rate,
            recent_rate=recent_rate,
        )

    decline = Decimal(1) - recent_rate / early_rate
    if decline < Decimal(str(policy.minimum_decline)):
        return result(
            DecliningStatus.DECLINE_THRESHOLD_NOT_MET,
            f"months with demand fell by {decline:.0%} ({early_nonzero} of {half} then, "
            f"{recent_nonzero} of {half} now); {policy.minimum_decline:.0%} is needed",
            early_rate=early_rate,
            recent_rate=recent_rate,
            decline_pct=decline,
        )

    if not latest_zero:
        return result(
            DecliningStatus.RECENT_DEMAND_PRESENT,
            f"there was demand in the latest {policy.zero_latest_months} months",
            early_rate=early_rate,
            recent_rate=recent_rate,
            decline_pct=decline,
        )

    return result(
        DecliningStatus.DECLINING_DEMAND,
        f"months with demand fell by {decline:.0%} ({early_nonzero} of {half} then, "
        f"{recent_nonzero} of {half} now) and the latest {policy.zero_latest_months} months are empty",
        early_rate=early_rate,
        recent_rate=recent_rate,
        decline_pct=decline,
    )


def evaluate_dense_series(
    observations: Sequence[object], policy: DecliningDemandPolicy
) -> DecliningEvaluation:
    """Apply the rule to a dense monthly series over the extract's window.

    The latest staged month is dropped first -- it may be partial and nothing
    says otherwise (see the module docstring). ``observations`` are the series'
    ``ConsumptionObservation`` values: anything with ``period`` and ``quantity``.
    """
    complete = [(observation.period, observation.quantity) for observation in observations[:-1]]
    return evaluate(complete, policy)
