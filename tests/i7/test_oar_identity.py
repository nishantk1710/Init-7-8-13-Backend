"""OAR identity validation (oar-similarity-10): a shared material group is not identity.

The defect: material group 308175 holds filters, spray nozzles, proximity sensors and wrenches, so a target
``FILTER PN:5112305242`` accepted ``FILTER PRESS ANDRITZ SPRAY NOZZLE`` and ``FILTER PRESS ANDRITZ PROXIMITY
SENSOR`` as neighbours on the group alone, and their ROPs went into the estimate.

Every case here is a hand-built pair or population. The text similarities are injected vectors -- they prove the
RULE, not that MiniLM scores real descriptions the way the vectors say; calibrating the floors needs the real
model and expert-reviewed pairs (``oar.evaluation``). The historical production records for the filter example
are not in this repository, so the example is reproduced from a deterministic fixture, not claimed as a
reproduction of production.
"""

from __future__ import annotations

import random
from decimal import Decimal

import numpy as np
import pytest

from app.initiatives.i7.oar import engine, identity, ranking, scoring, service
from app.initiatives.i7.oar.retrieval import DonorIndex, RetrievalConfig, cosine_block
from app.initiatives.i7.oar.text_similarity import PrecomputedEmbeddingProvider
from app.initiatives.i7.oar.types import CandidateAttributes, IdentityStatus
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import (
    load_mock_max_stock_policy,
    load_mock_service_level_policy,
)
from app.initiatives.i7.policy.thresholds import ComponentTypeSpec, SimilarityPolicy

GROUP = "308175"
FLOORS = {"minimum_identity_text_similarity": 0.50, "same_group_minimum_text_similarity": 0.50}
"""The mechanics tests below pin the floors at 0.50, the value they were written around, so they test the RULE at a
known floor. The shipped default (a provisional 0.65) has its own tests at the end of this file."""
TARGET = "FILTER PN:5112305242"
NOZZLE = "FILTER PRESS ANDRITZ SPRAY NOZZLE"
SENSOR = "FILTER PRESS ANDRITZ PROXIMITY SENSOR"


def policy(**similarity) -> PolicyDocument:
    return PolicyDocument(
        service_level=load_mock_service_level_policy(),
        max_stock=load_mock_max_stock_policy(),
        similarity=SimilarityPolicy(
            **{"text_embedding_weight": 1.0, "text_fuzzy_weight": 0.0, **FLOORS, **similarity}
        ),  # injected cosines: the text score is the cosine alone unless a test says otherwise
    )


def unit(cosine: float) -> np.ndarray:
    """A unit vector whose cosine with [1, 0] -- the target's vector -- is ``cosine``."""
    return np.array([cosine, (1 - cosine * cosine) ** 0.5], dtype=np.float32)


def part(
    number: str,
    description: str | None,
    *,
    group: str | None = GROUP,
    criticality: str = "NORMAL",
    material_type: str | None = None,
    price: str = "100",
) -> CandidateAttributes:
    return CandidateAttributes(
        sap_material_number=number,
        sap_plant_code="1300",
        criticality=criticality,
        is_active=True,
        history_months=24,
        material_group=group,
        equipment_type=None,
        base_unit_of_measure="EA",
        circuit=None,
        manufacturer=None,
        unit_price=Decimal(price),
        description=description,
        material_type=material_type,
    )


def verdict(
    target_description: str | None,
    candidate_description: str | None,
    *,
    same_group: bool = True,
    text: str | None = None,
    **similarity,
):
    config = identity.IdentityConfig.from_policy(SimilarityPolicy(**{**FLOORS, **similarity}))
    return identity.assess_descriptions(
        target_description,
        candidate_description,
        same_material_group=same_group,
        text_similarity=None if text is None else Decimal(text),
        config=config,
    )


# --- A. same-group spray nozzle ---------------------------------------------------------------------


