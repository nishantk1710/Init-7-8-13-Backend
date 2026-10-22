"""OAR estimate: the similarity-weighted median (oar-similarity-10) against the weighted mean it replaces.

The motivating defect: four filter donors with ROP 3, 9, 6 and 3 and a fifth with ROP 61. The weighted mean of
those is about 15 -- more than twice what any of the four suggests. Every expected figure below is worked out
by hand in the test, from the stated weights, not read back from the implementation.
"""

from __future__ import annotations

import itertools
import random
from decimal import Decimal

import pytest

from app.initiatives.i7.oar import estimate
from app.initiatives.i7.oar.estimate import weighted_mean, weighted_median
from app.initiatives.i7.oar.types import EstimateStatus
from app.initiatives.i7.recommendations import builder
from tests.i7.test_oar_similarity import neighbour_with

D = Decimal


# --- the primitive ---------------------------------------------------------------------------------


def test_the_weighted_median_of_nothing_is_nothing() -> None:
    assert weighted_median([]) is None
    assert weighted_median([(D(5), D(0))]) is None  # weightless donors do not vote


def test_a_single_donor_is_its_own_median() -> None:
    assert weighted_median([(D(7), D("0.9"))]) == 7


def test_an_odd_number_of_equal_weights_is_the_middle_value() -> None:
    assert weighted_median([(D(9), D(1)), (D(1), D(1)), (D(5), D(1))]) == 5


def test_an_exact_half_split_is_the_midpoint_of_the_two_values() -> None:
    assert weighted_median([(D(3), D(1)), (D(9), D(1))]) == 6
    assert weighted_median([(D(1), D(1)), (D(2), D(1)), (D(3), D(1)), (D(10), D(1))]) == D("2.5")


def test_a_heavy_donor_carries_the_median() -> None:
    """Weight 5 against 1 and 1: more than half of the total weight is on 10."""
    assert weighted_median([(D(1), D(1)), (D(2), D(1)), (D(10), D(5))]) == 10


def test_the_tie_is_exact_not_a_rounding_accident() -> None:
    """0.1 + 0.2 against 0.3 is not exactly equal in binary floating point; in Decimal it is."""
    assert weighted_median([(D(4), D("0.1")), (D(6), D("0.2")), (D(8), D("0.3"))]) == D(7)


def test_equal_values_split_across_donors_accumulate() -> None:
    assert weighted_median([(D(3), D("0.4")), (D(3), D("0.4")), (D(100), D("0.5"))]) == 3


def test_the_median_does_not_depend_on_input_order() -> None:
    pairs = [(D(3), D("0.9")), (D(9), D("0.8")), (D(6), D("0.75")), (D(3), D("0.7")), (D(61), D("0.65"))]
    answers = {weighted_median(list(order)) for order in itertools.permutations(pairs)}

    assert answers == {D(6)}


def test_the_mean_primitive_is_the_documented_formula() -> None:
    mean, numerator, denominator = weighted_mean([(D(10), D("0.9")), (D(20), D("0.8"))])

    assert (numerator, denominator) == (D(25), D("1.7"))
    assert abs(mean - D("14.70588235")) < D("0.0001")


# --- E. the outlier ---------------------------------------------------------------------------------

# similarity, safety stock, ROP, max stock -- four ordinary filters and one outlier
OUTLIER_DONORS = [
    ("0.90", 2, 3, 20),
    ("0.80", 3, 9, 30),
    ("0.75", 2, 6, 25),
    ("0.70", 2, 3, 20),
    ("0.65", 10, 61, 120),
]


def outlier_neighbours():
    return [neighbour_with(sim, ss, rop, mx) for sim, ss, rop, mx in OUTLIER_DONORS]


def test_one_donor_with_rop_61_does_not_dominate_the_robust_estimate() -> None:
    """Sorted by ROP: 3 (0.90), 3 (0.70), 6 (0.75), 9 (0.80), 61 (0.65). Total weight 3.80, half 1.90;
    accumulated 0.90, 1.60, 2.35 -> the median is 6. The mean is 56.15 / 3.80 = 14.78 -> 15."""
    median = estimate.calculate(outlier_neighbours(), True, strategy="weighted_median")
    mean = estimate.calculate(outlier_neighbours(), True, strategy="weighted_mean")

    assert median.rop == 6
    assert mean.rop == 15  # the defect, kept reproducible


def test_every_parameter_is_estimated_by_its_own_median() -> None:
    """SS sorted 2 (0.90), 2 (0.75), 2 (0.70), 3 (0.80), 10 (0.65): accumulated 0.90, 1.65, 2.35 -> 2.
    Max sorted 20 (0.90), 20 (0.70), 25 (0.75), 30 (0.80), 120 (0.65): 0.90, 1.60, 2.35 -> 25."""
    median = estimate.calculate(outlier_neighbours(), True)

    assert median.status is EstimateStatus.SUCCESS and median.strategy == "weighted_median"
    assert (median.safety_stock, median.rop, median.max_stock) == (2, 6, 25)


