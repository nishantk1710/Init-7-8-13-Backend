"""OAR similarity: eligibility, the three dimensions, scoring, ranking,
confidence and the weighted estimate.

Pure functions throughout, so every assertion is arithmetic against a fixture
rather than a property of the current extract. The embedding provider is
injected, so no test downloads a model.
"""

from decimal import Decimal

import pytest

from app.initiatives.i7.oar import (
    business_similarity,
    confidence,
    eligibility,
    estimate,
    ranking,
    scoring,
    structured_similarity,
    text_similarity,
)
from app.initiatives.i7.oar.types import (
    ESTIMATE_LABEL,
    CandidateAttributes,
    CombinedScore,
    DimensionScore,
    EligibilityRejection,
    EstimateStatus,
    Neighbour,
    OarConfidence,
    SimilarityStatus,
)

HISTORY_MINIMUM = 12


def material(
    number: str = "M1",
    plant: str = "1300",
    criticality: str | None = "CRITICAL",
    is_active: bool | None = True,
    history_months: int | None = 18,
    material_group: str | None = "MG1",
    equipment_type: str | None = None,
    uom: str | None = "EA",
    circuit: str | None = "Milling",
    manufacturer: str | None = "SKF",
    unit_price: Decimal | None = Decimal(1000),
    description: str | None = "SKF 6310-2RS deep groove ball bearing",
) -> CandidateAttributes:
    return CandidateAttributes(
        sap_material_number=number,
        sap_plant_code=plant,
        criticality=criticality,
        is_active=is_active,
        history_months=history_months,
        material_group=material_group,
        equipment_type=equipment_type,
        base_unit_of_measure=uom,
        circuit=circuit,
        manufacturer=manufacturer,
        unit_price=unit_price,
        description=description,
    )


class FakeProvider:
    """Deterministic embeddings, so tests never touch a model or a network."""

    model_name = "fake"
    model_version = "fake-1"

    def __init__(self, vectors: dict[str, list[float]] | None = None, available=True):
        self._vectors = vectors or {}
        self._available = available

    def encode(self, texts):
        if not self._available:
            return None
        return [self._vectors.get(text, [1.0, 0.0, 0.0]) for text in texts]


# --- Hard constraints: criticality -----------------------------------------


def test_same_criticality_is_eligible():
    assert (
        eligibility.reject_reason(material(), material("M2"), HISTORY_MINIMUM) is None
    )


def test_different_criticality_is_rejected():
    reason = eligibility.reject_reason(
        material(criticality="CRITICAL"),
        material("M2", criticality="NORMAL"),
        HISTORY_MINIMUM,
    )
    assert reason is EligibilityRejection.CRITICALITY_MISMATCH


def test_missing_target_criticality_is_rejected():
    """Never read as "unknown, therefore probably the same"."""
    reason = eligibility.reject_reason(
        material(criticality=None), material("M2"), HISTORY_MINIMUM
    )
    assert reason is EligibilityRejection.CRITICALITY_MISSING_TARGET


def test_missing_candidate_criticality_is_rejected():
    reason = eligibility.reject_reason(
        material(), material("M2", criticality=None), HISTORY_MINIMUM
    )
    assert reason is EligibilityRejection.CRITICALITY_MISSING_CANDIDATE


def test_both_missing_criticality_is_rejected():
    """Two unknowns are not a match. With 1.7% coverage the permissive reading
    would pair almost everything with almost everything."""
    reason = eligibility.reject_reason(
        material(criticality=None), material("M2", criticality=None), HISTORY_MINIMUM
    )
    assert reason is EligibilityRejection.CRITICALITY_MISSING_TARGET


# --- Hard constraints: active status ------------------------------------------


def test_inactive_candidate_is_rejected():
    reason = eligibility.reject_reason(
        material(), material("M2", is_active=False), HISTORY_MINIMUM
    )
    assert reason is EligibilityRejection.INACTIVE


