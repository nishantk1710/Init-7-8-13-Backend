"""ADI, CV-squared and demand statistics.

Pure functions, so the documents' worked examples can be asserted directly --
they are the best oracle available and they cost nothing to check.

The rest of the file is about the cases where a statistic does not exist. Those
matter more than the happy path: the natural stand-in for a missing ADI or
CV-squared is zero, and zero classifies as SMOOTH, which carries the least
safety stock. A material with no demand history would be assigned the most
confident class in the matrix.
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
from app.initiatives.i7.features import (
    StatisticStatus,
    average_demand_interval,
    demand_statistics,
    squared_coefficient_of_variation,
)

KEY = MaterialPlantKey(
    material=MaterialIdentity(sap_material_number="000000000010000000"),
    plant=PlantIdentity(sap_plant_code="1300"),
)


def series(quantities: list[int | float]) -> ConsumptionSeries:
    """A contiguous monthly series from January 2025."""
    observations = []
    year, month = 2025, 1
    for quantity in quantities:
        observations.append(
            ConsumptionObservation(period=date(year, month, 1), quantity=Decimal(str(quantity)))
        )
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return ConsumptionSeries(key=KEY, observations=tuple(observations))


# --- The documented worked example -------------------------------------


DOC_EXAMPLE = [4, 0, 6, 0, 0, 5, 8, 0, 4, 7, 0, 5, 0, 6, 3, 0, 7, 5, 0, 0, 4, 8, 0, 6]
"""Formula Reference, sections 1.4-1.6: n=24, n_nz=14, ADI=1.71, CV2=0.078."""


def test_documented_example_reproduces_exactly():
    statistics = demand_statistics(series(DOC_EXAMPLE))

    assert statistics.total_periods == 24
    assert statistics.non_zero_periods == 14
    assert round(float(statistics.adi.value), 2) == 1.71
    assert round(float(statistics.mean_non_zero), 2) == 5.57
    # The document rounds sigma_nz to 1.56; the exact sample value is 1.553.
    assert round(float(statistics.std_dev_non_zero), 1) == 1.6
    assert round(float(statistics.cv_squared.value), 3) == 0.078


def test_documented_example_counts_zeros_in_the_total():
    """ADI's numerator is every period, not just the ones with demand."""
    statistics = demand_statistics(series(DOC_EXAMPLE))
    assert statistics.total_periods == len(DOC_EXAMPLE)
    assert statistics.non_zero_periods == sum(1 for q in DOC_EXAMPLE if q > 0)


# --- ADI ---------------------------------------------------------------


def test_adi_is_total_over_non_zero():
    assert average_demand_interval(24, 14).value == Decimal(24) / Decimal(14)


def test_adi_of_one_means_demand_every_period():
    assert average_demand_interval(12, 12).value == Decimal(1)


def test_adi_is_undefined_with_no_demand():
    """Not zero -- the average interval between demands does not exist."""
    result = average_demand_interval(12, 0)
    assert result.value is None
    assert result.status is StatisticStatus.NO_NON_ZERO_DEMAND
    assert result.is_available is False


def test_adi_is_undefined_with_no_history():
    result = average_demand_interval(0, 0)
    assert result.value is None
    assert result.status is StatisticStatus.NO_HISTORY


def test_all_zero_series_yields_no_adi():
    statistics = demand_statistics(series([0, 0, 0, 0, 0, 0]))
    assert statistics.total_periods == 6
    assert statistics.non_zero_periods == 0
    assert statistics.adi.value is None
    assert statistics.adi.status is StatisticStatus.NO_NON_ZERO_DEMAND


# --- CV-squared ---------------------------------------------------------


def test_cv_squared_of_constant_demand_is_zero():
    """A genuine zero: identical quantities really do have no variation."""
    result = squared_coefficient_of_variation([Decimal(5)] * 6)
    assert result.status is StatisticStatus.AVAILABLE
    assert result.value == 0


def test_cv_squared_needs_two_observations():
    """A sample standard deviation divides by n_nz - 1."""
    result = squared_coefficient_of_variation([Decimal(5)])
    assert result.value is None
    assert result.status is StatisticStatus.INSUFFICIENT_NON_ZERO_OBSERVATIONS


def test_cv_squared_with_no_observations():
    result = squared_coefficient_of_variation([])
    assert result.value is None
    assert result.status is StatisticStatus.NO_NON_ZERO_DEMAND


def test_single_non_zero_observation_leaves_cv_squared_undefined():
    """ADI exists here but CV-squared does not, so the pair cannot classify."""
    statistics = demand_statistics(series([0, 0, 7, 0, 0, 0]))
    assert statistics.adi.is_available is True
    assert statistics.cv_squared.value is None
    assert statistics.cv_squared.status is StatisticStatus.INSUFFICIENT_NON_ZERO_OBSERVATIONS


def test_cv_squared_uses_only_non_zero_observations():
    """Zeros belong to sigma_D, not to CV-squared."""
    with_zeros = demand_statistics(series([5, 0, 5, 0, 5, 0]))
    without = squared_coefficient_of_variation([Decimal(5)] * 3)
    assert with_zeros.cv_squared.value == without.value


# --- The two dispersion measures are distinct --------------------------


def test_mean_over_all_periods_differs_from_mean_over_non_zero():
    """D_avg includes zeros; mu_nz does not. Conflating them changes both the
    classification and the safety stock."""
    statistics = demand_statistics(series([6, 0, 6, 0, 6, 0]))
    assert statistics.mean_all_periods == Decimal(3)
    assert statistics.mean_non_zero == Decimal(6)


def test_sigma_d_includes_zeros():
    """The Formula Reference: "Include zeros -- they represent real zero-demand
    months." A series that is constant when zeros are dropped is not constant
    with them."""
    statistics = demand_statistics(series([6, 0, 6, 0, 6, 0]))
    assert statistics.std_dev_non_zero == 0
    assert statistics.std_dev_all_periods > 0


def test_both_dispersion_measures_are_returned_separately():
    statistics = demand_statistics(series(DOC_EXAMPLE))
    assert statistics.std_dev_all_periods is not None
    assert statistics.std_dev_non_zero is not None
    assert statistics.std_dev_all_periods != statistics.std_dev_non_zero


# --- Edge cases ---------------------------------------------------------


def test_empty_series_yields_no_statistics():
    statistics = demand_statistics(ConsumptionSeries(key=KEY, observations=()))
    assert statistics.total_periods == 0
    assert statistics.mean_all_periods is None
    assert statistics.adi.status is StatisticStatus.NO_HISTORY


def test_single_period_has_no_standard_deviation():
    statistics = demand_statistics(series([5]))
    assert statistics.total_periods == 1
    assert statistics.std_dev_all_periods is None


def test_total_demand_sums_every_period():
    assert demand_statistics(series([4, 0, 6, 5])).total_demand == Decimal(15)


def test_decimal_precision_is_preserved():
    """Quantities are Decimal end to end -- these feed money and stock."""
    statistics = demand_statistics(series([Decimal("0.1"), Decimal("0.2")]))
    assert statistics.total_demand == Decimal("0.3")


@pytest.mark.parametrize("quantities", [[1, 1, 1], [0, 1, 0, 1], [100, 200, 300]])
def test_statistics_are_deterministic(quantities):
    assert demand_statistics(series(quantities)) == demand_statistics(series(quantities))
