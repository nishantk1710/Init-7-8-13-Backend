"""OAR -> Min-Max conversion eligibility.

The rule in force::

    ConversionEligible =
        OarMaterialPlant                      -- Stage 1: MARC.DISPO = OAR and MARC.DISMM in {ND, PD} or blank
    AND NOT Obsolete                          -- OBSOLETE is never eligible
    AND (    ConsumptionCount12M > 4
          OR Criticality in {CRITICAL, IMPACT, INSURANCE} )

Either trigger on its own is enough; the scope and OBSOLETE checks are gates
that always apply.

Identification and conversion are separate decisions. This module never
re-derives OAR status: it reads the Stage 1 verdict (``MaterialFeature.oar_scope``).
The DISPO and DISMM rule itself lives in :func:`app.initiatives.i7.policy.oar.current_oar_policy`.
A material-plant Stage 1 put OUT_OF_SCOPE is not evaluated at all -- the
builder does not call this for it.

**Three-valued logic.** Each condition answers True, False or ``None`` (cannot
be determined). A gate that definitely fails makes the verdict NOT_ELIGIBLE
whatever else is missing. Otherwise one trigger that definitely holds is
enough; only when no trigger holds and one of them could not be determined is
the verdict UNKNOWN -- missing data is never read as "does not qualify".

**What is counted.** ``ConsumptionCount12M`` is a transaction-level count --
MSEG issue movements (201/261) minus reversal movements (202/262), trailing 12
months ending at the extract's own last staged month -- computed and stored on
``MaterialFeature.consumption_count_12m`` by Phase 3 (see
``features/builder.py::consumption_count_12m``). It is NOT ``non_zero_periods``
(a count of months with non-zero demand, which feeds ADI/CV-squared only): a
material with 2 issues in January, 1 in February, 3 in March and 1 in April
has ``consumption_count_12m = 7`` and ``non_zero_periods = 4`` -- the rule must
see 7, not 4.

The tier set and the exclusions are policy, not code -- see
:func:`app.initiatives.i7.policy.thresholds.current_conversion_trigger_policy`.
"""

from app.core.criticality import CriticalityTier
from app.initiatives.i7.contracts.enums import ScopeDecision
from app.initiatives.i7.policy import ConversionTriggerPolicy
from app.initiatives.i7.recommendations.types import (
    ConversionDecision,
    ConversionEligibility,
    ConversionTrigger,
)

_KNOWN_TIERS = frozenset(tier.value for tier in CriticalityTier)


def _times(count: int) -> str:
    return f"{count} time" if count == 1 else f"{count} times"


def _tier(criticality: str | None) -> str:
    return criticality.strip().upper() if criticality else ""


def _scope_condition(oar_scope: str | None) -> tuple[bool | None, str]:
    """Is this material-plant OAR according to Stage 1? Returns ``(met, detail)``."""
    if oar_scope == ScopeDecision.IN_SCOPE:
        return True, ""
    if oar_scope == ScopeDecision.OUT_OF_SCOPE:
        return (
            False,
            "It is not ordered on demand (OAR) at this plant, so it is not "
            "considered for Min-Max ordering.",
        )
    return None, "Whether it is ordered on demand (OAR) at this plant could not be determined."


def _not_excluded_condition(
    criticality: str | None, policy: ConversionTriggerPolicy
) -> tuple[bool, str]:
    """The OBSOLETE gate. Definite either way: a missing tier is not obsolete."""
    tier = _tier(criticality)
    if tier and tier in policy.excluded_criticality_tiers:
        return (
            False,
            f"It is classified {tier.title()}, and such materials are never moved "
            f"to Min-Max ordering.",
        )
    return True, ""