def test_unknown_active_status_is_rejected():
    """Unknown is not assumed active -- an obsolete donor would propagate its
    parameters into a live material."""
    reason = eligibility.reject_reason(
        material(), material("M2", is_active=None), HISTORY_MINIMUM
    )
    assert reason is EligibilityRejection.ACTIVE_STATUS_UNKNOWN


# --- Hard constraints: history --------------------------------------------------


def test_twelve_months_is_eligible():
    assert (
        eligibility.reject_reason(
            material(), material("M2", history_months=12), HISTORY_MINIMUM
        )
        is None
    )


def test_eleven_months_is_rejected():
    reason = eligibility.reject_reason(
        material(), material("M2", history_months=11), HISTORY_MINIMUM
    )
    assert reason is EligibilityRejection.INSUFFICIENT_HISTORY


def test_unknown_history_is_rejected():
    reason = eligibility.reject_reason(
        material(), material("M2", history_months=None), HISTORY_MINIMUM
    )
    assert reason is EligibilityRejection.HISTORY_UNKNOWN


def test_a_material_is_not_its_own_neighbour():
    assert (
        eligibility.reject_reason(material(), material(), HISTORY_MINIMUM)
        is EligibilityRejection.SELF
    )


def test_failing_any_one_constraint_is_enough():
    for candidate in (
        material("M2", criticality="NORMAL"),
        material("M2", is_active=False),
        material("M2", history_months=5),
    ):
        assert eligibility.reject_reason(material(), candidate, HISTORY_MINIMUM) is not None


def test_filter_counts_rejection_reasons():
    _, rejections = eligibility.filter_candidates(
        material(),
        [
            material("M2"),
            material("M3", criticality="NORMAL"),
            material("M4", is_active=False),
            material("M5", history_months=3),
        ],
        HISTORY_MINIMUM,
    )
    assert rejections[EligibilityRejection.CRITICALITY_MISMATCH.value] == 1
    assert rejections[EligibilityRejection.INACTIVE.value] == 1
    assert rejections[EligibilityRejection.INSUFFICIENT_HISTORY.value] == 1


# --- Structured similarity -------------------------------------------------------


def test_identical_structured_attributes_score_one():
    result = structured_similarity.score(material(), material("M2"))
    assert result.value == Decimal(1)
    assert result.status is SimilarityStatus.AVAILABLE


def test_completely_different_structured_attributes_score_zero():
    result = structured_similarity.score(
        material(material_group="A", uom="EA"),
        material("M2", material_group="B", uom="KG"),
    )
    assert result.value == Decimal(0)


def test_partial_structured_match():
    """One of two known features matches: 1 - mean(0, 1) = 0.5."""
    result = structured_similarity.score(
        material(material_group="A", uom="EA"),
        material("M2", material_group="A", uom="KG"),
    )
    assert result.value == Decimal("0.5")


def test_missing_structured_features_are_excluded_not_matched():
    """Two unknowns are not evidence of resemblance."""
    result = structured_similarity.score(
        material(material_group="A", equipment_type=None),
        material("M2", material_group="A", equipment_type=None),
    )
    assert result.value == Decimal(1)
    assert result.available_features == 2  # material group and UoM
    assert result.missing_features == 1  # equipment type


def test_no_shared_structured_features_is_unavailable():
    result = structured_similarity.score(
        material(material_group=None, uom=None, equipment_type=None),
        material("M2", material_group=None, uom=None, equipment_type=None),
    )
    assert result.value is None
    assert result.status is SimilarityStatus.NOT_AVAILABLE_NO_FEATURES


def test_numeric_distance_is_normalised_by_the_population_range():
    distance = structured_similarity.numeric_distance(
        Decimal(100), Decimal(200), Decimal(1000)
    )
    assert distance == Decimal("0.1")


def test_zero_range_offers_no_discrimination():
    """Never a division by zero."""
    assert (
        structured_similarity.numeric_distance(Decimal(100), Decimal(100), Decimal(0))
        is None
    )


def test_numeric_distance_clamps_to_one():
    distance = structured_similarity.numeric_distance(
        Decimal(0), Decimal(5000), Decimal(1000)
    )
    assert distance == Decimal(1)


