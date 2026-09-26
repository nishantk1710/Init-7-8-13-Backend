"""OAR -> Min-Max conversion eligibility.

Formula Reference::

    ConversionEligible =
        (ConsumptionCount12M > 4)
     OR (CriticalOrSignificantProductionImpact = TRUE)
     OR (I13_HOD_Approved = TRUE)

    If none is true, no OAR -> Min-Max conversion recommendation is generated.

Identification and conversion are separate decisions. A material already
routed to OAR by Phase 3 does not automatically get a conversion
recommendation -- it needs one of these three triggers to fire, independently
evaluated here.

**What is actually counted for trigger 1.** ``ConsumptionCount12M`` is a
transaction-level count -- MSEG issue movements (201/261) minus reversal
movements (202/262), trailing 12 months ending at the extract's own last
staged month -- computed and stored on
``MaterialFeature.consumption_count_12m`` by Phase 3 (see
``features/builder.py::consumption_count_12m``). It is NOT
``non_zero_periods`` (a count of months with non-zero demand, which continues
to feed ADI/CV-squared only): a material with 2 issues in January, 1 in
February, 3 in March and 1 in April has ``consumption_count_12m = 7`` and
``non_zero_periods = 4`` -- the SOP 3.1.1 trigger must see 7, not 4.

**Trigger 2's tier set is business-confirmed as NORMAL.** The FRS's own two
statements disagree on wording ("Critical classification" vs "Critical or of
significant production impact"), but the business decision confirmed for this
system is that the NORMAL tier is what should trigger conversion review here
-- not CRITICAL/IMPACT, despite the FRS wording. See
:func:`app.initiatives.i7.policy.thresholds.current_conversion_trigger_policy`,
which is where this resolved rule lives (mirroring
:func:`app.initiatives.i7.policy.oar.current_oar_policy`'s pattern).

**Trigger 3 needs an I13 ledger that does not exist.** `app/initiatives/i13/` is
an empty extension point. The trigger is evaluated through an injected lookup
so the interface exists now and Phase 7 does not block on I13's absence --
absent a lookup, it reports UNKNOWN rather than assuming no request exists.
"""

from typing import Protocol

from app.initiatives.i7.errors import PolicyNotConfiguredError
from app.initiatives.i7.policy import ConversionTriggerPolicy
from app.initiatives.i7.recommendations.types import (
    ConversionDecision,
    ConversionEligibility,
    ConversionTrigger,
)


class HodApprovalLookup(Protocol):
    """Where a signed I13 HOD-approved request would come from.

    No implementation exists yet -- I13 is an empty stub. Returns ``None`` when
    no ledger is available at all, which is the only case reachable today.
    """

    def hod_approved(self, material: str, plant: str) -> bool | None: ...


class NoHodLedgerAvailable:
    """The only implementation that exists today: there is no I13 ledger."""

    def hod_approved(self, material: str, plant: str) -> bool | None:
        return None


def _consumption_trigger(
    consumption_count_12m: int | None, policy: ConversionTriggerPolicy
) -> tuple[bool | None, str, bool]:
    """Returns ``(fired, detail, is_unknown)``.

    ``consumption_count_12m`` is the MSEG issue-transaction count (reversals
    netted), NOT ``non_zero_periods`` -- see the module docstring.

    A *disabled* trigger definitely does not fire -- ``fired=False`` -- and
    must not be conflated with an *enabled* trigger whose input is missing,
    which genuinely cannot be answered either way.
    """
    if not policy.enable_consumption_trigger:
        return False, "consumption trigger disabled by policy", False
    if consumption_count_12m is None:
        return None, "12-month consumption transaction count unavailable", True
    fired = consumption_count_12m > policy.consumption_count_threshold
    return (
        fired,
        f"consumption count (trailing 12 months) = {consumption_count_12m} "
        f"({'>' if fired else '<='} {policy.consumption_count_threshold})",
        False,
    )


def _production_impact_trigger(
    criticality: str | None, policy: ConversionTriggerPolicy
) -> tuple[bool | None, str, bool]:
    """Returns ``(fired, detail, is_unknown)``. See :func:`_consumption_trigger`."""
    if not policy.enable_criticality_trigger:
        return False, "production-impact trigger disabled by policy", False
    if policy.criticality_trigger_tiers is None:
        # The unresolved tier set (Phase 1). Raising here, rather than
        # returning None silently, makes the caller's UNKNOWN traceable to a
        # named business decision instead of an unexplained gap.
        raise PolicyNotConfiguredError(
            "conversion_criticality_tiers",
            "the FRS gives two different tier sets for the production-impact "
            "trigger and neither has been confirmed",
        )
    if criticality is None:
        return None, "material criticality unavailable", True
    fired = criticality in policy.criticality_trigger_tiers
    return (
        fired,
        f"criticality={criticality} vs configured tiers {policy.criticality_trigger_tiers}",
        False,
    )


