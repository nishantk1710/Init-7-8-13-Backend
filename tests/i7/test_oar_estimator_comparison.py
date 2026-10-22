"""The stored-neighbour estimator comparison (scripts/compare_oar_estimators.py), on in-memory rows.

The script's SQL needs the database and is not exercised here; its arithmetic is, and must agree with the
estimator itself.
"""

from __future__ import annotations

from decimal import Decimal

from app.initiatives.i7.oar import estimate
from scripts.compare_oar_estimators import StoredDonor, compare_target, summarise
from tests.i7.test_oar_estimator import OUTLIER_DONORS, outlier_neighbours

D = Decimal


def stored(rows=OUTLIER_DONORS):
    return [StoredDonor(D(sim), ss, rop, mx) for sim, ss, rop, mx in rows]


def test_the_comparison_finds_the_outlier_and_agrees_with_the_estimator() -> None:
    comparison = compare_target("F", "1300", stored())
    median = estimate.calculate(outlier_neighbours(), True)
    mean = estimate.calculate(outlier_neighbours(), True, strategy="weighted_mean")

    assert comparison.median == {"safety_stock": median.safety_stock, "rop": median.rop, "max_stock": median.max_stock}
    assert comparison.mean == {"safety_stock": mean.safety_stock, "rop": mean.rop, "max_stock": mean.max_stock}
    assert (comparison.mean["rop"], comparison.median["rop"], comparison.rop_delta) == (15, 6, -9)
    assert comparison.material_difference is True


def test_agreeing_estimates_are_not_flagged() -> None:
    comparison = compare_target("F", "1300", stored([("0.90", 5, 10, 20), ("0.80", 5, 10, 20), ("0.70", 5, 10, 20)]))

    assert comparison.rop_delta == 0 and comparison.material_difference is False


def test_the_max_floor_applies_to_both_columns() -> None:
    comparison = compare_target("F", "1300", stored([("0.90", 5, 10, 12), ("0.80", 5, 10, 12)]))

    assert comparison.mean["max_stock"] == comparison.median["max_stock"] == 15  # ROP 10 + safety stock 5


def test_the_summary_counts_what_moved() -> None:
    comparisons = [
        compare_target("A", "1", stored()),
        compare_target("B", "1", stored([("0.90", 5, 10, 20), ("0.80", 5, 10, 20)])),
    ]

    summary = summarise(comparisons)

    assert summary["targets"] == 2 and summary["rop_unchanged"] == 1
    assert summary["rop_lower_with_median"] == 1 and summary["rop_higher_with_median"] == 0
    assert summary["material_difference"] == 1 and summary["largest_rop_reduction"] == -9
