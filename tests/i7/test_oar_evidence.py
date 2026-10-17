"""OAR similarity: evidence, missing features, circuit, explanations, donor gate.

The rules oar-similarity-2 added or changed, each tested on hand-built pairs so
the arithmetic is visible:

* a score resting on too little evidence is not a neighbour, however high it is;
* a missing value on either side is never a match;
* circuit awards nothing and breaks no tie;
* every neighbour carries an explanation of what entered its score;
* a donor lends only when all three of its Phase 5 statuses are SUCCESS.

The description tests use hand-made vectors. They prove the engine uses
embedding geometry the way it should -- close vectors rank close -- NOT that
MiniLM puts real spare-part descriptions in the right places. That needs the
real model and expert-labelled pairs.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import numpy as np

from app.initiatives.i7.oar import engine, repository, scoring, service
from app.initiatives.i7.oar.retrieval import RetrievalConfig
from app.initiatives.i7.oar.text_similarity import PrecomputedEmbeddingProvider
from app.initiatives.i7.oar.types import CandidateAttributes
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import (
    load_mock_max_stock_policy,
    load_mock_service_level_policy,
)
from app.initiatives.i7.policy.thresholds import SimilarityPolicy

POLICY = PolicyDocument(
    service_level=load_mock_service_level_policy(),
    max_stock=load_mock_max_stock_policy(),
)
NO_TEXT = PrecomputedEmbeddingProvider(None)


def part(
    number: str,
    *,
    group: str | None = "BRG",
    uom: str | None = "EA",
    price: Decimal | None = Decimal(100),
    manufacturer: str | None = None,
    circuit: str | None = None,
    description: str | None = None,
    criticality: str = "NORMAL",
) -> CandidateAttributes:
    return CandidateAttributes(
        sap_material_number=number,
        sap_plant_code="1300",
        criticality=criticality,
        is_active=True,
        history_months=24,
        material_group=group,
        equipment_type=None,
        base_unit_of_measure=uom,
        circuit=circuit,
        manufacturer=manufacturer,
        unit_price=price,
        description=description,
    )


def score(target, candidate, provider=NO_TEXT, policy=POLICY, price_range=Decimal(1000)):
    return service._score_pair(target, candidate, provider, price_range, policy.similarity)


# --- Minimum evidence --------------------------------------------------------


def test_a_perfect_score_on_unit_of_measure_alone_is_not_a_neighbour():
    """The failure renormalisation allows: only UoM is known on both sides, so
    the score renormalises to 1.0 on one weak feature. Thousands of unrelated
    parts share "EA"."""
    target = part("T", group=None, price=None)
    candidate = part("C", group=None, price=None)

    result = score(target, candidate)

    assert result.combined == Decimal(1)
    assert result.features_used == ("base_unit_of_measure",)
    assert result.evidence_sufficient is False


def test_unit_of_measure_and_price_are_not_identity():
    """Two features, but neither says what the part IS."""
    result = score(part("T", group=None), part("C", group=None))
    assert set(result.features_used) == {"base_unit_of_measure", "unit_price"}
    assert result.evidence_sufficient is False
    assert "identity" in result.evidence_detail


def test_material_group_and_unit_of_measure_are_enough():
    result = score(part("T"), part("C"))
    assert result.evidence_sufficient is True


def test_a_description_counts_as_identity_evidence():
    vectors = {"bearing 6310": np.array([1.0, 0.0], dtype=np.float32)}
    provider = PrecomputedEmbeddingProvider(vectors)
    result = score(
        part("T", group=None, description="bearing 6310"),
        part("C", group=None, description="bearing 6310"),
        provider=provider,
    )
    assert "description" in result.features_used
    assert result.evidence_sufficient is True


def test_the_rule_can_be_switched_off_by_policy():
    lenient = PolicyDocument(
        service_level=load_mock_service_level_policy(),
        max_stock=load_mock_max_stock_policy(),
        similarity=SimilarityPolicy(minimum_evidence_features=0, identity_evidence_features=()),
    )
    result = score(part("T", group=None, price=None), part("C", group=None, price=None), policy=lenient)
    assert result.evidence_sufficient is True


def test_low_evidence_candidates_are_counted_and_never_admitted():
    """End to end: five candidates whose only evidence is UoM, five with a real
    material-group match. Only the second five can be neighbours, and the
    result says how many were refused."""
    target = part("T", price=None)
    weak = [part(f"W{i}", group=None, price=None) for i in range(5)]
    strong = [part(f"S{i}", price=None) for i in range(5)]

    result = service.evaluate_target(target, weak + strong, NO_TEXT, None, {}, POLICY)

    assert {n.material for n in result.neighbours} == {f"S{i}" for i in range(5)}
    assert result.low_evidence_candidates == 5


def test_the_target_with_no_identity_evidence_explains_itself():
    target = part("T", group=None, price=None)
    candidates = [part(f"C{i}", group=None, price=None) for i in range(6)]

    result = service.evaluate_target(target, candidates, NO_TEXT, None, {}, POLICY)

    assert result.neighbours == ()
    assert result.low_evidence_candidates == 6
    assert "too little evidence" in result.detail


# --- Missing values are never matches ------------------------------------------


def test_two_missing_material_groups_are_not_a_match():
    result = score(part("T", group=None), part("C", group=None))
    assert "material_group" not in result.features_used
    assert "material_group" in result.features_missing


def test_a_missing_feature_does_not_dilute_the_score():
    """Absent, not zero: with the price missing on one side, the business
    dimension is simply not computed -- it does not drag the score down."""
    with_price = score(part("T"), part("C"))
    without_price = score(part("T"), part("C", price=None))
    assert without_price.business.value is None
    assert without_price.combined == with_price.structured.value


# --- Circuit ---------------------------------------------------------------------


def test_circuit_changes_no_score_and_no_order():
    target = part("T", circuit="Milling")
    same = part("B", circuit="Milling")
    different = part("A", circuit="Crushing")
    unknown = part("C", circuit=None)

    scores = {c.sap_material_number: score(target, c).combined for c in (same, different, unknown)}
    assert len(set(scores.values())) == 1

    result = service.evaluate_target(target, [same, different, unknown], NO_TEXT, Decimal(1000), {}, POLICY)
    assert [n.material for n in result.neighbours] == ["A", "B", "C"]
    assert all(n.same_circuit is None for n in result.neighbours)


def test_two_missing_circuits_are_not_the_same_circuit():
    result = service.evaluate_target(
        part("T", circuit=None), [part("C", circuit=None)], NO_TEXT, Decimal(1000), {}, POLICY
    )
    assert result.neighbours[0].same_circuit is None


# --- Explanations ------------------------------------------------------------------


def test_every_score_explains_what_entered_it():
    explanation = scoring.explain(score(part("T"), part("C")))
    assert explanation.startswith("used=material_group,base_unit_of_measure,unit_price")
    assert "missing=equipment_type,description,manufacturer" in explanation
    assert "weights=structured:" in explanation and "business:" in explanation
    assert "text=NOT_AVAILABLE_NO_MODEL" in explanation
    assert explanation.endswith("evidence=ok")


def test_a_refused_score_says_why():
    explanation = scoring.explain(score(part("T", group=None), part("C", group=None)))
    assert "evidence=low(" in explanation


def test_explanations_fit_the_stored_column():
    assert len(scoring.explain(score(part("T"), part("C")), limit=500)) <= 500


# --- Descriptions -------------------------------------------------------------------


def _description_vectors():
    return {
        "SKF 6310-2RS deep groove ball bearing": np.array([1.00, 0.10, 0.00], dtype=np.float32),
        "ball bearing 6310 2RS, deep groove, SKF": np.array([0.98, 0.15, 0.02], dtype=np.float32),
        "bearing housing for 6310": np.array([0.30, 0.95, 0.00], dtype=np.float32),
        "pump impeller 250mm": np.array([0.00, 0.05, 1.00], dtype=np.float32),
    }


def test_differently_worded_descriptions_of_one_part_rank_first():
    vectors = _description_vectors()
    provider = PrecomputedEmbeddingProvider(vectors)
    target = part("T", description="SKF 6310-2RS deep groove ball bearing")
    reworded = part("A", description="ball bearing 6310 2RS, deep groove, SKF")
    lookalike = part("B", description="bearing housing for 6310")

    result = service.evaluate_target(target, [lookalike, reworded], provider, Decimal(1000), {}, POLICY)

    assert [n.material for n in result.neighbours] == ["A", "B"]
    assert result.neighbours[0].score.text.value > result.neighbours[1].score.text.value


def test_a_superficially_similar_description_does_not_beat_structure():
    """Sharing the token "6310" does not make a housing a bearing: here the
    look-alike also sits in another material group, and ranks below a part in
    the right group whose description is merely unrelated."""
    vectors = _description_vectors()
    provider = PrecomputedEmbeddingProvider(vectors)
    target = part("T", description="SKF 6310-2RS deep groove ball bearing")
    lookalike = part("A", group="HSG", description="bearing housing for 6310")
    same_group = part("B", description="ball bearing 6310 2RS, deep groove, SKF")

    result = service.evaluate_target(target, [lookalike, same_group], provider, Decimal(1000), {}, POLICY)
    assert result.neighbours[0].material == "B"


def test_duplicate_descriptions_and_duplicate_candidates():
    """Many donors share one description; the engine embeds nothing twice and
    the ranking still breaks the tie by material number."""
    vectors = _description_vectors()
    text = "pump impeller 250mm"
    target = part("T", group="PMP", description=text)
    donors = [part(f"D{i:02d}", group="PMP", description=text) for i in range(12)]

    results = list(
        engine.evaluate_targets(
            [target], donors, provider=PrecomputedEmbeddingProvider(vectors), embeddings=vectors,
            price_range=Decimal(1000), inventory_values={}, policy=POLICY,
            config=RetrievalConfig(structured_pool=3, semantic_pool=3, fused_pool=10, exhaustive_below=0),
        )
    )
    assert [n.material for n in results[0].neighbours] == [f"D{i:02d}" for i in range(10)]


def test_without_a_model_the_text_dimension_is_reported_unavailable():
    result = score(part("T", description="x"), part("C", description="x"))
    assert result.text.value is None
    assert result.text.status.value == "NOT_AVAILABLE_NO_MODEL"
    assert "description" in result.features_missing


# --- Donor gate -----------------------------------------------------------------------


def test_a_donor_lends_only_when_all_three_phase_5_statuses_succeeded():
    """Unchanged by this work, and pinned here because the engine relies on it:
    a donor with any of SS, ROP or Max not SUCCESS lends nothing at all -- no
    partial borrowing."""
    rows = [
        SimpleNamespace(sap_material_number="OK", sap_plant_code="1300",
                        safety_stock_status="SUCCESS", safety_stock=5,
                        rop_status="SUCCESS", rop=9, max_stock_status="SUCCESS", max_stock=20),
        SimpleNamespace(sap_material_number="NO_MAX", sap_plant_code="1300",
                        safety_stock_status="SUCCESS", safety_stock=5,
                        rop_status="SUCCESS", rop=9, max_stock_status="NOT_CONFIGURED", max_stock=None),
        SimpleNamespace(sap_material_number="NO_SS", sap_plant_code="1300",
                        safety_stock_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET", safety_stock=None,
                        rop_status="SUCCESS", rop=9, max_stock_status="SUCCESS", max_stock=20),
    ]

    class FakeSession:
        """The first query is the Phase 5 results; the second is the SMOOTH materials' SAP values,
        of which this test has none."""

        calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            return rows if self.calls == 1 else []

    values = repository.load_inventory_values(FakeSession(), inventory_run_id=1)
    assert set(values) == {("OK", "1300")}
    assert values[("OK", "1300")] == {"safety_stock": 5, "rop": 9, "max_stock": 20}


def test_a_neighbour_without_donor_values_stays_a_neighbour_and_lends_nothing():
    target = part("T")
    donors = [part(f"D{i}") for i in range(5)]
    inventory = {("D0", "1300"): {"safety_stock": 4, "rop": 8, "max_stock": 12}}

    result = service.evaluate_target(target, donors, NO_TEXT, Decimal(1000), inventory, POLICY)

    assert len(result.neighbours) == 5
    assert [n.inventory_eligible for n in result.neighbours].count(True) == 1
    assert result.estimate.inventory_eligible_neighbours == 1
