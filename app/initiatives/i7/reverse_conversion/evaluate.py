"""The Min-Max -> OAR decision table, as one pure function.

``evaluate`` takes one material-plant's attributes and consumption metrics and returns its category and
reason. No database, no clock, no settings: the thresholds come in as a
:class:`~app.initiatives.i7.policy.reverse_conversion.ReverseConversionPolicy`.

THE TABLE (first match wins -- the order below is the business rule, so do not reorder it)

There is no Manual Review category. A material a planner has to look at stays in Convert to OAR
(``ReverseCategory.RECOMMENDED_FOR_OAR``) and carries a confidence; its reason says why.

 1. Outside VM/VB, plant or deletion-flag scope        -> Excluded            Outside scope
 2. Criticality unknown (NULL)                         -> Convert to OAR LOW  Unknown criticality
 3. CRITICAL, IMPACT or INSURANCE                      -> Not Eligible        Protected criticality
 4. OBSOLETE                                           -> Convert to OAR MED  Obsolete
 5. Issues and months-with-use disagree (latest 12m)   -> Convert to OAR LOW  Inconsistent movement data
 6. No issues in 24 months, never moved                -> Convert to OAR MED  Zero issues in 24 months and never moved
 7. No issues in 24 months, moved before               -> Convert to OAR MED  Zero issues in 24 months but moved before
 8. History span under 12 months, or undefined         -> Not Eligible        Insufficient history
 9. Lead time unknown or over 90 days                  -> Convert to OAR LOW  Lead time
10. No issues in the latest 12m, but used before       -> Convert to OAR MED  Lapsed
11. More than 4 issues OR more than 4 months with use  -> Not Eligible        Frequent consumption
12. Sparse, recent, and the lifetime criterion holds   -> Convert to OAR HIGH Sparse recurring demand
13. Any other active material                          -> Convert to OAR MED  Borderline frequency /
                                                                              Stale last movement /
                                                                              Lifetime criterion

Confidence: High where the demand pattern itself says OAR fits (row 12); Medium where the pattern is
borderline or has gone quiet, or the material is Obsolete (4, 6, 7, 10, 13); Low where an input the rule needs is missing or
contradictory (unknown criticality, inconsistent data, unknown or long lead time).

Unknown criticality is rule 2 on purpose: it outranks zero use, obsolescence and history, because a
material whose criticality nobody knows must be looked at by a person, whatever else is true of it.

Rule 5 looks at the latest 12 months only. A disagreement in the earlier window does not trigger it.

Stock, price, MINBE and MABST are deliberately not read here. They rank materials inside a category;
they never decide one, and a material with zero stock or no price is evaluated like any other.
"""

from app.core.criticality import CriticalityTier
from app.initiatives.i7.policy.reverse_conversion import ReverseConversionPolicy
from app.initiatives.i7.reverse_conversion.types import (
    ConsumptionMetrics,
    MaterialPlantInputs,
    ReverseCategory,
    ReverseConfidence,
    ReverseDecision,
    ReverseReason,
)

_KNOWN_TIERS = frozenset(tier.value for tier in CriticalityTier)


def _in_scope(inputs: MaterialPlantInputs, policy: ReverseConversionPolicy) -> bool:
    mrp_type = (inputs.mrp_type or "").strip().upper()
    plant = (inputs.plant or "").strip()
    # None (never maintained) is not deletion; only an explicit True is. The feature build treats it the same way.
    return mrp_type in policy.mrp_types and plant in policy.plants and inputs.deletion_flag is not True


def _tier(criticality: str | None) -> str | None:
    """The criticality tier, or None when it is missing or not one the system recognises.

    A text we cannot interpret is not evidence of a tier, so it is unknown -- never read as NORMAL.
    """
    text = (criticality or "").strip().upper()
    return text if text in _KNOWN_TIERS else None


def _lead_time_known(value: object) -> bool:
    """A lead time is known only when it is a positive number (SAP uses 0 for 'not maintained')."""
    try:
        return value is not None and float(value) > 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _is_new_with_short_record(metrics: ConsumptionMetrics, policy: ReverseConversionPolicy) -> bool:
    """A new material (first valid movement within the new-material age) with too few lifetime months."""
    return (
        metrics.material_age_months is not None
        and metrics.material_age_months <= policy.new_material_age_months
        and metrics.lifetime_months_with_use < policy.new_material_minimum_lifetime_months
    )


def _decision(
    category: ReverseCategory,
    reason: ReverseReason,
    detail: str,
    rule: int,
    confidence: ReverseConfidence | None = None,
) -> ReverseDecision:
    return ReverseDecision(category=category, reason=reason, detail=detail, rule=rule, confidence=confidence)


def _convert(reason: ReverseReason, detail: str, rule: int, confidence: ReverseConfidence) -> ReverseDecision:
    """A Convert to OAR decision. Every material that reaches OAR evaluation carries a confidence."""
    return _decision(ReverseCategory.RECOMMENDED_FOR_OAR, reason, detail, rule, confidence)


