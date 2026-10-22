"""Stage A of OAR donor matching: who is worth scoring exactly.

The exact score (``scoring.combine`` over the three dimensions, in ``Decimal``)
is explainable and auditable, and far too slow to run for every target against
every donor: tens of thousands of targets times thousands of donors is hundreds
of millions of pure-Python pair scores. This module narrows that to a shortlist
per target, and ``oar.engine`` scores only the shortlist exactly.

WHAT IS NOT CHANGED BY RETRIEVAL

* **Eligibility.** The hard gates (same known criticality, known-active, known
  history >= the minimum, not the target itself) partition the donor
  population *before* anything is scored, exactly as ``eligibility`` applies
  them before scoring. A shortlist is drawn only from the target's eligible
  partition, so an ineligible donor can never be retrieved, and the rejection
  counts are computed in closed form to be identical to
  ``eligibility.filter_candidates`` over the whole population
  (:class:`PopulationProfile`).
* **The final score and ranking.** Every shortlisted candidate gets the exact
  ``scoring.combine`` score, and ranking, the 0.60 floor, the evidence rule,
  Top-10, the 5-neighbour minimum and the donor gate run on those exact scores.

HOW THE SHORTLIST IS DRAWN

For a target, every eligible donor in its partition gets a vectorised
*pre-score*: the same formula as the exact score -- the same features, the same
missing-feature handling (a feature counts only when known on both sides), the
same renormalised weights and the same evidence rule -- computed in floating
point with numpy instead of ``Decimal`` per pair. Three channels then each take
their best candidates, and the union is the shortlist:

* **structured** -- best by structured + business pre-score (material group,
  unit of measure, manufacturer, price), so parts in compatible categories are
  retrieved even when their descriptions differ;
* **semantic** -- best by description cosine, so differently-worded
  descriptions of the same part are retrieved even across material groups;
* **fused** -- best by the full combined pre-score, restricted to pairs that
  would be admitted on evidence.

**Identity is applied before any channel picks** (``oar-similarity-10``). The identity stage
(``oar.identity``) refuses a candidate whose description names a component type incompatible with the
target's, and one whose only claim is a shared material group. Left to the exact score, such a candidate
would still take a slot in the structured and semantic pools -- slots that then hold nothing a neighbour can
come from. So the same rule is evaluated here, vectorised over the partition (``DonorIdentity``), and every
channel sees a refused donor as not there: it cannot enter through the structured pool because it missed the
semantic one. ``tests/i7/test_oar_identity.py`` holds this mask and the per-pair verdict to the same answer.

The fused channel is what keeps retrieval honest: it ranks by the same formula
the exact score uses, so the true Top-K is inside it whenever the pool is at
least K and floating point agrees with ``Decimal`` -- which it does to well
within the six-decimal precision scores are stored at. Equivalence with
exhaustive scoring is tested directly (``tests/i7/test_oar_retrieval.py``), and
``exhaustive_below`` makes small partitions exhaustive outright.

Ties are broken the way ranking breaks them -- material, then plant, ascending
-- so the shortlist is deterministic: donors are held in that order and every
selection is stable.

THE INDEX

The semantic channel is an exact inner-product search over unit-normalised
float32 embeddings, one matrix per criticality partition, queried a batch of
targets at a time with one matrix product. At the volumes OAR sees (donors
number in the thousands; a partition is one criticality class) an exact flat
index is faster to build and query than an approximate one, loses no recall,
and needs no new dependency -- FAISS, HNSW or a vector database would add a
service and an approximation for no gain here. The matrix is behind
:class:`DonorPartition`, so an approximate index can replace it if a partition
ever grows past what a matrix product handles.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence

import numpy as np

from app.initiatives.i7.oar import text_similarity
from app.initiatives.i7.oar.business_similarity import BUSINESS_CATEGORICAL_FEATURES, log_price
from app.initiatives.i7.oar.eligibility import material_types_conflict
from app.initiatives.i7.oar.identity import DescriptionProfile, EMPTY_PROFILE, IdentityConfig
from app.initiatives.i7.oar.structured_similarity import CATEGORICAL_FEATURES
from app.initiatives.i7.oar.types import CandidateAttributes, EligibilityRejection

UNKNOWN = -1
"""Category code for a missing value. Never equal to anything -- including
another missing value -- because the comparison is masked by ``known`` first."""

UNSEEN = -2
"""Category code for a value a target has that no donor has: known, so it
counts as compared, and it matches nothing."""

_ELIGIBLE = "ELIGIBLE"


@dataclass(frozen=True)
class RetrievalConfig:
    """How much of each partition gets the exact score. Operational, not policy:
    see ``Settings.i7_oar_*`` and docs/i07_oar_similarity_engine.md."""

    mode: str = "two_stage"
    structured_pool: int = 50
    semantic_pool: int = 50
    fused_pool: int = 100
    exhaustive_below: int = 200
    target_batch_size: int = 256

    @classmethod
    def from_settings(cls, settings=None) -> "RetrievalConfig":
        if settings is None:
            from app.core.config import get_settings

            settings = get_settings()
        return cls(
            mode=settings.i7_oar_retrieval_mode,
            structured_pool=settings.i7_oar_structured_pool,
            semantic_pool=settings.i7_oar_semantic_pool,
            fused_pool=settings.i7_oar_fused_pool,
            exhaustive_below=settings.i7_oar_exhaustive_below,
            target_batch_size=settings.i7_oar_target_batch_size,
        )

    def describe(self) -> str:
        """Recorded on the run, so a result can be traced to how it was found."""
        if self.mode == "exhaustive":
            return "mode=exhaustive"
        return (
            f"mode=two_stage;structured_pool={self.structured_pool};"
            f"semantic_pool={self.semantic_pool};fused_pool={self.fused_pool};"
            f"exhaustive_below={self.exhaustive_below}"
        )


# --- Rejection counts in closed form ------------------------------------------


def _candidate_outcome(candidate: CandidateAttributes, minimum_history_months: int) -> str:
    """The candidate-side half of ``eligibility.reject_reason``, in its order,
    for a candidate whose criticality already matches the target."""
    if candidate.is_active is None:
        return EligibilityRejection.ACTIVE_STATUS_UNKNOWN.value
    if not candidate.is_active:
        return EligibilityRejection.INACTIVE.value
    if candidate.history_months is None:
        return EligibilityRejection.HISTORY_UNKNOWN.value
    if candidate.history_months < minimum_history_months:
        return EligibilityRejection.INSUFFICIENT_HISTORY.value
    return _ELIGIBLE


class PopulationProfile:
    """Per-target rejection counts without visiting every donor per target.

    ``eligibility.reject_reason`` depends on the target only through its key
    (SELF) and its criticality; everything else is a property of the candidate.
    So the counts for any target follow from counts per criticality -- split by
    material type, for the MTART constraint -- computed once. The result equals
    ``eligibility.filter_candidates`` exactly -- same reasons, same precedence,
    same numbers -- which the tests check against it.
    """

    def __init__(
        self, population: Sequence[CandidateAttributes], minimum_history_months: int
    ) -> None:
        self.minimum_history_months = minimum_history_months
        self.total = len(population)
        self.missing_criticality = 0
        self.by_criticality: dict[str, dict[str | None, Counter]] = {}
        self.records_by_key: dict[tuple[str, str], list[CandidateAttributes]] = {}

        for candidate in population:
            key = (candidate.sap_material_number, candidate.sap_plant_code)
            self.records_by_key.setdefault(key, []).append(candidate)
            if candidate.criticality is None:
                self.missing_criticality += 1
                continue
            by_type = self.by_criticality.setdefault(candidate.criticality, {})
            counter = by_type.setdefault(candidate.material_type or None, Counter())
            counter[_candidate_outcome(candidate, minimum_history_months)] += 1

    def _outcome_against(self, target: CandidateAttributes, candidate: CandidateAttributes) -> str:
        """The bucket ``candidate`` would land in for ``target``, ignoring SELF."""
        if target.criticality is None:
            return EligibilityRejection.CRITICALITY_MISSING_TARGET.value
        if candidate.criticality is None:
            return EligibilityRejection.CRITICALITY_MISSING_CANDIDATE.value
        if candidate.criticality != target.criticality:
            return EligibilityRejection.CRITICALITY_MISMATCH.value
        if material_types_conflict(target.material_type, candidate.material_type):
            return EligibilityRejection.MATERIAL_TYPE_MISMATCH.value
        return _candidate_outcome(candidate, self.minimum_history_months)

    def profile(self, target: CandidateAttributes) -> tuple[int, dict[str, int]]:
        """``(eligible_count, rejections)`` exactly as ``filter_candidates`` gives."""
        counts: Counter = Counter()
        if target.criticality is None:
            counts[EligibilityRejection.CRITICALITY_MISSING_TARGET.value] = self.total
        else:
            counts[EligibilityRejection.CRITICALITY_MISSING_CANDIDATE.value] = (
                self.missing_criticality
            )
            same = self.by_criticality.get(target.criticality, {})
            same_total = sum(sum(counter.values()) for counter in same.values())
            counts[EligibilityRejection.CRITICALITY_MISMATCH.value] = (
                self.total - self.missing_criticality - same_total
            )
            for material_type, counter in same.items():
                if material_types_conflict(target.material_type, material_type):
                    counts[EligibilityRejection.MATERIAL_TYPE_MISMATCH.value] += sum(counter.values())
                else:
                    counts.update(counter)

        # SELF is checked first, so a record that is the target moves out of the
        # bucket it would otherwise have landed in.
        for record in self.records_by_key.get(
            (target.sap_material_number, target.sap_plant_code), ()
        ):
            counts[self._outcome_against(target, record)] -= 1
            counts[EligibilityRejection.SELF.value] += 1

        eligible = counts.pop(_ELIGIBLE, 0)
        return eligible, {reason: n for reason, n in counts.items() if n > 0}


# --- Encoded partitions ---------------------------------------------------------


class _Vocabulary:
    """Category strings to integer codes, shared by targets and donors so equal
    strings get equal codes. ``None`` is UNKNOWN; a target value no donor has is
    UNSEEN (known, matches nothing)."""

    def __init__(self) -> None:
        self.codes: dict[str, int] = {}

    def fit(self, value: str | None) -> int:
        if value is None:
            return UNKNOWN
        return self.codes.setdefault(value, len(self.codes))

    def lookup(self, value: str | None) -> int:
        if value is None:
            return UNKNOWN
        return self.codes.get(value, UNSEEN)


_CATEGORICAL = CATEGORICAL_FEATURES + BUSINESS_CATEGORICAL_FEATURES


def _ln_price(price: Decimal | None) -> float:
    """ln(price) as the exact score takes it (``business_similarity.log_price``), NaN when unknown."""
    value = log_price(price)
    return float(value) if value is not None else np.nan
"""Every categorical feature the exact score compares, in one place."""

_MATERIAL_TYPE = "material_type"
_ENCODED = _CATEGORICAL + (_MATERIAL_TYPE,)
"""Encoded per donor: the scored features plus material type, which is a hard
constraint (``eligibility.material_types_conflict``) and never scored."""


@dataclass
class DonorPartition:
    """The eligible donors of one criticality class, encoded for numpy.

    Held in (material, plant) order, which is the ranking tie-break, so a stable
    selection over these arrays breaks ties exactly as ranking does.
    """

    criticality: str
    donors: list[CandidateAttributes]
    codes: dict[str, np.ndarray]
    prices: np.ndarray
    vectors: np.ndarray | None
    has_vector: np.ndarray
    index_by_key: dict[tuple[str, str], int]
    identity: "DonorIdentity | None" = None

    def __len__(self) -> int:
        return len(self.donors)


@dataclass(frozen=True)
class TargetEncoding:
    codes: dict[str, int]
    price: float
    vector: np.ndarray | None
    profile: DescriptionProfile = EMPTY_PROFILE
    """The target description's identity profile (part numbers, component types, specific words)."""


