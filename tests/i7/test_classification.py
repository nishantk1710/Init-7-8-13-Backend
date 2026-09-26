"""History gate, classification matrix and model routing.

The boundary tests are the point. ``<=`` versus ``<`` on a cutoff decides
whether a material is SMOOTH or INTERMITTENT, which decides whether it is
forecast by SES or SBA and how much safety stock it carries. An off-by-one-tick
comparison is invisible in aggregate and wrong for every material sitting on the
line, so both sides of both cutoffs are asserted explicitly.
"""

from decimal import Decimal

import pytest

from app.initiatives.i7.contracts import DemandPattern
from app.initiatives.i7.features import (
    BaselineModel,
    ChallengerModel,
    DataSufficiency,
    HistoryStatus,
    assess_history,
    classify_demand,
    route_models,
)
from app.initiatives.i7.features.statistics import DemandStatistics, Statistic, StatisticStatus
from app.initiatives.i7.policy import ClassificationPolicy, ConfidencePolicy, HistoryGatePolicy

POLICY = ClassificationPolicy()
ADI_CUTOFF = Decimal("1.32")
CV_CUTOFF = Decimal("0.49")
TICK = Decimal("0.000001")


def statistics(total: int, non_zero: int) -> DemandStatistics:
    """Only the fields the history gate reads."""
    return DemandStatistics(
        total_periods=total,
        non_zero_periods=non_zero,
        total_demand=Decimal(0),
        mean_all_periods=None,
        std_dev_all_periods=None,
        mean_non_zero=None,
        std_dev_non_zero=None,
        adi=Statistic(None, StatisticStatus.AVAILABLE),
        cv_squared=Statistic(None, StatisticStatus.AVAILABLE),
    )


# --- Classification boundaries ------------------------------------------


def test_adi_exactly_on_the_cutoff_is_frequent():
    """``ADI <= cutoff``, so 1.32 itself falls in the frequent row."""
    assert classify_demand(ADI_CUTOFF, Decimal("0.1"), POLICY) is DemandPattern.SMOOTH


def test_adi_one_tick_above_the_cutoff_is_sporadic():
    assert (
        classify_demand(ADI_CUTOFF + TICK, Decimal("0.1"), POLICY)
        is DemandPattern.INTERMITTENT
    )


def test_cv_squared_exactly_on_the_cutoff_is_stable():
    assert classify_demand(Decimal("1.0"), CV_CUTOFF, POLICY) is DemandPattern.SMOOTH


def test_cv_squared_one_tick_above_the_cutoff_is_erratic():
    assert classify_demand(Decimal("1.0"), CV_CUTOFF + TICK, POLICY) is DemandPattern.ERRATIC


def test_both_exactly_on_their_cutoffs_is_smooth():
    """The corner of the matrix -- both comparisons must be inclusive."""
    assert classify_demand(ADI_CUTOFF, CV_CUTOFF, POLICY) is DemandPattern.SMOOTH


def test_both_one_tick_above_is_lumpy():
    assert (
        classify_demand(ADI_CUTOFF + TICK, CV_CUTOFF + TICK, POLICY) is DemandPattern.LUMPY
    )


# --- The four quadrants --------------------------------------------------


@pytest.mark.parametrize(
    "adi,cv_squared,expected",
    [
        ("1.05", "0.20", DemandPattern.SMOOTH),
        ("1.10", "0.85", DemandPattern.ERRATIC),
        ("1.71", "0.08", DemandPattern.INTERMITTENT),
        ("2.40", "1.30", DemandPattern.LUMPY),
    ],
)
def test_classification_matrix(adi, cv_squared, expected):
    assert classify_demand(Decimal(adi), Decimal(cv_squared), POLICY) is expected


def test_documented_example_classifies_as_intermittent():
    """Formula Reference: ADI 1.71 > 1.32, CV2 0.078 <= 0.49."""
    assert (
        classify_demand(Decimal("1.71"), Decimal("0.078"), POLICY)
        is DemandPattern.INTERMITTENT
    )


# --- Missing statistics --------------------------------------------------


