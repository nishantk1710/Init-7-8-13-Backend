"""SBA alpha selection: tuned by rolling-origin backtest, not by closeness to
the training window's own mean.

The Solution Design is explicit: "alpha = smoothing parameter (0.05 to 0.20,
tune via grid search on backtest)". Before this fix, ``select_alpha`` picked
whichever alpha's forecast rate sat closest to the mean of the *entire*
training window (including zero months) -- an in-sample fit against a number
the model is never asked to reproduce in production, not a backtest at all.
That method was sensitive to how many trailing zero months a series happened
to have, which is exactly the wrong thing for a parameter meant to trade off
responsiveness against stability.

These tests build synthetic series (not the three audited materials) so the
fix is proven general, not tuned to one case. Real materials are covered by
``scripts/audit_sba_alpha.py`` and by ``test_forecasting_models.py``'s
existing SBA suite, which this file leaves untouched.
"""

from datetime import date
from decimal import Decimal

from app.initiatives.i7.contracts import (
    ConsumptionObservation,
    ConsumptionSeries,
    MaterialIdentity,
    MaterialPlantKey,
    PlantIdentity,
)
from app.initiatives.i7.forecasting import backtest as backtest_engine
from app.initiatives.i7.forecasting import metrics as metric_functions
from app.initiatives.i7.forecasting import sba
from app.initiatives.i7.forecasting.series import prepare
from app.initiatives.i7.forecasting.types import ModelName, ModelStatus

KEY = MaterialPlantKey(
    material=MaterialIdentity(sap_material_number="000000000099999999"),
    plant=PlantIdentity(sap_plant_code="1300"),
)


def make_series(quantities: list, unit: str | None = "EA"):
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


def _backtest_mae(series, alpha, horizon=1) -> Decimal | None:
    """The score ``select_alpha`` is now supposed to be optimising -- computed
    independently here so the tests do not just re-assert the function's own
    internals back at itself."""
    result = backtest_engine.run(
        series,
        ModelName.SBA,
        "sba-1",
        lambda s, h, a=alpha: sba.forecast(s, h, alpha=a),
        horizon,
    )
    if not result.paths:
        return None
    return metric_functions.mean_absolute_error(list(result.paths))


# --- The core correction -----------------------------------------------------


def test_select_alpha_picks_the_grid_candidate_with_the_lowest_backtest_mae():
    """A long, steady series where a larger alpha (faster-adapting) tracks a
    genuine step change in demand better than a small one -- so backtest MAE,
    unlike the old in-sample-vs-mean method, has a real reason to prefer a
    larger alpha here."""
    series = make_series([1, 1, 1, 1, 1, 1, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3])
    chosen = sba.select_alpha(series, horizon_months=1)

    scored = {alpha: _backtest_mae(series, alpha) for alpha in sba.ALPHA_GRID}
    best = min((a for a, s in scored.items() if s is not None), key=lambda a: scored[a])
    assert chosen == best


def test_select_alpha_is_not_fooled_by_trailing_zero_months():
    """Regression for the actual defect: a series with several trailing
    zero-demand months used to pull the training-window mean down, biasing
    the old in-sample method toward whichever alpha's rate sat closest to
    that depressed mean -- not necessarily the alpha that forecasts best.
    Appending more trailing zeros must not change which alpha the backtest
    prefers, because none of those months are demand events SBA updates on.
    """
    base = [1, 2, 2, 1, 3, 1]
    short = make_series(base)
    padded = make_series(base + [0] * 7)

    assert sba.select_alpha(short, horizon_months=1) == sba.select_alpha(
        padded, horizon_months=1
    )


def test_select_alpha_uses_the_same_rolling_origin_protocol_as_the_champion_backtest():
    """Whatever ``select_alpha`` picks must be the genuine minimiser of the
    same backtest.run/mean_absolute_error pipeline the champion/challenger
    decision (selection.py) scores every model with -- not a second,
    differently-shaped evaluation invented just for alpha."""
    series = make_series([2, 0, 3, 0, 0, 4, 0, 2, 0, 5, 0, 0, 3, 6])
    chosen = sba.select_alpha(series, horizon_months=1)
    chosen_score = _backtest_mae(series, chosen)

    for alpha in sba.ALPHA_GRID:
        other_score = _backtest_mae(series, alpha)
        if other_score is not None:
            assert chosen_score <= other_score


