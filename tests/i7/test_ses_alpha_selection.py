"""SES alpha selection: tuned by rolling-origin backtest, not by a one-step-
ahead in-sample fit over the whole training window.

The Formula Reference is explicit: "The parameter alpha is selected using
rolling-origin backtesting." Before this fix, ``select_alpha`` walked the
*entire* training window once, accumulating each alpha's one-step-ahead
absolute error against the level computed up to that point -- a genuine
one-step-ahead error, but an in-sample one: it never re-fits from a growing
window at independent origins (``series.through(index)``, the same protocol
:mod:`app.initiatives.i7.forecasting.backtest` runs for the champion/
challenger decision), so it is not the rolling-origin backtest the spec
requires.

Unlike SBA, this is not a copy of the SBA fix applied blindly: SES has its
own, much wider 19-value grid (0.05-0.95) and its own, lower minimum-history
gate (``MINIMUM_OBSERVATIONS = 2``, below the backtest's own 3-month minimum
training window), both accounted for below.

These tests build synthetic series (not the three audited materials) so the
fix is proven general. Real materials are covered by
``scripts/audit_ses_alpha.py`` and by ``test_forecasting_models.py``'s
existing SES suite, which this file leaves untouched.
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
from app.initiatives.i7.forecasting import ses
from app.initiatives.i7.forecasting.series import prepare
from app.initiatives.i7.forecasting.types import ModelName, ModelStatus

KEY = MaterialPlantKey(
    material=MaterialIdentity(sap_material_number="000000000099999998"),
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
        ModelName.SES,
        "ses-1",
        lambda s, h, a=alpha: ses.forecast(s, h, alpha=a),
        horizon,
    )
    if not result.paths:
        return None
    return metric_functions.mean_absolute_error(list(result.paths))


# --- Alpha is selected using rolling-origin backtesting -----------------------


def test_select_alpha_picks_the_grid_candidate_with_the_lowest_backtest_mae():
    """A series with a genuine, sustained level shift partway through: a
    larger (faster-adapting) alpha tracks the shift better in a real
    out-of-sample backtest than a small one, giving backtest MAE a real
    reason to disagree with whatever the in-sample method would have picked.
    """
    series = make_series([2, 2, 2, 2, 2, 2, 8, 8, 8, 8, 8, 8, 8, 8])
    chosen = ses.select_alpha(series, horizon_months=1)

    scored = {alpha: _backtest_mae(series, alpha) for alpha in ses.ALPHA_GRID}
    best = min((a for a, s in scored.items() if s is not None), key=lambda a: scored[a])
    assert chosen == best


def test_select_alpha_uses_the_same_rolling_origin_protocol_as_the_champion_backtest():
    """Whatever select_alpha picks must be the genuine minimiser of the same
    backtest.run/mean_absolute_error pipeline selection.py scores every model
    with -- not a second, differently-shaped evaluation invented just for
    SES's alpha."""
    series = make_series([3, 5, 2, 6, 4, 1, 7, 3, 5, 2, 6, 4, 8, 2])
    chosen = ses.select_alpha(series, horizon_months=1)
    chosen_score = _backtest_mae(series, chosen)

    for alpha in ses.ALPHA_GRID:
        other_score = _backtest_mae(series, alpha)
        if other_score is not None:
            assert chosen_score <= other_score


def test_select_alpha_ties_break_toward_the_smaller_alpha():
    """A perfectly flat non-zero series scores identically under every alpha
    (the level equals every observation from the first update on), so the
    tie must resolve to the smallest -- least reactive -- candidate."""
    series = make_series([5] * 14)
    assert ses.select_alpha(series, horizon_months=1) == min(ses.ALPHA_GRID)


# --- Trailing zero padding: SES updates on every period, unlike SBA ----------


def test_trailing_zeros_are_genuine_new_origins_not_noise_for_ses():
    """SES updates its level on EVERY period, zero or not (see the module's
    own docstring: "Zero-demand months are ordinary observations here").
    Unlike SBA (which only updates on non-zero demand events, so trailing
    zeros merely lengthen an inter-arrival interval without adding new
    fittable events), each trailing zero month IS a new, independent backtest
    origin for SES -- "how well does this alpha track a series that has just
    gone quiet?" is real evidence, not noise. So selection is CORRECTLY
    allowed to change once real trailing zeros are added: a low, slow-to-
    adapt alpha that held onto the pre-zero level scores worse against those
    origins' actual-zero outcomes than a high, fast-adapting one. This is the
    opposite invariant from SBA's own trailing-zero test -- checked
    explicitly here, per SES's own update rule, rather than assumed by
    analogy to the SBA fix.
    """
    base = [4, 6, 5, 7, 4, 6]
    padded_values = base + [0] * 7
    padded = make_series(padded_values)

    chosen = ses.select_alpha(padded, horizon_months=1)
    # It must still be the genuine minimiser of the backtest MAE over the
    # padded series' own (now larger) set of origins -- not an arbitrary
    # value merely because it differs from the unpadded case.
    scored = {alpha: _backtest_mae(padded, alpha) for alpha in ses.ALPHA_GRID}
    best = min((a for a, s in scored.items() if s is not None), key=lambda a: scored[a])
    assert chosen == best

    # And a high alpha must score at least as well as a low one on the
    # now-mostly-zero tail -- confirming the padding really did shift the
    # evidence, rather than the test asserting a coincidence.
    assert scored[max(ses.ALPHA_GRID)] <= scored[min(ses.ALPHA_GRID)]


