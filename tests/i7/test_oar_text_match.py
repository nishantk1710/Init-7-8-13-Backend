"""OAR text similarity (oar-similarity-11): 0.6 x BGE cosine + 0.4 x rapidfuzz (set + sort) / 2, over normalised text.

The embedding vectors here are injected, so these tests prove the FORMULA and its wiring -- normalisation, the
blend, that the retrieval pre-score and the exact score agree, that the shortlist's semantic channel ranks by the
cosine alone -- not that BGE puts real spare-part descriptions in the right places. That needs the real model and
expert-reviewed pairs (``scripts.oar_identity_review``).
"""

from __future__ import annotations

import random
import sys
from decimal import Decimal

import numpy as np
import pytest

from app.initiatives.i7.oar import engine, scoring, service, text_match, text_similarity
from app.initiatives.i7.oar.retrieval import DonorIndex, RetrievalConfig, cosine_block
from app.initiatives.i7.oar.text_similarity import PrecomputedEmbeddingProvider
from app.initiatives.i7.oar.types import CandidateAttributes, SimilarityStatus
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import (
    load_mock_max_stock_policy,
    load_mock_service_level_policy,
)
from app.initiatives.i7.policy.thresholds import SimilarityPolicy

D = Decimal


# --- normalisation -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("BUSH,REDUCING  1 X 3/4 BSP", "BUSHING REDUCING 1 X 3/4 BSP"),
        ("pump warman cover pl", "PUMP WARMAN COVER PLATE"),
        ("BEARING ASSY,SKF", "BEARING ASSEMBLY SKF"),
        ("BRG 6310-2RS", "BEARING 6310-2RS"),
        ("FILTER,HYD,BARLOWS,FORKLIFT,MM:H2.50XM", "FILTER HYD BARLOWS FORKLIFT MM:H2.50XM"),
        ("  FILTER   PN:5112305242 ", "FILTER PN:5112305242"),
    ],
)
def test_descriptions_are_normalised_before_either_signal(raw: str, expected: str) -> None:
    assert text_match.normalise_for_match(raw) == expected


def test_abbreviations_are_expanded_as_whole_words_only() -> None:
    """BUSHING is not BUSHINGING, PLATE is not PLATEE, and PL inside PLANT or PLUG is left alone."""
    assert text_match.normalise_for_match("BUSHING PLATE PLANT PLUG ASSEMBLY BEARING") == (
        "BUSHING PLATE PLANT PLUG ASSEMBLY BEARING"
    )
    assert text_match.normalise_for_match("BUSH PL") == "BUSHING PLATE"


def test_a_comma_becomes_a_space_it_is_never_deleted() -> None:
    """Deleting the comma would glue the words into LINERSUCTION and lose both tokens. It is replaced by a space,
    so LINER and SUCTION stay separate -- in the normaliser the text score uses and in the identity tokeniser."""
    from app.initiatives.i7.oar import identity

    description = "LINER,SUCTION PN:2523447"

    assert text_match.normalise_for_match(description) == "LINER SUCTION PN:2523447"
    assert text_match.normalise_for_match(description).split(" ")[:2] == ["LINER", "SUCTION"]
    assert "LINERSUCTION" not in text_match.normalise_for_match(description)
    assert identity.tokenise(description) == ("LINER", "SUCTION", "PN", "2523447")
    assert {"LINER", "SUCTION"} <= identity.Vocabulary().profile(description).distinctive
    # a run of commas and spaces is still one separator
    assert text_match.normalise_for_match("LINER,, SUCTION ,PN:2523447") == "LINER SUCTION PN:2523447"


def test_nothing_to_normalise_is_none() -> None:
    assert text_match.normalise_for_match(None) is None
    assert text_match.normalise_for_match("  ,, ") is None


def test_the_abbreviations_asked_for_are_the_ones_expanded() -> None:
    assert dict(text_match.ABBREVIATIONS) == {
        "BUSH": "BUSHING", "PL": "PLATE", "ASSY": "ASSEMBLY", "BRG": "BEARING",
    }


# --- the fuzzy half ------------------------------------------------------------------------------------


def test_token_set_ratio_ignores_word_order() -> None:
    assert text_match.token_set_ratio("COVER PLATE WARMAN PUMP", "PUMP WARMAN COVER PLATE") == 100


def test_the_fuzzy_part_is_the_average_of_the_set_and_sort_ratios() -> None:
    a, b = "COVER PLATE WARMAN PUMP", "PUMP WARMAN COVER SEAL"

    set_ratio, sort_ratio, average = text_match.fuzzy_ratio(a, b)

    assert set_ratio == text_match.token_set_ratio(a, b)
    assert sort_ratio == text_match.token_sort_ratio(a, b)
    assert average == (set_ratio + sort_ratio) / 2
    assert 0 < sort_ratio < set_ratio < 100  # the two ratios genuinely disagree on this pair