@dataclass(frozen=True)
class Shortlist:
    """What Stage A hands to exact scoring for one target."""

    indices: np.ndarray
    """Positions in the partition's donor list, ascending."""
    low_evidence: int
    """Eligible donors whose pair would fail the evidence rule -- counted over
    the whole partition, so it does not depend on the shortlist."""
    exhaustive: bool
    type_conflicts: int = 0
    """Donors in the partition rejected for a different known material type --
    eligible by criticality, so in the partition, but never a neighbour."""
    structure_pool: int | None = None
    text_pool: int | None = None
    fused_pool: int | None = None
    """How many donors each channel picked, before the union. ``None`` when the partition was scored
    exhaustively and no channel ran."""
    identity_conflicts: int | None = None
    identity_insufficient: int | None = None
    """Eligible donors the identity stage refuses, over the whole partition. ``None`` when scoring was
    exhaustive: the exact verdicts are counted instead."""


def _unit(vector: np.ndarray) -> tuple[np.ndarray, bool]:
    vector = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if norm == 0.0 or not np.isfinite(norm):
        return np.zeros_like(vector), False
    return vector / norm, True


def top_n(scores: np.ndarray, n: int) -> np.ndarray:
    """Indices of the ``n`` best finite scores, ties to the lower index.

    O(P) with ``argpartition`` instead of a full sort. The tie rule matters:
    when several donors share the n-th best score, the ones taken are the
    earliest in (material, plant) order -- exactly the ones ranking would keep.
    """
    finite = np.isfinite(scores)
    if int(finite.sum()) <= n:
        return np.flatnonzero(finite)
    candidates = np.where(finite, scores, -np.inf)
    partitioned = np.argpartition(-candidates, n - 1)[:n]
    threshold = candidates[partitioned].min()
    above = np.flatnonzero(candidates > threshold)
    ties = np.flatnonzero(candidates == threshold)[: n - len(above)]
    return np.concatenate([above, ties])