def test_the_mean_of_the_same_donors_for_comparison() -> None:
    """SS (1.8 + 2.4 + 1.5 + 1.4 + 6.5) / 3.8 = 3.58 -> 4; Max (18 + 24 + 18.75 + 14 + 78) / 3.8 = 40.2 -> 41."""
    mean = estimate.calculate(outlier_neighbours(), True, strategy="weighted_mean")

    assert (mean.safety_stock, mean.rop, mean.max_stock) == (4, 15, 41)


def test_making_the_outlier_more_extreme_does_not_move_the_median() -> None:
    wild = [neighbour_with(sim, ss, rop, mx) for sim, ss, rop, mx in OUTLIER_DONORS[:-1]]
    wild.append(neighbour_with("0.65", 10_000, 6_100_000, 120_000))

    result = estimate.calculate(wild, True)
    mean = estimate.calculate(wild, True, strategy="weighted_mean")

    assert (result.safety_stock, result.rop, result.max_stock) == (2, 6, 25)
    assert mean.rop > 1_000_000


def test_the_audit_records_both_methods_and_the_difference() -> None:
    result = estimate.calculate(outlier_neighbours(), True)
    by_field = {a.field: a for a in result.audit}

    rop = by_field["rop"]
    assert rop.median_raw == 6 and (rop.median_final, rop.mean_final) == (6, 15)
    assert abs(rop.mean_raw - D("14.7763157895")) < D("0.0001")
    assert rop.chosen == "weighted_median" and rop.material_difference is True
    assert by_field["safety_stock"].material_difference is True  # 2 against 4

    trace = dict(result.trace)
    assert trace["estimator"] == "weighted_median"
    assert trace["rop_mean"] == "14.7763" and trace["rop_median"] == "6"
    assert trace["rop_material_difference"] == "true"
    assert trace["rop_raw"] == "6"  # the raw value of the estimator actually chosen


def test_the_audit_names_the_donors_with_their_weights_and_values() -> None:
    result = estimate.calculate(outlier_neighbours(), True)

    assert len(result.donors) == 5
    outlier = next(d for d in result.donors if d.rop == 61)
    assert (outlier.weight, outlier.safety_stock, outlier.max_stock) == (D("0.65"), 10, 120)


def test_a_difference_below_the_diagnostic_ratio_is_not_flagged() -> None:
    near = [neighbour_with("0.90", 10, 100, 200), neighbour_with("0.80", 10, 100, 200),
            neighbour_with("0.70", 10, 104, 200)]

    result = estimate.calculate(near, True, strategy="weighted_mean")

    assert not any(a.material_difference for a in result.audit)
    assert dict(result.trace)["rop_material_difference"] == "false"


def test_donor_sap_values_are_not_changed_by_estimating() -> None:
    neighbours = outlier_neighbours()
    before = [(n.safety_stock, n.rop, n.max_stock) for n in neighbours]

    estimate.calculate(neighbours, True)

    assert [(n.safety_stock, n.rop, n.max_stock) for n in neighbours] == before


def test_a_high_value_alone_never_removes_a_donor() -> None:
    result = estimate.calculate(outlier_neighbours(), True)

    assert result.inventory_eligible_neighbours == 5
    assert max(d.rop for d in result.donors) == 61


def test_the_estimate_is_the_same_whatever_order_the_donors_arrive_in() -> None:
    reference = estimate.calculate(outlier_neighbours(), True)
    for order in itertools.permutations(outlier_neighbours()):
        again = estimate.calculate(list(order), True)
        assert (again.safety_stock, again.rop, again.max_stock) == (
            reference.safety_stock, reference.rop, reference.max_stock,
        )


# --- rounding stays what it was ---------------------------------------------------------------------


def test_the_median_is_rounded_up_to_a_whole_unit() -> None:
    """Two donors of equal weight, ROP 3 and 6: the median is 4.5, which is rounded UP to 5."""
    result = estimate.calculate(
        [neighbour_with("0.80", 1, 3, 20), neighbour_with("0.80", 2, 6, 20)], True, minimum_neighbours=2
    )

    assert isinstance(result.rop, int) and result.rop == 5
    assert dict(result.trace)["rop_median"] == "4.5"
    assert all(isinstance(v, int) for v in (result.safety_stock, result.rop, result.max_stock))


def test_a_whole_unit_median_is_not_nudged_up() -> None:
    result = estimate.calculate(
        [neighbour_with("0.90", 1, 7, 20), neighbour_with("0.85", 1, 7, 20)], True, minimum_neighbours=2
    )

    assert result.rop == 7


def test_an_unknown_estimator_is_refused() -> None:
    with pytest.raises(ValueError):
        estimate.calculate(outlier_neighbours(), True, strategy="weighted_mode")


