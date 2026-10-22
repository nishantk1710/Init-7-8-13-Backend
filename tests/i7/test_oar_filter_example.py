"""The filter example, before and after oar-similarity-10, on one fixed set of donors.

The production records behind the reported case (``FILTER PN:5112305242``, material group 308175, a donor with
ROP 61 pulling the estimate from about 6 to 17) are not in this repository, so this is NOT a reproduction of
production. It is a deterministic fixture built to the same shape: four ordinary filters, plus a spray nozzle
and a proximity sensor from the same material group with a ROP of 61 and 25. The text similarities are injected
(0.90, 0.85, 0.80, 0.75 for the filters, 0.70 for the nozzle, 0.60 for the sensor), so what is demonstrated is
the rule and the arithmetic, not how MiniLM scores these descriptions.

Combined similarity here is ``0.4 x 1.0 (group, unit) + 0.4 x text + 0.2 x 1.0 (same price)``, i.e.
``0.6 + 0.4 x text`` -- 0.96, 0.94, 0.92, 0.90 for the filters, 0.88 for the nozzle, 0.84 for the sensor.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np

from app.initiatives.i7.oar import service
from app.initiatives.i7.oar.text_similarity import PrecomputedEmbeddingProvider
from app.initiatives.i7.oar.types import CandidateAttributes, OarConfidence
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import (
    load_mock_max_stock_policy,
    load_mock_service_level_policy,
)
from app.initiatives.i7.policy.thresholds import SimilarityPolicy

TARGET_DESCRIPTION = "FILTER PN:5112305242"
GROUP = "308175"

# material, description, text similarity to the target, (safety stock, ROP, max stock) the donor lends
DONORS = [
    ("F1", "FILTER ELEMENT 5112305242 A", 0.90, (1, 3, 12)),
    ("F2", "FILTER ELEMENT 5112305242 B", 0.85, (3, 9, 30)),
    ("F3", "FILTER ELEMENT 5112305242 C", 0.80, (2, 6, 20)),
    ("F4", "FILTER ELEMENT 5112305242 D", 0.75, (1, 3, 12)),
    ("N1", "FILTER PRESS ANDRITZ SPRAY NOZZLE", 0.70, (20, 61, 150)),
    ("S1", "FILTER PRESS ANDRITZ PROXIMITY SENSOR", 0.60, (8, 25, 60)),
]


def attributes(material: str, description: str) -> CandidateAttributes:
    return CandidateAttributes(
        sap_material_number=material, sap_plant_code="1300", criticality="NORMAL", is_active=True,
        history_months=24, material_group=GROUP, equipment_type=None, base_unit_of_measure="EA",
        circuit=None, manufacturer=None, unit_price=Decimal(100), description=description,
    )


def unit(cosine: float) -> np.ndarray:
    return np.array([cosine, (1 - cosine * cosine) ** 0.5], dtype=np.float32)


VECTORS = {TARGET_DESCRIPTION: np.array([1.0, 0.0], dtype=np.float32)}
VECTORS.update({description: unit(cosine) for _, description, cosine, _ in DONORS})


def policy(**similarity) -> PolicyDocument:
    return PolicyDocument(
        service_level=load_mock_service_level_policy(),
        max_stock=load_mock_max_stock_policy(),
        similarity=SimilarityPolicy(
            **{
                "text_embedding_weight": 1.0,
                "text_fuzzy_weight": 0.0,
                "minimum_identity_text_similarity": 0.50,
                "same_group_minimum_text_similarity": 0.50,
                **similarity,
            }
        ),  # injected cosines, floors at the 0.50 this fixture was written around
    )


OLD = policy(identity_validation=False, estimator="weighted_mean")
"""oar-similarity-9: a shared group is identity, and the estimate is the similarity-weighted mean."""
NEW = policy()
"""oar-similarity-10 defaults."""


def run(pol: PolicyDocument):
    target = attributes("TARGET", TARGET_DESCRIPTION)
    donors = [attributes(material, description) for material, description, _, _ in DONORS]
    values = {
        (material, "1300"): {"safety_stock": ss, "rop": rop, "max_stock": mx}
        for material, _, _, (ss, rop, mx) in DONORS
    }
    return service.evaluate_target(
        target, donors, PrecomputedEmbeddingProvider(VECTORS), Decimal(5), values, pol
    )


# --- before -----------------------------------------------------------------------------------------


def test_before_the_nozzle_was_a_neighbour_and_its_rop_of_61_pulled_the_estimate() -> None:
    result = run(OLD)

    assert [n.material for n in result.neighbours] == ["F1", "F2", "F3", "F4", "N1"]
    # SS (0.96x1 + 0.94x3 + 0.92x2 + 0.90x1 + 0.88x20) / 4.60 = 24.12 / 4.60 = 5.24 -> 6
    # ROP (2.88 + 8.46 + 5.52 + 2.70 + 53.68) / 4.60 = 73.24 / 4.60 = 15.92 -> 16
    # Max (11.52 + 28.20 + 18.40 + 10.80 + 132.00) / 4.60 = 200.92 / 4.60 = 43.68 -> 44
    assert (result.estimate.safety_stock, result.estimate.rop, result.estimate.max_stock) == (6, 16, 44)
    assert result.estimate.strategy == "weighted_mean"
    assert result.confidence is OarConfidence.MEDIUM


# --- after --------------------------------------------------------------------------------------------


def test_after_only_the_filters_are_neighbours() -> None:
    result = run(NEW)

    assert [n.material for n in result.neighbours] == ["F1", "F2", "F3", "F4"]
    refused = {c.material: c for c in result.rejected_candidates}
    assert set(refused) == {"N1", "S1"}
    assert refused["N1"].status == refused["S1"].status == "REJECTED_COMPONENT_CONFLICT"
    assert refused["N1"].combined > Decimal("0.60")  # it would have cleared the score floor


def test_after_the_estimate_is_the_weighted_median_of_the_four_filters() -> None:
    """ROP sorted 3 (0.96), 3 (0.90), 6 (0.92), 9 (0.94): total 3.72, half 1.86 -- the accumulated weight
    after the two 3s is exactly 1.86, so the median is the midpoint of 3 and 6, 4.5 -> 5.
    SS sorted 1 (0.96), 1 (0.90), 2 (0.92), 3 (0.94): midpoint of 1 and 2 = 1.5 -> 2.
    Max sorted 12 (0.96), 12 (0.90), 20 (0.92), 30 (0.94): midpoint of 12 and 20 = 16."""
    result = run(NEW)

    assert (result.estimate.safety_stock, result.estimate.rop, result.estimate.max_stock) == (2, 5, 16)
    assert result.estimate.strategy == "weighted_median"
    assert result.estimate.max_stock >= result.estimate.rop + result.estimate.safety_stock


def test_the_four_filters_alone_would_have_given_a_mean_of_6() -> None:
    """The identity fix does most of the work here (16 -> 6 on the mean); the median then settles it at 5."""
    result = run(policy(estimator="weighted_mean"))

    assert [n.material for n in result.neighbours] == ["F1", "F2", "F3", "F4"]
    assert result.estimate.rop == 6  # 19.56 / 3.72 = 5.26 -> 6


def test_after_the_stage_counts_say_what_happened() -> None:
    counts = run(NEW).stage_counts

    assert counts.donor_pool == 6 and counts.hard_filter_passes == 6
    assert counts.identity_conflicts == 2 and counts.identity_insufficient == 0
    assert counts.identity_passes == 4 and counts.score_floor_passes == 4 and counts.final_neighbours == 4


def test_the_four_genuine_filters_are_not_lost() -> None:
    old = {n.material for n in run(OLD).neighbours} - {"N1"}
    new = {n.material for n in run(NEW).neighbours}

    assert old == new == {"F1", "F2", "F3", "F4"}