def _postings(values: list[Iterable[str]]) -> dict[str, np.ndarray]:
    """Word -> positions of the donors whose description carries it."""
    table: dict[str, list[int]] = {}
    for position, words in enumerate(values):
        for word in words:
            table.setdefault(word, []).append(position)
    return {word: np.asarray(positions, dtype=np.int64) for word, positions in table.items()}


class DonorIdentity:
    """A partition's donors as the identity stage sees them, held as arrays.

    ``oar.identity.assess`` decides one pair; :meth:`evaluate` decides one target against every donor at
    once, by the same rule: component conflict first, then a shared part number, then the same-group or
    different-group route. Part numbers and specific words are looked up through postings (word -> donors)
    rather than compared donor by donor, so the cost is the size of the target's description, not of the
    partition.
    """

    def __init__(self, donors: Sequence[CandidateAttributes], config: IdentityConfig) -> None:
        profiles = [config.vocabulary.profile(donor.description) for donor in donors]
        self.size = len(donors)
        self.type_mask = np.array([p.type_mask for p in profiles], dtype=np.int64)
        self.has_distinctive = np.array([bool(p.distinctive) for p in profiles], dtype=bool)
        self.part_numbers = _postings([p.part_numbers for p in profiles])
        self.distinctive = _postings([p.distinctive for p in profiles])
        self.generic = _postings([p.generic for p in profiles])
        self.signatures = _postings([(p.signature,) if p.signature else () for p in profiles])

    def evaluate(
        self,
        target: DescriptionProfile,
        same_group: np.ndarray,
        text: np.ndarray,
        text_known: np.ndarray,
        config: IdentityConfig,
    ) -> tuple[np.ndarray, np.ndarray]:
        """``(passed, conflict)`` per donor. ``conflict`` implies not ``passed``."""
        size = self.size
        none = np.zeros(size, dtype=bool)

        if target.signature:
            # An identical description stands in for the text similarity the model would have given (1.0)
            # when there is none -- the rule in oar.identity.assess.
            identical = np.zeros(size, dtype=bool)
            positions = self.signatures.get(target.signature)
            if positions is not None:
                identical[positions] = True
            use_identical = identical & ~text_known
            if use_identical.any():
                text = np.where(use_identical, 1.0, text)
                text_known = text_known | use_identical

        if target.types:
            type_match = (self.type_mask & target.compatible_mask) != 0
            conflict = ((self.type_mask != 0) & ~type_match) if config.conflict_enabled else none
        else:
            type_match = none
            conflict = none

        part_number_match = np.zeros(size, dtype=bool)
        for number in target.part_numbers:
            positions = self.part_numbers.get(number)
            if positions is not None:
                part_number_match[positions] = True

        shared_specific = np.zeros(size, dtype=bool)
        for word in target.distinctive:
            positions = self.distinctive.get(word)
            if positions is not None:
                shared_specific[positions] = True
        shared_generic = np.zeros(size, dtype=bool)
        for word in target.generic:
            positions = self.generic.get(word)
            if positions is not None:
                shared_generic[positions] = True
        generic_driven = (
            (self.has_distinctive & ~shared_specific & shared_generic) if target.distinctive else none
        )

        group_floor = config.same_group_floor
        if group_floor is None:
            same_group_ok = np.ones(size, dtype=bool)
        else:
            text_ok = text_known & (text >= float(group_floor))
            same_group_ok = np.where(
                type_match, text_ok | shared_specific, text_ok & ~generic_driven
            )
        cross_floor = config.text_floor
        if cross_floor is None:
            cross_group_ok = np.ones(size, dtype=bool)
        else:
            cross_group_ok = text_known & (text >= float(cross_floor))

        passed = ~conflict & (part_number_match | np.where(same_group, same_group_ok, cross_group_ok))
        return passed, conflict