# --- F. insufficient neighbours -------------------------------------------------------------------------


def test_no_neighbour_is_not_evaluable_with_its_own_reason() -> None:
    result = estimate.calculate([], True, minimum_neighbours=2)

    assert result.status is EstimateStatus.NOT_EVALUABLE_NO_NEIGHBORS
    assert (result.safety_stock, result.rop, result.max_stock) == (None, None, None)
    assert result.donors == () and result.audit == ()


def test_one_neighbour_gives_no_numerical_estimate_under_either_estimator() -> None:
    for strategy in ("weighted_median", "weighted_mean"):
        result = estimate.calculate(
            [neighbour_with("0.99", 10, 20, 30)], True, minimum_neighbours=2, strategy=strategy
        )

        assert result.status is EstimateStatus.NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS
        assert (result.safety_stock, result.rop, result.max_stock) == (None, None, None)
        assert (result.qualifying_neighbours, result.minimum_neighbours) == (1, 2)
        assert "1 qualifying neighbour(s), fewer than the required minimum of 2" in result.detail
        assert "no partial estimate" in result.detail


def test_two_neighbours_meet_the_policy_minimum() -> None:
    result = estimate.calculate(
        [neighbour_with("0.90", 1, 3, 20), neighbour_with("0.80", 2, 9, 30)], True, minimum_neighbours=2
    )

    assert result.status is EstimateStatus.SUCCESS


# --- G. stock consistency ------------------------------------------------------------------------------


def test_the_max_is_raised_to_rop_plus_safety_stock_under_the_median() -> None:
    """Medians of independent parameters can disagree: ROP 10, SS 5, Max 12 -> Max 15."""
    result = estimate.calculate(
        [neighbour_with(s, 5, 10, 12) for s in ("0.90", "0.80", "0.70")], True
    )

    assert (result.safety_stock, result.rop, result.max_stock) == (5, 10, 15)
    trace = dict(result.trace)
    assert trace["max_stock_floor_applied"] == "true" and trace["max_stock_weighted"] == "12"


@pytest.mark.parametrize("strategy", ["weighted_median", "weighted_mean"])
def test_max_is_never_below_rop_plus_safety_stock_in_any_case(strategy: str) -> None:
    rng = random.Random(11)
    for _ in range(400):
        count = rng.randint(2, 6)
        neighbours = [
            neighbour_with(
                f"0.{rng.randint(60, 99)}", rng.randint(1, 50), rng.randint(1, 80), rng.randint(1, 60)
            )
            for _ in range(count)
        ]
        # equal similarities would collide on the helper's material name; the estimate does not care
        result = estimate.calculate(neighbours, True, strategy=strategy)

        assert result.status is EstimateStatus.SUCCESS
        assert result.max_stock >= result.rop + result.safety_stock


def test_the_floor_never_changes_rop_or_safety_stock() -> None:
    result = estimate.calculate([neighbour_with(s, 5, 10, 12) for s in ("0.90", "0.80")], True)

    assert (result.rop, result.safety_stock) == (10, 5)


# --- the trace survives its stored column --------------------------------------------------------------


def test_the_estimate_trace_fits_the_stored_column_with_the_audit_in_it() -> None:
    from app.initiatives.i7.oar.service import _trace_text

    big = [neighbour_with(sim, ss * 1000, rop * 1000, mx * 1000) for sim, ss, rop, mx in OUTLIER_DONORS]
    stored = _trace_text(estimate.calculate(big, True).trace)

    assert len(stored) <= 1000
    for key in ("estimator=", "rop_mean=", "rop_median=", "max_stock_floor_applied=", "rounding_method="):
        assert key in stored, key


# --- the approver sees it ------------------------------------------------------------------------------


def stored_trace(result) -> str:
    return ";".join(f"{key}={value}" for key, value in result.trace)


def test_the_recommendation_trace_carries_the_estimator_audit() -> None:
    from tests.i7.test_recommendation_detail_completeness import oar_row

    result = estimate.calculate(outlier_neighbours(), True)
    row = oar_row(
        estimate_status="SUCCESS", oar_safety_stock=result.safety_stock, oar_rop=result.rop,
        oar_max_stock=result.max_stock, estimate_trace=stored_trace(result),
    )
    from app.initiatives.i7.policy import PolicyDocument

    trace = dict(builder.build_oar_recommendation(row, PolicyDocument()).calculation_trace)

    assert trace["estimator"] == "weighted_median"
    assert trace["rop_mean"] == "14.7763" and trace["rop_median"] == "6"
    assert trace["rop"] == "6"
    assert trace["rop_material_difference"] == "true"


def test_a_result_stored_before_the_audit_existed_has_no_estimator_entries() -> None:
    assert builder._estimator_entries(None) == []
    assert builder._estimator_entries("max_stock_weighted=2;max_stock_floor_applied=false") == []
