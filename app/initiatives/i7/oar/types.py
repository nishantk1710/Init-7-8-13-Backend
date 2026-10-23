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

ALGORITHM_VERSION = "oar-similarity-12"
"""Identifies the similarity implementation behind a stored result. Not a
semantic version -- nothing here maintains a release contract.

``oar-similarity-12`` against ``-11``: HIGH confidence no longer requires a same-circuit neighbour
(``ConfidencePolicy.oar_require_same_circuit_for_high``, now False). Circuit is not a data field anywhere, so with
the requirement nothing could ever grade HIGH and every well-matched target was capped at MEDIUM; HIGH is now the
Formula Reference's own band (5 or more neighbours, best similarity above 0.80). Neighbours, scores and estimates
are unchanged -- only the stored confidence moves -- and a run stored under ``-11`` is not reused.

``oar-similarity-11`` against ``-10``: the text dimension is ``0.6 x BGE cosine + 0.4 x rapidfuzz
(token_set_ratio + token_sort_ratio) / 2`` over normalised descriptions (``oar.text_match``), with BAAI/bge-base-en-v1.5 replacing
all-MiniLM-L6-v2, which is no longer supported. Nothing else changes -- but every text score does, so the
identity floors set for MiniLM (``SimilarityPolicy.text_floors_calibrated``) are not calibrated for it, and a
run stored under ``-10`` is not reused.

``oar-similarity-10`` against ``-9``, three changes -- OAR scope, donor eligibility, the text similarity
calculation, the score floor and the weights are untouched:

* **Identity is a stage of its own** (``oar.identity``). A shared material group no longer proves identity:
  a same-group candidate needs description evidence (a shared part number, a shared component type, or
  text similarity at or above ``same_group_minimum_text_similarity``), and two descriptions that name
  different component types are an ``IdentityStatus.REJECTED_COMPONENT_CONFLICT``. The two-stage shortlist
  applies the same rule before it picks, so a rejected candidate cannot re-enter through another channel.
* **The estimate is a weighted median** of each donor SAP value, not a weighted mean
  (``SimilarityPolicy.estimator``), with both recorded side by side in the trace.
* **Stage counts** (``StageCounts``) and the best-scoring rejected candidates travel on ``OarResult``.

``oar-similarity-6`` added a consistency floor against ``-5`` (approved deviation):
the OAR Max stock is raised to ROP + safety stock when the similarity-weighted Max
is below it. ROP, safety stock, donor selection, scoring and rounding are unchanged.
A run stored under ``-5`` has the plain weighted Max, so it is not reused.

``oar-similarity-5`` added a hard constraint against ``-4``: a neighbour must
share the target's material type (MTART) when both are known. A blank type on
either side does not block a match.

``oar-similarity-4`` changed the targets against ``-3``: every OAR
material-plant (MRP type ND / PD / blank), whatever its history -- not every
cold-start one. A VB / other material is no longer a target.

``oar-similarity-3`` changed the donors against ``-2``:

* A neighbour must have its current SAP ROP, safety stock and max stock all
  above zero, and lends those SAP values (rounded up to whole units) -- never
  I07's Phase 5 calculation.
* Minimum neighbours 3 (was 5), Top-K 5 (was 10). Top-K is not part of the
  run-reuse key, so this bump is what stops an old run being reused.

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
    MATERIAL_TYPE_MISMATCH = "MATERIAL_TYPE_MISMATCH"
    """Both material types are known and differ. A blank type on either side
    never produces this -- it is skipped, not counted as a mismatch."""
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


class IdentityStatus(StrEnum):
    """What the identity stage concluded about one candidate (``oar.identity``)."""

    CONFIRMED_PART_NUMBER = "CONFIRMED_PART_NUMBER"
    """Both descriptions carry the same part number."""
    CONFIRMED_GROUP_COMPONENT_TYPE = "CONFIRMED_GROUP_COMPONENT_TYPE"
    """Same material group, and both descriptions name the same (or a compatible) component type."""
    CONFIRMED_GROUP_TEXT = "CONFIRMED_GROUP_TEXT"
    """Same material group, and the descriptions are similar enough on more than generic words."""
    CONFIRMED_GROUP_UNVERIFIED = "CONFIRMED_GROUP_UNVERIFIED"
    """Same material group, accepted on the group alone because the same-group description requirement
    is switched off. Never produced by default."""
    CONFIRMED_TEXT = "CONFIRMED_TEXT"
    """Different (or unknown) material group, and text similarity at or above the identity floor."""
    NOT_ASSESSED = "NOT_ASSESSED"
    """The text requirement for this route is switched off. Never produced by default."""

    REJECTED_COMPONENT_CONFLICT = "REJECTED_COMPONENT_CONFLICT"
    """Both descriptions name a component type, and the two are incompatible -- a spray nozzle is not a
    filter whatever its material group says."""
    INSUFFICIENT_GROUP_ONLY = "INSUFFICIENT_GROUP_ONLY"
    """Same material group and nothing else: no shared part number, and neither enough text similarity nor
    (when the component type agrees) a shared specific word."""
    INSUFFICIENT_GENERIC_OVERLAP = "INSUFFICIENT_GENERIC_OVERLAP"
    """Same material group and enough text similarity, but no component type agrees, both descriptions carry
    specific words and share none of them, and what they do share is generic (FILTER PRESS, ANDRITZ) -- the
    similarity rests on generic words."""
    INSUFFICIENT_TEXT = "INSUFFICIENT_TEXT"
    """Different material group and text similarity below the identity floor."""
    INSUFFICIENT_NO_TEXT = "INSUFFICIENT_NO_TEXT"
    """Different material group and no text comparison to fall back on."""


class IdentityVerdict(NamedTuple):
    """One candidate's identity outcome, with the evidence behind it, for audit."""

    status: IdentityStatus
    reason: str
    """A sentence that says why; refusals start ``no identity match`` or ``component type conflict``."""
    target_types: tuple[str, ...] = ()
    candidate_types: tuple[str, ...] = ()
    shared_part_numbers: tuple[str, ...] = ()
    text_similarity: Decimal | None = None

    @property
    def passed(self) -> bool:
        return self.status.name.startswith(("CONFIRMED", "NOT_ASSESSED"))

    @property
    def conflict(self) -> bool:
        return self.status is IdentityStatus.REJECTED_COMPONENT_CONFLICT


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
    components: tuple[tuple[str, str], ...] = ()
    """What the value was made of, for audit: the text dimension lists its ``embedding`` cosine and its ``fuzzy``
    ratio (0..1) here."""

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
    identity: "IdentityVerdict | None" = None
    """The identity stage's verdict (``oar.identity``), or ``None`` when that stage did not run
    (``SimilarityPolicy.identity_validation`` off, or a score built by hand). A verdict that did not
    pass has already made ``evidence_sufficient`` False."""


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
    material_type: str | None = None
    """MTART. A hard constraint when known on both sides -- a spare part is never
    sized from a consumable -- and no constraint when either side is blank: an
    unmaintained type is not evidence of a different type."""


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


