"""The one-way donor-tier rule for OAR matching.

A NORMAL target may borrow from NORMAL, IMPACT, CRITICAL and INSURANCE donors. IMPACT, CRITICAL and INSURANCE
targets borrow from their own tier only, and none of them may borrow from a lower one -- lending a NORMAL part's
stock values to a CRITICAL part would understate its cover. The rule is a policy table
(``SimilarityPolicy.donor_criticality_tiers``), and an empty table restores same-tier matching everywhere.

What is proved here:

* the per-pair rule (``eligibility.reject_reason``),
* the closed-form rejection counts (``PopulationProfile``) equal the per-pair filter under the wider rule,
* the two-stage engine and exhaustive scoring agree under it, and
* an unknown tier on either side is still a rejection.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from app.initiatives.i7.oar import eligibility, engine, service
from app.initiatives.i7.oar.retrieval import PopulationProfile, RetrievalConfig
from app.initiatives.i7.oar.text_similarity import PrecomputedEmbeddingProvider
from app.initiatives.i7.oar.types import ALGORITHM_VERSION, CandidateAttributes, EligibilityRejection
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.thresholds import SimilarityPolicy

from tests.i7.test_oar_retrieval import EMBEDDINGS, _population, _price_range

HISTORY = 12
ONE_WAY = SimilarityPolicy().donor_tier_map()
SAME_TIER = SimilarityPolicy(donor_criticality_tiers=()).donor_tier_map()


def _material(key: str, tier: str | None) -> CandidateAttributes:
    return CandidateAttributes(
        sap_material_number=key, sap_plant_code="1300", criticality=tier,
        is_active=True, history_months=24, material_group="G", equipment_type=None,
        base_unit_of_measure="EA", circuit=None, manufacturer=None,
        unit_price=Decimal(100), description="taper lock bush 2517", material_type="ERSA",
    )


# --- The policy table -------------------------------------------------------------


def test_the_default_rule_is_one_way_and_only_for_normal_targets() -> None:
    policy = SimilarityPolicy()
    assert policy.donor_tiers_for("NORMAL") == {"NORMAL", "IMPACT", "CRITICAL", "INSURANCE"}
    assert policy.donor_tiers_for("IMPACT") == {"IMPACT"}
    assert policy.donor_tiers_for("CRITICAL") == {"CRITICAL"}
    assert policy.donor_tiers_for("INSURANCE") == {"INSURANCE"}


def test_an_empty_table_restores_same_tier_matching() -> None:
    policy = SimilarityPolicy(donor_criticality_tiers=())
    for tier in ("NORMAL", "IMPACT", "CRITICAL", "INSURANCE"):
        assert policy.donor_tiers_for(tier) == {tier}


def test_the_targets_own_tier_is_always_allowed() -> None:
    policy = SimilarityPolicy(donor_criticality_tiers=(("NORMAL", ("IMPACT",)),))
    assert policy.donor_tiers_for("NORMAL") == {"NORMAL", "IMPACT"}


def test_a_misspelt_tier_is_refused_not_silently_ignored() -> None:
    with pytest.raises(ValueError):
        SimilarityPolicy(donor_criticality_tiers=(("NORMAL", ("CRITCAL",)),))
    with pytest.raises(ValueError):
        SimilarityPolicy(donor_criticality_tiers=(("NORMEL", ("NORMAL",)),))


def test_a_target_tier_listed_twice_is_refused() -> None:
    with pytest.raises(ValueError):
        SimilarityPolicy(donor_criticality_tiers=(("NORMAL", ("IMPACT",)), ("NORMAL", ("CRITICAL",))))


def test_the_algorithm_version_moved_so_old_runs_are_not_reused() -> None:
    assert ALGORITHM_VERSION == "oar-similarity-13"


# --- The per-pair rule -------------------------------------------------------------


@pytest.mark.parametrize("donor_tier", ["NORMAL", "IMPACT", "CRITICAL", "INSURANCE"])
def test_a_normal_target_may_borrow_from_every_non_obsolete_tier(donor_tier) -> None:
    assert eligibility.reject_reason(_material("T", "NORMAL"), _material("D", donor_tier), HISTORY, ONE_WAY) is None


def test_a_normal_target_still_cannot_borrow_from_obsolete() -> None:
    reason = eligibility.reject_reason(_material("T", "NORMAL"), _material("D", "OBSOLETE"), HISTORY, ONE_WAY)
    assert reason is EligibilityRejection.CRITICALITY_MISMATCH


@pytest.mark.parametrize("target_tier", ["IMPACT", "CRITICAL", "INSURANCE"])
def test_a_higher_tier_target_never_borrows_from_normal(target_tier) -> None:
    reason = eligibility.reject_reason(_material("T", target_tier), _material("D", "NORMAL"), HISTORY, ONE_WAY)
    assert reason is EligibilityRejection.CRITICALITY_MISMATCH


@pytest.mark.parametrize(
    ("target_tier", "donor_tier"),
    [("IMPACT", "CRITICAL"), ("IMPACT", "INSURANCE"), ("CRITICAL", "IMPACT"), ("CRITICAL", "INSURANCE"),
     ("INSURANCE", "IMPACT"), ("INSURANCE", "CRITICAL")],
)
def test_the_other_tiers_do_not_cross_with_each_other(target_tier, donor_tier) -> None:
    reason = eligibility.reject_reason(_material("T", target_tier), _material("D", donor_tier), HISTORY, ONE_WAY)
    assert reason is EligibilityRejection.CRITICALITY_MISMATCH


@pytest.mark.parametrize("tier", ["NORMAL", "IMPACT", "CRITICAL", "INSURANCE"])
def test_same_tier_is_still_allowed_for_every_tier(tier) -> None:
    assert eligibility.reject_reason(_material("T", tier), _material("D", tier), HISTORY, ONE_WAY) is None


def test_an_unknown_tier_is_still_a_rejection_on_either_side() -> None:
    assert (
        eligibility.reject_reason(_material("T", None), _material("D", "IMPACT"), HISTORY, ONE_WAY)
        is EligibilityRejection.CRITICALITY_MISSING_TARGET
    )
    assert (
        eligibility.reject_reason(_material("T", "NORMAL"), _material("D", None), HISTORY, ONE_WAY)
        is EligibilityRejection.CRITICALITY_MISSING_CANDIDATE
    )


def test_without_a_table_the_rule_is_the_old_same_tier_rule() -> None:
    reason = eligibility.reject_reason(_material("T", "NORMAL"), _material("D", "IMPACT"), HISTORY)
    assert reason is EligibilityRejection.CRITICALITY_MISMATCH


def test_the_other_gates_still_apply_to_a_borrowed_donor() -> None:
    inactive = _material("D", "CRITICAL")._replace(is_active=False)
    short = _material("D2", "CRITICAL")._replace(history_months=3)
    other_type = _material("D3", "CRITICAL")._replace(material_type="HIBE")
    target = _material("T", "NORMAL")
    assert eligibility.reject_reason(target, inactive, HISTORY, ONE_WAY) is EligibilityRejection.INACTIVE
    assert eligibility.reject_reason(target, short, HISTORY, ONE_WAY) is EligibilityRejection.INSUFFICIENT_HISTORY
    assert eligibility.reject_reason(target, other_type, HISTORY, ONE_WAY) is EligibilityRejection.MATERIAL_TYPE_MISMATCH


# --- The closed-form counts equal the per-pair filter ----------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("donor_tiers", [ONE_WAY, SAME_TIER])
def test_rejection_counts_equal_filter_candidates(seed, donor_tiers) -> None:
    population, targets, _ = _population(seed, 300, 80)
    targets += population[:5]  # SELF is exercised
    profile = PopulationProfile(population, HISTORY, donor_tiers)

    for target in targets:
        eligible, rejections = eligibility.filter_candidates(target, population, HISTORY, donor_tiers)
        count, counted = profile.profile(target)
        assert count == len(eligible)
        assert counted == rejections


def test_the_wider_rule_only_adds_donors_for_normal_targets() -> None:
    population, targets, _ = _population(4, 400, 120)
    wide = PopulationProfile(population, HISTORY, ONE_WAY)
    narrow = PopulationProfile(population, HISTORY, SAME_TIER)
    grew = 0
    for target in targets:
        wide_count, _ = wide.profile(target)
        narrow_count, _ = narrow.profile(target)
        if target.criticality == "NORMAL":
            assert wide_count >= narrow_count
            grew += wide_count > narrow_count
        else:
            assert wide_count == narrow_count
    assert grew > 0  # the rule actually widened something on this population


# --- The engine agrees with exhaustive scoring under the rule ------------------------


def _summary(result) -> tuple:
    return (
        result.sap_material_number, result.sap_plant_code, result.status, result.confidence,
        result.candidates_considered, result.eligible_candidates,
        tuple((n.material, n.plant, n.rank, n.score.combined) for n in result.neighbours),
        tuple(sorted(dict(result.rejection_counts).items())),
    )


@pytest.mark.parametrize("seed", [5, 6])
def test_two_stage_and_exhaustive_agree_under_the_one_way_rule(seed) -> None:
    population, targets, inventory = _population(seed, 500, 60)
    policy = PolicyDocument(similarity=SimilarityPolicy(minimum_similarity=0.0, minimum_neighbours=1))
    provider = PrecomputedEmbeddingProvider(EMBEDDINGS)
    price_range = _price_range(population)

    two_stage = list(
        engine.evaluate_targets(
            targets, population, provider=provider, embeddings=EMBEDDINGS, price_range=price_range,
            inventory_values=inventory, policy=policy, config=RetrievalConfig(exhaustive_below=0),
        )
    )
    exhaustive = [
        service.evaluate_target(t, population, provider, price_range, inventory, policy) for t in targets
    ]
    assert [_summary(r) for r in two_stage] == [_summary(r) for r in exhaustive]


def test_a_borrowed_higher_tier_donor_can_become_a_normal_targets_neighbour() -> None:
    rng = random.Random(7)
    donors = [
        _material(f"D{i}", tier)._replace(sap_plant_code="1500", unit_price=Decimal(rng.randint(50, 150)))
        for i, tier in enumerate(["CRITICAL", "IMPACT", "INSURANCE", "NORMAL"])
    ]
    target = _material("T", "NORMAL")
    inventory = {(d.sap_material_number, d.sap_plant_code): {"safety_stock": 2, "rop": 5, "max_stock": 12} for d in donors}
    provider = PrecomputedEmbeddingProvider(None)  # no model: the text dimension is unavailable
    policy = PolicyDocument(similarity=SimilarityPolicy(minimum_neighbours=1))

    wide =service.evaluate_target(target, donors, provider, Decimal(100), inventory, policy)
    narrow = service.evaluate_target(
        target, donors, provider, Decimal(100), inventory,
        PolicyDocument(similarity=SimilarityPolicy(minimum_neighbours=1, donor_criticality_tiers=())),
    )
    assert wide.eligible_candidates == 4
    assert narrow.eligible_candidates == 1


def test_a_critical_target_is_never_offered_a_normal_donor() -> None:
    donors = [_material("D1", "NORMAL"), _material("D2", "IMPACT"), _material("D3", "CRITICAL")]
    target = _material("T", "CRITICAL")
    inventory = {(d.sap_material_number, d.sap_plant_code): {"safety_stock": 2, "rop": 5, "max_stock": 12} for d in donors}
    policy = PolicyDocument(similarity=SimilarityPolicy(minimum_neighbours=1))
    result = service.evaluate_target(target, donors, PrecomputedEmbeddingProvider(None), Decimal(100), inventory, policy)
    assert result.eligible_candidates == 1
    assert dict(result.rejection_counts)["CRITICALITY_MISMATCH"] == 2