def test_a_same_group_spray_nozzle_does_not_pass_on_the_group() -> None:
    result = verdict(TARGET, NOZZLE, text="0.55")

    assert result.status is IdentityStatus.REJECTED_COMPONENT_CONFLICT
    assert not result.passed and result.conflict
    assert result.target_types == ("filter",) and result.candidate_types == ("nozzle",)
    assert "component type conflict" in result.reason


def test_the_nozzle_is_refused_on_the_group_alone_even_without_the_vocabulary() -> None:
    """With component conflict switched off the same-group route still wants description evidence: the group
    cannot carry the candidate by itself."""
    result = verdict(TARGET, NOZZLE, text="0.30", identity_component_conflict=False)

    assert result.status is IdentityStatus.INSUFFICIENT_GROUP_ONLY
    assert "same material group is not identity on its own" in result.reason


# --- B. same-group proximity sensor ---------------------------------------------------------------


def test_a_same_group_proximity_sensor_is_a_component_conflict() -> None:
    result = verdict(TARGET, SENSOR, text="0.50")

    assert result.status is IdentityStatus.REJECTED_COMPONENT_CONFLICT
    assert result.candidate_types == ("proximity_sensor",)


def test_a_conflict_is_a_conflict_whatever_the_text_similarity_or_the_group() -> None:
    assert verdict(TARGET, NOZZLE, text="0.99").conflict
    assert verdict(TARGET, NOZZLE, text="0.99", same_group=False).conflict
    assert verdict(TARGET, SENSOR, text=None, same_group=False).conflict


def test_unknown_is_never_a_conflict() -> None:
    """A description that names no vocabulary type is unknown, not different."""
    for other in ("PN:5112305242 ASSY", "SPARE PART 99", None):
        assert not verdict(TARGET, other, text="0.60").conflict


def test_a_general_sensor_is_compatible_with_its_specific_kind() -> None:
    assert verdict("SENSOR 24V", "PROXIMITY SENSOR 24V", text="0.80").passed
    assert verdict("PROXIMITY SENSOR 24V", "SENSOR 24V", text="0.80").passed


# --- C. genuine equivalents with different wording ---------------------------------------------------


def test_a_genuine_equivalent_sharing_the_part_number_passes_whatever_the_wording() -> None:
    result = verdict(TARGET, "CARTRIDGE FILTER ELEMENT 5112305242 ANDRITZ", text="0.20")

    assert result.status is IdentityStatus.CONFIRMED_PART_NUMBER
    assert result.shared_part_numbers == ("5112305242",)


def test_a_differently_worded_equivalent_of_the_same_type_passes_on_text() -> None:
    result = verdict(
        "OIL FILTER ELEMENT 10 MICRON", "FILTER CARTRIDGE, LUBE OIL, 10 MICRON", text="0.72"
    )

    assert result.status is IdentityStatus.CONFIRMED_GROUP_COMPONENT_TYPE


def test_an_equivalent_with_no_words_in_common_is_not_treated_as_generic_overlap() -> None:
    """BRG against BEARING share nothing -- not even a generic word -- so nothing here suggests the text score
    is generic-driven, and a text score at the floor stands."""
    result = verdict("BRG DEEP GROOVE 6310", "BALL BEARING 6310-2RS SKF", text="0.80")

    assert result.status is IdentityStatus.CONFIRMED_GROUP_TEXT


def test_identical_wording_is_not_required_to_clear_the_floor_exactly() -> None:
    assert verdict("STRAINER BASKET", "FILTER BASKET", text="0.50").passed  # the floor is inclusive
    assert not verdict("STRAINER BASKET", "FILTER BASKET", text="0.49").passed


# --- generic words are not proof ----------------------------------------------------------------