def test_missing_adi_yields_unclassified():
    """Substituting zero would classify a material with no demand as SMOOTH."""
    assert classify_demand(None, Decimal("0.1"), POLICY) is DemandPattern.UNCLASSIFIED


def test_missing_cv_squared_yields_unclassified():
    assert classify_demand(Decimal("1.5"), None, POLICY) is DemandPattern.UNCLASSIFIED


def test_both_missing_yields_unclassified():
    assert classify_demand(None, None, POLICY) is DemandPattern.UNCLASSIFIED


# --- Cutoffs come from configuration --------------------------------------


def test_cutoffs_are_policy_not_constants():
    """Recalibration must be a config change, not a code change."""
    relaxed = ClassificationPolicy(adi_cutoff=2.0, cv_squared_cutoff=0.49)
    adi = Decimal("1.71")
    assert classify_demand(adi, Decimal("0.1"), POLICY) is DemandPattern.INTERMITTENT
    assert classify_demand(adi, Decimal("0.1"), relaxed) is DemandPattern.SMOOTH


def test_changing_cv_cutoff_moves_the_class():
    strict = ClassificationPolicy(cv_squared_cutoff=0.05)
    cv = Decimal("0.078")
    assert classify_demand(Decimal("1.1"), cv, POLICY) is DemandPattern.SMOOTH
    assert classify_demand(Decimal("1.1"), cv, strict) is DemandPattern.ERRATIC


# --- History gate ---------------------------------------------------------


GATE = HistoryGatePolicy()
CONFIDENCE = ConfidencePolicy()


def test_exactly_five_non_zero_and_six_months_passes():
    """Both thresholds are inclusive minimums: ``< 5`` and ``< 6`` fail."""
    assessment = assess_history(statistics(6, 5), GATE, CONFIDENCE)
    assert assessment.status is HistoryStatus.SUFFICIENT


def test_four_non_zero_periods_is_cold_start():
    assessment = assess_history(statistics(12, 4), GATE, CONFIDENCE)
    assert assessment.status is HistoryStatus.COLD_START
    assert "non-zero" in assessment.reason


def test_five_months_is_cold_start():
    assessment = assess_history(statistics(5, 5), GATE, CONFIDENCE)
    assert assessment.status is HistoryStatus.COLD_START
    assert "months of history" in assessment.reason


def test_no_history_is_its_own_status():
    """Distinct from COLD_START: nothing was ever recorded."""
    assessment = assess_history(statistics(0, 0), GATE, CONFIDENCE)
    assert assessment.status is HistoryStatus.NO_HISTORY


def test_cold_start_is_a_routing_decision_not_an_error():
    """The material goes to the OAR path; nothing raises."""
    assessment = assess_history(statistics(3, 1), GATE, CONFIDENCE)
    assert assessment.status is HistoryStatus.COLD_START
    assert assessment.reason


def test_gate_reason_names_both_failures():
    assessment = assess_history(statistics(4, 2), GATE, CONFIDENCE)
    assert "non-zero" in assessment.reason and "months of history" in assessment.reason


def test_gate_thresholds_are_configurable():
    lenient = HistoryGatePolicy(minimum_non_zero_periods=2, minimum_history_months=3)
    assert assess_history(statistics(4, 2), GATE, CONFIDENCE).status is HistoryStatus.COLD_START
    assert assess_history(statistics(4, 2), lenient, CONFIDENCE).status is HistoryStatus.SUFFICIENT


# --- Data sufficiency -----------------------------------------------------


def test_twelve_months_is_limited_not_full():
    """The extract reaches ~13 months against a HIGH bar of 24. Recorded as a
    limitation rather than resolved by lowering the bar."""
    assessment = assess_history(statistics(13, 10), GATE, CONFIDENCE)
    assert assessment.sufficiency is DataSufficiency.LIMITED
    assert assessment.required_months == 24


def test_twenty_four_months_is_full():
    assert assess_history(statistics(24, 14), GATE, CONFIDENCE).sufficiency is DataSufficiency.FULL


def test_under_twelve_months_is_insufficient():
    assert (
        assess_history(statistics(8, 6), GATE, CONFIDENCE).sufficiency
        is DataSufficiency.INSUFFICIENT
    )


