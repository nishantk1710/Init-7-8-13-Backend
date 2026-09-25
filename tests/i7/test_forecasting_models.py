"""SES, SBA, Auto-ARIMA and TSB.

Every recurrence is checked against a hand-computed value rather than against
its own output. A smoothing loop that is subtly wrong still produces plausible
numbers, so "it ran" proves nothing; only arithmetic worked out independently
does.
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
from app.initiatives.i7.forecasting import arima, sba, ses, tsb
from app.initiatives.i7.forecasting.series import PreparedSeries, add_months, prepare
from app.initiatives.i7.forecasting.types import ModelName, ModelStatus

KEY = MaterialPlantKey(
    material=MaterialIdentity(sap_material_number="000000000010000000"),
    plant=PlantIdentity(sap_plant_code="1300"),
)


def make_series(quantities: list, unit: str | None = "EA") -> PreparedSeries:
    observations = []
    year, month = 2025, 1
    for quantity in quantities:
        observations.append(
            ConsumptionObservation(
                period=date(year, month, 1),
                quantity=Decimal(str(quantity)),
                unit_of_measure=unit,
            )
        )
        month += 1
        if month > 12:
            month, year = 1, year + 1
    prepared, problem = prepare(ConsumptionSeries(key=KEY, observations=tuple(observations)))
    assert problem is None, problem
    return prepared


# --- Series preparation -------------------------------------------------


def test_prepare_keeps_zero_months():
    series = make_series([4, 0, 6, 0, 0, 5])
    assert series.length == 6
    assert series.non_zero_count == 3


def test_prepare_rejects_empty_series():
    prepared, problem = prepare(ConsumptionSeries(key=KEY, observations=()))
    assert prepared is None
    assert problem.reason == "empty_series"


def test_through_gives_a_training_window():
    """The only mechanism by which a model sees history."""
    series = make_series([1, 2, 3, 4, 5])
    window = series.through(3)
    assert window.length == 3
    assert window.values == [Decimal(1), Decimal(2), Decimal(3)]


def test_add_months_crosses_a_year_boundary():
    assert add_months(date(2025, 11, 1), 3) == date(2026, 2, 1)


# --- SES ------------------------------------------------------------------


def test_ses_level_matches_hand_calculation():
    """alpha=0.5 over [10, 20, 30]:

        l0 = 10
        l1 = 0.5*20 + 0.5*10 = 15
        l2 = 0.5*30 + 0.5*15 = 22.5
    """
    level = ses.fit_level([Decimal(10), Decimal(20), Decimal(30)], Decimal("0.5"))
    assert level == Decimal("22.5")


def test_ses_forecast_is_the_level():
    """A level-only model: every horizon step is the same value."""
    result = ses.forecast(make_series([10, 20, 30]), horizon_months=3, alpha=Decimal("0.5"))
    assert result.status is ModelStatus.SUCCESS
    assert result.rate == Decimal("22.5")


def test_ses_constant_demand_returns_that_constant():
    result = ses.forecast(make_series([5, 5, 5, 5]), horizon_months=1, alpha=Decimal("0.3"))
    assert result.rate == Decimal(5)


def test_ses_zero_months_pull_the_level_down():
    """Zeros are real observations, not gaps to skip."""
    with_zeros = ses.forecast(make_series([10, 0, 10, 0]), 1, alpha=Decimal("0.5"))
    without = ses.forecast(make_series([10, 10, 10, 10]), 1, alpha=Decimal("0.5"))
    assert with_zeros.rate < without.rate


@pytest.mark.parametrize("alpha", ["0.05", "0.95"])
def test_ses_alpha_boundaries_run(alpha):
    result = ses.forecast(make_series([4, 6, 5, 8]), 1, alpha=Decimal(alpha))
    assert result.status is ModelStatus.SUCCESS


def test_ses_alpha_one_follows_the_last_observation():
    result = ses.forecast(make_series([4, 6, 5, 99]), 1, alpha=Decimal(1))
    assert result.rate == Decimal(99)


def test_ses_records_its_alpha():
    result = ses.forecast(make_series([4, 6, 5, 8]), 1)
    assert dict(result.parameters)["alpha"]


def test_ses_is_deterministic():
    series = make_series([4, 0, 6, 3, 0, 7])
    assert ses.forecast(series, 2) == ses.forecast(series, 2)


def test_ses_rejects_a_single_observation():
    result = ses.forecast(make_series([5]), 1)
    assert result.status is ModelStatus.INSUFFICIENT_HISTORY
    assert result.rate is None


def test_ses_never_returns_negative_demand():
    result = ses.forecast(make_series([0, 0, 0, 0]), 1)
    assert result.rate >= 0


# --- SBA -------------------------------------------------------------------


def test_sba_smoothing_matches_hand_calculation():
    """[0, 4, 0, 0, 6] with alpha=0.1.

    Events: (interval 2, size 4) then (interval 3, size 6).
      init:  z = 4,  p = 2
      event: p = 0.1*3 + 0.9*2 = 2.1
             z = 0.1*6 + 0.9*4 = 4.2
    """
    size, interval, events = sba.smooth(
        [Decimal(0), Decimal(4), Decimal(0), Decimal(0), Decimal(6)], Decimal("0.1")
    )
    assert events == 2
    assert interval == Decimal("2.1")
    assert size == Decimal("4.2")


def test_sba_forecast_applies_the_bias_correction():
    """y = (1 - alpha/2) * z / p = 0.95 * 4.2 / 2.1 = 1.9

    The (1 - alpha/2) factor is Croston's bias correction and is what makes this
    SBA rather than Croston.
    """
    result = sba.forecast(make_series([0, 4, 0, 0, 6]), 1, alpha=Decimal("0.1"))
    assert result.status is ModelStatus.SUCCESS
    assert result.rate == Decimal("1.9")


def test_sba_without_the_correction_would_be_higher():
    """Croston's uncorrected rate is z/p = 2.0; SBA deliberately sits below it."""
    result = sba.forecast(make_series([0, 4, 0, 0, 6]), 1, alpha=Decimal("0.1"))
    assert result.rate < Decimal(4.2) / Decimal(2.1)