def test_filter_press_andritz_and_filter_alone_are_not_proof_of_equivalence() -> None:
    """High text similarity, same group, nothing specific in common -- the shared words are FILTER."""
    result = verdict(TARGET, "FILTER PRESS ANDRITZ PLATE", text="0.80")

    assert result.status is IdentityStatus.INSUFFICIENT_GENERIC_OVERLAP
    assert "generic words" in result.reason and "FILTER" in result.reason


def test_the_machine_name_is_context_not_a_component_type() -> None:
    vocabulary = identity.Vocabulary()

    assert vocabulary.profile("FILTER PRESS ANDRITZ SPRAY NOZZLE").types == {"nozzle"}
    assert vocabulary.profile("FILTER PRESS").types == frozenset()
    assert vocabulary.profile("FILTER PRESS FILTER ELEMENT").types == {"filter"}
    assert vocabulary.profile("PRESS FILTER PLATE").types == frozenset()


# --- part numbers survive normalisation --------------------------------------------------------------


def test_part_numbers_are_preserved_whole() -> None:
    assert identity.tokenise("FILTER PN:5112305242") == ("FILTER", "PN", "5112305242")
    assert identity.tokenise("BEARING 6310-2RS/C3") == ("BEARING", "6310-2RS/C3")
    assert identity.tokenise("p/n 5112305242") == ("P/N", "5112305242")

    vocabulary = identity.Vocabulary()
    assert vocabulary.profile(TARGET).part_numbers == {"5112305242"}
    assert vocabulary.profile("BEARING 6310-2RS").part_numbers == {"63102RS"}


@pytest.mark.parametrize("token", ["5112305242", "6310-2RS", "ABC-123456", "SKF6310"])
def test_these_are_part_numbers(token: str) -> None:
    assert identity.is_part_number(token)


@pytest.mark.parametrize("token", ["6310", "55KW", "100X50X6", "M12X1.5", "10MM", "FILTER", "12345"])
def test_these_are_specifications_or_words_not_part_numbers(token: str) -> None:
    assert not identity.is_part_number(token)


def test_a_part_number_written_with_separators_still_matches() -> None:
    assert verdict("BEARING 6310-2RS", "DEEP GROOVE 63102RS", text=None).status is (
        IdentityStatus.CONFIRMED_PART_NUMBER
    )
    assert verdict("BEARING 6310-2RS", "DEEP GROOVE 6310-2RS", text=None).passed


def test_plurals_meet_their_singulars() -> None:
    vocabulary = identity.Vocabulary()

    assert vocabulary.profile("SPRAY NOZZLES").types == {"nozzle"}
    assert vocabulary.profile("IMPACT WRENCHES").types == {"wrench"}
    assert vocabulary.profile("BALL VALVES").types == {"valve"}


# --- D. cross-group candidates ---------------------------------------------------------------------


def test_a_genuine_equivalent_from_another_group_with_adequate_text_is_confirmed() -> None:
    result = verdict(TARGET, "FILTER ELEMENT 10 MICRON", same_group=False, text="0.70")

    assert result.status is IdentityStatus.CONFIRMED_TEXT


def test_the_baseline_text_floor_for_another_group_is_unchanged() -> None:
    assert verdict(TARGET, "X", same_group=False, text="0.50").passed
    weak = verdict(TARGET, "X", same_group=False, text="0.49")
    assert weak.status is IdentityStatus.INSUFFICIENT_TEXT
    assert "text similarity 0.49 below 0.5" in weak.reason
    assert verdict(TARGET, "X", same_group=False, text=None).status is IdentityStatus.INSUFFICIENT_NO_TEXT


def test_a_part_number_is_enough_across_groups() -> None:
    assert verdict(TARGET, "5112305242", same_group=False, text=None).status is (
        IdentityStatus.CONFIRMED_PART_NUMBER
    )


# --- the policy switches ------------------------------------------------------------------------------


def test_the_same_group_requirement_can_be_dropped() -> None:
    result = verdict(TARGET, "SPARE PART 99", text="0.10", same_group_minimum_text_similarity=None)

    assert result.status is IdentityStatus.CONFIRMED_GROUP_UNVERIFIED


