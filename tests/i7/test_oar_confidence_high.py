"""OAR confidence reaches HIGH without a same-circuit neighbour (oar-similarity-12).

The Solution Design adds "a neighbour on the same circuit" to HIGH, but circuit is not a data field anywhere, so
with that condition nothing could ever grade HIGH and every well-matched target sat at MEDIUM -- the Recommendation
Confidence filter had no High to offer. HIGH is now the Formula Reference's own band: 5 or more neighbours and the
best similarity above 0.80. These tests drive the real engine, not just ``confidence.grade``.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np

from app.initiatives.i7.oar import service
from app.initiatives.i7.oar.text_similarity import PrecomputedEmbeddingProvider
from app.initiatives.i7.oar.types import ALGORITHM_VERSION, CandidateAttributes, OarConfidence
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import (
    load_mock_max_stock_policy,
    load_mock_service_level_policy,
)
from app.initiatives.i7.policy.thresholds import ConfidencePolicy, SimilarityPolicy

TARGET = "FILTER PN:5112305242"


def unit(cosine: float) -> np.ndarray:
    return np.array([cosine, (1 - cosine * cosine) ** 0.5], dtype=np.float32)


def part(number: str, description: str) -> CandidateAttributes:
    return CandidateAttributes(
        sap_material_number=number, sap_plant_code="1300", criticality="NORMAL", is_active=True,
        history_months=24, material_group="308175", equipment_type=None, base_unit_of_measure="EA",
        circuit=None, manufacturer=None, unit_price=Decimal(100), description=description,
    )


def policy(**confidence) -> PolicyDocument:
    return PolicyDocument(
        service_level=load_mock_service_level_policy(),
        max_stock=load_mock_max_stock_policy(),
        # injected cosines, floors at 0.50: these tests are about confidence, not the text score
        similarity=SimilarityPolicy(
            text_embedding_weight=1.0, text_fuzzy_weight=0.0,
            minimum_identity_text_similarity=0.50, same_group_minimum_text_similarity=0.50,
        ),
        confidence=ConfidencePolicy(**confidence),
    )


def grade(cosines: list[float], **confidence) -> OarConfidence:
    """The confidence of a target whose donors have these text cosines.

    Combined similarity is ``0.4 x 1 (group, unit) + 0.4 x cosine + 0.2 x 1 (price)`` = ``0.6 + 0.4 x cosine``,
    so a cosine of 0.9 is a combined 0.96 and 0.4 is 0.76.
    """
    vectors = {TARGET: np.array([1.0, 0.0], dtype=np.float32)}
    donors = []
    for i, cosine in enumerate(cosines):
        description = f"FILTER ELEMENT 5112305242 {i}"  # shares the part number, so identity is confirmed
        vectors[description] = unit(cosine)
        donors.append(part(f"D{i}", description))
    inventory = {(d.sap_material_number, "1300"): {"safety_stock": 2, "rop": 5, "max_stock": 9} for d in donors}
    result = service.evaluate_target(
        part("T", TARGET), donors, PrecomputedEmbeddingProvider(vectors), Decimal(5), inventory,
        policy(**confidence),
    )
    return result.confidence


def test_five_strong_neighbours_are_high_by_default() -> None:
    assert grade([0.95, 0.90, 0.90, 0.85, 0.85]) is OarConfidence.HIGH  # best combined 0.98


def test_the_circuit_requirement_can_be_switched_back_on_and_then_caps_at_medium() -> None:
    strong = [0.95, 0.90, 0.90, 0.85, 0.85]

    assert grade(strong, oar_require_same_circuit_for_high=True) is OarConfidence.MEDIUM


def test_the_count_and_similarity_bars_for_high_still_apply() -> None:
    assert grade([0.95, 0.90, 0.90, 0.85]) is OarConfidence.MEDIUM  # four neighbours: not enough for HIGH
    # five neighbours, but the best combined similarity is 0.6 + 0.4 x 0.45 = 0.78, not above 0.80
    assert grade([0.45, 0.40, 0.40, 0.40, 0.40]) is OarConfidence.MEDIUM


def test_the_other_grades_are_unchanged() -> None:
    assert grade([0.9, 0.9]) is OarConfidence.LOW  # two neighbours: LOW (fewer than 3)
    assert grade([0.9, 0.9, 0.9]) is OarConfidence.MEDIUM  # three neighbours, strong
    assert grade([]) is OarConfidence.LOW


def test_the_policy_default_no_longer_requires_a_circuit() -> None:
    assert ConfidencePolicy().oar_require_same_circuit_for_high is False


def test_the_algorithm_version_marks_the_change_so_an_old_run_is_not_reused() -> None:
    assert int(ALGORITHM_VERSION.rsplit("-", 1)[1]) >= 12