# --- Text similarity ----------------------------------------------------------------


def test_identical_descriptions_score_one():
    provider = FakeProvider({"bearing": [1.0, 2.0, 3.0]})
    result = text_similarity.score("bearing", "bearing", provider)
    assert result.value == Decimal(1)


def test_orthogonal_descriptions_score_zero():
    provider = FakeProvider({"a": [1.0, 0.0], "b": [0.0, 1.0]})
    result = text_similarity.score("a", "b", provider)
    assert result.value == Decimal(0)


def test_missing_description_is_null_not_zero():
    """Zero would read as "completely dissimilar" rather than "not measured"."""
    result = text_similarity.score(None, "bearing", FakeProvider())
    assert result.value is None
    assert result.status is SimilarityStatus.NOT_AVAILABLE_NO_TEXT


def test_empty_description_is_treated_as_missing():
    result = text_similarity.score("   ", "bearing", FakeProvider())
    assert result.status is SimilarityStatus.NOT_AVAILABLE_NO_TEXT


def test_unavailable_model_is_reported_not_substituted():
    """No TF-IDF or token-overlap stand-in."""
    result = text_similarity.score("a", "b", FakeProvider(available=False))
    assert result.value is None
    assert result.status is SimilarityStatus.NOT_AVAILABLE_NO_MODEL


def test_zero_vector_has_no_direction():
    assert text_similarity.cosine([0.0, 0.0], [1.0, 1.0]) is None


def test_description_normalisation_is_conservative():
    """Technical identifiers carry the meaning and must survive."""
    assert (
        text_similarity.normalise_description("  SKF   6310-2RS  bearing ")
        == "SKF 6310-2RS bearing"
    )


def test_description_hash_changes_with_the_description():
    """A changed description must never return a stale cached embedding."""
    assert text_similarity.description_hash("a") != text_similarity.description_hash("b")


def test_embeddings_are_deterministic():
    provider = FakeProvider({"x": [0.5, 0.5]})
    first = text_similarity.score("x", "x", provider)
    second = text_similarity.score("x", "x", provider)
    assert first.value == second.value


def test_the_specified_model_is_named():
    assert text_similarity.MODEL_NAME == "all-MiniLM-L6-v2"
    assert text_similarity.EMBEDDING_DIMENSIONS == 384


# --- Business similarity ---------------------------------------------------------------


def test_business_similarity_with_everything_matching():
    result = business_similarity.score(material(), material("M2"), Decimal(10000))
    assert result.value == Decimal(1)


def test_circuit_mismatch_lowers_the_business_score():
    same = business_similarity.score(material(), material("M2"), Decimal(10000))
    different = business_similarity.score(
        material(), material("M2", circuit="Crushing"), Decimal(10000)
    )
    assert different.value < same.value


def test_manufacturer_mismatch_lowers_the_business_score():
    result = business_similarity.score(
        material(), material("M2", manufacturer="Other"), Decimal(10000)
    )
    assert result.value < Decimal(1)


def test_price_proximity_contributes():
    near = business_similarity.score(
        material(unit_price=Decimal(1000)),
        material("M2", unit_price=Decimal(1100)),
        Decimal(10000),
    )
    far = business_similarity.score(
        material(unit_price=Decimal(1000)),
        material("M2", unit_price=Decimal(9000)),
        Decimal(10000),
    )
    assert near.value > far.value


def test_missing_price_is_excluded_not_scored():
    result = business_similarity.score(
        material(unit_price=None), material("M2", unit_price=None), Decimal(10000)
    )
    assert result.available_features == 2  # circuit and manufacturer
    assert result.missing_features == 1


def test_criticality_is_not_a_business_similarity_feature():
    """It is a filter. Scoring it too would add a constant 0 to every pair."""
    import inspect

    source = inspect.getsource(business_similarity)
    assert "criticality" not in source.split('"""')[2]


# --- Combined score -------------------------------------------------------------------


