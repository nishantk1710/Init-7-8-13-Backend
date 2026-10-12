"""Two-stage OAR retrieval: same answers as exhaustive scoring, far less work.

The engine (``oar.engine``) shortlists candidates with a vectorised pre-score and
scores only the shortlist exactly. Its correctness claim is narrow and testable:
on the same inputs it produces the SAME neighbours, scores, confidence and
estimate as ``service.evaluate_target``, which scores every eligible candidate.
Everything here either checks that claim or checks a piece of the machinery it
rests on (the closed-form rejection counts, the tie-breaking selection, the
partition).

Populations are random but seeded, so every run is the same run. Partitions are
made much larger than the pools, and ``exhaustive_below`` is 0, so retrieval is
genuinely exercised rather than falling back to scoring everything.

Synthetic data only: this proves the engine finds what exhaustive scoring
finds. It says nothing about whether either finds genuinely similar spare
parts -- that needs expert-labelled pairs (``oar.evaluation``).
"""

from __future__ import annotations

import hashlib
import random
from decimal import Decimal

import numpy as np
import pytest

from app.initiatives.i7.oar import eligibility, engine, retrieval, service
from app.initiatives.i7.oar.retrieval import (
    DonorIndex,
    PopulationProfile,
    RetrievalConfig,
    top_n,
)
from app.initiatives.i7.oar.text_similarity import (
    PrecomputedEmbeddingProvider,
    normalise_description,
)
from app.initiatives.i7.oar.types import CandidateAttributes, OarResult
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import (
    load_mock_max_stock_policy,
    load_mock_service_level_policy,
)
from app.initiatives.i7.policy.thresholds import SimilarityPolicy

CRITICALITIES = ["CRITICAL", "IMPACT", "NORMAL", None]
GROUPS = ["BRG", "PMP", "VLV", "MOT", "SEAL", None]
UOMS = ["EA", "EA", "EA", "SET", "M", None]
FAMILIES = {
    "bearing": ["SKF 6310-2RS deep groove ball bearing", "bearing ball 6310 2RS SKF",
                "deep groove bearing 6310", "ball bearing 6310-2RS"],
    "pump": ["pump seal kit mechanical", "mechanical seal kit for pump",
             "pump impeller 250mm", "impeller pump 250 mm cast"],
    "valve": ["gate valve DN100 PN16", "valve gate 100mm PN16", "butterfly valve DN150"],
    "motor": ["motor 55kW 4 pole", "electric motor 55 kW", "motor 75kW 2 pole"],
}
ALL_TEXTS = [text for texts in FAMILIES.values() for text in texts]


def _vector(text: str, family: str, dims: int = 16) -> np.ndarray:
    """Deterministic: a family direction plus a text-specific perturbation, so
    descriptions within a family are close and across families are not."""
    family_seed = int(hashlib.sha256(family.encode()).hexdigest()[:8], 16)
    text_seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
    base = np.random.default_rng(family_seed).normal(size=dims)
    noise = np.random.default_rng(text_seed).normal(size=dims)
    return (base + 0.6 * noise).astype(np.float32)


EMBEDDINGS = {
    normalise_description(text): _vector(text, family)
    for family, texts in FAMILIES.items()
    for text in texts
}


def _material(rng: random.Random, i: int, *, donor: bool) -> CandidateAttributes:
    return CandidateAttributes(
        sap_material_number=f"{'D' if donor else 'T'}{i:06d}",
        sap_plant_code=rng.choice(["1300", "1500"]),
        criticality=rng.choice(CRITICALITIES),
        is_active=rng.choice([True, True, True, True, False, None]),
        history_months=rng.choice([24, 36, 18, 12, 11, None]) if donor else rng.choice([0, 3, None]),
        material_group=rng.choice(GROUPS),
        equipment_type=None,
        base_unit_of_measure=rng.choice(UOMS),
        circuit=rng.choice(["Milling", "Crushing", None]),
        manufacturer=rng.choice([None, None, None, "SKF", "ACME"]),
        unit_price=rng.choice([None, Decimal(rng.randint(10, 5000))]),
        description=rng.choice(ALL_TEXTS + [None, "   "]),
    )