class EstimateDonor(NamedTuple):
    """One donor as the estimate saw it: its weight and the SAP values it lent."""

    material: str
    plant: str
    weight: Decimal
    safety_stock: int | None
    rop: int | None
    max_stock: int | None


class FieldAudit(NamedTuple):
    """One parameter's estimate under both methods, for audit.

    ``mean_raw`` and ``median_raw`` are before rounding; ``mean_final`` and ``median_final`` after the
    round-up. ``chosen`` names the one that became the estimate. ``material_difference`` is a diagnostic:
    the two finals differ by at least ``SimilarityPolicy.estimator_material_difference_ratio`` of the mean.
    """

    field: str
    mean_raw: Decimal
    median_raw: Decimal
    mean_final: int
    median_final: int
    chosen: str
    material_difference: bool


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
    strategy: str | None = None
    """``weighted_median`` or ``weighted_mean`` -- which estimator produced the numbers."""
    donors: tuple[EstimateDonor, ...] = ()
    """The donors that contributed, with their weights and SAP values."""
    audit: tuple[FieldAudit, ...] = ()
    """Mean and median per parameter, side by side."""


class StageCounts(NamedTuple):
    """How many candidates survived each step of retrieval, in order, for one target.

    ``structure_shortlist``, ``text_shortlist`` and ``fused_shortlist`` are what each channel picked (``None``
    when the partition was scored exhaustively, so there were no channels). ``merged_candidates`` is the
    de-duplicated union, which is exactly what received the exact score.
    """

    donor_pool: int
    """Distinct donor material-plants, after de-duplication."""
    duplicates_dropped: int
    hard_filter_passes: int
    """Same criticality, compatible material type, active, enough history, not the target itself."""
    structure_shortlist: int | None
    text_shortlist: int | None
    fused_shortlist: int | None
    merged_candidates: int
    identity_conflicts: int
    """Eligible donors whose description names a component type incompatible with the target's."""
    identity_insufficient: int
    """Eligible donors refused for lack of identity evidence (and not a conflict)."""
    identity_passes: int
    """Exactly scored candidates the identity stage confirmed."""
    score_floor_passes: int
    """Of those, the ones at or above ``minimum_similarity``."""
    final_neighbours: int

    def describe(self) -> str:
        def show(value: int | None) -> str:
            return "-" if value is None else str(value)

        return (
            f"donors={self.donor_pool};duplicates={self.duplicates_dropped};"
            f"hard_filter={self.hard_filter_passes};"
            f"structure={show(self.structure_shortlist)};text={show(self.text_shortlist)};"
            f"fused={show(self.fused_shortlist)};merged={self.merged_candidates};"
            f"identity_conflicts={self.identity_conflicts};"
            f"identity_insufficient={self.identity_insufficient};"
            f"identity_pass={self.identity_passes};score_floor={self.score_floor_passes};"
            f"final={self.final_neighbours}"
        )


class RetrievalTally(NamedTuple):
    """What retrieval knows about one target that scoring cannot: how each channel picked, how many
    duplicate donors it dropped, and how many eligible donors the identity stage refused across the WHOLE
    pool (not only the shortlist). ``None`` for a count means "not known to retrieval" -- the result then
    counts it from the candidates that were actually scored."""

    duplicates_dropped: int = 0
    structure_shortlist: int | None = None
    text_shortlist: int | None = None
    fused_shortlist: int | None = None
    identity_conflicts: int | None = None
    identity_insufficient: int | None = None


class RejectedCandidate(NamedTuple):
    """A scored candidate that was refused, kept so a refusal can be examined."""

    material: str
    plant: str
    combined: Decimal | None
    reason: str
    status: str | None = None


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
    stage_counts: StageCounts | None = None
    rejected_candidates: tuple[RejectedCandidate, ...] = ()
    """The best-scoring candidates refused on identity or evidence, highest combined score first."""