def available(value: str) -> DimensionScore:
    return DimensionScore(Decimal(value), SimilarityStatus.AVAILABLE, 1, 0)


UNAVAILABLE = DimensionScore(None, SimilarityStatus.NOT_AVAILABLE_NO_TEXT, 0, 1)

W_STRUCT, W_TEXT, W_BUSINESS = Decimal("0.35"), Decimal("0.30"), Decimal("0.35")


def test_combined_score_applies_the_documented_weights():
    """0.35x0.80 + 0.30x0.60 + 0.35x0.90 = 0.775"""
    result = scoring.combine(
        available("0.80"), available("0.60"), available("0.90"),
        W_STRUCT, W_TEXT, W_BUSINESS,
    )
    assert result.combined == Decimal("0.775")
    assert result.score_completeness == Decimal(1)


def test_missing_dimension_renormalises_rather_than_scoring_zero():
    """With text absent: (0.35x0.80 + 0.35x0.90) / 0.70 = 0.85, not 0.595."""
    result = scoring.combine(
        available("0.80"), UNAVAILABLE, available("0.90"),
        W_STRUCT, W_TEXT, W_BUSINESS,
    )
    assert result.combined == Decimal("0.85")
    assert result.score_completeness == Decimal("0.7")


def test_score_completeness_records_how_much_was_measured():
    """A 0.9 from one dimension must be distinguishable from a 0.9 from three."""
    one = scoring.combine(
        available("0.90"), UNAVAILABLE, UNAVAILABLE, W_STRUCT, W_TEXT, W_BUSINESS
    )
    assert one.combined == Decimal("0.90")
    assert one.score_completeness == Decimal("0.35")


def test_no_available_dimension_yields_no_score():
    result = scoring.combine(
        UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, W_STRUCT, W_TEXT, W_BUSINESS
    )
    assert result.combined is None
    assert result.score_completeness == Decimal(0)


def test_combined_score_stays_within_bounds():
    result = scoring.combine(
        available("1"), available("1"), available("1"), W_STRUCT, W_TEXT, W_BUSINESS
    )
    assert Decimal(0) <= result.combined <= Decimal(1)


def test_applied_weights_are_recorded():
    result = scoring.combine(
        available("0.80"), UNAVAILABLE, available("0.90"),
        W_STRUCT, W_TEXT, W_BUSINESS,
    )
    weights = dict(result.applied_weights)
    assert "structured" in weights and "business" in weights
    assert "text" not in weights


# --- Ranking ---------------------------------------------------------------------------


def scored(material_id: str, value: str, same_circuit: bool = True):
    return (
        material_id,
        "1300",
        CombinedScore(
            Decimal(value), available(value), available(value), available(value), Decimal(1)
        ),
        same_circuit,
        {},
    )


def test_ranking_is_descending_by_similarity():
    neighbours = ranking.select_top_k(
        [scored("A", "0.70"), scored("B", "0.90"), scored("C", "0.80")], 5
    )
    assert [n.material for n in neighbours] == ["B", "C", "A"]
    assert [n.rank for n in neighbours] == [1, 2, 3]


def test_same_circuit_breaks_a_score_tie():
    neighbours = ranking.select_top_k(
        [scored("A", "0.80", same_circuit=False), scored("B", "0.80", same_circuit=True)], 5
    )
    assert neighbours[0].material == "B"


def test_material_id_breaks_a_remaining_tie():
    neighbours = ranking.select_top_k([scored("Z", "0.80"), scored("A", "0.80")], 5)
    assert [n.material for n in neighbours] == ["A", "Z"]


def test_ranking_is_deterministic():
    entries = [scored("A", "0.80"), scored("B", "0.80"), scored("C", "0.90")]
    assert [n.material for n in ranking.select_top_k(entries, 5)] == [
        n.material for n in ranking.select_top_k(list(reversed(entries)), 5)
    ]


def test_top_k_limits_the_selection():
    neighbours = ranking.select_top_k([scored(str(i), "0.80") for i in range(20)], 5)
    assert len(neighbours) == 5