def _population(seed: int, donors: int, targets: int):
    rng = random.Random(seed)
    population = [_material(rng, i, donor=True) for i in range(donors)]
    target_list = [_material(rng, i, donor=False) for i in range(targets)]
    inventory = {
        (d.sap_material_number, d.sap_plant_code): {
            "safety_stock": rng.randint(0, 40),
            "rop": rng.randint(0, 80),
            "max_stock": rng.randint(10, 200),
        }
        for d in population
        if rng.random() < 0.6
    }
    return population, target_list, inventory


def _price_range(population):
    from app.initiatives.i7.oar import repository

    return repository.price_range(population)


def _summary(result: OarResult) -> tuple:
    """Everything that must match between retrieval modes. Retrieval metadata
    (shortlist size, mode) and the generation timestamp are excluded -- they
    describe the run, not the answer."""
    return (
        result.sap_material_number,
        result.sap_plant_code,
        result.status,
        result.confidence,
        result.candidates_considered,
        result.eligible_candidates,
        result.rejection_counts,
        result.best_similarity,
        result.low_evidence_candidates,
        tuple(
            (
                n.material, n.plant, n.rank, n.score.combined,
                n.score.structured.value, n.score.text.value, n.score.business.value,
                n.score.score_completeness, n.score.features_used,
                n.inventory_eligible, n.safety_stock, n.rop, n.max_stock,
            )
            for n in result.neighbours
        ),
        (
            result.estimate.status, result.estimate.safety_stock, result.estimate.rop,
            result.estimate.max_stock, result.estimate.qualifying_neighbours,
            result.estimate.inventory_eligible_neighbours,
            result.estimate.inventory_ineligible_neighbours, result.estimate.trace,
        ),
    )


DEV_POLICY = PolicyDocument(
    service_level=load_mock_service_level_policy(),
    max_stock=load_mock_max_stock_policy(),
)
LOW_FLOOR_POLICY = PolicyDocument(
    service_level=load_mock_service_level_policy(),
    max_stock=load_mock_max_stock_policy(),
    similarity=SimilarityPolicy(minimum_similarity=0.30),
)
"""A lower admission floor admits many more neighbours, so the comparison
covers full Top-10 lists and weighted estimates, not mostly empty ones."""

TIGHT = RetrievalConfig(
    structured_pool=10, semantic_pool=10, fused_pool=25, exhaustive_below=0
)


def _compare(seed: int, policy: PolicyDocument, embeddings, config=TIGHT, donors=480, targets=60):
    population, target_list, inventory = _population(seed, donors, targets)
    price_span = _price_range(population)
    provider = PrecomputedEmbeddingProvider(embeddings)

    stats = engine.EngineStats()
    two_stage = list(
        engine.evaluate_targets(
            target_list, population, provider=provider, embeddings=embeddings,
            price_range=price_span, inventory_values=inventory, policy=policy,
            config=config, stats=stats,
        )
    )
    exhaustive = [
        service.evaluate_target(t, population, provider, price_span, inventory, policy)
        for t in target_list
    ]
    return two_stage, exhaustive, stats


# --- The central claim ------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize(
    "policy", [PolicyDocument(), DEV_POLICY, LOW_FLOOR_POLICY], ids=["production", "dev", "low-floor"]
)
def test_two_stage_matches_exhaustive_with_text(seed, policy) -> None:
    two_stage, exhaustive, stats = _compare(seed, policy, EMBEDDINGS)

    assert [_summary(r) for r in two_stage] == [_summary(r) for r in exhaustive]
    assert stats.scored_fraction < 0.6  # and it really did skip most exact scores


@pytest.mark.parametrize("seed", [4, 5])
@pytest.mark.parametrize("policy", [DEV_POLICY, LOW_FLOOR_POLICY], ids=["dev", "low-floor"])
def test_two_stage_matches_exhaustive_without_a_model(seed, policy) -> None:
    """No embedding model: the text dimension is unavailable for every pair,
    in both paths, and the answers still agree."""
    two_stage, exhaustive, _ = _compare(seed, policy, None)

    assert [_summary(r) for r in two_stage] == [_summary(r) for r in exhaustive]
    for result in two_stage:
        for neighbour in result.neighbours:
            assert neighbour.score.text.value is None
            assert neighbour.score.text.status.value == "NOT_AVAILABLE_NO_MODEL"