def _consumption_condition(
    consumption_count_12m: int | None, policy: ConversionTriggerPolicy
) -> tuple[bool | None, str]:
    """More than the threshold net issue transactions in 12 months? ``(met, detail)``.

    ``consumption_count_12m`` is the MSEG issue-transaction count (reversals
    netted), NOT ``non_zero_periods`` -- see the module docstring.
    """
    if consumption_count_12m is None:
        return None, "The last 12 months of usage could not be counted for this material."
    used = f"It was used {_times(consumption_count_12m)} in the last 12 months"
    if consumption_count_12m > policy.consumption_count_threshold:
        return (
            True,
            f"{used}, more than the {policy.consumption_count_threshold} needed to "
            f"qualify for Min-Max ordering.",
        )
    return (
        False,
        f"{used}, which does not exceed the {policy.consumption_count_threshold} "
        f"needed to qualify for Min-Max ordering.",
    )


def _criticality_condition(
    criticality: str | None, policy: ConversionTriggerPolicy
) -> tuple[bool | None, str]:
    """Is the tier one that qualifies on its own? ``(met, detail)``."""
    tier = _tier(criticality)
    if not tier:
        return None, "Its criticality level is not known."
    if tier not in _KNOWN_TIERS:
        # Not guessed into a tier: text we cannot interpret is not evidence
        # that the material fails the condition.
        return None, "Its criticality level is not one the system recognises."
    if policy.criticality_trigger_tiers is None:
        return None, "The business has not yet confirmed which criticality levels qualify."
    if tier in policy.criticality_trigger_tiers:
        return True, f"Its criticality is {tier.title()}, which qualifies it for Min-Max ordering."
    qualifying = ", ".join(t.title() for t in policy.criticality_trigger_tiers)
    return (
        False,
        f"Its criticality is {tier.title()}, which is not one of the levels "
        f"that qualify ({qualifying}).",
    )


def evaluate(
    consumption_count_12m: int | None,
    criticality: str | None,
    policy: ConversionTriggerPolicy,
    oar_scope: str | None,
) -> ConversionDecision:
    """The scope and OBSOLETE gates, then the OR of usage and criticality.

    ``oar_scope`` is Stage 1's stored verdict (a ``ScopeDecision`` value).
    ``consumption_count_12m`` must be the MSEG transaction count (see the
    module docstring), not ``non_zero_periods``.
    """
    gates = (_scope_condition(oar_scope), _not_excluded_condition(criticality, policy))
    count_met, count_detail = _consumption_condition(consumption_count_12m, policy)
    tier_met, tier_detail = _criticality_condition(criticality, policy)
    excluded = gates[1][0] is False

    def decision(
        eligibility: ConversionEligibility, trigger: ConversionTrigger, detail: str
    ) -> ConversionDecision:
        return ConversionDecision(
            eligibility=eligibility,
            trigger=trigger,
            consumption_count_12m=consumption_count_12m,
            consumption_count_threshold=policy.consumption_count_threshold,
            # An excluded tier is a definite "no" for the criticality evidence,
            # never "unknown".
            production_impact=False if excluded else tier_met,
            detail=detail,
        )

    failed_gates = [detail for met, detail in gates if met is False]
    if failed_gates:
        return decision(
            ConversionEligibility.NOT_ELIGIBLE, ConversionTrigger.NONE, " ".join(failed_gates)
        )

    if count_met is False and tier_met is False:
        return decision(
            ConversionEligibility.NOT_ELIGIBLE,
            ConversionTrigger.NONE,
            f"{count_detail} {tier_detail}",
        )

    undetermined_gates = [detail for met, detail in gates if met is None]
    fired = [detail for met, detail in ((count_met, count_detail), (tier_met, tier_detail)) if met]
    if fired and not undetermined_gates:
        if count_met and tier_met:
            trigger = ConversionTrigger.CONSUMPTION_AND_CRITICALITY
        elif count_met:
            trigger = ConversionTrigger.CONSUMPTION_FREQUENCY
        else:
            trigger = ConversionTrigger.PRODUCTION_IMPACT
        return decision(ConversionEligibility.ELIGIBLE, trigger, " ".join(fired))

    undetermined = undetermined_gates + [
        detail
        for met, detail in ((count_met, count_detail), (tier_met, tier_detail))
        if met is None
    ]
    return decision(ConversionEligibility.UNKNOWN, ConversionTrigger.UNKNOWN, " ".join(undetermined))