def test_fewer_candidates_than_k_are_not_padded():
    neighbours = ranking.select_top_k([scored("A", "0.80"), scored("B", "0.70")], 5)
    assert len(neighbours) == 2


# --- Minimum-similarity admission floor -------------------------------------


def test_similarity_0_59_is_excluded_by_the_admission_floor():
    neighbours = ranking.select_top_k(
        [scored("A", "0.59")], 10, minimum_similarity=Decimal("0.60")
    )
    assert neighbours == []


def test_similarity_0_60_is_included_by_the_admission_floor():
    neighbours = ranking.select_top_k(
        [scored("A", "0.60")], 10, minimum_similarity=Decimal("0.60")
    )
    assert [n.material for n in neighbours] == ["A"]


def test_similarity_0_61_is_included_by_the_admission_floor():
    neighbours = ranking.select_top_k(
        [scored("A", "0.61")], 10, minimum_similarity=Decimal("0.60")
    )
    assert [n.material for n in neighbours] == ["A"]


def test_low_similarity_candidates_are_dropped_not_used_to_pad():
    """4 candidates >= 0.60 and 6 below it -- the below-floor ones must not
    appear in the result even though there is room under Top-K."""
    entries = [scored(f"H{i}", "0.65") for i in range(4)] + [
        scored(f"L{i}", "0.50") for i in range(6)
    ]
    neighbours = ranking.select_top_k(entries, 10, minimum_similarity=Decimal("0.60"))
    assert len(neighbours) == 4
    assert all(n.material.startswith("H") for n in neighbours)


def test_more_than_ten_qualifying_uses_only_top_ten():
    entries = [scored(f"M{i}", "0.70") for i in range(15)]
    neighbours = ranking.select_top_k(entries, 10, minimum_similarity=Decimal("0.60"))
    assert len(neighbours) == 10


def test_no_minimum_similarity_means_no_admission_filter():
    """Backward compatible: omitting the parameter behaves as before."""
    neighbours = ranking.select_top_k([scored("A", "0.10")], 10)
    assert len(neighbours) == 1


def test_no_candidates_yields_no_neighbours():
    assert ranking.select_top_k([], 5) == []


def test_unmeasurable_candidates_are_dropped():
    unmeasured = (
        "X", "1300",
        CombinedScore(None, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, Decimal(0)),
        True, {},
    )
    neighbours = ranking.select_top_k([scored("A", "0.80"), unmeasured], 5)
    assert [n.material for n in neighbours] == ["A"]


# --- Confidence -----------------------------------------------------------------------------


HIGH_N, HIGH_S = 5, Decimal("0.80")
MED_N, MED_S = 3, Decimal("0.60")


def neighbours_at(values: list[str], same_circuit: bool = True) -> list[Neighbour]:
    return [
        Neighbour(
            material=f"M{i}",
            plant="1300",
            rank=i + 1,
            score=CombinedScore(
                Decimal(v), available(v), available(v), available(v), Decimal(1)
            ),
            same_circuit=same_circuit,
            same_material_group=True,
            criticality="CRITICAL",
            history_months=18,
            is_active=True,
        )
        for i, v in enumerate(values)
    ]


def test_five_neighbours_above_080_in_same_circuit_is_high():
    graded = confidence.grade(
        neighbours_at(["0.90", "0.85", "0.82", "0.81", "0.81"]),
        HIGH_N, HIGH_S, MED_N, MED_S,
    )
    assert graded is OarConfidence.HIGH


def test_four_neighbours_at_072_is_medium():
    graded = confidence.grade(
        neighbours_at(["0.72", "0.70", "0.68", "0.65"]), HIGH_N, HIGH_S, MED_N, MED_S
    )
    assert graded is OarConfidence.MEDIUM


def test_two_neighbours_is_low_however_similar():
    """Count is evidence. One excellent match is not a basis for a parameter."""
    graded = confidence.grade(
        neighbours_at(["0.99", "0.98"]), HIGH_N, HIGH_S, MED_N, MED_S
    )
    assert graded is OarConfidence.LOW


