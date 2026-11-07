"""Hard-constraint filtering.

Solution Design, Stage 5::

    Candidate must:
      - be in the SAME criticality class
      - have >= 12 months of consumption history
      - be an active material

All three are hard. "Same criticality class" is one-way wider for NORMAL targets under the business rule
in ``SimilarityPolicy.donor_criticality_tiers``: a NORMAL target may also borrow from IMPACT, CRITICAL and
INSURANCE donors; every other tier keeps to its own. They run *before* any similarity is computed -- partly
because embedding a description costs far more than comparing two strings, and
partly because an ineligible candidate must never influence a ranking.

**Unknown is a rejection, not a pass.** Missing criticality does not mean
"probably the same class", unknown status does not mean "probably active", and
absent history does not mean "probably enough". Each permissive reading would
manufacture neighbours out of missing data, and on this extract -- where
criticality reaches 1.7% of material-plants -- a permissive criticality rule
would match nearly everything to nearly everything.

The constraints are not weakened to raise the neighbour count. A material with
no eligible neighbour is a real and reportable outcome.
"""

from collections.abc import Mapping

from app.initiatives.i7.oar.types import CandidateAttributes, EligibilityRejection

DonorTiers = Mapping[str, frozenset[str]]
"""Target tier -> the donor tiers it may borrow from. A tier that is not a key borrows from itself only."""


def allowed_donor_tiers(target_tier: str, donor_tiers: DonorTiers | None) -> frozenset[str]:
    """The donor tiers a target of ``target_tier`` may borrow from; its own tier is always one of them."""
    if donor_tiers is None:
        return frozenset({target_tier})
    return frozenset(donor_tiers.get(target_tier, ())) | {target_tier}


def material_types_conflict(target_type: str | None, candidate_type: str | None) -> bool:
    """Both material types known and different. Blank on either side never conflicts."""
    return bool(target_type) and bool(candidate_type) and target_type != candidate_type


def reject_reason(
    target: CandidateAttributes,
    candidate: CandidateAttributes,
    minimum_history_months: int,
    donor_tiers: DonorTiers | None = None,
) -> EligibilityRejection | None:
    """Why ``candidate`` cannot be a neighbour of ``target``, or ``None``.

    Ordered cheapest first, and criticality before everything else because it
    eliminates the most candidates on this data. ``donor_tiers`` widens the tiers a target may borrow
    from (``SimilarityPolicy.donor_criticality_tiers``); ``None`` is the same-tier rule.
    """
    if (
        candidate.sap_material_number == target.sap_material_number
        and candidate.sap_plant_code == target.sap_plant_code
    ):
        return EligibilityRejection.SELF

    if target.criticality is None:
        return EligibilityRejection.CRITICALITY_MISSING_TARGET
    if candidate.criticality is None:
        return EligibilityRejection.CRITICALITY_MISSING_CANDIDATE
    if candidate.criticality not in allowed_donor_tiers(target.criticality, donor_tiers):
        return EligibilityRejection.CRITICALITY_MISMATCH

    if material_types_conflict(target.material_type, candidate.material_type):
        return EligibilityRejection.MATERIAL_TYPE_MISMATCH

    if candidate.is_active is None:
        return EligibilityRejection.ACTIVE_STATUS_UNKNOWN
    if not candidate.is_active:
        return EligibilityRejection.INACTIVE

    if candidate.history_months is None:
        return EligibilityRejection.HISTORY_UNKNOWN
    if candidate.history_months < minimum_history_months:
        return EligibilityRejection.INSUFFICIENT_HISTORY

    return None


def filter_candidates(
    target: CandidateAttributes,
    candidates: list[CandidateAttributes],
    minimum_history_months: int,
    donor_tiers: DonorTiers | None = None,
) -> tuple[list[CandidateAttributes], dict[str, int]]:
    """Eligible candidates, plus a count of why the rest were rejected."""
    eligible: list[CandidateAttributes] = []
    rejections: dict[str, int] = {}

    for candidate in candidates:
        reason = reject_reason(target, candidate, minimum_history_months, donor_tiers)
        if reason is None:
            eligible.append(candidate)
        else:
            rejections[reason.value] = rejections.get(reason.value, 0) + 1

    return eligible, rejections
