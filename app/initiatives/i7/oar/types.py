"""OAR similarity value objects and statuses.

Phase 6 answers one question for a cold-start material: *which materials does
this most resemble, and how much should we trust the resemblance?* It produces
neighbours and evidence, not recommendations.

**Similarity and confidence are different things.** A similarity score is
arithmetic over attributes; confidence is a business judgement about whether
enough good neighbours were found to rely on. A single 0.95 neighbour scores
high and is still LOW confidence. Both are persisted.

**A missing dimension is absent, not zero.** Scoring a NULL text similarity as
0.0 would drag a genuinely close match down by 30% of the total and rank it
below a worse candidate that happened to have a description. Unavailable
dimensions are excluded and the weights renormalised over what remains, with
``score_completeness`` recording how much of the intended score was actually
computed.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import NamedTuple

ALGORITHM_VERSION = "oar-similarity-2"
"""Identifies the similarity implementation behind a stored result. Not a
semantic version -- nothing here maintains a release contract.

``oar-similarity-2`` changed three things against ``-1``, all recorded so a
stored result is never compared with one built under different rules:

* Circuit no longer contributes to the business score or to tie-breaking. No
  trustworthy source exists (see ``CandidateAttributes.circuit``).
* A pair must carry a minimum amount of evidence to be admitted as a neighbour
  (``SimilarityPolicy.minimum_evidence_features`` and
  ``identity_evidence_features``) -- a 1.0 resting on a shared unit of measure
  alone is no longer a neighbour.
* Candidates are retrieved in two stages (``oar.retrieval``) instead of every
  donor being scored exactly against every target. The exact scoring of the
  shortlist is unchanged.
"""

FEATURE_NAMES = (
    "material_group",
    "equipment_type",
    "base_unit_of_measure",
    "manufacturer",
    "unit_price",
    "description",
)
"""Every feature a score can rest on, by the name explanations use. Circuit is
deliberately absent -- see ``CandidateAttributes.circuit``."""


class EligibilityRejection(StrEnum):
    """Why a candidate cannot be a neighbour.

    Every hard constraint has its own code so the rejection profile is
    measurable: "no neighbours" is not actionable, but "41,000 rejected for
    missing criticality" points straight at the MARA coverage gap.
    """

    CRITICALITY_MISMATCH = "CRITICALITY_MISMATCH"
    CRITICALITY_MISSING_TARGET = "CRITICALITY_MISSING_TARGET"
    CRITICALITY_MISSING_CANDIDATE = "CRITICALITY_MISSING_CANDIDATE"
    """Missing criticality never means "probably the same". The Solution Design
    makes same-class a hard constraint, and with 1.7% coverage a permissive
    reading would match almost everything to almost everything."""

    INACTIVE = "INACTIVE"
    ACTIVE_STATUS_UNKNOWN = "ACTIVE_STATUS_UNKNOWN"
    """Unknown is not assumed active -- an obsolete donor would propagate its
    parameters into a live material."""

    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    HISTORY_UNKNOWN = "HISTORY_UNKNOWN"
    SELF = "SELF"
    """A material is not its own neighbour."""


class SimilarityStatus(StrEnum):
    """Whether one similarity dimension could be computed."""

    AVAILABLE = "AVAILABLE"
    NOT_AVAILABLE_NO_FEATURES = "NOT_AVAILABLE_NO_FEATURES"
    """No attribute was known on both sides, so there is nothing to compare."""

    NOT_AVAILABLE_NO_TEXT = "NOT_AVAILABLE_NO_TEXT"
    """One or both descriptions are missing."""

    NOT_AVAILABLE_NO_MODEL = "NOT_AVAILABLE_NO_MODEL"
    """The embedding model could not be loaded. Reported rather than silently
    substituting a different model or a bag-of-words stand-in."""


class OarStatus(StrEnum):
    """Outcome for one cold-start target."""

    SUCCESS = "SUCCESS"
    NO_ELIGIBLE_NEIGHBORS = "NO_ELIGIBLE_NEIGHBORS"
    """No candidate passed the hard constraints. A valid and important business
    outcome -- never softened by relaxing a constraint."""

    NOT_A_COLD_START_TARGET = "NOT_A_COLD_START_TARGET"


class EstimateStatus(StrEnum):
    """Whether weighted inventory parameters could be derived."""

    SUCCESS = "SUCCESS"
    NOT_EVALUABLE_NO_NEIGHBORS = "NOT_EVALUABLE_NO_NEIGHBORS"

    NOT_EVALUABLE_SERVICE_LEVEL_UNSET = "NOT_EVALUABLE_SERVICE_LEVEL_UNSET"
    """Neighbours exist, but none carries a Phase 5 safety stock because the
    service-level matrix is unsigned. The expected state today."""

    NOT_EVALUABLE_NEIGHBOR_INVENTORY = "NOT_EVALUABLE_NEIGHBOR_INVENTORY"
    """Neighbours exist and the service level is signed, but no neighbour has a
    successful Phase 5 calculation to borrow from."""

    NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS = "NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS"
    """Fewer than ``SimilarityPolicy.minimum_neighbours`` candidates cleared the
    ``minimum_similarity`` admission floor. Distinct from
    NOT_EVALUABLE_NO_NEIGHBORS (zero candidates existed at all): here, 1-4
    qualifying neighbours may exist -- just not enough to trust a weighted
    estimate on. Never padded with low-similarity candidates to reach the
    minimum."""


class OarConfidence(StrEnum):
    """Business-quality grade for a set of neighbours."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


