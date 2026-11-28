"""The Min-Max -> OAR decision table, as one pure function.

``evaluate`` takes one material-plant's attributes and consumption metrics and returns its category and
reason. No database, no clock, no settings: the thresholds come in as a
:class:`~app.initiatives.i7.policy.reverse_conversion.ReverseConversionPolicy`.

THE TABLE (first match wins -- the order below is the business rule, so do not reorder it)

There is no Manual Review category. A material a planner has to look at stays in Convert to OAR
(``ReverseCategory.RECOMMENDED_FOR_OAR``) and carries a confidence; its reason says why.

 1. Outside VM/VB, plant or deletion-flag scope, or    -> Excluded            Outside scope
    a price that is not in ZAR or NAD
 1b. Changed between OAR and Min-Max within the        -> Not Eligible        Recently changed planning type
    cooldown (12 months to begin with, configurable)
    (decision row 14 in the audit field; it is checked right after scope)
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

from dataclasses import replace

from app.core.criticality import CriticalityTier
from app.initiatives.i7.kpi_formulas.currency import TO_ZAR
from app.initiatives.i7.policy.reverse_conversion import ReverseConversionPolicy
from app.initiatives.i7.reverse_conversion.cooldown import cooldown_ends, in_cooldown
from app.initiatives.i7.reverse_conversion.metrics import add_months
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
    return (
        mrp_type in policy.mrp_types
        and plant in policy.plants
        and inputs.deletion_flag is not True
        and _price_currency_counted(inputs)
    )


def _price_currency_counted(inputs: MaterialPlantInputs) -> bool:
    """A material with a price is considered only when the price is in ZAR or NAD -- the currencies the OAR and
    Stock item money figures count. A price in any other currency, or with none stated, leaves the material out.
    A material with no price at all is kept: it simply has no value."""
    if inputs.unit_price is None:
        return True
    return inputs.price_currency in TO_ZAR


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


def _n(count: int | None, one: str, many: str | None = None) -> str:
    """``1 month`` / ``2 months``: a count with its noun in the right number."""
    shown = "unknown" if count is None else str(count)
    return f"{shown} {one if count == 1 else (many or one + 's')}"


def _mon(day) -> str:
    """A month as a person writes it: ``Apr 2026``; ``unknown`` when there is none."""
    return f"{day:%b %Y}" if day is not None else "unknown"


def _between(anchor, months: int) -> str:
    """The months a count covers, as dates: ``Nov 2025 and Oct 2026`` (read after "between") -- never "the anchor"."""
    return f"{_mon(add_months(anchor, -(months - 1)))} and {_mon(anchor)}"


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


def _decide(inputs: MaterialPlantInputs, policy: ReverseConversionPolicy) -> ReverseDecision:
    """The category and reason for one material-plant, by the approved decision table."""
    m = inputs.metrics
    issues, months = m.issues_latest_window, m.months_with_use_latest_window
    window = policy.window_months
    # Every sentence a planner reads names dates, not "the anchor" or "the latest 12 months".
    recent_months = _between(m.anchor, window)
    two_years = _between(m.anchor, policy.zero_use_window_months)
    last_used = _mon(m.last_valid_month)

    # 1. Scope.
    if not _in_scope(inputs, policy):
        return _decision(
            ReverseCategory.EXCLUDED,
            ReverseReason.OUTSIDE_SCOPE,
            "Outside the scope of this recommendation (MRP type, plant, deletion flag, or a price not in ZAR or NAD).",
            1,
        )

    # 1b. Cooldown: it moved between OAR and Min-Max recently, so it is left alone for now. Not a verdict on its
    # usage -- only that converting it back so soon would just bounce it between the two.
    if in_cooldown(inputs.group_changed_on, inputs.as_of, policy.cooldown_months):
        return _decision(
            ReverseCategory.NOT_ELIGIBLE,
            ReverseReason.RECENTLY_CHANGED,
            f"It changed between OAR and Min-Max in {_mon(inputs.group_changed_on)}. It is not recommended for "
            f"conversion again before {_mon(cooldown_ends(inputs.group_changed_on, policy.cooldown_months))}.",
            14,
        )

    # 2. Unknown criticality outranks every other condition below.
    tier = _tier(inputs.criticality)
    if tier is None:
        return _convert(
            ReverseReason.UNKNOWN_CRITICALITY,
            "Criticality is not recorded in SAP, so we cannot judge the risk of converting. A planner should check it first.",
            2,
            ReverseConfidence.LOW,
        )

    # 3. Protected tiers are never moved to OAR.
    if tier in policy.protected_criticality_tiers:
        return _decision(
            ReverseCategory.NOT_ELIGIBLE,
            ReverseReason.PROTECTED_CRITICALITY,
            f"Criticality is {tier.title()}, which always stays on Min-Max.",
            3,
        )

    # 4. OBSOLETE: converts to OAR, but a planner also decides keep-or-dispose, so Medium confidence.
    if tier == policy.obsolete_criticality_tier:
        return _convert(
            ReverseReason.OBSOLETE_CRITICALITY,
            "Criticality is Obsolete: SAP marks it as no longer needed. Ordering it only when required fits, but a "
            "planner should first decide whether to keep or dispose of the stock.",
            4,
            ReverseConfidence.MEDIUM,
        )

    # 5. The two latest-window measures must agree on whether the material was used at all.
    if (issues == 0) != (months == 0):
        return _convert(
            ReverseReason.INCONSISTENT_MOVEMENT,
            f"Between {recent_months}, SAP shows {_n(issues, 'issue')} but {_n(months, 'active month')}, which do not "
            "agree. A planner should check the movements first.",
            5,
            ReverseConfidence.LOW,
        )

    # 6 and 7. No issues across the whole zero-use window (latest + prior).
    if issues == 0 and m.issues_prior_window == 0:
        if m.lifetime_months_with_use == 0:
            return _convert(
                ReverseReason.NEVER_MOVED,
                f"Zero issues between {two_years}, and it has never moved.",
                6,
                ReverseConfidence.MEDIUM,
            )
        return _convert(
            ReverseReason.ZERO_USE_24M,
            f"Zero issues between {two_years}. It last moved in {last_used}.",
            7,
            ReverseConfidence.MEDIUM,
        )

    # 8. Not enough history to judge.
    span = m.history_span_months
    if span is None or span < policy.minimum_history_span_months:
        if span is None:
            history = "It has no consumption history yet, so we cannot judge it."
        else:
            history = (
                f"Only {_n(span, 'month')} of history ({_mon(m.first_valid_month)} to {last_used}); "
                f"at least {policy.minimum_history_span_months} are needed before we can judge it."
            )
        return _decision(
            ReverseCategory.NOT_ELIGIBLE,
            ReverseReason.INSUFFICIENT_HISTORY,
            history,
            8,
        )

    # 9. Lead time: unknown, or too long to rely on OAR.
    if not _lead_time_known(inputs.lead_time_days):
        return _convert(
            ReverseReason.LEAD_TIME,
            "Lead time is not recorded in SAP, so we cannot tell whether ordering only when needed is fast enough. "
            "A planner should check it.",
            9,
            ReverseConfidence.LOW,
        )
    if float(inputs.lead_time_days) > policy.maximum_lead_time_days:  # type: ignore[arg-type]
        return _convert(
            ReverseReason.LEAD_TIME,
            f"Lead time is {float(inputs.lead_time_days):g} days, longer than the "  # type: ignore[arg-type]
            f"{policy.maximum_lead_time_days} days that ordering only when needed can safely wait. A planner should check it.",
            9,
            ReverseConfidence.LOW,
        )

    # 10. Used before, but not in the latest window.
    if issues == 0:
        return _convert(
            ReverseReason.LAPSED,
            f"No issues between {recent_months}, but it was used before, last in {last_used}. "
            "A planner should decide whether demand has stopped.",
            10,
            ReverseConfidence.MEDIUM,
        )

    # 11. Frequent use keeps a material on Min-Max.
    if issues > policy.frequent_issues_above or months > policy.frequent_months_above:
        return _decision(
            ReverseCategory.NOT_ELIGIBLE,
            ReverseReason.FREQUENT_CONSUMPTION,
            f"Issued {_n(issues, 'time')} in {_n(months, 'month')} between {recent_months}. That is too frequent to "
            "order only when needed, so it stays on Min-Max.",
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
            f"Issued {_n(issues, 'time')} in {_n(months, 'month')} between {recent_months}, last in {last_used}. "
            "Demand is low and recent, so ordering only when needed fits.",
            12,
            ReverseConfidence.HIGH,
        )

    # 13. Every other active material goes to a planner, with the reason that stopped it.
    confidence = ReverseConfidence.MEDIUM
    if not sparse:
        reason = ReverseReason.BORDERLINE_FREQUENCY
        detail = (
            f"Issued {_n(issues, 'time')} in {_n(months, 'month')} between {recent_months}. More than a rare item but "
            "not frequent, so a planner should decide."
        )
    elif not recent:
        reason = ReverseReason.STALE_LAST_MOVEMENT
        detail = (
            f"Last used in {last_used}, more than {policy.maximum_months_since_last_movement} months before "
            f"{_mon(m.anchor)}. Demand may be fading, so a planner should decide."
        )
    elif short_record:
        reason = ReverseReason.LIFETIME_CRITERION
        detail = (
            f"A new material, first used in {_mon(m.first_valid_month)} and used in only "
            f"{_n(m.lifetime_months_with_use, 'month')} so far. It is too early to tell, so a planner should decide."
        )
    else:
        reason = ReverseReason.UNCLASSIFIED_ACTIVE
        detail = "It is active but matches none of the usual reasons, so a planner should check it."
        confidence = ReverseConfidence.LOW
    return _convert(reason, detail, 13, confidence)


SAW = "What we saw"
MEANS = "What it means"
TODO = "What to do"

_PLANNER = "A planner should decide."


def _points(decision: ReverseDecision, inputs: MaterialPlantInputs, policy: ReverseConversionPolicy) -> tuple[tuple[str, str], ...]:
    """The reason as three short labelled lines, for a person who scans rather than reads: what the data shows (with
    dates), what that means for ordering, and what to do. Words only -- the decision is already made, and every
    figure here is one the decision used."""
    if decision.category is ReverseCategory.EXCLUDED:
        return ()
    m = inputs.metrics
    issues, months = m.issues_latest_window, m.months_with_use_latest_window
    recent = _between(m.anchor, policy.window_months)
    two_years = _between(m.anchor, policy.zero_use_window_months)
    last, first = _mon(m.last_valid_month), _mon(m.first_valid_month)
    used = f"Issued {_n(issues, 'time')} in {_n(months, 'month')} between {recent}."
    stays = "Nothing: it stays on Min-Max."
    confirm = "A planner should confirm it can be ordered only when needed."

    match decision.reason:
        case ReverseReason.SPARSE_RECURRING_DEMAND:
            saw, means, todo = f"{used} Last used {last}.", "Demand is low and recent, so ordering only when needed fits.", "Convert it if you agree. The approvers confirm it."
        case ReverseReason.BORDERLINE_FREQUENCY:
            saw, means, todo = used, "More than a rare item, but not frequent: it is not clearly one or the other.", _PLANNER
        case ReverseReason.STALE_LAST_MOVEMENT:
            saw = f"Last used {last}, more than {policy.maximum_months_since_last_movement} months before {_mon(m.anchor)}."
            means, todo = "Demand may be fading.", _PLANNER
        case ReverseReason.LIFETIME_CRITERION:
            saw = f"First used {first}; used in only {_n(m.lifetime_months_with_use, 'month')} so far."
            means, todo = "It is a new material, so it is too early to tell.", _PLANNER
        case ReverseReason.LAPSED:
            saw = f"No issues between {recent}. It was used before, last in {last}."
            means, todo = "Demand may have stopped, so the Min-Max stock may no longer be needed.", "A planner should decide whether demand has stopped."
        case ReverseReason.NEVER_MOVED:
            saw, means, todo = f"No issues between {two_years}. It has never moved.", "There is no demand to hold stock for.", confirm
        case ReverseReason.ZERO_USE_24M:
            saw, means, todo = f"No issues between {two_years}. It last moved in {last}.", "There is no recent demand to hold stock for.", confirm
        case ReverseReason.LEAD_TIME:
            if not _lead_time_known(inputs.lead_time_days):
                saw = "Lead time is not recorded in SAP."
                means = "Without it we cannot tell whether ordering only when needed is fast enough."
            else:
                saw = f"Lead time is {float(inputs.lead_time_days):g} days; the limit is {policy.maximum_lead_time_days} days."  # type: ignore[arg-type]
                means = "Ordering only when needed may be too slow."
            todo = "A planner should check the lead time."
        case ReverseReason.UNKNOWN_CRITICALITY:
            saw, means, todo = "Criticality is not recorded in SAP.", "Without it we cannot judge the risk of converting.", "A planner should check it first."
        case ReverseReason.OBSOLETE_CRITICALITY:
            saw, means = "Criticality is Obsolete: SAP marks it as no longer needed.", "Ordering it only when required fits."
            todo = "A planner should decide whether to keep or dispose of the stock."
        case ReverseReason.INCONSISTENT_MOVEMENT:
            saw = f"Between {recent}, SAP shows {_n(issues, 'issue')} but {_n(months, 'active month')}."
            means, todo = "The two counts should agree, so the movements may be wrong.", "A planner should check the movements first."
        case ReverseReason.PROTECTED_CRITICALITY:
            saw = f"Criticality is {(inputs.criticality or '').strip().title()}."
            means, todo = "These materials always stay on Min-Max.", stays
        case ReverseReason.INSUFFICIENT_HISTORY:
            span = m.history_span_months
            saw = (
                "It has no consumption history yet."
                if span is None
                else f"Only {_n(span, 'month')} of history ({first} to {last})."
            )
            means, todo = f"At least {policy.minimum_history_span_months} months are needed to judge it.", "Nothing yet: it stays on Min-Max."
        case ReverseReason.FREQUENT_CONSUMPTION:
            saw, means, todo = used, "Too frequent to order only when needed.", stays
        case ReverseReason.RECENTLY_CHANGED:
            saw = f"It changed between OAR and Min-Max in {_mon(inputs.group_changed_on)}."
            ends = _mon(cooldown_ends(inputs.group_changed_on, policy.cooldown_months))
            means, todo = "Converting it back so soon would just bounce it between the two.", f"Nothing before {ends}."
        case _:
            saw, means, todo = decision.detail, "It matches none of the usual reasons.", _PLANNER
    return ((SAW, saw), (MEANS, means), (TODO, todo))


def evaluate(inputs: MaterialPlantInputs, policy: ReverseConversionPolicy) -> ReverseDecision:
    """The category, reason and confidence for one material-plant, by the approved decision table, with the reason
    also written as three short labelled lines (``points``)."""
    decision = _decide(inputs, policy)
    return replace(decision, points=_points(decision, inputs, policy))