def test_select_alpha_still_scores_every_alpha_over_the_series_own_origins():
    """Whatever the series, whatever it does to the origin count, the chosen
    alpha must be traceable to backtest MAE over exactly that series' own
    origins -- the general correctness property the trailing-zero case above
    is one instance of."""
    for values in (
        [4, 6, 5, 7, 4, 6],
        [4, 6, 5, 7, 4, 6, 0, 0, 0, 0, 0, 0, 0],
        [1, 1, 1, 1, 1, 1, 1],
    ):
        series = make_series(values)
        chosen = ses.select_alpha(series, horizon_months=1)
        chosen_score = _backtest_mae(series, chosen)
        for alpha in ses.ALPHA_GRID:
            other = _backtest_mae(series, alpha)
            if other is not None:
                assert chosen_score <= other


# --- No recursive call to alpha selection -------------------------------------


def test_select_alpha_candidates_never_re_enter_selection():
    """Each candidate's backtest must call forecast() with alpha= pinned --
    never re-triggering select_alpha for the same series. SES's 19-value grid
    makes an accidental re-entry far more expensive than SBA's 4-value one,
    so this guard matters even more here."""
    series = make_series([1, 3, 2, 5, 4, 6, 3, 2, 5])
    calls = {"select_alpha_reentries": 0}
    real_select_alpha = ses.select_alpha

    def counting_select_alpha(*args, **kwargs):
        calls["select_alpha_reentries"] += 1
        return real_select_alpha(*args, **kwargs)

    ses.select_alpha = counting_select_alpha
    try:
        ses.select_alpha(series, horizon_months=1)
    finally:
        ses.select_alpha = real_select_alpha

    assert calls["select_alpha_reentries"] == 1


def test_forecast_without_an_explicit_alpha_still_terminates_quickly_and_matches_selection():
    """The end-to-end path (forecast() with no alpha=) must produce exactly
    what select_alpha() chose, and stay fast despite the 19-candidate grid --
    this is the path every routed material actually takes."""
    import time

    series = make_series([1, 2, 2, 1, 3, 1, 0, 0, 0, 0, 0, 0, 0])
    start = time.monotonic()
    result = ses.forecast(series, 1)
    elapsed = time.monotonic() - start

    assert result.status is ModelStatus.SUCCESS
    assert Decimal(dict(result.parameters)["alpha"]) == ses.select_alpha(series, 1)
    assert elapsed < 1.0


# --- SES forecast formula remains unchanged -----------------------------------


def test_the_ses_recurrence_is_unchanged():
    """This fix only changes how alpha is chosen -- fit_level() and
    forecast()'s l_t = alpha*y_t + (1-alpha)*l_(t-1) recurrence are
    byte-for-byte the same computation as before, pinned again here
    independently of test_forecasting_models.py."""
    values = [Decimal(4), Decimal(6), Decimal(5)]
    level = ses.fit_level(values, Decimal("0.3"))
    # l0 = 4; l1 = 0.3*6 + 0.7*4 = 4.6; l2 = 0.3*5 + 0.7*4.6 = 4.72
    assert level == Decimal("4.72")

    result = ses.forecast(make_series([4, 6, 5]), 1, alpha=Decimal("0.3"))
    assert result.rate == Decimal("4.72")


# --- Fallback for insufficient history ----------------------------------------


def test_select_alpha_falls_back_to_in_sample_fit_when_no_origin_is_scorable():
    """A series at exactly SES's own MINIMUM_OBSERVATIONS (2) -- below the
    backtest's 3-month minimum training window, so zero origins exist for
    every alpha. select_alpha must still return a grid value (via the
    in-sample fallback) rather than raising or returning None."""
    series = make_series([4, 6])
    chosen = ses.select_alpha(series, horizon_months=1)
    assert chosen in ses.ALPHA_GRID

    fallback = min(
        ses.ALPHA_GRID,
        key=lambda a: ses._in_sample_error(series.values, a),
    )
    assert chosen == fallback


def test_forecast_succeeds_at_the_minimum_observation_gate_via_the_fallback():
    """SES's own gate (2 observations) is intentionally below the backtest's
    minimum training window (3) -- forecast() must still produce a real
    SUCCESS result here, using the fallback alpha, not fail."""
    result = ses.forecast(make_series([4, 6]), 1)
    assert result.status is ModelStatus.SUCCESS
    assert result.rate is not None


def test_forecast_still_rejects_a_single_observation():
    """Below MINIMUM_OBSERVATIONS entirely -- the fix must not change this
    failure mode, which is unrelated to alpha selection."""
    result = ses.forecast(make_series([4]), 1)
    assert result.status is ModelStatus.INSUFFICIENT_HISTORY
    assert result.rate is None