def test_the_vocabulary_is_configurable() -> None:
    custom = dict(
        identity_component_types=(
            ComponentTypeSpec(name="widget", patterns=("WIDGET",)),
            ComponentTypeSpec(name="gadget", patterns=("GADGET",)),
        )
    )

    assert verdict("WIDGET A", "GADGET A", text="0.90", **custom).conflict
    # the default vocabulary knows neither, so with it they are merely unknown
    assert not verdict("WIDGET A", "GADGET A", text="0.90").conflict


def test_the_generic_terms_and_context_phrases_are_configurable() -> None:
    assert identity.Vocabulary(context_phrases=()).profile("FILTER PRESS PLATE").types == {"filter"}
    assert identity.Vocabulary(generic_terms=()).profile("FILTER PRESS ANDRITZ").distinctive == {
        "FILTER", "PRESS", "ANDRITZ",
    }


def test_a_bad_vocabulary_is_refused() -> None:
    with pytest.raises(ValueError):
        identity.Vocabulary(types=(ComponentTypeSpec(name="a", patterns=("A",), parent="missing"),))
    with pytest.raises(ValueError):
        identity.Vocabulary(
            types=(
                ComponentTypeSpec(name="a", patterns=("A",), parent="b"),
                ComponentTypeSpec(name="b", patterns=("B",), parent="a"),
            )
        )


def test_the_default_vocabulary_has_the_conservative_set_the_task_names() -> None:
    names = {spec.name for spec in identity.DEFAULT_COMPONENT_TYPES}

    assert {"filter", "nozzle", "proximity_sensor", "valve", "wrench"} <= names
    assert len(names) <= identity.MAX_TYPES


# --- the score carries the verdict -----------------------------------------------------------------


VECTORS = {
    TARGET: np.array([1.0, 0.0], dtype=np.float32),
    NOZZLE: unit(0.55),
    SENSOR: unit(0.50),
}
PROVIDER = PrecomputedEmbeddingProvider(VECTORS)


def score(target, candidate, **similarity):
    return service._score_pair(
        target, candidate, PROVIDER, Decimal(5), SimilarityPolicy(**{**FLOORS, **similarity})
    )


def test_a_refused_candidate_keeps_its_scores_and_says_why() -> None:
    result = score(part("T", TARGET), part("N", NOZZLE))

    assert result.combined is not None and result.combined > Decimal("0.60")  # the old rule's view
    assert result.evidence_sufficient is False
    assert result.identity.status is IdentityStatus.REJECTED_COMPONENT_CONFLICT
    assert result.text.value is not None and result.structured.value is not None
    assert "identity=REJECTED_COMPONENT_CONFLICT" in scoring.explain(result)
    assert "component type conflict" in scoring.explain(result)


def test_with_identity_validation_off_the_old_rule_returns_exactly() -> None:
    old = score(part("T", TARGET), part("N", NOZZLE), identity_validation=False)

    assert old.evidence_sufficient is True  # same group: the old rule admitted it
    assert old.identity is None
    assert "identity=" not in scoring.explain(old)


def test_a_score_with_no_verdict_is_not_refused_for_lacking_one() -> None:
    result = score(part("T", TARGET), part("N", NOZZLE), identity_validation=False)

    assert ranking.identity_passed(result)


# --- H. retrieval accounting ---------------------------------------------------------------------------

POPULATION_VECTORS: dict[str, np.ndarray] = {TARGET: np.array([1.0, 0.0], dtype=np.float32)}


def _register(description: str, cosine: float) -> str:
    POPULATION_VECTORS[description] = unit(cosine)
    return description


