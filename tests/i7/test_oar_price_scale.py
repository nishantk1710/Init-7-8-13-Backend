"""OAR price similarity on a log scale (``oar-similarity-7``).

On the linear scale one repairable at several million stretched the population range until a
100-rupee item and a 10,000-rupee item scored ~0.999 alike, so price separated nothing and every
neighbour of the impact wrench scored 75% on the strength of "also cheap". Price is now compared by
ratio, over the 5th-95th percentile of ln(price).
"""

from decimal import Decimal

from app.initiatives.i7.oar import business_similarity
from app.initiatives.i7.oar.business_similarity import log_price
from app.initiatives.i7.oar.repository import price_range
from app.initiatives.i7.oar.types import ALGORITHM_VERSION, CandidateAttributes


def attrs(price: str | None, material: str = "M") -> CandidateAttributes:
    return CandidateAttributes(
        sap_material_number=material, sap_plant_code="1300", criticality="NORMAL",
        is_active=True, history_months=24, material_group=None, equipment_type=None,
        base_unit_of_measure=None, circuit=None, manufacturer=None, description=None,
        unit_price=Decimal(price) if price is not None else None,
    )


def population(*prices: str) -> list[CandidateAttributes]:
    return [attrs(p, f"M{i}") for i, p in enumerate(prices)]


def test_the_algorithm_version_changed_so_old_results_are_not_reused() -> None:
    assert ALGORITHM_VERSION == "oar-similarity-7"


def test_log_price_is_none_without_a_positive_price() -> None:
    assert log_price(None) is None
    assert log_price(Decimal("0")) is None
    assert log_price(Decimal("-5")) is None
    assert log_price(Decimal("1")) == Decimal("0")


def test_the_range_ignores_a_few_extreme_prices() -> None:
    """Twenty ordinary prices plus two repairables at millions: the band stays ordinary."""
    ordinary = [str(10 * (n + 1)) for n in range(40)]  # 10 .. 400
    extremes = ["3000000", "5000000"]

    with_outliers = price_range(population(*ordinary, *extremes))
    without = price_range(population(*ordinary))

    assert with_outliers is not None and without is not None
    assert with_outliers < without * 2  # nowhere near the ln(5e6/10) ~ 13 a min-max range would give


def test_no_range_without_two_positive_prices() -> None:
    assert price_range(population("100")) is None
    assert price_range(population("100", "0")) is None
    assert price_range([]) is None


def test_ordinary_priced_parts_are_no_longer_almost_identical() -> None:
    """The defect: 100 vs 10,000 scored ~0.999 beside millions. Now a 100x gap is a real distance."""
    spread = price_range(population(*[str(10 * (n + 1)) for n in range(40)], "3000000", "5000000"))

    near = business_similarity.score(attrs("100"), attrs("120"), spread).value
    far = business_similarity.score(attrs("100"), attrs("10000"), spread).value

    assert near is not None and far is not None
    assert near > Decimal("0.9")
    assert far < Decimal("0.5")
    assert near - far > Decimal("0.4")


def test_the_same_ratio_is_the_same_distance_at_any_price_level() -> None:
    spread = price_range(population("10", "100", "1000", "10000", "100000"))

    low = business_similarity.score(attrs("10"), attrs("100"), spread).value
    high = business_similarity.score(attrs("1000"), attrs("10000"), spread).value

    assert abs(low - high) < Decimal("0.0001")


def test_a_part_with_no_price_does_not_compare_on_price() -> None:
    spread = price_range(population("10", "100", "1000"))

    result = business_similarity.score(attrs(None), attrs("100"), spread)

    assert "unit_price" in result.missing