def _hod_trigger(
    material: str, plant: str, policy: ConversionTriggerPolicy, lookup: HodApprovalLookup
) -> tuple[bool | None, str, bool]:
    """Returns ``(fired, detail, is_unknown)``. See :func:`_consumption_trigger`."""
    if not policy.enable_i13_hod_trigger:
        return False, "I13 HOD trigger disabled by policy", False
    approved = lookup.hod_approved(material, plant)
    if approved is None:
        return None, "no I13 HOD-approval ledger is available", True
    return approved, f"I13 HOD-approved request = {approved}", False


def evaluate(
    material: str,
    plant: str,
    consumption_count_12m: int | None,
    criticality: str | None,
    policy: ConversionTriggerPolicy,
    hod_lookup: HodApprovalLookup | None = None,
) -> ConversionDecision:
    """The OR of the three triggers, each evaluated independently.

    A single ``TRUE`` is sufficient regardless of what the others report --
    ``ELIGIBLE`` never waits on an unresolved trigger once one has already
    fired. Only when nothing fires does the state of the unresolved ones
    matter: any of them still unknown makes the overall verdict UNKNOWN rather
    than a confident NOT_ELIGIBLE.

    ``consumption_count_12m`` must be the MSEG transaction count (see the
    module docstring), not ``non_zero_periods``.
    """
    lookup = hod_lookup or NoHodLedgerAvailable()

    consumption_fired, consumption_detail, consumption_unknown = _consumption_trigger(
        consumption_count_12m, policy
    )

    try:
        impact_fired, impact_detail, impact_unknown = _production_impact_trigger(
            criticality, policy
        )
    except PolicyNotConfiguredError as exc:
        impact_fired, impact_detail, impact_unknown = None, str(exc), True

    hod_fired, hod_detail, hod_unknown = _hod_trigger(material, plant, policy, lookup)

    if consumption_fired:
        return ConversionDecision(
            eligibility=ConversionEligibility.ELIGIBLE,
            trigger=ConversionTrigger.CONSUMPTION_FREQUENCY,
            consumption_count_12m=consumption_count_12m,
            consumption_count_threshold=policy.consumption_count_threshold,
            production_impact=impact_fired,
            i13_hod_approved=hod_fired,
            detail=consumption_detail,
        )
    if impact_fired:
        return ConversionDecision(
            eligibility=ConversionEligibility.ELIGIBLE,
            trigger=ConversionTrigger.PRODUCTION_IMPACT,
            consumption_count_12m=consumption_count_12m,
            consumption_count_threshold=policy.consumption_count_threshold,
            production_impact=impact_fired,
            i13_hod_approved=hod_fired,
            detail=impact_detail,
        )
    if hod_fired:
        return ConversionDecision(
            eligibility=ConversionEligibility.ELIGIBLE,
            trigger=ConversionTrigger.I13_HOD_APPROVED_REQUEST,
            consumption_count_12m=consumption_count_12m,
            consumption_count_threshold=policy.consumption_count_threshold,
            production_impact=impact_fired,
            i13_hod_approved=hod_fired,
            detail=hod_detail,
        )

    any_unknown = consumption_unknown or impact_unknown or hod_unknown
    if any_unknown:
        reasons = [
            detail
            for unknown, detail in (
                (consumption_unknown, consumption_detail),
                (impact_unknown, impact_detail),
                (hod_unknown, hod_detail),
            )
            if unknown
        ]
        return ConversionDecision(
            eligibility=ConversionEligibility.UNKNOWN,
            trigger=ConversionTrigger.UNKNOWN,
            consumption_count_12m=consumption_count_12m,
            consumption_count_threshold=policy.consumption_count_threshold,
            production_impact=impact_fired,
            i13_hod_approved=hod_fired,
            detail="; ".join(reasons),
        )

    return ConversionDecision(
        eligibility=ConversionEligibility.NOT_ELIGIBLE,
        trigger=ConversionTrigger.NONE,
        consumption_count_12m=consumption_count_12m,
        consumption_count_threshold=policy.consumption_count_threshold,
        production_impact=impact_fired,
        i13_hod_approved=hod_fired,
        detail="no trigger condition was satisfied",
    )
