"""I07 OAR similarity and cold-start estimate engine.

    NO_HISTORY / COLD_START target (Phase 3)
        |
    Hard constraints: same criticality, active, >= 12 months history
        |                        (partition the donors before anything is scored)
    Stage A: vectorised pre-score -> structured + semantic + fused shortlist
        |
    Stage B: Structured (Gower) + Text (BGE cosine + fuzzy ratio) + Business similarity,
             exact, for the shortlist only
        |
    Combined score, renormalised over available dimensions, minimum evidence
        |
    Rank, Top-K
        |
    Confidence (HIGH / MEDIUM / LOW)
        |
    Similarity-weighted SS/ROP/Max -- only from neighbours with a successful
    Phase 5 calculation
        |
    Human review (Phase 7)

Consumes the Phase 3 history gate and OAR routing as given; recomputes neither.
No neighbour is invented when none is eligible, and no inventory value is
invented when neighbours exist but the service-level matrix is unsigned.

| Module | Responsibility |
| --- | --- |
| ``repository`` | targets, candidate population, Phase 5 inventory lookups |
| ``eligibility`` | the three hard constraints |
| ``retrieval`` | Stage A: eligible partitions, closed-form rejection counts, shortlist |
| ``engine`` | Stage A + Stage B over every target, in batches |
| ``structured_similarity`` | Gower distance over material group, equipment, UoM |
| ``text_similarity`` | BGE embeddings, pluggable, absent by default; the cache |
| ``text_match`` | description normalisation and the rapidfuzz half of the text score |
| ``embedding_store`` | embeddings kept in ``i7_oar_embedding`` between runs |
| ``business_similarity`` | price proximity, manufacturer (circuit not scored) |
| ``scoring`` | weighted combination with renormalisation; evidence; explanations |
| ``ranking`` | deterministic Top-K |
| ``confidence`` | HIGH/MEDIUM/LOW grading |
| ``estimate`` | similarity-weighted SS/ROP/Max |
| ``evaluation`` | precision/recall, retrieval recall, threshold calibration |
| ``service`` | orchestration and persistence; the exhaustive reference |

See docs/i07_oar_similarity_engine.md for the design, the formula, the
configuration and the calibration procedure.
"""

from app.initiatives.i7.oar.service import OarRunResult, evaluate_target, run_oar_similarity
from app.initiatives.i7.oar.types import (
    ALGORITHM_VERSION,
    ESTIMATE_LABEL,
    CandidateAttributes,
    CombinedScore,
    DimensionScore,
    EligibilityRejection,
    EstimateStatus,
    Neighbour,
    OarConfidence,
    OarEstimate,
    OarResult,
    OarStatus,
    SimilarityStatus,
)

__all__ = [
    "ALGORITHM_VERSION",
    "ESTIMATE_LABEL",
    "CandidateAttributes",
    "CombinedScore",
    "DimensionScore",
    "EligibilityRejection",
    "EstimateStatus",
    "Neighbour",
    "OarConfidence",
    "OarEstimate",
    "OarResult",
    "OarRunResult",
    "OarStatus",
    "SimilarityStatus",
    "evaluate_target",
    "run_oar_similarity",
]