def test_best_similarity_below_060_is_low():
    graded = confidence.grade(
        neighbours_at(["0.59", "0.55", "0.50", "0.45", "0.40"]),
        HIGH_N, HIGH_S, MED_N, MED_S,
    )
    assert graded is OarConfidence.LOW


@pytest.mark.parametrize(
    "best,expected",
    [
        ("0.599999", OarConfidence.LOW),
        ("0.60", OarConfidence.MEDIUM),
        ("0.80", OarConfidence.MEDIUM),
        ("0.800001", OarConfidence.HIGH),
    ],
)
def test_confidence_boundaries_are_exact(best, expected):
    """HIGH needs > 0.80 and LOW is < 0.60, so both boundaries sit in MEDIUM.

    ``best`` is the maximum across all neighbours, so the rest are held at
    0.10 -- below every threshold -- to guarantee ``best`` really is the value
    under test rather than being overtaken by a filler value.
    """
    values = [best] + ["0.10"] * 4
    assert confidence.grade(neighbours_at(values), HIGH_N, HIGH_S, MED_N, MED_S) is expected


def test_high_requires_a_same_circuit_neighbour():
    graded = confidence.grade(
        neighbours_at(["0.90", "0.85", "0.82", "0.81", "0.81"], same_circuit=False),
        HIGH_N, HIGH_S, MED_N, MED_S,
    )
    assert graded is OarConfidence.MEDIUM


def test_no_neighbours_is_low():
    assert confidence.grade([], HIGH_N, HIGH_S, MED_N, MED_S) is OarConfidence.LOW


# --- Weighted estimate --------------------------------------------------------------------------


def neighbour_with(value: str, ss: int | None, rop: int | None, mx: int | None, eligible=True):
    return Neighbour(
        material=f"M{value}",
        plant="1300",
        rank=1,
        score=CombinedScore(
            Decimal(value), available(value), available(value), available(value), Decimal(1)
        ),
        same_circuit=True,
        same_material_group=True,
        criticality="CRITICAL",
        history_months=18,
        is_active=True,
        safety_stock=ss,
        rop=rop,
        max_stock=mx,
        inventory_eligible=eligible,
    )


def test_weighted_estimate_matches_the_documented_formula():
    """(0.90x10 + 0.80x20) / (0.90+0.80) = 25/1.7 = 14.7 -> 15."""
    result = estimate.calculate(
        [neighbour_with("0.90", 10, 20, 30), neighbour_with("0.80", 20, 40, 60)],
        service_level_configured=True,
    )
    assert result.status is EstimateStatus.SUCCESS
    trace = dict(result.trace)
    assert abs(float(trace["safety_stock_raw"]) - 14.7058823529) < 1e-6
    assert result.safety_stock == 15


def test_weighted_estimate_covers_rop_and_max():
    result = estimate.calculate(
        [neighbour_with("0.90", 10, 20, 30), neighbour_with("0.80", 20, 40, 60)],
        service_level_configured=True,
    )
    assert result.rop == 30  # 29.41 -> 30
    assert result.max_stock == 45  # 44.12 -> 45


def test_a_closer_neighbour_weighs_more():
    close = estimate.calculate(
        [neighbour_with("0.99", 10, 10, 10), neighbour_with("0.10", 100, 100, 100)],
        service_level_configured=True,
    )
    assert close.safety_stock < 50


def test_estimate_is_labelled_similarity_based():
    """Required by the Solution Design -- not a forecast, not an ML prediction."""
    result = estimate.calculate(
        [neighbour_with("0.90", 10, 20, 30)], service_level_configured=True
    )
    assert result.label == ESTIMATE_LABEL == "SIMILARITY-BASED ESTIMATE"


def test_unsigned_service_level_blocks_the_estimate():
    """Neighbours exist and similarity is real; no inventory value is invented."""
    result = estimate.calculate(
        [neighbour_with("0.90", None, None, None, eligible=False)],
        service_level_configured=False,
    )
    assert result.status is EstimateStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET
    assert result.safety_stock is None


