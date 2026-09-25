"""LightGBM's (and every other model's) backtest pinball loss is scored
against THIS material-plant's own signed service level, not against
whichever tier happens to sit highest in the matrix.

Root cause: ``_target_quantile(policy)`` returns ``max(levels)`` across the
whole signed matrix -- correct for LightGBM's own *training*, because a
single pooled model can only be trained at one quantile (Solution Design:
"Training: Global pooled model across all intermittent/lumpy materials"), a
genuine architectural constraint, not a bug. But the same value was ALSO
passed as the ``quantile`` argument to ``metric_functions.evaluate()`` for
EVERY model's backtest (SES, Auto-ARIMA, SBA, LightGBM, TSB alike) --
scoring, not training. Pinball loss is well-defined against any quantile
regardless of what a forecast was optimised for, so this silently scored a
NORMAL material's models as if its target were CRITICAL's -- exactly the
"assume 98% for critical [for everyone]" the Solution Design's Rule 1
explicitly forbids (``policy/unresolved.py``'s own docstring quotes it
verbatim: "Do NOT invent percentages. Do NOT assume 98% for critical").

The fix adds ``_scoring_quantile(policy, criticality)``, which resolves this
material's own tier via ``ServiceLevelPolicy.service_level_for`` -- the same
per-criticality lookup ``inventory/service_level.py::resolve`` already uses
for Safety Stock's Z factor -- and never falls back to a neighbouring tier or
to the pooled training value. ``_target_quantile`` itself, LightGBM's
training call, and the SBA/SES modules are all unchanged.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.initiatives.i7.contracts.enums import Criticality
from app.initiatives.i7.errors import PolicyNotConfiguredError
from app.initiatives.i7.forecasting import metrics as metric_functions
from app.initiatives.i7.forecasting.service import (
    _criticality,
    _run_models,
    _scoring_quantile,
    _target_quantile,
)
from app.initiatives.i7.forecasting.types import OriginForecast
from app.initiatives.i7.policy import PolicyDocument, ServiceLevelKey, ServiceLevelPolicy

SIGNED_MULTI_TIER_POLICY = PolicyDocument(
    service_level=ServiceLevelPolicy(
        matrix=(
            (ServiceLevelKey(criticality=Criticality.CRITICAL), 0.98),
            (ServiceLevelKey(criticality=Criticality.IMPACT), 0.95),
            (ServiceLevelKey(criticality=Criticality.INSURANCE), 0.90),
            (ServiceLevelKey(criticality=Criticality.NORMAL), 0.85),
        )
    )
)

UNSIGNED_POLICY = PolicyDocument()


# --- The training quantile is unaffected (unchanged, pooled, correct) --------


def test_target_quantile_stays_the_pooled_maximum():
    """This is the TRAINING quantile -- unavoidably pooled across every
    signed tier, because one model is trained once. Untouched by this fix."""
    assert _target_quantile(SIGNED_MULTI_TIER_POLICY) == 0.98


def test_target_quantile_is_none_when_unsigned():
    assert _target_quantile(UNSIGNED_POLICY) is None


# --- The scoring quantile resolves per material, not to the pooled max ------


def test_scoring_quantile_resolves_normals_own_tier_not_the_pooled_maximum():
    """The exact defect: a NORMAL material must be scored at 0.85, never at
    0.98 just because CRITICAL happens to also be signed in the same matrix.
    """
    quantile = _scoring_quantile(SIGNED_MULTI_TIER_POLICY, Criticality.NORMAL)
    assert quantile == 0.85
    assert quantile != _target_quantile(SIGNED_MULTI_TIER_POLICY)


@pytest.mark.parametrize(
    "tier,expected",
    [
        (Criticality.CRITICAL, 0.98),
        (Criticality.IMPACT, 0.95),
        (Criticality.INSURANCE, 0.90),
        (Criticality.NORMAL, 0.85),
    ],
)
def test_scoring_quantile_resolves_every_signed_tier_to_its_own_level(tier, expected):
    assert _scoring_quantile(SIGNED_MULTI_TIER_POLICY, tier) == expected


def test_scoring_quantile_is_none_when_the_matrix_is_unsigned():
    """No number is invented merely because SOME quantile is needed --
    unresolvable is reported as None, which flows to pinball's own
    NOT_EVALUABLE status, not to a guessed value."""
    assert _scoring_quantile(UNSIGNED_POLICY, Criticality.NORMAL) is None


def test_scoring_quantile_is_none_when_criticality_is_unresolved():
    """The 465-of-470 real-world case: SBA/LightGBM-routed materials whose
    criticality is absent from this extract must not silently borrow the
    pooled training value -- unresolvable stays unresolvable."""
    assert _scoring_quantile(SIGNED_MULTI_TIER_POLICY, None) is None


def test_scoring_quantile_is_none_for_a_signed_matrix_silent_on_this_tier():
    """A matrix signed for CRITICAL only, asked about NORMAL: no neighbouring
    tier's level may stand in -- an unlisted pair is a gap, not a licence to
    fall back to whatever else happens to be configured."""
    critical_only = PolicyDocument(
        service_level=ServiceLevelPolicy(
            matrix=((ServiceLevelKey(criticality=Criticality.CRITICAL), 0.98),)
        )
    )
    assert _scoring_quantile(critical_only, Criticality.NORMAL) is None
    # And CRITICAL itself still resolves correctly on the same policy.
    assert _scoring_quantile(critical_only, Criticality.CRITICAL) == 0.98


def test_scoring_quantile_never_raises_even_though_service_level_for_does():
    """service_level_for() raises PolicyNotConfiguredError on a gap;
    _scoring_quantile must catch it and return None, not propagate -- a
    forecasting run must not crash because one material's tier is unsigned.
    """
    with pytest.raises(PolicyNotConfiguredError):
        SIGNED_MULTI_TIER_POLICY.service_level.service_level_for(Criticality.OBSOLETE)
    assert _scoring_quantile(SIGNED_MULTI_TIER_POLICY, Criticality.OBSOLETE) is None


# --- _criticality: the raw feature-store string, safely converted -----------


def test_criticality_helper_parses_the_raw_tier_string():
    assert _criticality("NORMAL") is Criticality.NORMAL
    assert _criticality(" normal ") is Criticality.NORMAL  # tolerant of case/whitespace


def test_criticality_helper_returns_none_for_absent_or_unrecognised_values():
    assert _criticality(None) is None
    assert _criticality("") is None
    assert _criticality("NOT_A_REAL_TIER") is None


# --- Every model's backtest is scored at the material's own quantile --------


def test_run_models_scores_every_candidates_backtest_at_its_own_scoring_quantile(monkeypatch):
    """_run_models receives scoring_quantile as its own explicit argument, and
    every model pinball-scored here (not just LightGBM) must use it -- the
    root cause was never LightGBM-specific: metric_functions.evaluate() is
    the same call for SES, Auto-ARIMA, SBA, LightGBM and TSB alike.
    """
    calls = []
    real_evaluate = metric_functions.evaluate

    def spy_evaluate(paths, *, quantile, **kwargs):
        calls.append(quantile)
        return real_evaluate(paths, quantile=quantile, **kwargs)

    monkeypatch.setattr(metric_functions, "evaluate", spy_evaluate)

    # A real PreparedSeries via the module's own prepare(), matching every
    # other test in this suite, rather than constructing one by hand.
    from datetime import date

    from app.initiatives.i7.contracts import (
        ConsumptionObservation,
        ConsumptionSeries,
        MaterialIdentity,
        MaterialPlantKey,
        PlantIdentity,
    )
    from app.initiatives.i7.forecasting.series import prepare
    from app.initiatives.i7.forecasting.types import ModelStatus

    key = MaterialPlantKey(
        material=MaterialIdentity(sap_material_number="9999999999"),
        plant=PlantIdentity(sap_plant_code="1300"),
    )
    observations = tuple(
        ConsumptionObservation(
            period=date(2025 + (m - 1) // 12, (m - 1) % 12 + 1, 1),
            quantity=Decimal(q),
            unit_of_measure="EA",
        )
        for m, q in zip(range(1, 14), [1, 2, 2, 1, 3, 1, 0, 0, 0, 0, 0, 0, 0])
    )
    prepared, problem = prepare(ConsumptionSeries(key=key, observations=observations))
    assert problem is None

    candidate = SimpleNamespace(
        material="9999999999",
        plant="1300",
        demand_class="INTERMITTENT",
        history_status="SUFFICIENT",
        baseline_model="SBA",
        lead_time_months=1,
        lead_time_source="PLANNED_DELIVERY_TIME",
        criticality=Criticality.NORMAL,
        series=prepared,
    )

    _run_models(
        candidate,
        None,  # no pooled LightGBM model -- irrelevant to this test
        ModelStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET,
        "",
        False,
        0.85,  # this material's own NORMAL-tier scoring quantile
    )

    assert calls, "metric_functions.evaluate was never called"
    assert all(q == 0.85 for q in calls), (
        f"expected every backtest scored at 0.85 (this material's own tier), got {calls}"
    )


# --- End-to-end: pinball loss and the resulting metrics differ correctly ----


def _synthetic_paths() -> list[OriginForecast]:
    """A handful of forecast/actual pairs where under- and over-forecasting
    genuinely differ, so pinball loss at different quantiles gives different
    numbers -- not a degenerate all-zero series."""
    from datetime import date

    return [
        OriginForecast(
            origin_period=date(2025, m, 1),
            horizon_step=1,
            forecast_period=date(2025, m, 1),
            predicted=Decimal(predicted),
            actual=Decimal(actual),
        )
        for m, predicted, actual in [
            (1, 5, 3), (2, 5, 7), (3, 5, 2), (4, 5, 8), (5, 5, 1),
        ]
    ]


def test_pinball_loss_genuinely_differs_between_the_pooled_and_the_materials_own_quantile():
    """The end-to-end proof the fix matters: the same fixed forecast paths
    score differently depending on which quantile is used -- confirming this
    is not a cosmetic parameter that happens not to affect anything."""
    paths = _synthetic_paths()
    at_pooled_098, _ = metric_functions.pinball_loss(paths, 0.98)
    at_own_085, _ = metric_functions.pinball_loss(paths, 0.85)
    assert at_pooled_098 != at_own_085


def test_evaluate_reports_not_evaluable_rather_than_a_number_when_quantile_is_none():
    """Consistent with pinball_loss's own contract: an unresolved scoring
    quantile must never silently become 0.5 (which would turn pinball loss
    into MAE) or the pooled training value."""
    from app.initiatives.i7.forecasting.types import MetricStatus

    metrics = metric_functions.evaluate(_synthetic_paths(), quantile=None)
    assert metrics.pinball_loss is None
    assert metrics.pinball_status is MetricStatus.NOT_EVALUABLE