def test_a_word_subset_no_longer_scores_100_but_a_reordering_still_does() -> None:
    """The reason for averaging in token_sort_ratio: FILTER against FILTER PRESS ANDRITZ SPRAY NOZZLE was a
    perfect 100 on the set ratio alone; every extra word now counts against it."""
    subset = text_match.fuzzy_ratio("FILTER", "FILTER PRESS ANDRITZ SPRAY NOZZLE")
    reordered = text_match.fuzzy_ratio("COVER PLATE WARMAN PUMP", "PUMP WARMAN COVER PLATE")

    assert subset[0] == 100  # the set ratio alone is fooled...
    assert subset[1] == pytest.approx(30.8, abs=0.1)  # ...the sort ratio is not...
    assert subset[2] == pytest.approx(65.4, abs=0.1) and subset[2] < 100  # ...and the average is neither
    assert reordered == (100, 100, 100)


def test_token_set_ratio_is_100_when_one_description_contains_the_other() -> None:
    """The reason a high text score is not identity: FILTER against a filter-press nozzle is a perfect 100.
    The identity stage (component conflict, generic overlap) is what keeps such a pair from being a neighbour."""
    assert text_match.token_set_ratio("FILTER", "FILTER PRESS ANDRITZ SPRAY NOZZLE") == 100


def test_a_missing_description_scores_zero_not_an_error() -> None:
    assert text_match.token_set_ratio(None, "FILTER") == 0
    assert text_match.token_set_ratio("FILTER", "") == 0


def test_the_fuzzy_matrix_is_the_pairwise_ratio_over_100() -> None:
    rng = random.Random(2)
    words = ["PUMP", "WARMAN", "COVER", "PLATE", "SEAL", "6/4", "E", "AH", "FILTER", "BUSHING", "SKF"]
    queries = [" ".join(rng.sample(words, 3)) for _ in range(12)] + [None]
    choices = [" ".join(rng.sample(words, 4)) for _ in range(30)] + [None]

    matrix = text_match.fuzzy_matrix(queries, choices)

    assert matrix.shape == (13, 31)
    for i, q in enumerate(queries):
        for j, c in enumerate(choices):
            assert matrix[i, j] == pytest.approx(
                text_match.fuzzy_ratio(q, c)[2] / 100, abs=1e-6
            )  # (token_set_ratio + token_sort_ratio) / 2 / 100


# --- the blend -----------------------------------------------------------------------------------------


def test_the_text_score_is_point_six_cosine_plus_point_four_fuzzy() -> None:
    assert text_match.combine(D("0.8"), 50.0, D("0.6"), D("0.4")) == D("0.680000")  # 0.48 + 0.20
    assert text_match.combine(D("1"), 100.0, D("0.6"), D("0.4")) == D(1)
    assert text_match.combine(D("0"), 0.0, D("0.6"), D("0.4")) == D(0)


def test_the_blend_stays_inside_zero_and_one() -> None:
    assert text_match.combine(D("0.9"), 100.0, D("0.9"), D("0.9")) == D(1)


class FakeProvider:
    is_available = True
    model_name = "fake"
    model_version = "fake-1"

    def __init__(self, vectors):
        self.vectors = vectors

    def encode(self, texts):
        return [self.vectors[t] for t in texts]


def test_the_score_blends_the_cosine_and_the_fuzzy_ratio_and_keeps_both_parts() -> None:
    """cosine 0.8; 'COVER PLATE' vs 'COVER PL' both normalise to COVER PLATE, ratio 100 -> 0.6x0.8 + 0.4x1 = 0.88."""
    vectors = {"COVER PLATE": [1.0, 0.0], "COVER PL": [0.8, 0.6]}

    result = text_similarity.score(
        "COVER PLATE", "COVER PL", FakeProvider(vectors), embedding_weight=D("0.6"), fuzzy_weight=D("0.4")
    )

    assert result.status is SimilarityStatus.AVAILABLE
    assert result.value == D("0.880000")
    assert dict(result.components) == {"embedding": "0.8", "fuzzy_set": "1.0000", "fuzzy_sort": "1.0000"}


def test_a_fuzzy_ratio_of_zero_pulls_the_score_down() -> None:
    vectors = {"AAA": [1.0, 0.0], "ZZZ": [1.0, 0.0]}  # identical vectors, no character in common

    result = text_similarity.score("AAA", "ZZZ", FakeProvider(vectors), D("0.6"), D("0.4"))

    assert result.value == D("0.600000")  # 0.6 x 1.0 + 0.4 x 0