def accounting_population() -> list[CandidateAttributes]:
    donors: list[CandidateAttributes] = []
    for i in range(6):  # genuine filters: share the part number
        donors.append(part(f"F{i}", _register(f"FILTER ELEMENT 5112305242 {i}", 0.90 - 0.02 * i)))
    for i in range(5):  # same group, a different component
        donors.append(part(f"N{i}", _register(f"{NOZZLE} {i}", 0.55)))
    for i in range(3):
        donors.append(part(f"P{i}", _register(f"{SENSOR} {i}", 0.50)))
    for i in range(4):  # the right kind of part, the wrong criticality: a hard filter
        donors.append(part(f"C{i}", _register(f"FILTER ELEMENT 5112305242 C{i}", 0.88), criticality="CRITICAL"))
    for i in range(5):  # another group, weak text
        donors.append(part(f"U{i}", _register(f"GENERIC ITEM {i}", 0.10), group="G9"))
    for i in range(2):  # same group, only generic words in common
        donors.append(part(f"L{i}", _register(f"FILTER PRESS ANDRITZ PLATE {i}", 0.80)))
    return donors


def _accounting():
    donors = accounting_population()
    return donors, PrecomputedEmbeddingProvider(POPULATION_VECTORS)


def run_two_stage(target, population, **config):
    pol = policy()
    return list(
        engine.evaluate_targets(
            [target], population,
            provider=PrecomputedEmbeddingProvider(POPULATION_VECTORS),
            embeddings=POPULATION_VECTORS, price_range=Decimal(5), inventory_values={}, policy=pol,
            config=RetrievalConfig(exhaustive_below=0, **config),
        )
    )[0]


def test_stage_counts_for_the_two_stage_path() -> None:
    donors, _ = _accounting()
    duplicated = donors + [donors[0], donors[6]]  # F0 and N0 listed twice
    result = run_two_stage(part("T", TARGET), duplicated)
    counts = result.stage_counts

    assert counts.donor_pool == 25 and counts.duplicates_dropped == 2
    assert counts.hard_filter_passes == 21  # four CRITICAL donors fail the criticality filter
    assert counts.identity_conflicts == 8  # five nozzles and three sensors
    assert counts.identity_insufficient == 7  # five weak other-group, two generic-overlap
    # Only donors the identity stage accepts were ever shortlisted, by every channel.
    assert (counts.structure_shortlist, counts.text_shortlist, counts.fused_shortlist) == (6, 6, 6)
    assert counts.merged_candidates == 6 == result.shortlisted_candidates
    assert counts.identity_passes == 6 and counts.score_floor_passes == 6
    assert counts.final_neighbours == 5 == len(result.neighbours)
    assert result.eligible_candidates == 21 and result.candidates_considered == 25


def test_the_exhaustive_path_counts_the_same_refusals_and_has_no_channels() -> None:
    donors, provider = _accounting()
    result = service.evaluate_target(
        part("T", TARGET), donors + [donors[0]], provider, Decimal(5), {}, policy()
    )
    counts = result.stage_counts

    assert counts.donor_pool == 25 and counts.duplicates_dropped == 1
    assert counts.hard_filter_passes == 21
    assert (counts.structure_shortlist, counts.text_shortlist, counts.fused_shortlist) == (None, None, None)
    assert counts.merged_candidates == 21  # everything eligible is scored
    assert counts.identity_conflicts == 8 and counts.identity_insufficient == 7
    assert counts.identity_passes == 6 and counts.final_neighbours == 5


def test_two_stage_and_exhaustive_agree_on_neighbours_and_refusal_counts() -> None:
    donors, provider = _accounting()
    two = run_two_stage(part("T", TARGET), donors)
    exact = service.evaluate_target(part("T", TARGET), donors, provider, Decimal(5), {}, policy())

    assert [(n.material, n.score.combined) for n in two.neighbours] == [
        (n.material, n.score.combined) for n in exact.neighbours
    ]
    for field in ("identity_conflicts", "identity_insufficient", "identity_passes", "final_neighbours"):
        assert getattr(two.stage_counts, field) == getattr(exact.stage_counts, field), field


