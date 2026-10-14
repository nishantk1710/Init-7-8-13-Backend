"""OAR -> Min-Max conversion eligibility.

The FRS's own OR of three triggers, each independently evaluated. Missing
inputs are UNKNOWN, never assumed false.
"""

from app.initiatives.i7.contracts.enums import Criticality
from app.initiatives.i7.policy import ConversionTriggerPolicy
from app.initiatives.i7.recommendations.conversion import evaluate
from app.initiatives.i7.recommendations.types import ConversionEligibility, ConversionTrigger

DEFAULT_POLICY = ConversionTriggerPolicy(
    enable_consumption_trigger=True,
    enable_criticality_trigger=False,  # avoid the unresolved-tiers raise by default
    enable_i13_hod_trigger=True,
)


class FixedHodLookup:
    def __init__(self, approved: bool | None):
        self._approved = approved

    def hod_approved(self, material, plant):
        return self._approved


def test_consumption_count_above_four_is_eligible():
    decision = evaluate("M1", "1300", 5, None, DEFAULT_POLICY, FixedHodLookup(None))
    assert decision.eligibility is ConversionEligibility.ELIGIBLE
    assert decision.trigger is ConversionTrigger.CONSUMPTION_FREQUENCY


def test_consumption_count_and_threshold_are_structured_on_the_decision():
    """Part 7: the count must be readable as a typed field, not only
    recoverable by parsing detail text."""
    decision = evaluate("M1", "1300", 7, None, DEFAULT_POLICY, FixedHodLookup(None))
    assert decision.consumption_count_12m == 7
    assert decision.consumption_count_threshold == 4


def test_rationale_detail_includes_the_actual_count():
    decision = evaluate("M1", "1300", 7, None, DEFAULT_POLICY, FixedHodLookup(None))
    assert "7" in decision.detail


def test_consumption_count_exactly_four_is_not_eligible():
    """The FRS says "> 4", so 4 itself does not fire."""
    decision = evaluate("M1", "1300", 4, None, DEFAULT_POLICY, FixedHodLookup(False))
    assert decision.eligibility is ConversionEligibility.NOT_ELIGIBLE
    assert decision.trigger is ConversionTrigger.NONE


def test_i13_hod_approved_is_eligible():
    decision = evaluate("M1", "1300", 1, None, DEFAULT_POLICY, FixedHodLookup(True))
    assert decision.eligibility is ConversionEligibility.ELIGIBLE
    assert decision.trigger is ConversionTrigger.I13_HOD_APPROVED_REQUEST


def test_no_trigger_fires_is_not_eligible():
    decision = evaluate("M1", "1300", 2, None, DEFAULT_POLICY, FixedHodLookup(False))
    assert decision.eligibility is ConversionEligibility.NOT_ELIGIBLE
    assert decision.trigger is ConversionTrigger.NONE


def test_missing_consumption_count_is_unknown_not_false():
    """Absent data must not read as "definitely does not qualify"."""
    decision = evaluate("M1", "1300", None, None, DEFAULT_POLICY, FixedHodLookup(False))
    assert decision.eligibility is ConversionEligibility.UNKNOWN
    assert decision.trigger is ConversionTrigger.UNKNOWN


def test_no_hod_ledger_is_unknown_when_nothing_else_fires():
    decision = evaluate("M1", "1300", 2, None, DEFAULT_POLICY, None)
    assert decision.eligibility is ConversionEligibility.UNKNOWN


def test_a_fired_trigger_wins_even_if_others_are_unknown():
    """ELIGIBLE never waits on an unresolved trigger once one has fired."""
    decision = evaluate("M1", "1300", 10, None, DEFAULT_POLICY, None)
    assert decision.eligibility is ConversionEligibility.ELIGIBLE
    assert decision.trigger is ConversionTrigger.CONSUMPTION_FREQUENCY