def test_unrelated_words_still_score_a_small_fuzzy_baseline() -> None:
    """token_set_ratio compares characters inside the tokens, so descriptions with no word in common are not 0:
    'PUMP SEAL' against 'ZZZ QQQ' is 12.5. An unrelated pair therefore starts a little above the embedding
    alone, which is one reason the identity floors must be set for THIS score, not carried over."""
    assert 0 < text_match.token_set_ratio("PUMP SEAL", "ZZZ QQQ") < 20
    assert 0 < text_match.fuzzy_ratio("PUMP SEAL", "ZZZ QQQ")[2] < 20


def test_without_a_fuzzy_weight_the_score_is_the_cosine_alone() -> None:
    vectors = {"A B": [1.0, 0.0], "C D": [0.6, 0.8]}

    result = text_similarity.score("A B", "C D", FakeProvider(vectors))

    assert result.value == D("0.6")
    assert result.components == (("embedding", "0.6"),)


def test_no_model_still_means_no_text_dimension() -> None:
    result = text_similarity.score("A", "B", PrecomputedEmbeddingProvider(None), D("0.6"), D("0.4"))

    assert result.value is None and result.status is SimilarityStatus.NOT_AVAILABLE_NO_MODEL


def test_a_missing_description_is_unavailable_not_scored_on_the_other_half() -> None:
    result = text_similarity.score(None, "FILTER", FakeProvider({}), D("0.6"), D("0.4"))

    assert result.value is None and result.status is SimilarityStatus.NOT_AVAILABLE_NO_TEXT


# --- rapidfuzz is required, never silently skipped ---------------------------------------------------


def test_without_rapidfuzz_the_provider_is_unavailable(monkeypatch) -> None:
    class Model:
        def encode(self, *args, **kwargs):  # pragma: no cover - never reached
            raise AssertionError

    monkeypatch.setattr(text_similarity, "_load_model", lambda name: Model())
    assert text_similarity.BgeEmbeddingProvider().is_available is True

    monkeypatch.setattr(text_match, "available", lambda: False)

    assert text_similarity.BgeEmbeddingProvider().is_available is False


def test_the_fuzzy_ratio_fails_loudly_if_the_library_is_missing(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "rapidfuzz", None)

    with pytest.raises(RuntimeError, match="rapidfuzz is required"):
        text_match.token_set_ratio("A", "B")


# --- the policy ------------------------------------------------------------------------------------------


def test_the_policy_defaults_are_the_requested_weights() -> None:
    policy = SimilarityPolicy()

    assert (policy.text_embedding_weight, policy.text_fuzzy_weight) == (0.6, 0.4)
    assert policy.text_floors_calibrated is False  # the 0.50 floors were set for MiniLM


def test_the_text_weights_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match="text_embedding_weight and text_fuzzy_weight"):
        SimilarityPolicy(text_embedding_weight=0.7, text_fuzzy_weight=0.4)
    SimilarityPolicy(text_embedding_weight=1.0, text_fuzzy_weight=0.0)  # the embedding alone is allowed


def test_the_exact_score_uses_the_policys_weights() -> None:
    target = part("T", "COVER PLATE")
    candidate = part("C", "COVER PL")
    provider = PrecomputedEmbeddingProvider({"COVER PLATE": np.array([1.0, 0.0], dtype=np.float32),
                                             "COVER PL": np.array([0.8, 0.6], dtype=np.float32)})

    blended = service._score_pair(target, candidate, provider, D(5), SimilarityPolicy())
    cosine_only = service._score_pair(
        target, candidate, provider, D(5), SimilarityPolicy(text_embedding_weight=1.0, text_fuzzy_weight=0.0)
    )

    assert blended.text.value == D("0.880000")
    assert cosine_only.text.value == D("0.8")
    assert "text_parts=embedding:0.8,fuzzy_set:1.0000,fuzzy_sort:1.0000" in scoring.explain(blended)


# --- the retrieval pre-score is the exact score --------------------------------------------------------


def part(number, description, *, group="G1", criticality="NORMAL", price="100"):
    return CandidateAttributes(
        number, "1300", criticality, True, 24, group, None, "EA", None, None, D(price), description
    )


WORDS = ["PUMP", "WARMAN", "COVER", "PL", "PLATE", "BUSH", "BUSHING", "SEAL", "6/4", "AH", "ASSY", "BRG",
         "FILTER", "ELEMENT", "HYD", "PN:5112305242", "SKF", "6310-2RS", "NOZZLE", "SENSOR"]


