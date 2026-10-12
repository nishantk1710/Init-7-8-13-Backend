"""OAR evaluation and threshold calibration -- the arithmetic, on hand-built data.

These prove the metrics are computed correctly. They say nothing about how good
OAR matching is: that needs expert-reviewed pairs from VZI, which do not exist
yet.
"""

from decimal import Decimal

import pytest

from app.initiatives.i7.oar.evaluation import (
    LabelledPair,
    ThresholdPoint,
    judgements,
    load_labelled_pairs,
    precision_at_k,
    recall_at_k,
    recommend_threshold,
    retrieval_recall,
    threshold_sweep,
)

T = ("T1", "1300")
A, B, C, D = ("A", "1300"), ("B", "1300"), ("C", "1300"), ("D", "1300")


def test_labelled_pairs_load_from_csv(tmp_path):
    path = tmp_path / "pairs.csv"
    path.write_text(
        "target_material,target_plant,candidate_material,candidate_plant,relevant,reviewer,note\n"
        "T1,1300,A,1300,yes,eng1,same part\n"
        "T1,1300,B,1300,No,eng1,\n",
        encoding="utf-8",
    )
    pairs = load_labelled_pairs(path)
    assert pairs == [
        LabelledPair(T, A, True, "eng1", "same part"),
        LabelledPair(T, B, False, "eng1", None),
    ]


def test_an_unreadable_verdict_is_an_error_not_a_skip(tmp_path):
    path = tmp_path / "pairs.csv"
    path.write_text(
        "target_material,target_plant,candidate_material,candidate_plant,relevant\n"
        "T1,1300,A,1300,maybe\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="line 2"):
        load_labelled_pairs(path)


def test_precision_counts_only_reviewed_candidates_by_default():
    judged = judgements([LabelledPair(T, A, True), LabelledPair(T, B, False)])
    rankings = {T: [A, C, B]}  # C was never reviewed
    assert precision_at_k(rankings, judged, 3) == pytest.approx(0.5)
    assert precision_at_k(rankings, judged, 3, judged_only=False) == pytest.approx(1 / 3)


def test_recall_is_the_share_of_known_relevant_candidates_found():
    judged = judgements([LabelledPair(T, A, True), LabelledPair(T, D, True)])
    assert recall_at_k({T: [A, B, C]}, judged, 3) == pytest.approx(0.5)
    assert recall_at_k({T: [A, B, C, D]}, judged, 4) == pytest.approx(1.0)


def test_retrieval_recall_measures_the_shortlist_against_exhaustive():
    assert retrieval_recall({T: {A, B, C}}, {T: [A, B]}) == 1.0
    assert retrieval_recall({T: {A}}, {T: [A, B]}) == 0.5


def test_the_sweep_trades_precision_against_recall():
    scored = [(0.9, True), (0.8, True), (0.7, False), (0.65, True), (0.5, False)]
    points = {str(p.threshold): p for p in threshold_sweep(scored, [0.6, 0.75])}
    assert points["0.6"].admitted == 4 and points["0.6"].precision == pytest.approx(0.75)
    assert points["0.75"].admitted == 2 and points["0.75"].precision == pytest.approx(1.0)
    assert points["0.75"].recall == pytest.approx(2 / 3)


def test_the_default_sweep_brackets_the_current_floor():
    thresholds = [p.threshold for p in threshold_sweep([(0.6, True)])]
    assert Decimal("0.60") in thresholds and min(thresholds) < Decimal("0.60") < max(thresholds)


def test_a_recommendation_needs_enough_evidence():
    thin = [ThresholdPoint(Decimal("0.5"), admitted=5, true_positives=5, precision=1.0, recall=1.0)]
    assert recommend_threshold(thin, minimum_precision=0.8) is None

    points = [
        ThresholdPoint(Decimal("0.5"), 60, 40, 40 / 60, 1.0),
        ThresholdPoint(Decimal("0.6"), 45, 38, 38 / 45, 0.95),
        ThresholdPoint(Decimal("0.7"), 30, 28, 28 / 30, 0.7),
    ]
    assert recommend_threshold(points, minimum_precision=0.8).threshold == Decimal("0.6")
