"""Value objects for the I07 domain.

Two rules govern this module.

**Criticality and Risk stay separate.** Criticality is an attribute of the
material -- how badly its absence hurts production, sourced from ZMM065 and
fixed until someone reclassifies it. Risk is an attribute of one recommendation
-- how exposed this material is *right now* under its current parameters, and it
changes every time the pipeline runs. The frontend already models them as two
scales (``Criticality`` capitalised, ``RiskLevel`` lower-case) and collapsing
them would make "a critical material at low risk" inexpressible, which is
exactly the case a planner most wants to see.

**Wire values are the SAP/business vocabulary, not the UI's.** Criticality here
carries the five ZMM065 tiers the FRS calls authoritative (CRITICAL, IMPACT,
INSURANCE, NORMAL, OBSOLETE), not the frontend's four-value Low/Medium/High/
Critical scale. The frontend's scale is a *presentation* of these, produced by a
mapping that already exists in its SAP mapper. The backend keeps the source
taxonomy so nothing is lost on the way through; translating to the UI's scale is
the API layer's job in a later phase.

``Criticality`` below is not a local definition: it is the shared
``app.core.criticality.CriticalityTier`` -- the one port I07, I08 and I13 all
read through -- re-exported here under I07's contracts name so consumers of
this module are unaffected. See ``app.core.criticality`` for the source of
truth on the five tiers.
"""

from enum import StrEnum

# Re-export of the shared app.core.criticality tier, NOT a local redefinition.
# I07, I08 and I13 must share one Criticality vocabulary (see root CLAUDE.md);
# this alias exists only so the many I07 contracts that already say
# `Criticality` keep working unchanged.
from app.core.criticality import CriticalityTier as Criticality

__all__ = [
    "Criticality",
    "RiskLevel",
    "DemandPattern",
    "DataQualityGrade",
    "ConfidenceGrade",
    "ScopeDecision",
    "RecommendationStatus",
    "PolicyStatus",
    "LeadTimeSource",
]


class RiskLevel(StrEnum):
    """Exposure of one recommendation -- an attribute of the recommendation.

    Deliberately distinct from :class:`Criticality`. Lower-case values match the
    frontend's ``RiskLevel`` union so no translation is needed for this one.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DemandPattern(StrEnum):
    """ADI/CV2 classification outcome.

    The four Syntetos-Boylan quadrants, plus UNCLASSIFIED for materials that
    never reached classification -- they failed the history gate or are OAR. A
    material with no pattern is a real and common state, and representing it as
    an absent value rather than a guessed one keeps the history gate's decision
    visible downstream.
    """

    SMOOTH = "SMOOTH"
    ERRATIC = "ERRATIC"
    INTERMITTENT = "INTERMITTENT"
    LUMPY = "LUMPY"
    UNCLASSIFIED = "UNCLASSIFIED"


class DataQualityGrade(StrEnum):
    """Stage 1 data-quality grade."""

    GOOD = "GOOD"
    WARNING = "WARNING"
    LIMITED = "LIMITED"
    INSUFFICIENT = "INSUFFICIENT"


class ConfidenceGrade(StrEnum):
    """Confidence attached to every recommendation (Stage 6)."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ScopeDecision(StrEnum):
    """Three-state answer to "is this material-plant OAR?".

    UNKNOWN exists because a blank MRP type means "not maintained", which is not
    the same as "not OAR" -- 47% of rows in the live scan had no DISMM at all.
    Collapsing UNKNOWN into OUT_OF_SCOPE would silently drop those materials out
    of the OAR population and nobody would see it happen.
    """

    IN_SCOPE = "IN_SCOPE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    UNKNOWN = "UNKNOWN"


class RecommendationStatus(StrEnum):
    """Lifecycle state of a recommendation.

    Values match the frontend's ``RecommendationStatus`` union so the two ends
    agree on the workflow vocabulary.
    """

    PENDING_REVIEW = "Pending Review"
    IN_APPROVAL = "In Approval"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    RETURNED = "Returned"
    IMPLEMENTED = "Implemented"


class PolicyStatus(StrEnum):
    """Whether a policy version may be used to produce recommendations.

    DRAFT is the state every policy starts in and the one the current
    configuration sits in: the Solution Design requires that an unsigned policy
    blocks recommendations, so only SIGNED may be used for calculation.
    """

    DRAFT = "DRAFT"
    SIGNED = "SIGNED"
    SUPERSEDED = "SUPERSEDED"


class LeadTimeSource(StrEnum):
    """Where a lead-time figure came from.

    Provenance is a hard acceptance criterion: every recommendation's lead time
    must be traceable to the I11 Z-program output or explicitly flagged as a
    PLIFZ placeholder. Ownership between CALCULATED and I11_PROGRAM is itself
    unresolved (Phase 0, R5), so all three are representable.
    """

    CALCULATED = "CALCULATED"
    I11_PROGRAM = "I11_PROGRAM"
    PLANNED_DELIVERY_TIME = "PLANNED_DELIVERY_TIME"