def test_signed_service_level_but_no_neighbour_values():
    result = estimate.calculate(
        [neighbour_with("0.90", None, None, None, eligible=False)],
        service_level_configured=True,
    )
    assert result.status is EstimateStatus.NOT_EVALUABLE_NEIGHBOR_INVENTORY


def test_no_neighbours_yields_no_estimate():
    result = estimate.calculate([], service_level_configured=True)
    assert result.status is EstimateStatus.NOT_EVALUABLE_NO_NEIGHBORS


def test_partial_neighbour_eligibility_is_counted():
    """Ineligible neighbours are excluded, never treated as zero."""
    result = estimate.calculate(
        [
            neighbour_with("0.90", 10, 20, 30),
            neighbour_with("0.80", None, None, None, eligible=False),
        ],
        service_level_configured=True,
    )
    assert result.inventory_eligible_neighbours == 1
    assert result.inventory_ineligible_neighbours == 1
    # Only the eligible neighbour contributes, so the estimate is its own value.
    assert result.safety_stock == 10


def test_rounding_happens_after_the_weighted_average():
    """Rounding each neighbour first would compound several ceilings."""
    result = estimate.calculate(
        [neighbour_with("0.50", 1, 1, 1), neighbour_with("0.50", 2, 2, 2)],
        service_level_configured=True,
    )
    # (0.5x1 + 0.5x2) / 1.0 = 1.5 -> 2, not ceil(1) + ceil(2) averaged.
    assert result.safety_stock == 2


# --- Minimum-neighbour gate (Task 2/3): a hard admission floor, not a
# --- confidence input. 1-4 qualifying neighbours never produce a partial
# --- estimate. ---------------------------------------------------------


def _neighbours(count: int, similarity: str = "0.90") -> list:
    return [neighbour_with(similarity, 10, 20, 30) for _ in range(count)]


def test_zero_qualifying_neighbours_is_not_evaluable_no_neighbours():
    """Zero candidates at all keeps the existing, more specific status."""
    result = estimate.calculate([], service_level_configured=True, minimum_neighbours=5)
    assert result.status is EstimateStatus.NOT_EVALUABLE_NO_NEIGHBORS


def test_one_qualifying_neighbour_is_insufficient():
    result = estimate.calculate(
        _neighbours(1), service_level_configured=True, minimum_neighbours=5
    )
    assert result.status is EstimateStatus.NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS
    assert result.safety_stock is None
    assert result.rop is None
    assert result.max_stock is None
    assert result.qualifying_neighbours == 1
    assert result.minimum_neighbours == 5


def test_four_qualifying_neighbours_is_insufficient():
    result = estimate.calculate(
        _neighbours(4), service_level_configured=True, minimum_neighbours=5
    )
    assert result.status is EstimateStatus.NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS
    assert result.safety_stock is None
    assert result.rop is None
    assert result.max_stock is None


def test_exactly_five_qualifying_neighbours_succeeds():
    result = estimate.calculate(
        _neighbours(5), service_level_configured=True, minimum_neighbours=5
    )
    assert result.status is EstimateStatus.SUCCESS
    assert result.safety_stock is not None


def test_ten_qualifying_neighbours_succeeds():
    result = estimate.calculate(
        _neighbours(10), service_level_configured=True, minimum_neighbours=5
    )
    assert result.status is EstimateStatus.SUCCESS


def test_no_partial_estimate_is_computed_below_the_minimum():
    """1-4 neighbours must never leak a safety_stock/rop/max_stock number,
    however good their similarity."""
    for count in (1, 2, 3, 4):
        result = estimate.calculate(
            _neighbours(count, similarity="0.99"),
            service_level_configured=True,
            minimum_neighbours=5,
        )
        assert result.safety_stock is None, count
        assert result.rop is None, count
        assert result.max_stock is None, count