def test_select_alpha_ties_break_toward_the_smaller_alpha():
    """A perfectly flat non-zero series scores identically under every alpha
    (the recurrence has converged by the first backtest origin), so the tie
    must resolve to the smallest -- least aggressive -- candidate."""
    series = make_series([4] * 14)
    assert sba.select_alpha(series, horizon_months=1) == min(sba.ALPHA_GRID)


# --- No recursion / explosion -------------------------------------------------


def test_select_alpha_candidates_never_re_enter_selection():
    """Each candidate's backtest must call forecast() with alpha= pinned --
    never re-triggering select_alpha for the same series. If it did, a single
    top-level call would run 4**depth nested backtests instead of 4."""
    series = make_series([1, 2, 2, 1, 3, 1, 0, 0, 0])
    calls = {"select_alpha_reentries": 0}
    real_select_alpha = sba.select_alpha

    def counting_select_alpha(*args, **kwargs):
        calls["select_alpha_reentries"] += 1
        return real_select_alpha(*args, **kwargs)

    sba.select_alpha = counting_select_alpha
    try:
        sba.select_alpha(series, horizon_months=1)
    finally:
        sba.select_alpha = real_select_alpha

    # Exactly the one, top-level call -- nothing inside the backtests it ran
    # called back into select_alpha.
    assert calls["select_alpha_reentries"] == 1


def test_forecast_without_an_explicit_alpha_still_terminates_and_matches_selection():
    """The end-to-end path (forecast() with no alpha=) must produce exactly
    what select_alpha() chose, still in negligible time -- this is the path
    every routed material actually takes."""
    import time

    series = make_series([1, 2, 2, 1, 3, 1, 0, 0, 0, 0, 0, 0, 0])
    start = time.monotonic()
    result = sba.forecast(series, 1)
    elapsed = time.monotonic() - start

    assert result.status is ModelStatus.SUCCESS
    assert Decimal(dict(result.parameters)["alpha"]) == sba.select_alpha(series, 1)
    assert elapsed < 1.0


# --- Fallback when nothing is backtestable ------------------------------------


def test_select_alpha_falls_back_to_in_sample_fit_when_no_origin_is_scorable():
    """A series too short for the 3-month minimum training window to produce
    even one backtest origin: select_alpha must still return a grid value
    (via the in-sample fallback) rather than raising or returning None --
    forecast() then proceeds to its own INSUFFICIENT_* status downstream."""
    series = make_series([0, 4, 6])  # length 3 == minimum_training, so 0 origins
    chosen = sba.select_alpha(series, horizon_months=1)
    assert chosen in sba.ALPHA_GRID

    # And it agrees with the documented in-sample fallback specifically.
    fallback = min(
        sba.ALPHA_GRID,
        key=lambda a: (
            sba._in_sample_error(series.values, a)
            if sba._in_sample_error(series.values, a) is not None
            else Decimal("Infinity")
        ),
    )
    assert chosen == fallback


def test_forecast_still_reports_insufficient_observations_when_alpha_cannot_help():
    """Below MINIMUM_DEMAND_EVENTS, no alpha fixes a fundamentally unfittable
    series -- the fix must not change this failure mode."""
    result = sba.forecast(make_series([0, 0, 5, 0, 0]), 1)
    assert result.status is ModelStatus.INSUFFICIENT_NON_ZERO_OBSERVATIONS
    assert result.rate is None


# --- The formula itself is untouched ------------------------------------------


def test_the_sba_recurrence_and_bias_correction_are_unchanged():
    """This fix only changes how alpha is chosen -- smooth() and the
    (1 - alpha/2) correction in forecast() are byte-for-byte the same
    computation as before, pinned again here independently of
    test_forecasting_models.py."""
    size, interval, events = sba.smooth(
        [Decimal(0), Decimal(4), Decimal(0), Decimal(0), Decimal(6)], Decimal("0.1")
    )
    assert (size, interval, events) == (Decimal("4.2"), Decimal("2.1"), 2)

    result = sba.forecast(make_series([0, 4, 0, 0, 6]), 1, alpha=Decimal("0.1"))
    assert result.rate == Decimal("1.9")