def test_criticality_trigger_raises_when_tiers_are_unresolved():
    """The unresolved tier set from Phase 1 -- reported as UNKNOWN, not
    silently skipped and not resolved by picking a tier set here."""
    policy = ConversionTriggerPolicy(
        enable_consumption_trigger=False,
        enable_criticality_trigger=True,
        enable_i13_hod_trigger=False,
        criticality_trigger_tiers=None,
    )
    decision = evaluate("M1", "1300", None, Criticality.CRITICAL.value, policy, None)
    assert decision.eligibility is ConversionEligibility.UNKNOWN
    assert "criticality" in decision.detail.lower() or "tier" in decision.detail.lower()


def test_criticality_trigger_fires_once_tiers_are_configured():
    policy = ConversionTriggerPolicy(
        enable_consumption_trigger=False,
        enable_criticality_trigger=True,
        enable_i13_hod_trigger=False,
        criticality_trigger_tiers=(Criticality.CRITICAL.value,),
    )
    decision = evaluate("M1", "1300", None, Criticality.CRITICAL.value, policy, None)
    assert decision.eligibility is ConversionEligibility.ELIGIBLE
    assert decision.trigger is ConversionTrigger.PRODUCTION_IMPACT


def test_criticality_trigger_does_not_fire_for_a_different_tier():
    policy = ConversionTriggerPolicy(
        enable_consumption_trigger=False,
        enable_criticality_trigger=True,
        enable_i13_hod_trigger=False,
        criticality_trigger_tiers=(Criticality.CRITICAL.value,),
    )
    decision = evaluate("M1", "1300", 0, Criticality.NORMAL.value, policy, FixedHodLookup(False))
    assert decision.eligibility is ConversionEligibility.NOT_ELIGIBLE


def test_disabled_trigger_never_fires():
    policy = ConversionTriggerPolicy(
        enable_consumption_trigger=False,
        enable_criticality_trigger=False,
        enable_i13_hod_trigger=False,
    )
    decision = evaluate("M1", "1300", 100, Criticality.CRITICAL.value, policy, FixedHodLookup(True))
    assert decision.eligibility is ConversionEligibility.NOT_ELIGIBLE
    assert decision.trigger is ConversionTrigger.NONE


def test_the_threshold_is_read_from_policy_not_hardcoded():
    """5 clears the default threshold of 4 but not a configured 10."""
    policy = ConversionTriggerPolicy(
        enable_consumption_trigger=True,
        enable_criticality_trigger=False,
        enable_i13_hod_trigger=False,
        consumption_count_threshold=10,
    )
    below_new_threshold = evaluate("M1", "1300", 5, None, policy, None)
    assert below_new_threshold.eligibility is ConversionEligibility.NOT_ELIGIBLE

    above_default_only = evaluate("M1", "1300", 5, None, DEFAULT_POLICY, FixedHodLookup(False))
    assert above_default_only.eligibility is ConversionEligibility.ELIGIBLE


def test_disabled_trigger_is_not_the_same_as_unknown_trigger():
    """Regression: a disabled trigger definitely does not fire (False), which
    is a different fact from an enabled trigger whose input is missing (None).
    Conflating the two turned every all-disabled decision into UNKNOWN even
    though nothing was actually unresolved."""
    policy = ConversionTriggerPolicy(
        enable_consumption_trigger=False,
        enable_criticality_trigger=False,
        enable_i13_hod_trigger=False,
    )
    decision = evaluate("M1", "1300", None, None, policy, None)
    assert decision.eligibility is ConversionEligibility.NOT_ELIGIBLE


# --- FR-2 demand class is a display signal, never a trigger input -----------
#
# The FRS: "the FR-2 demand class as a confidence signal" and "the ADI / CV
# squared class is shown as a supporting regularity and confidence signal,
# not the trigger." evaluate() must therefore have no way to receive it.


def test_evaluate_has_no_demand_class_parameter():
    """The eligibility function cannot be influenced by demand_class because
    it is not part of its signature at all -- there is no argument to pass.
    """
    import inspect

    parameters = inspect.signature(evaluate).parameters
    assert "demand_class" not in parameters
    assert "adi" not in parameters
    assert "cv_squared" not in parameters