def random_population(seed: int, donors: int = 250):
    rng = random.Random(seed)
    descriptions = sorted({" ".join(rng.sample(WORDS, rng.randint(1, 5))) for _ in range(500)})
    vectors = {}
    for d in descriptions:
        v = np.array([rng.gauss(0, 1) for _ in range(16)], dtype=np.float32)
        vectors[d] = v / np.linalg.norm(v)
    population = [
        part(f"D{i:04d}", rng.choice(descriptions), group=rng.choice(["G1", "G2"]),
             price=str(rng.choice([50, 100, 200])))
        for i in range(donors)
    ]
    return rng, descriptions, vectors, population


def test_the_prescore_text_equals_the_exact_text_score_for_every_donor() -> None:
    rng, descriptions, vectors, population = random_population(5)
    policy = SimilarityPolicy()
    index = engine.build_index(
        population, price_range=D(5), policy=PolicyDocument(similarity=policy), embeddings=vectors
    )
    partition = index.partitions["NORMAL"]
    provider = PrecomputedEmbeddingProvider(vectors)

    for t in range(15):
        target = part(f"T{t}", rng.choice(descriptions))
        encoding = index.encode_target(target)
        cosines = cosine_block(partition, [encoding.vector])[0]
        scores = index.prescore(partition, encoding, cosines)
        for j, donor in enumerate(partition.donors):
            exact = text_similarity.score(
                target.description, donor.description, provider,
                D(str(policy.text_embedding_weight)), D(str(policy.text_fuzzy_weight)),
            )
            assert float(scores["text"][j]) == pytest.approx(float(exact.value), abs=2e-4)
            # and the channel the shortlist ranks the semantic pool by is the cosine alone
            assert float(scores["cosine"][j]) == pytest.approx(
                float(min(max(cosines[j], 0.0), 1.0)), abs=1e-6
            )


def test_the_semantic_channel_ranks_by_the_cosine_not_the_blend() -> None:
    """'Top 50 by BGE, then re-scored with the combined formula': a donor with the best cosine but no words in
    common is still shortlisted by the semantic channel, though its blended text score is low."""
    target_text = "PUMP WARMAN COVER PLATE"
    vectors = {target_text: np.array([1.0, 0.0], dtype=np.float32)}
    donors = []
    for i in range(6):  # same words as the target, middling cosine
        text = f"PUMP WARMAN COVER PLATE {i}"
        vectors[text] = np.array([0.5, 0.866], dtype=np.float32)
        donors.append(part(f"A{i}", text))
    vectors["ZZZ QQQ"] = np.array([0.999, 0.045], dtype=np.float32)  # best cosine, shares nothing
    donors.append(part("Z9", "ZZZ QQQ"))
    index = DonorIndex(
        donors, minimum_history_months=12, price_range=D(5), weights=(0.4, 0.4, 0.2),
        minimum_evidence_features=2, identity_evidence_features=("material_group", "description"),
        embeddings=vectors, text_weights=(0.6, 0.4),
    )
    partition = index.partitions["NORMAL"]
    target = part("T", target_text)
    encoding = index.encode_target(target)
    cosines = cosine_block(partition, [encoding.vector])[0]
    config = RetrievalConfig(structured_pool=2, semantic_pool=2, fused_pool=2, exhaustive_below=0)

    scores = index.prescore(partition, encoding, cosines)
    shortlist = index.shortlist(partition, target, encoding, cosines, config)
    picked = {partition.donors[j].sap_material_number for j in shortlist.indices}

    z = next(j for j, d in enumerate(partition.donors) if d.sap_material_number == "Z9")
    assert scores["text"][z] < scores["text"][0]  # the blend ranks it last...
    assert "Z9" in picked  # ...but the semantic channel still takes it, to be re-scored exactly
    assert shortlist.text_pool == 2


def test_two_stage_and_exhaustive_agree_with_the_blended_text_score() -> None:
    rng, descriptions, vectors, population = random_population(9, donors=300)
    policy = PolicyDocument(
        service_level=load_mock_service_level_policy(), max_stock=load_mock_max_stock_policy(),
        similarity=SimilarityPolicy(minimum_similarity=0.30),
    )
    provider = PrecomputedEmbeddingProvider(vectors)
    config = RetrievalConfig(exhaustive_below=0)
    targets = [part(f"T{t}", rng.choice(descriptions), group=rng.choice(["G1", "G2"])) for t in range(8)]

    two_stage = list(engine.evaluate_targets(
        targets, population, provider=provider, embeddings=vectors, price_range=D(5),
        inventory_values={}, policy=policy, config=config,
    ))
    for target, result in zip(targets, two_stage):
        exact = service.evaluate_target(target, population, provider, D(5), {}, policy)
        assert [(n.material, n.score.combined) for n in result.neighbours] == [
            (n.material, n.score.combined) for n in exact.neighbours
        ], target.description
