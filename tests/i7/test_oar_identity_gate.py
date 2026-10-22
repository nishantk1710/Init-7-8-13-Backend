"""A neighbour must MATCH on identity (oar-similarity-8), not merely have had an identity feature compared.

The defect: the impact wrench's five neighbours were a blasting product, two crusher bolts, a pipe wrench
and a valve. Every one was in a different material group with weak text similarity (0.31-0.46), yet each
scored 0.61-0.63 -- over the 0.60 floor -- because a similar price supplies over half the score. The old
evidence rule only asked that material group or description had been *compared*; it never asked that either
*matched*. Now a neighbour needs the same material group, or text similarity at or above the floor.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np

from app.initiatives.i7.oar import engine, scoring, service
from app.initiatives.i7.oar.retrieval import RetrievalConfig
from app.initiatives.i7.oar.text_similarity import PrecomputedEmbeddingProvider
from app.initiatives.i7.oar.types import CandidateAttributes
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import load_mock_max_stock_policy, load_mock_service_level_policy
from app.initiatives.i7.policy.thresholds import SimilarityPolicy

NO_TEXT = PrecomputedEmbeddingProvider(None)


def policy(floor: float | None = 0.50, *, validation: bool = True) -> PolicyDocument:
    return PolicyDocument(
        service_level=load_mock_service_level_policy(),
        max_stock=load_mock_max_stock_policy(),
        similarity=SimilarityPolicy(
            minimum_identity_text_similarity=floor,
            minimum_similarity=0.30,
            identity_validation=validation,
        ),
    )


def part(number: str, *, group: str | None, description: str | None = None, price: str = "100") -> CandidateAttributes:
    return CandidateAttributes(
        sap_material_number=number, sap_plant_code="1300", criticality="NORMAL", is_active=True,
        history_months=24, material_group=group, equipment_type=None, base_unit_of_measure="EA",
        circuit=None, manufacturer=None, unit_price=Decimal(price), description=description,
    )


def unit(cosine: float) -> np.ndarray:
    """A unit vector whose cosine with [1, 0] is ``cosine``."""
    return np.array([cosine, (1 - cosine * cosine) ** 0.5], dtype=np.float32)


# normalised descriptions -> vectors; "target" is [1, 0], the others sit at the stated cosine to it
VECTORS = {
    "target": np.array([1.0, 0.0], dtype=np.float32),
    "close": unit(0.80),
    "edge": unit(0.50),
    "weak": unit(0.40),
    "far": unit(0.10),
}
WITH_TEXT = PrecomputedEmbeddingProvider(VECTORS)


def score(target, candidate, provider=WITH_TEXT, floor=0.50):
    return service._score_pair(target, candidate, provider, Decimal(5), policy(floor).similarity)


# --- The rule, on the exact score --------------------------------------------


def test_the_same_material_group_alone_is_not_identity() -> None:
    """oar-similarity-10: the old rule passed this (same group, text 0.10). Group membership is not proof of
    what a part is -- see tests/i7/test_oar_identity.py for the component-level cases."""
    result = score(part("T", group="G1", description="target"), part("C", group="G1", description="far"))

    assert result.evidence_sufficient is False
    assert result.identity.status.value == "INSUFFICIENT_GROUP_ONLY"
    assert "no identity match" in result.evidence_detail
    assert "0.10" in result.evidence_detail


def test_the_same_material_group_with_enough_text_matches() -> None:
    result = score(part("T", group="G1", description="target"), part("C", group="G1", description="close"))

    assert result.evidence_sufficient is True
    assert result.identity.status.value == "CONFIRMED_GROUP_TEXT"


def test_a_different_group_with_weak_text_is_not_a_match() -> None:
    """The impact wrench: other group, text 0.40, price identical -- previously admitted on price."""
    result = score(part("T", group="G1", description="target"), part("C", group="G2", description="weak"))

    assert result.evidence_sufficient is False
    assert "no identity match" in result.evidence_detail
    assert "0.40" in result.evidence_detail


def test_a_different_group_with_strong_text_is_a_match() -> None:
    result = score(part("T", group="G1", description="target"), part("C", group="G2", description="close"))

    assert result.evidence_sufficient is True


def test_the_text_floor_is_inclusive() -> None:
    result = score(part("T", group="G1", description="target"), part("C", group="G2", description="edge"))

    assert result.evidence_sufficient is True


def test_without_a_text_model_a_same_group_candidate_needs_a_shared_part_or_specific_wording() -> None:
    """With no model the text route is closed. Same group alone no longer qualifies; the descriptions can
    still prove identity themselves."""
    bare = score(part("T", group="G1"), part("C", group="G1"), provider=NO_TEXT)
    other_bearing = score(
        part("T", group="G1", description="BEARING 6310"),
        part("C", group="G1", description="BEARING 7000"),
        provider=NO_TEXT,
    )
    shared_number = score(
        part("T", group="G1", description="BEARING 6310-2RS"),
        part("C", group="G1", description="DEEP GROOVE 6310-2RS"),
        provider=NO_TEXT,
    )
    shared_wording = score(
        part("T", group="G1", description="BEARING 6310"),
        part("C", group="G1", description="BEARING 6310 SHIELDED"),
        provider=NO_TEXT,
    )
    other_group = score(part("T", group="G1"), part("C", group="G2"), provider=NO_TEXT)

    assert bare.evidence_sufficient is False
    assert other_bearing.evidence_sufficient is False  # the same type, but nothing specific in common
    assert shared_number.identity.status.value == "CONFIRMED_PART_NUMBER"
    assert shared_wording.identity.status.value == "CONFIRMED_GROUP_COMPONENT_TYPE"
    assert other_group.evidence_sufficient is False
    assert "no text comparison" in other_group.evidence_detail


def test_the_rule_can_be_switched_off() -> None:
    result = score(part("T", group="G1", description="target"), part("C", group="G2", description="weak"), floor=None)

    assert result.evidence_sufficient is True


def test_the_refusal_is_stated_in_the_stored_explanation() -> None:
    result = score(part("T", group="G1", description="target"), part("C", group="G2", description="weak"))

    assert "evidence=low(no identity match" in scoring.explain(result)


# --- End to end: the wrench ----------------------------------------------------


def test_a_target_whose_only_candidates_are_in_other_groups_with_weak_text_gets_no_neighbours() -> None:
    target = part("WRENCH", group="G1", description="target")
    candidates = [part(f"X{i}", group=f"G{i + 2}", description="weak") for i in range(5)]

    result = service.evaluate_target(target, candidates, WITH_TEXT, Decimal(5), {}, policy())

    assert result.neighbours == ()
    assert result.low_evidence_candidates == 5


def test_the_same_candidates_were_neighbours_before_the_gate() -> None:
    target = part("WRENCH", group="G1", description="target")
    candidates = [part(f"X{i}", group=f"G{i + 2}", description="weak") for i in range(5)]

    result = service.evaluate_target(target, candidates, WITH_TEXT, Decimal(5), {}, policy(floor=None))

    assert len(result.neighbours) == 5


def test_same_group_and_strongly_described_candidates_still_qualify() -> None:
    target = part("T", group="G1", description="target")
    same_group = [part(f"S{i}", group="G1", description="close") for i in range(3)]
    described = [part(f"D{i}", group="G9", description="close") for i in range(2)]
    unrelated = [part(f"U{i}", group="G8", description="weak") for i in range(4)]
    group_only = [part(f"G{i}", group="G1", description="far") for i in range(3)]

    result = service.evaluate_target(
        target, same_group + described + unrelated + group_only, WITH_TEXT, Decimal(5), {}, policy()
    )

    assert {n.material for n in result.neighbours} == {f"S{i}" for i in range(3)} | {f"D{i}" for i in range(2)}
    assert result.low_evidence_candidates == 7  # four weak other-group, three same-group-only


# --- The two-stage retrieval agrees with the exact score ----------------------------


def test_two_stage_retrieval_applies_the_same_gate() -> None:
    target = part("T", group="G1", description="target")
    population = (
        [part(f"S{i}", group="G1", description="far") for i in range(4)]
        + [part(f"D{i}", group="G2", description="close") for i in range(4)]
        + [part(f"E{i}", group="G3", description="edge") for i in range(2)]
        + [part(f"U{i}", group="G4", description="weak") for i in range(6)]
        + [part(f"V{i}", group="G5", description="far") for i in range(6)]
    )
    pol = policy()
    config = RetrievalConfig(structured_pool=50, semantic_pool=50, fused_pool=100, exhaustive_below=0)

    [two_stage] = list(
        engine.evaluate_targets(
            [target], population, provider=WITH_TEXT, embeddings=VECTORS, price_range=Decimal(5),
            inventory_values={}, policy=pol, config=config,
        )
    )
    exact = service.evaluate_target(target, population, WITH_TEXT, Decimal(5), {}, pol)

    chosen = {n.material for n in two_stage.neighbours}
    assert chosen == {n.material for n in exact.neighbours}
    assert not chosen & {f"U{i}" for i in range(6)} and not chosen & {f"V{i}" for i in range(6)}


def test_the_policy_default_is_a_first_setting_of_one_half() -> None:
    assert SimilarityPolicy().minimum_identity_text_similarity == 0.50