def test_the_low_floor_comparison_is_not_vacuous() -> None:
    """Guards the guard: the comparison has to include real neighbour lists
    and real estimates, or agreement proves nothing."""
    two_stage, _, _ = _compare(1, LOW_FLOOR_POLICY, EMBEDDINGS)
    assert sum(len(r.neighbours) for r in two_stage) > 100
    assert any(r.estimate.status.value == "SUCCESS" for r in two_stage)
    assert any(len(r.neighbours) == 10 for r in two_stage)  # the Top-10 cap is reached


def test_default_pools_also_match() -> None:
    two_stage, exhaustive, _ = _compare(
        6, LOW_FLOOR_POLICY, EMBEDDINGS, config=RetrievalConfig(exhaustive_below=0)
    )
    assert [_summary(r) for r in two_stage] == [_summary(r) for r in exhaustive]


def test_small_partitions_are_scored_exhaustively() -> None:
    """At or below ``exhaustive_below`` retrieval would save nothing."""
    two_stage, exhaustive, stats = _compare(
        7, DEV_POLICY, EMBEDDINGS, config=RetrievalConfig(exhaustive_below=10_000)
    )
    assert [_summary(r) for r in two_stage] == [_summary(r) for r in exhaustive]
    assert stats.scored_fraction == pytest.approx(1.0)


# --- Eligibility is preserved -----------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_rejection_counts_equal_filter_candidates(seed) -> None:
    population, target_list, _ = _population(seed, 300, 80)
    # Make some targets also appear in the population, so SELF is exercised.
    target_list += population[:5]
    profile = PopulationProfile(population, 12)

    for target in target_list:
        eligible, rejections = eligibility.filter_candidates(target, population, 12)
        count, counted = profile.profile(target)
        assert count == len(eligible)
        assert counted == rejections


def test_the_shortlist_only_ever_contains_eligible_donors() -> None:
    population, target_list, _ = _population(8, 400, 40)
    index = engine.build_index(
        population, price_range=_price_range(population), policy=DEV_POLICY, embeddings=EMBEDDINGS
    )
    for target in target_list:
        partition = index.partition_for(target)
        if partition is None:
            continue
        shortlist = index.shortlist(partition, target, index.encode_target(target), None, TIGHT)
        for j in shortlist.indices:
            donor = partition.donors[j]
            assert eligibility.reject_reason(target, donor, 12) is None


def test_a_target_is_never_its_own_neighbour() -> None:
    population, _, inventory = _population(9, 300, 0)
    targets = [d for d in population if d.criticality is not None][:10]
    results = list(
        engine.evaluate_targets(
            targets, population, provider=PrecomputedEmbeddingProvider(EMBEDDINGS),
            embeddings=EMBEDDINGS, price_range=_price_range(population),
            inventory_values=inventory, policy=LOW_FLOOR_POLICY, config=TIGHT,
        )
    )
    for target, result in zip(targets, results):
        assert (target.sap_material_number, target.sap_plant_code) not in {
            (n.material, n.plant) for n in result.neighbours
        }
        assert dict(result.rejection_counts).get("SELF") == 1


def test_missing_criticality_on_the_target_finds_nothing() -> None:
    population, _, inventory = _population(10, 200, 0)
    target = population[0]._replace(sap_material_number="TX", criticality=None)
    [result] = list(
        engine.evaluate_targets(
            [target], population, provider=PrecomputedEmbeddingProvider(EMBEDDINGS),
            embeddings=EMBEDDINGS, price_range=_price_range(population),
            inventory_values=inventory, policy=DEV_POLICY, config=TIGHT,
        )
    )
    assert result.status.value == "NO_ELIGIBLE_NEIGHBORS"
    assert dict(result.rejection_counts) == {"CRITICALITY_MISSING_TARGET": len(population)}


# --- The selection machinery ---------------------------------------------------


def test_top_n_takes_ties_in_index_order() -> None:
    """Donors are held in (material, plant) order, so lower index = the one
    ranking would keep. Ties at the cut must resolve to it."""
    scores = np.array([0.5, 0.9, 0.7, 0.7, 0.7, 0.1])
    assert sorted(top_n(scores, 3).tolist()) == [1, 2, 3]
    assert sorted(top_n(scores, 2).tolist()) == [1, 2]