ESTIMATE_LABEL = "SIMILARITY-BASED ESTIMATE"
"""Required by the Solution Design. Not a forecast and not an ML prediction --
it is borrowed from comparable materials, and the label has to say so."""


class DimensionScore(NamedTuple):
    """One similarity dimension, with the evidence behind it."""

    value: Decimal | None
    status: SimilarityStatus
    available_features: int = 0
    missing_features: int = 0
    features: tuple[str, ...] = ()
    """The features (``FEATURE_NAMES``) known on BOTH sides, so they were
    actually compared. Empty for a dimension that could not be computed."""
    missing: tuple[str, ...] = ()
    """The features this dimension could not compare, because one side or both
    lacked them. Never counted as a match."""

    @property
    def is_available(self) -> bool:
        return self.value is not None and self.status is SimilarityStatus.AVAILABLE


class CombinedScore(NamedTuple):
    """The weighted similarity between a target and one candidate."""

    combined: Decimal | None
    structured: DimensionScore
    text: DimensionScore
    business: DimensionScore

    score_completeness: Decimal
    """Fraction of the intended weight that was actually computable. 1.0 means
    all three dimensions contributed; 0.70 means text was unavailable and the
    score rests on structured and business alone."""

    applied_weights: tuple[tuple[str, str], ...] = ()
    """The renormalised weights actually used, so a score can be recomputed."""

    features_used: tuple[str, ...] = ()
    """Every feature that entered the score, across all three dimensions."""
    features_missing: tuple[str, ...] = ()
    """Every feature that could not be compared for this pair."""

    evidence_sufficient: bool = True
    """Whether the score rests on enough evidence to admit the pair as a
    neighbour -- see ``scoring.assess_evidence``. A score can be high and still
    insufficient: 1.0 from a shared unit of measure alone says almost nothing
    about whether two spare parts are alike. Defaults True so a score built by
    hand, without an evidence policy, behaves as it always did."""
    evidence_detail: str | None = None
    """Why the evidence fell short, when it did."""


class CandidateAttributes(NamedTuple):
    """What the engine needs to know about a material-plant.

    Used for both targets and candidates -- the comparison is symmetric, and a
    separate target type would duplicate every field.
    """

    sap_material_number: str
    sap_plant_code: str

    criticality: str | None
    is_active: bool | None
    """``None`` means unknown, which is a rejection -- distinct from ``False``."""

    history_months: int | None

    material_group: str | None
    equipment_type: str | None
    base_unit_of_measure: str | None
    circuit: str | None
    """NOT USED BY SIMILARITY. Circuit (Crushing / Milling / ...) has no
    trustworthy source in the SAP data delivered so far, so it awards no match
    bonus, no difference penalty and no tie-break, and two missing circuits are
    never read as "the same circuit". The field stays so existing callers and
    stored shapes keep working; scoring it again needs a real source first."""
    manufacturer: str | None
    unit_price: Decimal | None
    description: str | None


class Neighbour(NamedTuple):
    """One selected neighbour and the evidence for it."""

    material: str
    plant: str
    rank: int
    score: CombinedScore
    same_circuit: bool | None
    """Always ``None`` since ``oar-similarity-2``: circuit is not evaluated
    (see ``CandidateAttributes.circuit``). Kept so stored and API shapes hold."""
    same_material_group: bool | None
    criticality: str | None
    history_months: int | None
    is_active: bool | None

    safety_stock: int | None = None
    rop: int | None = None
    max_stock: int | None = None
    inventory_eligible: bool = False
    """Whether this neighbour's Phase 5 values may contribute to a weighted
    estimate. Deliberately separate from similarity eligibility: a material can
    be an excellent match whose own parameters are blocked."""


class OarEstimate(NamedTuple):
    """Weighted inventory parameters borrowed from neighbours."""

    status: EstimateStatus
    label: str = ESTIMATE_LABEL
    safety_stock: int | None = None
    rop: int | None = None
    max_stock: int | None = None
    inventory_eligible_neighbours: int = 0
    inventory_ineligible_neighbours: int = 0
    qualifying_neighbours: int = 0
    """How many candidates cleared the minimum_similarity admission floor,
    before the top-K cap. Distinct from neighbour_count on OarResult (the
    final, top-K-capped list) so a NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS
    result can still show how close the target came (e.g. "4 qualifying,
    5 required")."""
    minimum_neighbours: int = 0
    minimum_similarity: Decimal | None = None
    trace: tuple[tuple[str, str], ...] = ()
    detail: str | None = None


class OarResult(NamedTuple):
    """Everything Phase 6 produces for one cold-start target."""

    sap_material_number: str
    sap_plant_code: str
    status: OarStatus
    confidence: OarConfidence
    neighbours: tuple[Neighbour, ...]
    estimate: OarEstimate
    candidates_considered: int
    eligible_candidates: int
    rejection_counts: tuple[tuple[str, int], ...]
    best_similarity: Decimal | None
    generated_at: datetime
    detail: str | None = None

    low_evidence_candidates: int = 0
    """Eligible candidates whose score rested on too little evidence to be
    admitted (``CombinedScore.evidence_sufficient`` False). Counted over every
    eligible candidate, whichever retrieval mode ran."""
    shortlisted_candidates: int | None = None
    """How many candidates received the exact, explainable score. Every eligible
    candidate under exhaustive retrieval; the merged shortlist under two-stage."""
    retrieval: str | None = None
    """``"exhaustive"`` or ``"two_stage"`` -- which retrieval produced this."""