def test_a_rejected_conflict_cannot_re_enter_through_any_channel() -> None:
    donors, _ = _accounting()
    pol = policy()
    index = engine.build_index(
        donors, price_range=Decimal(5), policy=pol, embeddings=POPULATION_VECTORS
    )
    target = part("T", TARGET)
    partition = index.partition_for(target)
    encoding = index.encode_target(target)
    cosines = cosine_block(partition, [encoding.vector])[0]
    # pools far larger than the partition: every channel would take every donor it was allowed to
    config = RetrievalConfig(structured_pool=500, semantic_pool=500, fused_pool=500, exhaustive_below=0)

    shortlist = index.shortlist(partition, target, encoding, cosines, config)
    picked = {partition.donors[j].sap_material_number for j in shortlist.indices}

    assert picked == {f"F{i}" for i in range(6)}
    assert not picked & ({f"N{i}" for i in range(5)} | {f"P{i}" for i in range(3)})
    assert shortlist.structure_pool == shortlist.text_pool == shortlist.fused_pool == 6


def test_an_ordinary_filter_donor_is_not_lost_to_the_refusals() -> None:
    donors, _ = _accounting()
    result = run_two_stage(part("T", TARGET), donors)

    assert {n.material for n in result.neighbours} == {f"F{i}" for i in range(5)}


def test_the_refused_are_kept_for_audit_best_first_with_their_reasons() -> None:
    donors, provider = _accounting()
    result = service.evaluate_target(part("T", TARGET), donors, provider, Decimal(5), {}, policy())

    refused = result.rejected_candidates
    assert len(refused) == 10  # bounded
    assert [r.combined for r in refused] == sorted((r.combined for r in refused), reverse=True)
    assert all(r.reason for r in refused)
    statuses = {r.status for r in refused}
    assert statuses <= {s.value for s in IdentityStatus}
    assert "REJECTED_COMPONENT_CONFLICT" in statuses


def test_the_persisted_rejection_summary_names_the_identity_refusals() -> None:
    donors, provider = _accounting()
    result = service.evaluate_target(part("T", TARGET), donors, provider, Decimal(5), {}, policy())

    summary = service._rejection_summary(result)

    assert "CRITICALITY_MISMATCH=4" in summary
    assert "IDENTITY_COMPONENT_CONFLICT=8" in summary
    assert "IDENTITY_INSUFFICIENT_EVIDENCE=7" in summary


def test_fewer_than_five_qualifying_candidates_are_not_padded_to_five() -> None:
    target = part("T", TARGET)
    vectors = {TARGET: np.array([1.0, 0.0], dtype=np.float32), "FILTER ELEMENT 5112305242 A": unit(0.9),
               "FILTER ELEMENT 5112305242 B": unit(0.85), NOZZLE: unit(0.9), SENSOR: unit(0.9)}
    donors = [
        part("A", "FILTER ELEMENT 5112305242 A"),
        part("B", "FILTER ELEMENT 5112305242 B"),
        part("N", NOZZLE),
        part("S", SENSOR),
    ]
    result = service.evaluate_target(
        target, donors, PrecomputedEmbeddingProvider(vectors), Decimal(5), {}, policy()
    )

    assert [n.material for n in result.neighbours] == ["A", "B"]
    assert result.stage_counts.final_neighbours == 2


def test_the_duplicate_donor_guard_in_ranking_keeps_the_best_score() -> None:
    donors, provider = _accounting()
    low = service._score_pair(part("T", TARGET), donors[5], provider, Decimal(5), policy().similarity)
    high = service._score_pair(part("T", TARGET), donors[0], provider, Decimal(5), policy().similarity)
    entries = [("F0", "1300", low, None, {}), ("F0", "1300", high, None, {})]

    kept = ranking.unique_entries(entries)

    assert len(kept) == 1 and kept[0][2] is high
    assert len(ranking.select_top_k(entries, 5)) == 1