def test_only_qualifying_neighbours_contribute_to_safety_stock():
    """Neighbours below the similarity floor must already have been excluded
    by ranking.select_top_k before reaching calculate() -- this pins that
    calculate() itself only ever sees (and uses) what it's given."""
    qualifying = _neighbours(5, similarity="0.90")
    result = estimate.calculate(qualifying, service_level_configured=True, minimum_neighbours=5)
    assert result.status is EstimateStatus.SUCCESS
    # All 5 neighbours share safety_stock=10, so the weighted average is 10
    # regardless of weight -- a low-similarity neighbour smuggled in with a
    # different value would change this.
    assert result.safety_stock == 10


def test_only_qualifying_neighbours_contribute_to_rop():
    qualifying = _neighbours(5, similarity="0.90")
    result = estimate.calculate(qualifying, service_level_configured=True, minimum_neighbours=5)
    assert result.rop == 20


def test_only_qualifying_neighbours_contribute_to_max_stock():
    qualifying = _neighbours(5, similarity="0.90")
    result = estimate.calculate(qualifying, service_level_configured=True, minimum_neighbours=5)
    assert result.max_stock == 30


# --- End-to-end: ranking's admission floor feeding estimate's minimum-count
# --- gate, matching the required pipeline sequence (filter -> sort -> top-K
# --- -> minimum-neighbour check -> calculate). --------------------------


def _scored_with_inventory(material_id: str, similarity: str, ss=10, rop=20, mx=30):
    return (
        material_id,
        "1300",
        CombinedScore(
            Decimal(similarity),
            available(similarity),
            available(similarity),
            available(similarity),
            Decimal(1),
        ),
        True,
        {
            "safety_stock": ss,
            "rop": rop,
            "max_stock": mx,
            "inventory_eligible": True,
        },
    )


def test_four_above_floor_and_six_below_yields_not_evaluable():
    """4 candidates >= 0.60 and 6 candidates < 0.60 -- ranking drops the 6,
    leaving 4 qualifying, which is below the minimum of 5."""
    entries = [_scored_with_inventory(f"H{i}", "0.65") for i in range(4)] + [
        _scored_with_inventory(f"L{i}", "0.50") for i in range(6)
    ]
    neighbours = ranking.select_top_k(entries, 10, minimum_similarity=Decimal("0.60"))
    result = estimate.calculate(
        list(neighbours),
        service_level_configured=True,
        minimum_neighbours=5,
        minimum_similarity=Decimal("0.60"),
    )
    assert result.status is EstimateStatus.NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS
    assert result.safety_stock is None


def test_five_above_floor_and_extra_below_yields_success_using_only_qualifying():
    """5 candidates >= 0.60 plus additional candidates < 0.60 -- the estimate
    succeeds using only the 5 qualifying candidates."""
    entries = [_scored_with_inventory(f"H{i}", "0.65") for i in range(5)] + [
        _scored_with_inventory(f"L{i}", "0.40", ss=999, rop=999, mx=999) for i in range(3)
    ]
    neighbours = ranking.select_top_k(entries, 10, minimum_similarity=Decimal("0.60"))
    assert len(neighbours) == 5
    result = estimate.calculate(
        list(neighbours),
        service_level_configured=True,
        minimum_neighbours=5,
        minimum_similarity=Decimal("0.60"),
    )
    assert result.status is EstimateStatus.SUCCESS
    # The low-similarity 999-valued candidates were never admitted, so they
    # cannot have dragged the weighted average toward 999.
    assert result.safety_stock == 10


def test_fifteen_qualifying_selects_top_ten_before_the_gate():
    entries = [_scored_with_inventory(f"M{i}", "0.70") for i in range(15)]
    neighbours = ranking.select_top_k(entries, 10, minimum_similarity=Decimal("0.60"))
    assert len(neighbours) == 10
    result = estimate.calculate(
        list(neighbours),
        service_level_configured=True,
        minimum_neighbours=5,
        minimum_similarity=Decimal("0.60"),
    )
    assert result.status is EstimateStatus.SUCCESS
