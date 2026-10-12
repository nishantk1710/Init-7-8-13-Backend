"""Two-stage OAR matching over every cold-start target in one pass.

    Stage A  oar.retrieval  -- eligible partition, vectorised pre-score,
                               structured + semantic + fused shortlist
    Stage B  this module    -- the exact, explainable score for the shortlist
                               only, then the same ranking, confidence and
                               estimate as the exhaustive reference

Stage B calls exactly what :func:`oar.service.evaluate_target` calls --
``_score_pair`` for each candidate and ``finalise_target`` for the result -- so
the two paths can differ only in which candidates were scored. That is the
property the tests pin: on the same inputs, two-stage and exhaustive produce
the same neighbours, scores, confidence and estimate.

Work per run, for T targets and an eligible partition of P donors:

* each description is embedded at most once (``EmbeddingCache``), and only if
  no earlier run stored it;
* one float32 matrix product per batch of targets per partition for the
  description cosines, instead of a Python dot product per pair;
* a vectorised pre-score over P per target, instead of P exact ``Decimal``
  scores;
* exact scores for the shortlist only -- at most the sum of the pool sizes,
  usually fewer once the channels overlap.

Results are yielded one target at a time, in input order, so the caller can
write them in batches and never hold every neighbour row at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterator, Sequence

import numpy as np

from app.initiatives.i7.oar.retrieval import (
    DonorIndex,
    RetrievalConfig,
    cosine_block,
)
from app.initiatives.i7.oar.text_similarity import EmbeddingProvider
from app.initiatives.i7.oar.types import CandidateAttributes, OarResult
from app.initiatives.i7.policy import PolicyDocument


@dataclass
class EngineStats:
    """What a pass did, for the run log and the benchmark."""

    targets: int = 0
    exhaustive_targets: int = 0
    shortlisted_pairs: int = 0
    eligible_pairs: int = 0
    low_evidence_pairs: int = 0
    shortlist_sizes: list[int] = field(default_factory=list)

    @property
    def scored_fraction(self) -> float:
        """Exact scores computed, as a share of what exhaustive would compute."""
        if not self.eligible_pairs:
            return 0.0
        return self.shortlisted_pairs / self.eligible_pairs


def build_index(
    population: Sequence[CandidateAttributes],
    *,
    price_range: Decimal | None,
    policy: PolicyDocument,
    embeddings: dict[str, np.ndarray] | None,
) -> DonorIndex:
    similarity = policy.similarity
    return DonorIndex(
        population,
        minimum_history_months=similarity.minimum_neighbour_history_months,
        price_range=price_range,
        weights=(
            float(similarity.structural_weight),
            float(similarity.text_weight),
            float(similarity.business_weight),
        ),
        minimum_evidence_features=similarity.minimum_evidence_features,
        identity_evidence_features=tuple(similarity.identity_evidence_features),
        embeddings=embeddings,
    )


def evaluate_targets(
    targets: Sequence[CandidateAttributes],
    population: Sequence[CandidateAttributes],
    *,
    provider: EmbeddingProvider,
    embeddings: dict[str, np.ndarray] | None,
    price_range: Decimal | None,
    inventory_values: dict[tuple[str, str], dict],
    policy: PolicyDocument,
    config: RetrievalConfig,
    index: DonorIndex | None = None,
    stats: EngineStats | None = None,
) -> Iterator[OarResult]:
    """Every target's :class:`OarResult`, in input order.

    ``provider`` is what exact scoring encodes with -- normally a
    ``PrecomputedEmbeddingProvider`` over ``embeddings``, so scoring never calls
    a model. ``embeddings`` is ``None`` when no model is available, which makes
    the text dimension unavailable for every pair, as in exact scoring.
    """
    # Imported here: service imports this module's callers, not the reverse.
    from app.initiatives.i7.oar.service import _score_pair, finalise_target, scored_entry

    index = index or build_index(
        population, price_range=price_range, policy=policy, embeddings=embeddings
    )
    stats = stats if stats is not None else EngineStats()
    population_size = len(population)
    batch_size = max(1, config.target_batch_size)

    for start in range(0, len(targets), batch_size):
        batch = targets[start : start + batch_size]
        encodings = [index.encode_target(target) for target in batch]

        # One matrix product per partition for the whole batch.
        cosines: list[np.ndarray | None] = [None] * len(batch)
        by_partition: dict[str, list[int]] = {}
        for i, target in enumerate(batch):
            if target.criticality is not None and target.criticality in index.partitions:
                by_partition.setdefault(target.criticality, []).append(i)
        for criticality, positions in by_partition.items():
            rows = cosine_block(
                index.partitions[criticality], [encodings[i].vector for i in positions]
            )
            for i, row in zip(positions, rows):
                cosines[i] = row

        for i, target in enumerate(batch):
            stats.targets += 1
            eligible_count, rejections = index.profile.profile(target)
            partition = index.partition_for(target)

            if partition is None or eligible_count == 0:
                yield finalise_target(
                    target,
                    [],
                    population_size=population_size,
                    eligible_count=0,
                    rejections=rejections,
                    low_evidence=0,
                    shortlisted=0,
                    retrieval=config.mode,
                    policy=policy,
                )
                continue

            shortlist = index.shortlist(partition, target, encodings[i], cosines[i], config)

            expected = len(partition) - (
                1
                if (target.sap_material_number, target.sap_plant_code) in partition.index_by_key
                else 0
            )
            if expected != eligible_count:
                # The partition and the closed-form counts are two views of the
                # same eligibility rules. If they disagree, one of them is wrong,
                # and a result built on either would be silently off.
                raise RuntimeError(
                    f"OAR retrieval inconsistency for {target.sap_material_number}/"
                    f"{target.sap_plant_code}: {eligible_count} eligible by count, "
                    f"{expected} in the partition"
                )

            scored = [
                scored_entry(
                    target,
                    partition.donors[j],
                    _score_pair(
                        target, partition.donors[j], provider, price_range, policy.similarity
                    ),
                    inventory_values,
                )
                for j in shortlist.indices
            ]

            stats.eligible_pairs += eligible_count
            stats.shortlisted_pairs += len(scored)
            stats.low_evidence_pairs += shortlist.low_evidence
            stats.shortlist_sizes.append(len(scored))
            if shortlist.exhaustive:
                stats.exhaustive_targets += 1

            yield finalise_target(
                target,
                scored,
                population_size=population_size,
                eligible_count=eligible_count,
                rejections=rejections,
                low_evidence=shortlist.low_evidence,
                shortlisted=len(scored),
                retrieval="exhaustive" if shortlist.exhaustive else "two_stage",
                policy=policy,
            )
