"""Hard-constraint filtering.

Solution Design, Stage 5::

    Candidate must:
      - be in the SAME criticality class
      - have >= 12 months of consumption history
      - be an active material

All three are hard. They run *before* any similarity is computed -- partly
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

from app.initiatives.i7.oar.types import CandidateAttributes, EligibilityRejection


def reject_reason(
    target: CandidateAttributes,
    candidate: CandidateAttributes,
    minimum_history_months: int,
) -> EligibilityRejection | None:
    """Why ``candidate`` cannot be a neighbour of ``target``, or ``None``.

    Ordered cheapest first, and criticality before everything else because it
    eliminates the most candidates on this data.
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
    if candidate.criticality != target.criticality:
        return EligibilityRejection.CRITICALITY_MISMATCH

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
) -> tuple[list[CandidateAttributes], dict[str, int]]:
    """Eligible candidates, plus a count of why the rest were rejected."""
    eligible: list[CandidateAttributes] = []
    rejections: dict[str, int] = {}

    for candidate in candidates:
        reason = reject_reason(target, candidate, minimum_history_months)
        if reason is None:
            eligible.append(candidate)
        else:
            rejections[reason.value] = rejections.get(reason.value, 0) + 1

    return eligible, rejections