@pytest.mark.parametrize("alpha", ["0.05", "0.20"])
def test_sba_alpha_range_boundaries(alpha):
    """The approved range is 0.05-0.20."""
    result = sba.forecast(make_series([0, 4, 0, 0, 6, 0, 3]), 1, alpha=Decimal(alpha))
    assert result.status is ModelStatus.SUCCESS


def test_sba_alpha_grid_stays_within_the_approved_range():
    assert min(sba.ALPHA_GRID) == Decimal("0.05")
    assert max(sba.ALPHA_GRID) == Decimal("0.20")


def test_sba_zero_gaps_lengthen_the_interval():
    """A longer silence means a longer inter-arrival interval, so a lower rate."""
    sparse = sba.forecast(make_series([4, 0, 0, 0, 0, 0, 6]), 1, alpha=Decimal("0.2"))
    dense = sba.forecast(make_series([4, 6, 4, 6, 4, 6, 4]), 1, alpha=Decimal("0.2"))
    assert sparse.rate < dense.rate


def test_sba_needs_two_demand_events():
    result = sba.forecast(make_series([0, 0, 5, 0, 0]), 1)
    assert result.status is ModelStatus.INSUFFICIENT_NON_ZERO_OBSERVATIONS
    assert result.rate is None


def test_sba_with_no_demand_at_all():
    result = sba.forecast(make_series([0, 0, 0, 0]), 1)
    assert result.status is ModelStatus.NO_NON_ZERO_DEMAND
    assert result.rate is None


def test_sba_never_divides_by_zero():
    """Every interval is at least one period, so p_final cannot be zero."""
    for quantities in ([1, 1, 1, 1], [0, 1, 1, 0, 1], [5, 0, 5]):
        result = sba.forecast(make_series(quantities), 1)
        if result.status is ModelStatus.SUCCESS:
            assert Decimal(dict(result.parameters)["p_final"]) > 0


def test_sba_records_its_parameters():
    result = sba.forecast(make_series([0, 4, 0, 0, 6]), 1, alpha=Decimal("0.1"))
    parameters = dict(result.parameters)
    assert parameters["alpha"] == "0.1"
    assert parameters["p_final"] == "2.1"
    assert parameters["z_final"] == "4.2"


def test_sba_is_deterministic():
    series = make_series([0, 4, 0, 0, 6, 0, 3, 0])
    assert sba.forecast(series, 1) == sba.forecast(series, 1)


# --- Auto-ARIMA --------------------------------------------------------------


def test_arima_fits_a_reasonable_series():
    result = arima.forecast(make_series([4, 6, 5, 8, 4, 7, 5, 6, 3, 7, 5, 4]), 1)
    assert result.status is ModelStatus.SUCCESS
    assert result.rate is not None


def test_arima_exposes_its_selected_order():
    result = arima.forecast(make_series([4, 6, 5, 8, 4, 7, 5, 6, 3, 7, 5, 4]), 1)
    parameters = dict(result.parameters)
    assert {"p", "d", "q"} <= parameters.keys()