def test_sufficiency_is_independent_of_the_gate():
    """A material can pass the gate and still be LIMITED -- the two answer
    different questions."""
    assessment = assess_history(statistics(13, 10), GATE, CONFIDENCE)
    assert assessment.status is HistoryStatus.SUFFICIENT
    assert assessment.sufficiency is DataSufficiency.LIMITED


# --- Routing --------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,baseline,challenger",
    [
        (DemandPattern.SMOOTH, BaselineModel.SES, ChallengerModel.AUTO_ARIMA),
        (DemandPattern.ERRATIC, BaselineModel.SES, ChallengerModel.AUTO_ARIMA),
        (DemandPattern.INTERMITTENT, BaselineModel.SBA, ChallengerModel.LIGHTGBM),
        (DemandPattern.LUMPY, BaselineModel.SBA, ChallengerModel.LIGHTGBM),
    ],
)
def test_routing_matrix(pattern, baseline, challenger):
    decision = route_models(pattern)
    assert decision.baseline is baseline
    assert decision.challenger is challenger
    assert decision.reason


def test_unclassified_routes_nowhere():
    """Cold-start materials reach the OAR similarity engine, not a forecaster."""
    decision = route_models(DemandPattern.UNCLASSIFIED)
    assert decision.baseline is None
    assert decision.challenger is None


def test_routing_records_no_champion():
    """Champion selection needs Phase 4 backtesting -- nothing here claims one."""
    decision = route_models(DemandPattern.INTERMITTENT)
    assert not hasattr(decision, "champion")
    assert not hasattr(decision, "selected")


def test_tsb_is_not_routed_yet():
    """TSB is a Phase 4+ obsolescence candidate."""
    for pattern in DemandPattern:
        decision = route_models(pattern)
        for model in (decision.baseline, decision.challenger):
            assert model is None or "TSB" not in model.value


# --- Densification window (regression) ------------------------------------


def test_densify_spans_the_given_window_not_the_observed_rows():
    """Regression: leading and trailing zero months belong in ``n``.

    A material whose first movement is late still had no demand in the earlier
    months of the window, and ADI counts those. Densifying only between the
    first and last observed movement drops them from ``n`` while keeping every
    ``n_nz``, which understates ADI and can move a material a whole class.
    """
    from datetime import date
    from decimal import Decimal

    from app.initiatives.i7.contracts import MaterialIdentity, MaterialPlantKey, PlantIdentity
    from app.initiatives.i7.features.builder import _densify

    key = MaterialPlantKey(
        material=MaterialIdentity(sap_material_number="X"),
        plant=PlantIdentity(sap_plant_code="1300"),
    )
    # Movements only in March and May, inside a January-June window.
    rows = [
        (date(2025, 3, 1), Decimal("4"), "EA"),
        (date(2025, 5, 1), Decimal("6"), "EA"),
    ]
    series = _densify(rows, key, (date(2025, 1, 1), date(2025, 6, 1)))

    assert series.total_periods == 6
    assert series.non_zero_periods == 2
    assert [str(o.period) for o in series.observations] == [
        "2025-01-01",
        "2025-02-01",
        "2025-03-01",
        "2025-04-01",
        "2025-05-01",
        "2025-06-01",
    ]
    # ADI over the window, not over the March-May span.
    assert series.total_periods / series.non_zero_periods == 3.0


def test_densify_never_invents_months_outside_the_window():
    """The window is the extract's; beyond it is history nobody recorded."""
    from datetime import date
    from decimal import Decimal

    from app.initiatives.i7.contracts import MaterialIdentity, MaterialPlantKey, PlantIdentity
    from app.initiatives.i7.features.builder import _densify

    key = MaterialPlantKey(
        material=MaterialIdentity(sap_material_number="X"),
        plant=PlantIdentity(sap_plant_code="1300"),
    )
    series = _densify(
        [(date(2025, 3, 1), Decimal("4"), "EA")], key, (date(2025, 3, 1), date(2025, 4, 1))
    )
    assert series.total_periods == 2
    assert series.observations[0].period == date(2025, 3, 1)
    assert series.observations[-1].period == date(2025, 4, 1)