class DonorIndex:
    """Eligible donors partitioned by criticality, with everything Stage A needs.

    Built once per OAR run. ``embeddings`` maps a normalised description to its
    vector, or is ``None`` when no embedding model is available -- the text
    dimension is then unavailable for every pair, exactly as in exact scoring.
    """

    def __init__(
        self,
        population: Sequence[CandidateAttributes],
        *,
        minimum_history_months: int,
        price_range: Decimal | None,
        weights: tuple[float, float, float],
        minimum_evidence_features: int,
        identity_evidence_features: tuple[str, ...],
        embeddings: dict[str, np.ndarray] | None,
        identity_text_floor: float | None = None,
        identity: IdentityConfig | None = None,
    ) -> None:
        # One donor per material-plant, the first kept. A duplicate would be eligible, shortlisted and
        # scored twice -- and weigh twice in the estimate -- so it is dropped here and counted.
        seen: set[tuple[str, str]] = set()
        unique: list[CandidateAttributes] = []
        for candidate in population:
            key = (candidate.sap_material_number, candidate.sap_plant_code)
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
        self.duplicates_dropped = len(population) - len(unique)
        self.donor_count = len(unique)
        population = unique
        self.identity = identity
        self.profile = PopulationProfile(population, minimum_history_months)
        self.price_range = float(price_range) if price_range is not None and price_range > 0 else None
        self.weights = weights
        self.minimum_evidence_features = minimum_evidence_features
        self.identity_evidence_features = identity_evidence_features
        self.identity_text_floor = identity_text_floor
        self.embeddings = embeddings
        self.vocabularies = {feature: _Vocabulary() for feature in _ENCODED}

        grouped: dict[str, list[CandidateAttributes]] = {}
        for candidate in population:
            if candidate.criticality is None:
                continue
            if _candidate_outcome(candidate, minimum_history_months) != _ELIGIBLE:
                continue
            grouped.setdefault(candidate.criticality, []).append(candidate)

        self.partitions: dict[str, DonorPartition] = {
            criticality: self._encode(criticality, donors)
            for criticality, donors in sorted(grouped.items())
        }

    def _description_vector(self, description: str | None) -> np.ndarray | None:
        if self.embeddings is None:
            return None
        text = text_similarity.normalise_description(description)
        if text is None:
            return None
        return self.embeddings.get(text)

    def _encode(self, criticality: str, donors: list[CandidateAttributes]) -> DonorPartition:
        donors = sorted(donors, key=lambda d: (d.sap_material_number, d.sap_plant_code))
        codes = {
            feature: np.array(
                [self.vocabularies[feature].fit(getattr(d, feature) or None) for d in donors],
                dtype=np.int64,
            )
            for feature in _ENCODED
        }
        prices = np.array(
            [_ln_price(d.unit_price) for d in donors],
            dtype=np.float64,
        )
        vectors = None
        has_vector = np.zeros(len(donors), dtype=bool)
        if self.embeddings is not None and donors:
            dimension = None
            rows: list[np.ndarray] = []
            for i, donor in enumerate(donors):
                raw = self._description_vector(donor.description)
                if raw is None:
                    rows.append(None)  # type: ignore[arg-type]
                    continue
                unit, ok = _unit(raw)
                dimension = dimension or unit.shape[0]
                rows.append(unit)
                has_vector[i] = ok
            if dimension is not None:
                vectors = np.zeros((len(donors), dimension), dtype=np.float32)
                for i, row in enumerate(rows):
                    if row is not None:
                        vectors[i] = row
        return DonorPartition(
            criticality=criticality,
            donors=donors,
            codes=codes,
            prices=prices,
            vectors=vectors,
            has_vector=has_vector,
            index_by_key={
                (d.sap_material_number, d.sap_plant_code): i for i, d in enumerate(donors)
            },
            identity=DonorIdentity(donors, self.identity) if self.identity is not None else None,
        )

    def partition_for(self, target: CandidateAttributes) -> DonorPartition | None:
        if target.criticality is None:
            return None
        return self.partitions.get(target.criticality)

    def encode_target(self, target: CandidateAttributes) -> TargetEncoding:
        vector = None
        raw = self._description_vector(target.description)
        if raw is not None:
            unit, ok = _unit(raw)
            vector = unit if ok else None
        return TargetEncoding(
            codes={f: self.vocabularies[f].lookup(getattr(target, f) or None) for f in _ENCODED},
            price=_ln_price(target.unit_price),
            vector=vector,
            profile=(
                self.identity.vocabulary.profile(target.description)
                if self.identity is not None
                else EMPTY_PROFILE
            ),
        )

    # --- the pre-score --------------------------------------------------------

    def prescore(
        self,
        partition: DonorPartition,
        target: TargetEncoding,
        cosines: np.ndarray | None,
    ) -> dict[str, np.ndarray]:
        """Vectorised mirror of the exact score for one target over a partition.

        Returns the per-donor arrays the channels select on, plus the evidence
        mask. ``cosines`` is this target's row of the batch matrix product, or
        ``None`` when the text dimension is unavailable for the target.
        """
        size = len(partition)
        w_struct, w_text, w_business = self.weights

        # Structured: Gower over the categorical features known on both sides.
        s_known = np.zeros(size, dtype=np.int64)
        s_diff = np.zeros(size, dtype=np.int64)
        known_by_feature: dict[str, np.ndarray] = {}
        for feature in CATEGORICAL_FEATURES:
            t_code = target.codes[feature]
            d_codes = partition.codes[feature]
            known = (d_codes != UNKNOWN) & (t_code != UNKNOWN)
            known_by_feature[feature] = known
            s_known += known
            s_diff += known & (d_codes != t_code)
        s_avail = s_known > 0
        structured = 1.0 - s_diff / np.maximum(s_known, 1)

        # Business: manufacturer, plus price proximity over the population range.
        b_known = np.zeros(size, dtype=np.int64)
        b_dist = np.zeros(size, dtype=np.float64)
        for feature in BUSINESS_CATEGORICAL_FEATURES:
            t_code = target.codes[feature]
            d_codes = partition.codes[feature]
            known = (d_codes != UNKNOWN) & (t_code != UNKNOWN)
            known_by_feature[feature] = known
            b_known += known
            b_dist += known & (d_codes != t_code)
        if self.price_range is not None and np.isfinite(target.price):
            price_known = np.isfinite(partition.prices)
            distance = np.minimum(
                np.abs(np.where(price_known, partition.prices, 0.0) - target.price)
                / self.price_range,
                1.0,
            )
            b_dist += np.where(price_known, distance, 0.0)
        else:
            price_known = np.zeros(size, dtype=bool)
        known_by_feature["unit_price"] = price_known
        b_known += price_known
        b_avail = b_known > 0
        business = 1.0 - b_dist / np.maximum(b_known, 1)

        # Text: cosine of unit vectors, floored at 0 like the exact score.
        if cosines is not None:
            t_avail = partition.has_vector.copy()
            text = np.clip(cosines.astype(np.float64), 0.0, 1.0)
        else:
            t_avail = np.zeros(size, dtype=bool)
            text = np.zeros(size, dtype=np.float64)
        known_by_feature[text_similarity.FEATURE] = t_avail

        numerator = (
            w_struct * structured * s_avail + w_text * text * t_avail + w_business * business * b_avail
        )
        denominator = w_struct * s_avail + w_text * t_avail + w_business * b_avail
        with np.errstate(invalid="ignore", divide="ignore"):
            combined = np.where(denominator > 0, numerator / denominator, np.nan)
            sb_denominator = w_struct * s_avail + w_business * b_avail
            struct_business = np.where(
                sb_denominator > 0,
                (w_struct * structured * s_avail + w_business * business * b_avail) / sb_denominator,
                np.nan,
            )

        # Evidence: the same rule as scoring.assess_evidence, on the same features.
        used = s_known + b_known + t_avail
        evidence = (denominator > 0) & (used >= self.minimum_evidence_features)
        if self.identity_evidence_features:
            identity = np.zeros(size, dtype=bool)
            for feature in self.identity_evidence_features:
                identity |= known_by_feature.get(feature, np.zeros(size, dtype=bool))
            evidence &= identity
        same_group = known_by_feature["material_group"] & (
            partition.codes["material_group"] == target.codes["material_group"]
        )
        if self.identity is not None and partition.identity is not None:
            # The identity stage (oar.identity), over the whole partition.
            identity_pass, identity_conflict = partition.identity.evaluate(
                target.profile, same_group, text, t_avail, self.identity
            )
            evidence &= identity_pass
        else:
            if self.identity_text_floor is not None:
                # The oar-similarity-8/9 rule (scoring.combine's): the same material group, or text at
                # or above the floor. Compared-and-different does not count.
                evidence &= same_group | (t_avail & (text >= self.identity_text_floor))
            identity_pass = np.ones(size, dtype=bool)
            identity_conflict = np.zeros(size, dtype=bool)

        return {
            "combined": combined,
            "struct_business": struct_business,
            "text": np.where(t_avail, text, np.nan),
            "evidence": evidence,
            "identity_pass": identity_pass,
            "identity_conflict": identity_conflict,
        }

    def shortlist(
        self,
        partition: DonorPartition,
        target_attributes: CandidateAttributes,
        target: TargetEncoding,
        cosines: np.ndarray | None,
        config: RetrievalConfig,
    ) -> Shortlist:
        """The donors to score exactly for one target, and the low-evidence count."""
        scores = self.prescore(partition, target, cosines)
        self_index = partition.index_by_key.get(
            (target_attributes.sap_material_number, target_attributes.sap_plant_code)
        )

        evidence = scores["evidence"]
        fused = np.where(evidence, scores["combined"], np.nan)
        struct_business = scores["struct_business"]
        text = scores["text"]

        # Material type: a donor of another known type is rejected by eligibility, so like
        # SELF it is neither low-evidence nor allowed a shortlist slot. Blank is compatible.
        target_type = target.codes[_MATERIAL_TYPE]
        donor_types = partition.codes[_MATERIAL_TYPE]
        if target_type == UNKNOWN:
            conflicting = np.zeros(len(partition), dtype=bool)
        else:
            conflicting = (donor_types != UNKNOWN) & (donor_types != target_type)
        if conflicting.any():
            evidence = evidence | conflicting
            for channel in (fused, struct_business, text):
                channel[conflicting] = np.nan

        if self_index is not None:
            # The target's own record is SELF: rejected by eligibility, so it is
            # neither low-evidence nor allowed to take a shortlist slot.
            evidence = evidence.copy()
            evidence[self_index] = True
            for channel in (fused, struct_business, text):
                channel[self_index] = np.nan
        low_evidence = int((~evidence).sum())

        size = len(partition)
        structure_pool = text_pool = fused_pool = None
        identity_conflicts = identity_insufficient = None
        if config.mode == "exhaustive" or size <= config.exhaustive_below:
            indices = np.arange(size)
            exhaustive = True
        else:
            # A donor the identity stage refuses is invisible to every channel, not just to the one that
            # would have ranked it last: it cannot take a slot, and cannot return by another route.
            refused = ~scores["identity_pass"]
            if refused.any():
                for channel in (fused, struct_business, text):
                    channel[refused] = np.nan
            picked = [
                top_n(struct_business, config.structured_pool),
                top_n(text, config.semantic_pool),
                top_n(fused, config.fused_pool),
            ]
            structure_pool, text_pool, fused_pool = (len(p) for p in picked)
            indices = np.unique(np.concatenate(picked)).astype(np.int64)
            exhaustive = False

            not_candidates = conflicting.copy()
            if self_index is not None:
                not_candidates[self_index] = True
            identity_conflicts = int((scores["identity_conflict"] & ~not_candidates).sum())
            identity_insufficient = int(
                (refused & ~scores["identity_conflict"] & ~not_candidates).sum()
            )

        if self_index is not None:
            indices = indices[indices != self_index]
        indices = indices[~conflicting[indices]]
        return Shortlist(
            indices=indices,
            low_evidence=low_evidence,
            exhaustive=exhaustive,
            type_conflicts=int(conflicting.sum()),
            structure_pool=structure_pool,
            text_pool=text_pool,
            fused_pool=fused_pool,
            identity_conflicts=identity_conflicts,
            identity_insufficient=identity_insufficient,
        )


def cosine_block(
    partition: DonorPartition, target_vectors: Iterable[np.ndarray | None]
) -> list[np.ndarray | None]:
    """Cosine rows for a batch of targets against one partition: one matrix
    product for the whole batch instead of a dot product per pair.

    A target without a vector, or a partition without vectors, gets ``None`` --
    the text dimension is unavailable for those pairs.
    """
    targets = list(target_vectors)
    rows: list[np.ndarray | None] = [None] * len(targets)
    if partition.vectors is None:
        return rows
    present = [i for i, v in enumerate(targets) if v is not None]
    if not present:
        return rows
    block = np.stack([targets[i] for i in present]).astype(np.float32) @ partition.vectors.T
    for row, i in enumerate(present):
        rows[i] = block[row]
    return rows