# --- the vectorised mask is the per-pair rule -------------------------------------------------------

WORDS = [
    "FILTER", "FILTER ELEMENT", "FILTER PRESS", "ANDRITZ", "SPRAY NOZZLE", "NOZZLE", "PROXIMITY SENSOR",
    "SENSOR", "VALVE", "BALL VALVE", "WRENCH", "BEARING", "BRG", "PLATE", "CLOTH", "SKF", "10 MICRON",
    "6310-2RS", "5112305242", "5112305243", "PN:5112305242", "HOUSING", "SEAL KIT", "PUMP", "ASSY",
    "SPARE", "24V", "100X50X6", "55KW",
]


def _random_description(rng: random.Random) -> str | None:
    if rng.random() < 0.08:
        return None
    return " ".join(rng.sample(WORDS, rng.randint(1, 4)))


def test_the_vectorised_identity_mask_agrees_with_the_per_pair_rule() -> None:
    rng = random.Random(7)
    pool = sorted({d for d in (_random_description(rng) for _ in range(400)) if d})
    vectors: dict[str, np.ndarray] = {}
    for text in pool:  # already single-spaced, so equal to its normalised form
        angle = rng.uniform(0.0, 1.5)
        vectors[text] = np.array([np.cos(angle), np.sin(angle)], dtype=np.float32)

    population = [
        part(
            f"M{i:04d}",
            rng.choice(pool) if rng.random() < 0.97 else None,
            group=rng.choice(["G1", "G2", "G3", None]),
        )
        for i in range(300)
    ]
    config = identity.config_for(policy().similarity)
    index = DonorIndex(
        population, minimum_history_months=12, price_range=Decimal(5), weights=(0.4, 0.4, 0.2),
        minimum_evidence_features=2, identity_evidence_features=("material_group", "description"),
        embeddings=vectors, identity=config,
    )
    partition = index.partitions["NORMAL"]

    checked = 0
    for t in range(40):
        target = part(f"T{t}", rng.choice(pool), group=rng.choice(["G1", "G2", "G3", None]))
        encoding = index.encode_target(target)
        cosines = cosine_block(partition, [encoding.vector])[0]
        scores = index.prescore(partition, encoding, cosines)
        for j, donor in enumerate(partition.donors):
            text = None
            if partition.has_vector[j]:
                text = Decimal(str(round(min(max(float(cosines[j]), 0.0), 1.0), 6)))
                if abs(text - Decimal("0.5")) < Decimal("0.001"):
                    continue  # a float32 cosine and a six-place Decimal may straddle the floor exactly
            exact = identity.assess_descriptions(
                target.description,
                donor.description,
                same_material_group=bool(
                    target.material_group and donor.material_group
                    and target.material_group == donor.material_group
                ),
                text_similarity=text,
                config=config,
            )
            where = (target.description, donor.description, exact.status)
            assert bool(scores["identity_pass"][j]) == exact.passed, where
            assert bool(scores["identity_conflict"][j]) == exact.conflict, where
            checked += 1
    assert checked > 5000  # a real sweep, not a handful of lucky pairs


# --- where the reported counts come from -----------------------------------------------------------------


