"""Measuring OAR matching quality, and calibrating its threshold, from evidence.

Nothing in this module changes a production setting. It turns expert-reviewed
material pairs into numbers -- precision, recall, how often retrieval keeps the
right candidates, what a threshold would admit -- so that a change to the 0.60
floor or to the weights is argued from measurements, not from taste.

WHY THIS EXISTS

oar-similarity-2 changed what a score is allowed to rest on (the evidence rule)
and removed circuit from it. The 0.60 admission floor was set for the earlier
scoring and was never calibrated against reviewed pairs; there is no reason to
assume it is still right, and no evidence yet that it is wrong. The honest
position is the one this module supports: keep 0.60 until reviewed pairs show
what it should be, then change it in ``SimilarityPolicy`` with the evidence
recorded alongside.

THE LABELLED-PAIR FORMAT (CSV, one row per reviewed pair)::

    target_material,target_plant,candidate_material,candidate_plant,relevant,reviewer,note
    000000004000000123,1300,000000004000000456,1300,yes,j.smith,same bearing, other supplier
    000000004000000123,1300,000000004000000999,1300,no,j.smith,housing, not a bearing

``relevant`` is yes/no (also 1/0, true/false). A pair nobody reviewed is
UNJUDGED, not irrelevant -- see :func:`precision_at_k`.

COLLECTING PAIRS (the procedure docs/i07_oar_similarity_engine.md describes in full)

1. Draw ~50 cold-start targets, stratified by criticality and material group.
2. For each, take the engine's top 10 AND a few random eligible donors, so the
   reviewer is not only shown what the engine already liked.
3. An inventory or maintenance engineer marks each pair: could this part's
   stocking history stand in for the target's?
4. Run :func:`threshold_sweep` and the ranking metrics; record the result next
   to any policy change.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Sequence

Key = tuple[str, str]
"""(material, plant)"""

_TRUE = {"yes", "y", "1", "true", "t", "relevant"}
_FALSE = {"no", "n", "0", "false", "f", "not relevant", "irrelevant"}


@dataclass(frozen=True)
class LabelledPair:
    target: Key
    candidate: Key
    relevant: bool
    reviewer: str | None = None
    note: str | None = None


def load_labelled_pairs(path: str | Path) -> list[LabelledPair]:
    """Read the CSV above. A row with an unreadable ``relevant`` value is an
    error, not a skip -- a silently dropped judgement biases every metric."""
    pairs: list[LabelledPair] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            verdict = (row.get("relevant") or "").strip().lower()
            if verdict in _TRUE:
                relevant = True
            elif verdict in _FALSE:
                relevant = False
            else:
                raise ValueError(f"line {line}: 'relevant' must be yes/no, got {verdict!r}")
            pairs.append(
                LabelledPair(
                    target=(row["target_material"].strip(), row["target_plant"].strip()),
                    candidate=(row["candidate_material"].strip(), row["candidate_plant"].strip()),
                    relevant=relevant,
                    reviewer=(row.get("reviewer") or "").strip() or None,
                    note=(row.get("note") or "").strip() or None,
                )
            )
    return pairs


def judgements(pairs: Iterable[LabelledPair]) -> dict[Key, dict[Key, bool]]:
    """target -> {candidate: relevant}. A later row for the same pair wins."""
    out: dict[Key, dict[Key, bool]] = {}
    for pair in pairs:
        out.setdefault(pair.target, {})[pair.candidate] = pair.relevant
    return out


# --- Ranking metrics -----------------------------------------------------------


def precision_at_k(
    rankings: Mapping[Key, Sequence[Key]],
    judged: Mapping[Key, Mapping[Key, bool]],
    k: int,
    *,
    judged_only: bool = True,
) -> float | None:
    """Mean over targets of (relevant in top k) / (counted in top k).

    ``judged_only`` (default) counts only reviewed candidates in the
    denominator, so an unreviewed neighbour neither helps nor hurts. With it
    off, unreviewed neighbours count as not relevant -- a pessimistic bound.
    Report both when the review is thin. ``None`` when no target qualifies.
    """
    values: list[float] = []
    for target, ranked in rankings.items():
        verdicts = judged.get(target)
        if not verdicts:
            continue
        top = list(ranked)[:k]
        if judged_only:
            top = [c for c in top if c in verdicts]
        if not top:
            continue
        values.append(sum(1 for c in top if verdicts.get(c) is True) / len(top))
    return sum(values) / len(values) if values else None


def recall_at_k(
    rankings: Mapping[Key, Sequence[Key]],
    judged: Mapping[Key, Mapping[Key, bool]],
    k: int,
) -> float | None:
    """Mean over targets with at least one known-relevant candidate of the share
    of those candidates found in the top k."""
    values: list[float] = []
    for target, verdicts in judged.items():
        relevant = {c for c, ok in verdicts.items() if ok}
        if not relevant:
            continue
        top = set(list(rankings.get(target, ()))[:k])
        values.append(len(relevant & top) / len(relevant))
    return sum(values) / len(values) if values else None


def retrieval_recall(
    shortlists: Mapping[Key, Iterable[Key]], reference: Mapping[Key, Sequence[Key]]
) -> float | None:
    """Share of the exhaustive Top-K that the two-stage shortlist contained.

    1.0 means retrieval lost nothing the exact score would have chosen; this is
    what ``scripts/benchmark_oar_similarity.py`` reports for a candidate-pool
    size. It needs no labels -- the exhaustive run is the reference.
    """
    found = total = 0
    for target, expected in reference.items():
        listed = set(shortlists.get(target, ()))
        found += sum(1 for c in expected if c in listed)
        total += len(expected)
    return found / total if total else None


def donor_eligibility_rate(results) -> float | None:
    """Share of selected neighbours that may lend Phase 5 values."""
    neighbours = [n for r in results for n in r.neighbours]
    if not neighbours:
        return None
    return sum(1 for n in neighbours if n.inventory_eligible) / len(neighbours)


def targets_with_minimum_neighbours(results, minimum: int) -> int:
    return sum(1 for r in results if len(r.neighbours) >= minimum)


def inventory_error(
    estimates: Mapping[Key, Mapping[str, int | None]],
    reference: Mapping[Key, Mapping[str, int | None]],
) -> dict[str, dict[str, float | int]]:
    """Mean absolute error per parameter, over targets where both an estimate
    and a TRUSTED reference value exist. Only meaningful against reference
    values someone vouches for -- not against another model's output."""
    out: dict[str, dict[str, float | int]] = {}
    for parameter in ("safety_stock", "rop", "max_stock"):
        errors = [
            abs(float(estimates[key][parameter]) - float(reference[key][parameter]))
            for key in estimates.keys() & reference.keys()
            if estimates[key].get(parameter) is not None and reference[key].get(parameter) is not None
        ]
        out[parameter] = {
            "n": len(errors),
            "mae": sum(errors) / len(errors) if errors else float("nan"),
        }
    return out