def test_identical_trigger_inputs_produce_the_same_decision_regardless_of_context():
    """Two materials with the same trigger inputs but (conceptually) different
    demand classes must reach the identical eligibility/trigger -- proving no
    hidden coupling exists between classification and conversion eligibility.
    Demand class is not even passed in, so this is really pinning that fact.
    """
    smooth_like = evaluate("SMOOTH_MATERIAL", "1300", 5, None, DEFAULT_POLICY, FixedHodLookup(None))
    lumpy_like = evaluate("LUMPY_MATERIAL", "1300", 5, None, DEFAULT_POLICY, FixedHodLookup(None))
    assert smooth_like.eligibility is lumpy_like.eligibility is ConversionEligibility.ELIGIBLE
    assert smooth_like.trigger is lumpy_like.trigger is ConversionTrigger.CONSUMPTION_FREQUENCY


# --- The policy in force: the I13 HOD trigger is off ---------------------------


def test_the_policy_in_force_does_not_use_the_i13_hod_trigger():
    from app.initiatives.i7.policy.thresholds import current_conversion_trigger_policy

    assert current_conversion_trigger_policy().enable_i13_hod_trigger is False


def test_with_the_hod_trigger_off_nothing_firing_is_not_eligible_not_unknown():
    """With the trigger on and no I13 ledger, a material that fired neither
    other trigger was UNKNOWN. Off, the answer is definite."""
    from app.initiatives.i7.policy.thresholds import current_conversion_trigger_policy

    decision = evaluate("M1", "1300", 2, "HIGH", current_conversion_trigger_policy(), None)
    assert decision.eligibility is ConversionEligibility.NOT_ELIGIBLE


def test_with_the_hod_trigger_off_an_hod_approval_cannot_make_a_material_eligible():
    from app.initiatives.i7.policy.thresholds import current_conversion_trigger_policy

    decision = evaluate(
        "M1", "1300", 2, "HIGH", current_conversion_trigger_policy(), FixedHodLookup(True)
    )
    assert decision.eligibility is ConversionEligibility.NOT_ELIGIBLE


def test_with_the_hod_trigger_off_consumption_and_criticality_still_make_a_material_eligible():
    from app.initiatives.i7.policy.thresholds import current_conversion_trigger_policy

    policy = current_conversion_trigger_policy()
    assert evaluate("M1", "1300", 5, "HIGH", policy, None).trigger is ConversionTrigger.CONSUMPTION_FREQUENCY
    assert evaluate("M1", "1300", 0, "NORMAL", policy, None).trigger is ConversionTrigger.PRODUCTION_IMPACT


# --- The reasons read as plain sentences -----------------------------------------


def test_the_reasons_are_plain_sentences_not_internal_notation():
    from app.initiatives.i7.policy.thresholds import current_conversion_trigger_policy

    policy = current_conversion_trigger_policy()
    by_tier = evaluate("M1", "1300", 0, "NORMAL", policy, None)
    assert by_tier.detail == "Its criticality is Normal, which qualifies it for Min-Max ordering."

    by_usage = evaluate("M1", "1300", 7, "HIGH", policy, None)
    assert by_usage.detail == (
        "It was used 7 times in the last 12 months, more than the 4 needed to qualify for Min-Max ordering."
    )

    neither = evaluate("M1", "1300", 1, "HIGH", policy, None)
    assert neither.detail == "None of the reasons for moving this material to Min-Max ordering apply."


def test_a_single_use_is_not_pluralised():
    from app.initiatives.i7.policy.thresholds import current_conversion_trigger_policy

    decision = evaluate("M1", "1300", 1, "NORMAL", current_conversion_trigger_policy(), None)
    # Eligible by tier; the consumption sentence is still correct when it is the unknown/other branch.
    from app.initiatives.i7.recommendations import conversion

    assert conversion._times(1) == "1 time" and conversion._times(2) == "2 times"
    assert decision.eligibility is ConversionEligibility.ELIGIBLE
