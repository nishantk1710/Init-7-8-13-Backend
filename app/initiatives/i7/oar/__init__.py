"""I07 OAR similarity and cold-start estimate engine.

    NO_HISTORY / COLD_START target (Phase 3)
        |
    Hard constraints: same criticality, active, >= 12 months history
        |
    Structured (Gower) + Text (MiniLM cosine) + Business similarity
        |
    Combined score, renormalised over available dimensions
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
| ``structured_similarity`` | Gower distance over material group, equipment, UoM |
| ``text_similarity`` | MiniLM embeddings, pluggable, absent by default |
| ``business_similarity`` | circuit, price proximity, manufacturer |
| ``scoring`` | weighted combination with renormalisation |
| ``ranking`` | deterministic Top-K |
| ``confidence`` | HIGH/MEDIUM/LOW grading |
| ``estimate`` | similarity-weighted SS/ROP/Max |
| ``service`` | orchestration and persistence |
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