# --- Threshold calibration --------------------------------------------------------


@dataclass(frozen=True)
class ThresholdPoint:
    threshold: Decimal
    admitted: int
    true_positives: int
    precision: float | None
    recall: float | None

    @property
    def f1(self) -> float | None:
        if not self.precision or not self.recall:
            return None
        return 2 * self.precision * self.recall / (self.precision + self.recall)


def threshold_sweep(
    scored: Sequence[tuple[Decimal | float, bool]],
    thresholds: Iterable[Decimal | float] | None = None,
) -> list[ThresholdPoint]:
    """Precision and recall of ``score >= threshold`` over reviewed pairs.

    ``scored`` is (combined score, relevant) for each reviewed pair whose score
    was computable AND passed the evidence rule -- the population the threshold
    actually decides on. Thresholds default to 0.30 .. 0.95 in steps of 0.05,
    which brackets the current 0.60.
    """
    if thresholds is None:
        thresholds = [Decimal("0.30") + Decimal("0.05") * i for i in range(14)]
    relevant_total = sum(1 for _, ok in scored if ok)
    points: list[ThresholdPoint] = []
    for raw in thresholds:
        threshold = Decimal(str(raw))
        admitted = [ok for score, ok in scored if Decimal(str(score)) >= threshold]
        true_positives = sum(1 for ok in admitted if ok)
        points.append(
            ThresholdPoint(
                threshold=threshold,
                admitted=len(admitted),
                true_positives=true_positives,
                precision=true_positives / len(admitted) if admitted else None,
                recall=true_positives / relevant_total if relevant_total else None,
            )
        )
    return points


def recommend_threshold(
    points: Sequence[ThresholdPoint], minimum_precision: float, minimum_admitted: int = 20
) -> ThresholdPoint | None:
    """The LOWEST threshold whose precision meets ``minimum_precision`` on at
    least ``minimum_admitted`` admitted pairs -- the most neighbours that can be
    admitted without dropping below the precision the business asks for.

    A recommendation to put in front of a person, with its numbers. It is never
    applied here, and ``None`` (no threshold qualifies, or too few pairs) is a
    real answer: collect more reviews before moving the floor.
    """
    qualifying = [
        p for p in points
        if p.precision is not None and p.precision >= minimum_precision
        and p.admitted >= minimum_admitted
    ]
    return min(qualifying, key=lambda p: p.threshold) if qualifying else None
