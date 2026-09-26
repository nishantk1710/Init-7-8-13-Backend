"""Demand variability for the safety-stock formula.

Formula Reference, Stage 2B::

    D_avg   = mean(c_1 .. c_n)
    sigma_D = sqrt( sum((c_i - D_avg)^2) / (n - 1) )

**Over ALL periods, zeros included.** The document says so outright: "Include
zeros -- they represent real zero-demand months." This is the measure that
distinguishes Path A's safety stock from Path B's, and using only non-zero
months would understate the variability that safety stock exists to absorb --
the worked examples show sigma_D rising from 1.37 to 3.12 precisely because the
zeros are counted.

Deliberately not the same as CV-squared's sigma_nz, which excludes zeros.
Phase 3 stores both, separately named, for this reason.

**Phase 3 already computes D_avg and sigma_D.** :func:`analyse` recomputes them
from the raw per-period series for callers that only have that series (tests,
worked-example verification). :func:`from_feature` is the one every real
service should call: it reads the same two numbers back off the feature-store
row instead of re-deriving them, so a material's variability is computed
exactly once, in Phase 3, not once per consumer.
"""

from decimal import Decimal
from statistics import stdev

from app.initiatives.i7.inventory.types import CalculationStatus, DemandVariabilityResult

MINIMUM_PERIODS = 2
"""A sample standard deviation divides by ``n - 1``."""


def analyse(values: list[Decimal]) -> DemandVariabilityResult:
    """D_avg and sigma_D across every period supplied."""
    periods = len(values)
    zeros = sum(1 for value in values if value == 0)

    if periods == 0:
        return DemandVariabilityResult(
            status=CalculationStatus.NOT_EVALUABLE_NO_HISTORY,
            detail="no demand periods",
        )

    mean = sum(values, Decimal(0)) / Decimal(periods)

    if periods < MINIMUM_PERIODS:
        # One observation has a mean but no dispersion. Reported as
        # insufficient rather than as sigma_D = 0, which would claim perfectly
        # steady demand from a single data point.
        return DemandVariabilityResult(
            status=CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND,
            n_periods=periods,
            zero_period_count=zeros,
            d_avg=mean,
            detail=f"{periods} period(s); {MINIMUM_PERIODS} needed for a sample "
            "standard deviation",
        )

    sigma = Decimal(str(stdev(float(value) for value in values)))

    return DemandVariabilityResult(
        status=CalculationStatus.SUCCESS,
        n_periods=periods,
        zero_period_count=zeros,
        d_avg=mean,
        sigma_d=sigma,
    )


def from_feature(
    total_periods: int | None,
    non_zero_periods: int | None,
    mean_demand_all_periods: Decimal | None,
    std_dev_demand_all_periods: Decimal | None,
) -> DemandVariabilityResult:
    """The same result as :func:`analyse`, read from the feature store.

    Phase 3 (:mod:`app.initiatives.i7.features.builder`) already computed
    ``mean_demand_all_periods`` and ``std_dev_demand_all_periods`` with this
    exact formula, over this exact population (all densified periods, zeros
    included) -- see :func:`app.initiatives.i7.features.statistics.demand_statistics`.
    A caller with a ``MaterialFeature`` row in hand should read the number, not
    recompute it from the underlying consumption rows a second time.

    Mirrors :func:`analyse`'s status logic from the stored counts rather than
    the raw series, so the two functions agree on every boundary (no history,
    a single period) without either one reading the other's input shape.
    """
    periods = total_periods or 0
    zeros = periods - (non_zero_periods or 0)

    if periods == 0:
        return DemandVariabilityResult(
            status=CalculationStatus.NOT_EVALUABLE_NO_HISTORY,
            detail="no demand periods",
        )

    if periods < MINIMUM_PERIODS or std_dev_demand_all_periods is None:
        return DemandVariabilityResult(
            status=CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND,
            n_periods=periods,
            zero_period_count=zeros,
            d_avg=mean_demand_all_periods,
            detail=f"{periods} period(s); {MINIMUM_PERIODS} needed for a sample "
            "standard deviation",
        )

    return DemandVariabilityResult(
        status=CalculationStatus.SUCCESS,
        n_periods=periods,
        zero_period_count=zeros,
        d_avg=mean_demand_all_periods,
        sigma_d=std_dev_demand_all_periods,
    )