def test_the_scored_count_is_the_union_of_three_channels_not_the_eligible_pool() -> None:
    """The shape behind the reported 2,045 donors / 1,598 eligible / 149 scored: the donor pool is everything
    with something to lend, the eligible count is what the hard filters leave, and above ``exhaustive_below``
    only the merged union of the structured (50), semantic (50) and fused (100) picks is scored exactly."""
    rng = random.Random(3)
    vectors: dict[str, np.ndarray] = {TARGET: np.array([1.0, 0.0], dtype=np.float32)}
    donors = []
    for i in range(1600):
        description = f"FILTER ELEMENT 5112305242 V{i}"
        vectors[description] = unit(rng.uniform(0.55, 0.99))
        donors.append(part(f"D{i:04d}", description, price=str(rng.choice([80, 90, 100, 110, 120]))))

    [result] = list(
        engine.evaluate_targets(
            [part("T", TARGET)], donors, provider=PrecomputedEmbeddingProvider(vectors), embeddings=vectors,
            price_range=Decimal(5), inventory_values={}, policy=policy(), config=RetrievalConfig(),
        )
    )
    counts = result.stage_counts

    assert counts.donor_pool == counts.hard_filter_passes == 1600
    assert result.retrieval == "two_stage"  # 1,600 is above exhaustive_below (200)
    assert (counts.structure_shortlist, counts.text_shortlist, counts.fused_shortlist) == (50, 50, 100)
    assert 100 <= counts.merged_candidates <= 200  # the union of the three, which overlap
    assert counts.merged_candidates == result.shortlisted_candidates < 1600
    assert counts.identity_passes == counts.merged_candidates  # all of them are filters
    assert counts.final_neighbours == len(result.neighbours) == 5


# --- the shipped default floor: a provisional 0.65 ----------------------------------------------------------


def default_verdict(target_description, candidate_description, *, same_group, text):
    config = identity.IdentityConfig.from_policy(SimilarityPolicy())
    return identity.assess_descriptions(
        target_description, candidate_description, same_material_group=same_group,
        text_similarity=Decimal(text), config=config,
    )


def test_both_identity_floors_default_to_a_provisional_point_six_five() -> None:
    config = identity.IdentityConfig.from_policy(SimilarityPolicy())

    assert config.text_floor == Decimal("0.65") and config.same_group_floor == Decimal("0.65")
    assert SimilarityPolicy().text_floors_calibrated is False


@pytest.mark.parametrize(
    "target, candidate, text",
    [
        # the first picks the BGE + fuzzy score let through at 0.50, from the real review sample
        ("TAPER LOCK BUSH 3535 85MM FENNER", "NUT SELF LOCKING M64 GR 8", "0.528"),
        ("TAPER LOCK BUSH 3535 85MM FENNER", "HOUSING BEARING 513-60 MM SKF PLUMMER BL", "0.533"),
        ("BUSHING HALF PN:3128310422", "HOSE FEED/FILTRATE PFH PNP4113821900", "0.543"),
        ("BUSHING HALF PN:3128310422", "CRUSHER JAW METSO BOLT HEXAGON N01532895", "0.528"),
    ],
)
def test_the_default_floor_refuses_the_weak_cross_group_picks_that_0_50_let_through(target, candidate, text) -> None:
    refused = default_verdict(target, candidate, same_group=False, text=text)
    admitted_at_old_floor = verdict(target, candidate, same_group=False, text=text)

    assert admitted_at_old_floor.passed  # at 0.50 these were neighbours
    assert refused.status is IdentityStatus.INSUFFICIENT_TEXT and "below 0.65" in refused.reason


def test_the_default_floor_keeps_the_scores_that_looked_like_genuine_matches() -> None:
    """Filter oil 0.664, pump wear plate 0.680, gland assembly 0.703 -- plausible matches, kept at 0.65."""
    for text in ("0.664", "0.680", "0.703"):
        assert default_verdict("PUMP WARMAN 6/4 E- AHF RUBBER - COVER PL", "PUMP LSA-S WEAR PLATE", same_group=True,
                               text=text).passed


def test_the_provisional_floor_costs_recall_and_the_tests_say_so() -> None:
    """Filter elements at 0.546 and 0.572 look plausible and are now refused. A provisional floor trades recall
    for precision; the expert review decides whether that trade is right."""
    for text in ("0.546", "0.572"):
        result = default_verdict("FILTER PN:5112305242", "FILTER ELEMENT F495 MM 48 MM", same_group=False, text=text)
        assert not result.passed