def test_top_n_ignores_unmeasured_scores() -> None:
    scores = np.array([np.nan, 0.4, np.nan, 0.2])
    assert sorted(top_n(scores, 3).tolist()) == [1, 3]


def test_channels_merge_and_deduplicate() -> None:
    """A donor found by more than one channel appears once; a donor found only
    by its description (different material group) is still found."""
    def donor(n, group, text):
        return CandidateAttributes(
            sap_material_number=n, sap_plant_code="1300", criticality="NORMAL",
            is_active=True, history_months=24, material_group=group, equipment_type=None,
            base_unit_of_measure="EA", circuit=None, manufacturer=None,
            unit_price=Decimal(100), description=text,
        )

    bearing = "SKF 6310-2RS deep groove ball bearing"
    population = [donor(f"D{i:03d}", "PMP", "pump impeller 250mm") for i in range(40)]
    population.append(donor("D900", "BRG", "pump impeller 250mm"))   # structured match only
    population.append(donor("D901", "VLV", "ball bearing 6310-2RS"))  # semantic match only
    target = donor("T1", "BRG", bearing)

    index = DonorIndex(
        population, minimum_history_months=12, price_range=Decimal(1000),
        weights=(0.35, 0.30, 0.35), minimum_evidence_features=2,
        identity_evidence_features=("material_group", "description"), embeddings=EMBEDDINGS,
    )
    partition = index.partition_for(target)
    encoding = index.encode_target(target)
    [cosines] = retrieval.cosine_block(partition, [encoding.vector])
    config = RetrievalConfig(structured_pool=1, semantic_pool=1, fused_pool=1, exhaustive_below=0)
    shortlist = index.shortlist(partition, target, encoding, cosines, config)

    chosen = {partition.donors[j].sap_material_number for j in shortlist.indices}
    assert {"D900", "D901"} <= chosen
    assert len(shortlist.indices) == len(set(shortlist.indices.tolist()))


def test_an_unseen_category_is_compared_and_matches_nothing() -> None:
    population, _, inventory = _population(11, 150, 0)
    target = population[0]._replace(
        sap_material_number="TNEW", material_group="NEVER-SEEN", criticality="NORMAL"
    )
    two_stage = list(
        engine.evaluate_targets(
            [target], population, provider=PrecomputedEmbeddingProvider(EMBEDDINGS),
            embeddings=EMBEDDINGS, price_range=_price_range(population),
            inventory_values=inventory, policy=LOW_FLOOR_POLICY, config=TIGHT,
        )
    )
    exhaustive = service.evaluate_target(
        target, population, PrecomputedEmbeddingProvider(EMBEDDINGS),
        _price_range(population), inventory, LOW_FLOOR_POLICY,
    )
    assert _summary(two_stage[0]) == _summary(exhaustive)


def test_scoring_is_limited_to_the_shortlist(monkeypatch) -> None:
    """The work claim, measured directly: exact scores are computed for the
    shortlist only, never for every eligible donor."""
    calls = {"n": 0}
    original = service._score_pair

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_score_pair", counting)
    population, target_list, inventory = _population(12, 600, 30)
    stats = engine.EngineStats()
    list(
        engine.evaluate_targets(
            target_list, population, provider=PrecomputedEmbeddingProvider(EMBEDDINGS),
            embeddings=EMBEDDINGS, price_range=_price_range(population),
            inventory_values=inventory, policy=DEV_POLICY, config=TIGHT, stats=stats,
        )
    )
    per_target_cap = TIGHT.structured_pool + TIGHT.semantic_pool + TIGHT.fused_pool
    assert calls["n"] == stats.shortlisted_pairs
    assert calls["n"] <= per_target_cap * len(target_list)
    assert calls["n"] < stats.eligible_pairs


def test_retrieval_config_is_described_and_read_from_settings() -> None:
    from app.core.config import Settings

    config = RetrievalConfig.from_settings(
        Settings(i7_oar_structured_pool=7, i7_oar_semantic_pool=8, i7_oar_fused_pool=9)
    )
    assert (config.structured_pool, config.semantic_pool, config.fused_pool) == (7, 8, 9)
    assert "structured_pool=7" in config.describe()
    assert RetrievalConfig(mode="exhaustive").describe() == "mode=exhaustive"