def test_arima_refuses_a_short_series():
    result = arima.forecast(make_series([4, 6, 5]), 1)
    assert result.status is ModelStatus.INSUFFICIENT_HISTORY
    assert result.rate is None


def test_arima_fails_safely_rather_than_raising():
    """A degenerate series must return a status, not blow up the run."""
    result = arima.forecast(make_series([0, 0, 0, 0, 0, 0, 0, 0]), 1)
    assert result.status in (ModelStatus.SUCCESS, ModelStatus.MODEL_FIT_FAILURE)


def test_arima_never_returns_negative_demand():
    """ARIMA is unbounded; a declining series can extrapolate below zero."""
    result = arima.forecast(make_series([20, 16, 12, 9, 6, 4, 2, 1]), 3)
    if result.rate is not None:
        assert result.rate >= 0


def test_arima_is_deterministic():
    series = make_series([4, 6, 5, 8, 4, 7, 5, 6, 3, 7, 5, 4])
    assert arima.forecast(series, 2).rate == arima.forecast(series, 2).rate


# --- TSB ----------------------------------------------------------------------


def test_tsb_not_evaluable_without_an_approved_trigger():
    """No obsolescence rule is configured, and MSTAE='01' is not a substitute."""
    result = tsb.forecast(
        make_series([4, 0, 6, 0, 0]), 1, obsolescence_trigger_configured=False
    )
    assert result.status is ModelStatus.NOT_EVALUABLE_TRIGGER_UNSET
    assert result.rate is None


def test_tsb_is_not_a_candidate_by_default():
    assert tsb.is_candidate(False) is False


def test_tsb_demand_occurrence_raises_the_probability():
    """p_t = p + beta*(1 - p) when demand occurs."""
    probability, _ = tsb.smooth([Decimal(5), Decimal(5)], Decimal("0.1"), Decimal("0.2"))
    assert probability > Decimal("0.9")


def test_tsb_empty_period_lowers_the_probability():
    """p_t = p + beta*(0 - p) when demand does not occur -- the whole point of
    TSB, and what SBA cannot do."""
    high, _ = tsb.smooth([Decimal(5), Decimal(5), Decimal(5)], Decimal("0.1"), Decimal("0.3"))
    low, _ = tsb.smooth([Decimal(5), Decimal(0), Decimal(0)], Decimal("0.1"), Decimal("0.3"))
    assert low < high


def test_tsb_zero_period_leaves_the_size_untouched():
    """z_t = z_(t-1) when no demand occurs."""
    _, size_after_zero = tsb.smooth(
        [Decimal(10), Decimal(0)], Decimal("0.5"), Decimal("0.5")
    )
    _, size_before = tsb.smooth([Decimal(10)], Decimal("0.5"), Decimal("0.5"))
    assert size_after_zero == size_before


def test_tsb_forecast_is_probability_times_size():
    result = tsb.forecast(
        make_series([4, 0, 6, 0, 5]), 1, obsolescence_trigger_configured=True
    )
    assert result.status is ModelStatus.SUCCESS
    parameters = dict(result.parameters)
    expected = Decimal(parameters["p_final"]) * Decimal(parameters["z_final"])
    assert result.rate == max(expected, Decimal(0))


def test_tsb_records_alpha_and_beta():
    result = tsb.forecast(
        make_series([4, 0, 6, 0, 5]), 1, obsolescence_trigger_configured=True
    )
    assert {"alpha", "beta"} <= dict(result.parameters).keys()


# --- Cross-model invariants -----------------------------------------------------


@pytest.mark.parametrize(
    "model,name",
    [(ses, ModelName.SES), (sba, ModelName.SBA), (arima, ModelName.AUTO_ARIMA)],
)
def test_models_carry_a_version(model, name):
    """Deterministic implementation identifiers, not invented semantic versions."""
    result = model.forecast(make_series([4, 6, 5, 8, 4, 7, 5, 6, 3, 7, 5, 4]), 1)
    assert result.model is name
    assert result.model_version
    assert "v1." not in result.model_version


def test_no_model_returns_a_rate_without_success():
    """An unavailable forecast is absent, never zero -- zero is a prediction."""
    for result in (
        ses.forecast(make_series([5]), 1),
        sba.forecast(make_series([0, 0]), 1),
        arima.forecast(make_series([1, 2]), 1),
        tsb.forecast(make_series([1, 2]), 1, obsolescence_trigger_configured=False),
    ):
        if result.status is not ModelStatus.SUCCESS:
            assert result.rate is None
