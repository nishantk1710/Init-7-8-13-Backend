"""Demand statistics: ADI, CV-squared, and the dispersion measures.

Pure functions over a canonical :class:`ConsumptionSeries`. No database, no
policy, no thresholds -- given the same series they return the same numbers, so
the documents' worked examples can be asserted directly.

**Two means, deliberately not one.** The Formula Reference uses different
populations for different purposes:

    CV-squared  -> non-zero observations only   (sigma_nz / mu_nz)^2
    sigma_D     -> ALL periods, zeros included

and states it outright: "Include zeros -- they represent real zero-demand
months." Merging them would change both the classification and the safety stock.
:func:`demand_statistics` therefore returns both, separately named.

**Undefined is a status, not a zero.** ADI has no value when a material never
had demand; CV-squared needs two non-zero observations before a standard
deviation exists. Both are ordinary in this data. Returning 0.0 would classify
such a material as SMOOTH -- the class with the lowest safety stock -- on the
strength of having no evidence at all.
"""

from decimal import Decimal
from enum import StrEnum
from statistics import stdev
from typing import NamedTuple

from app.initiatives.i7.contracts import ConsumptionSeries


class StatisticStatus(StrEnum):
    """Whether a statistic could be computed, and if not why."""

    AVAILABLE = "AVAILABLE"

    NO_NON_ZERO_DEMAND = "NO_NON_ZERO_DEMAND"
    """n_nz = 0. ADI would divide by zero; CV-squared has no population."""

    INSUFFICIENT_NON_ZERO_OBSERVATIONS = "INSUFFICIENT_NON_ZERO_OBSERVATIONS"
    """Exactly one non-zero observation. A sample standard deviation needs two
    -- its denominator is ``n_nz - 1``."""

    ZERO_MEAN = "ZERO_MEAN"
    """mu_nz = 0, so CV-squared would divide by zero. Only reachable if a
    "non-zero" observation is itself zero, which the series contract prevents,
    but the guard costs nothing and documents the division."""

    NO_HISTORY = "NO_HISTORY"
    """No periods at all."""


class Statistic(NamedTuple):
    """A value that may legitimately not exist."""

    value: Decimal | None
    status: StatisticStatus

    @property
    def is_available(self) -> bool:
        return self.value is not None and self.status is StatisticStatus.AVAILABLE


class DemandStatistics(NamedTuple):
    """Everything Phase 3 derives from one consumption series."""

    total_periods: int
    """``n`` -- all months, zeros included."""

    non_zero_periods: int
    """``n_nz``."""

    total_demand: Decimal

    mean_all_periods: Decimal | None
    """D_avg across all periods. A Phase 5 input."""

    std_dev_all_periods: Decimal | None
    """sigma_D, zeros included. A Phase 5 input -- no safety stock here."""

    mean_non_zero: Decimal | None
    """mu_nz -- CV-squared's denominator."""

    std_dev_non_zero: Decimal | None
    """sigma_nz -- CV-squared's numerator."""

    adi: Statistic
    cv_squared: Statistic


def _sample_std_dev(values: list[Decimal]) -> Decimal | None:
    """Sample standard deviation (``n - 1`` denominator), or ``None``.

    ``n - 1`` matches the Formula Reference, which writes both sigma_D and
    sigma_nz over ``n - 1``. Fewer than two values has no sample deviation --
    ``None``, not zero, since zero would assert perfectly steady demand.
    """
    if len(values) < 2:
        return None
    return Decimal(str(stdev(float(value) for value in values)))


def average_demand_interval(total_periods: int, non_zero_periods: int) -> Statistic:
    """``ADI = n / n_nz``.

    Undefined when ``n_nz`` is 0: the material has no demand, so the average
    interval *between* demands does not exist.
    """
    if total_periods == 0:
        return Statistic(None, StatisticStatus.NO_HISTORY)
    if non_zero_periods == 0:
        return Statistic(None, StatisticStatus.NO_NON_ZERO_DEMAND)
    return Statistic(
        Decimal(total_periods) / Decimal(non_zero_periods), StatisticStatus.AVAILABLE
    )


def squared_coefficient_of_variation(
    non_zero_values: list[Decimal],
) -> Statistic:
    """``CV^2 = (sigma_nz / mu_nz)^2`` over non-zero observations only."""
    if not non_zero_values:
        return Statistic(None, StatisticStatus.NO_NON_ZERO_DEMAND)
    if len(non_zero_values) < 2:
        return Statistic(None, StatisticStatus.INSUFFICIENT_NON_ZERO_OBSERVATIONS)

    mean = sum(non_zero_values) / Decimal(len(non_zero_values))
    if mean == 0:
        return Statistic(None, StatisticStatus.ZERO_MEAN)

    deviation = _sample_std_dev(non_zero_values)
    if deviation is None:
        return Statistic(None, StatisticStatus.INSUFFICIENT_NON_ZERO_OBSERVATIONS)

    ratio = deviation / mean
    return Statistic(ratio * ratio, StatisticStatus.AVAILABLE)


def demand_statistics(series: ConsumptionSeries) -> DemandStatistics:
    """Every Phase 3 statistic for one series, in one pass."""
    all_values = [observation.quantity for observation in series.observations]
    non_zero_values = [value for value in all_values if value != 0]

    total_periods = len(all_values)
    non_zero_periods = len(non_zero_values)
    total_demand = sum(all_values, Decimal(0))

    mean_all = total_demand / Decimal(total_periods) if total_periods else None
    mean_non_zero = (
        sum(non_zero_values, Decimal(0)) / Decimal(non_zero_periods)
        if non_zero_periods
        else None
    )

    return DemandStatistics(
        total_periods=total_periods,
        non_zero_periods=non_zero_periods,
        total_demand=total_demand,
        mean_all_periods=mean_all,
        # Zeros included, per the Formula Reference.
        std_dev_all_periods=_sample_std_dev(all_values),
        mean_non_zero=mean_non_zero,
        std_dev_non_zero=_sample_std_dev(non_zero_values),
        adi=average_demand_interval(total_periods, non_zero_periods),
        cv_squared=squared_coefficient_of_variation(non_zero_values),
    )