def evaluate(inputs: MaterialPlantInputs, policy: ReverseConversionPolicy) -> ReverseDecision:
    """The category and reason for one material-plant, by the approved decision table."""
    m = inputs.metrics
    issues, months = m.issues_latest_window, m.months_with_use_latest_window
    window = policy.window_months

    # 1. Scope.
    if not _in_scope(inputs, policy):
        return _decision(
            ReverseCategory.EXCLUDED,
            ReverseReason.OUTSIDE_SCOPE,
            "Outside the scope of this recommendation (MRP type, plant or deletion flag).",
            1,
        )

    # 2. Unknown criticality outranks every other condition below.
    tier = _tier(inputs.criticality)
    if tier is None:
        return _convert(
            ReverseReason.UNKNOWN_CRITICALITY,
            "Its criticality is not known, so a planner must evaluate it manually before converting.",
            2,
            ReverseConfidence.LOW,
        )

    # 3. Protected tiers are never moved to OAR.
    if tier in policy.protected_criticality_tiers:
        return _decision(
            ReverseCategory.NOT_ELIGIBLE,
            ReverseReason.PROTECTED_CRITICALITY,
            f"Its criticality is {tier.title()}, which is never recommended for OAR.",
            3,
        )

    # 4. OBSOLETE: converts to OAR, but a planner also decides keep-or-dispose, so Medium confidence.
    if tier == policy.obsolete_criticality_tier:
        return _convert(
            ReverseReason.OBSOLETE_CRITICALITY,
            "Its criticality is Obsolete: SAP marks it as no longer needed in operation. Ordering it only when "
            "required is a natural fit, but a planner must first decide whether the stock should be kept or disposed of.",
            4,
            ReverseConfidence.MEDIUM,
        )

    # 5. The two latest-window measures must agree on whether the material was used at all.
    if (issues == 0) != (months == 0):
        return _convert(
            ReverseReason.INCONSISTENT_MOVEMENT,
            f"In the latest {window} months it shows {issues} net issue transactions but {months} months "
            "with consumption; the two disagree, so a planner must check the movements before converting.",
            5,
            ReverseConfidence.LOW,
        )

    # 6 and 7. No issues across the whole zero-use window (latest + prior).
    if issues == 0 and m.issues_prior_window == 0:
        if m.lifetime_months_with_use == 0:
            return _convert(
                ReverseReason.NEVER_MOVED,
                f"Zero issues in {policy.zero_use_window_months} months and never moved.",
                6,
                ReverseConfidence.MEDIUM,
            )
        return _convert(
            ReverseReason.ZERO_USE_24M,
            f"Zero issues in {policy.zero_use_window_months} months but moved before.",
            7,
            ReverseConfidence.MEDIUM,
        )

    # 8. Not enough history to judge.
    span = m.history_span_months
    if span is None or span < policy.minimum_history_span_months:
        shown = "no" if span is None else f"only {span}"
        return _decision(
            ReverseCategory.NOT_ELIGIBLE,
            ReverseReason.INSUFFICIENT_HISTORY,
            f"It has {shown} months of consumption history; at least "
            f"{policy.minimum_history_span_months} are needed.",
            8,
        )

    # 9. Lead time: unknown, or too long to rely on OAR.
    if not _lead_time_known(inputs.lead_time_days):
        return _convert(
            ReverseReason.LEAD_TIME,
            "Its lead time is not known, so a planner must evaluate it manually before converting.",
            9,
            ReverseConfidence.LOW,
        )
    if float(inputs.lead_time_days) > policy.maximum_lead_time_days:  # type: ignore[arg-type]
        return _convert(
            ReverseReason.LEAD_TIME,
            f"Its lead time is {float(inputs.lead_time_days):g} days, over the "  # type: ignore[arg-type]
            f"{policy.maximum_lead_time_days} that OAR can be relied on for, so a planner must evaluate it manually.",
            9,
            ReverseConfidence.LOW,
        )

    # 10. Used before, but not in the latest window.
    if issues == 0:
        return _convert(
            ReverseReason.LAPSED,
            f"It had no issues in the latest {window} months but was used before, so a planner must "
            "decide whether the demand has stopped.",
            10,
            ReverseConfidence.MEDIUM,
        )

    # 11. Frequent use keeps a material on Min-Max.
    if issues > policy.frequent_issues_above or months > policy.frequent_months_above:
        return _decision(
            ReverseCategory.NOT_ELIGIBLE,
            ReverseReason.FREQUENT_CONSUMPTION,
            f"It had {issues} issues across {months} months in the latest {window} months, which is "
            "frequent enough to keep it on Min-Max.",
            11,
        )

    # 12. Sparse, recent demand -- the one path to a recommendation.
    sparse = issues <= policy.recommended_max_issues and months <= policy.recommended_max_months
    recent = (
        m.months_since_last_movement is not None
        and m.months_since_last_movement <= policy.maximum_months_since_last_movement
    )
    short_record = _is_new_with_short_record(m, policy)
    if sparse and recent and not short_record:
        return _convert(
            ReverseReason.SPARSE_RECURRING_DEMAND,
            f"It had {issues} issues across {months} months in the latest {window} months, last used "
            f"{m.months_since_last_movement} months before the anchor.",
            12,
            ReverseConfidence.HIGH,
        )

    # 13. Every other active material goes to a planner, with the reason that stopped it.
    confidence = ReverseConfidence.MEDIUM
    if not sparse:
        reason = ReverseReason.BORDERLINE_FREQUENCY
        detail = f"It had {issues} issues across {months} months in the latest {window} months, between sparse and frequent."
    elif not recent:
        reason = ReverseReason.STALE_LAST_MOVEMENT
        shown = "unknown" if m.months_since_last_movement is None else str(m.months_since_last_movement)
        detail = (
            f"Its latest movement was {shown} months before the anchor, longer than the "
            f"{policy.maximum_months_since_last_movement} allowed for a recommendation."
        )
    elif short_record:
        reason = ReverseReason.LIFETIME_CRITERION
        detail = (
            f"It is a new material with only {m.lifetime_months_with_use} months of use so far; at least "
            f"{policy.new_material_minimum_lifetime_months} are needed to recommend it."
        )
    else:
        reason = ReverseReason.UNCLASSIFIED_ACTIVE
        detail = "It is active but matches none of the review reasons; a planner must check it."
        confidence = ReverseConfidence.LOW
    return _convert(reason, detail, 13, confidence)
