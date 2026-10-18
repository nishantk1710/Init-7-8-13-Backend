"""OAR -> Min-Max conversion eligibility.

An AND of three conditions -- Stage 1 OAR scope, more than 4 net issues in
12 months, and a CRITICAL/IMPACT tier -- with OBSOLETE never eligible. Missing
inputs are UNKNOWN, never assumed false; a definite failure is NOT_ELIGIBLE
even when another input is missing.
"""

import inspect

import pytest
from pydantic import ValidationError

from app.initiatives.i7.contracts.enums import Criticality, ScopeDecision
from app.initiatives.i7.policy import ConversionTriggerPolicy, PolicyDocument
from app.initiatives.i7.policy.thresholds import current_conversion_trigger_policy
from app.initiatives.i7.recommendations import conversion
from app.initiatives.i7.recommendations.conversion import evaluate
from app.initiatives.i7.recommendations.types import ConversionEligibility, ConversionTrigger

POLICY = current_conversion_trigger_policy()
IN_SCOPE = ScopeDecision.IN_SCOPE.value
CRITICAL = Criticality.CRITICAL.value
IMPACT = Criticality.IMPACT.value
NORMAL = Criticality.NORMAL.value
OBSOLETE = Criticality.OBSOLETE.value


def decide(count, criticality, scope=IN_SCOPE, policy=POLICY):
    return evaluate(count, criticality, policy, scope)


# --- The policy in force ------------------------------------------------------


def test_the_policy_in_force_qualifies_critical_and_impact_only():
    assert POLICY.criticality_trigger_tiers == (CRITICAL, IMPACT)
    assert POLICY.consumption_count_threshold == 4


def test_the_policy_in_force_excludes_obsolete():
    assert POLICY.excluded_criticality_tiers == (OBSOLETE,)


def test_the_hod_trigger_is_gone_from_the_policy():
    assert not any("hod" in field for field in ConversionTriggerPolicy.model_fields)


def test_the_hod_trigger_is_gone_from_the_evaluator():
    parameters = inspect.signature(evaluate).parameters
    assert not any("hod" in name for name in parameters)
    assert not hasattr(conversion, "HodApprovalLookup")
    assert "I13_HOD_APPROVED_REQUEST" not in {member.value for member in ConversionTrigger}


def test_the_conditions_cannot_be_switched_off_individually():
    """Both conditions are required; a per-condition switch would quietly turn
    the AND back into a single condition."""
    for name in ("enable_consumption_trigger", "enable_criticality_trigger"):
        assert name not in ConversionTriggerPolicy.model_fields


# --- Rule 2: both conditions must hold ----------------------------------------


@pytest.mark.parametrize("tier", [CRITICAL, IMPACT])
def test_more_than_four_issues_and_a_qualifying_tier_is_eligible(tier):
    decision = decide(5, tier)
    assert decision.eligibility is ConversionEligibility.ELIGIBLE
    assert decision.trigger is ConversionTrigger.CONSUMPTION_AND_CRITICALITY
    assert decision.production_impact is True


@pytest.mark.parametrize("tier", [CRITICAL, IMPACT])
def test_a_qualifying_tier_alone_is_not_eligible(tier):
    decision = decide(2, tier)
    assert decision.eligibility is ConversionEligibility.NOT_ELIGIBLE
    assert decision.trigger is ConversionTrigger.NONE
    assert decision.production_impact is True


@pytest.mark.parametrize("tier", [NORMAL, Criticality.INSURANCE.value])
def test_frequent_use_alone_is_not_eligible(tier):
    decision = decide(10, tier)
    assert decision.eligibility is ConversionEligibility.NOT_ELIGIBLE
    assert decision.trigger is ConversionTrigger.NONE
    assert decision.production_impact is False


def test_exactly_four_issues_does_not_qualify():
    """The rule is "more than 4", so 4 itself does not fire."""
    assert decide(4, CRITICAL).eligibility is ConversionEligibility.NOT_ELIGIBLE
    assert decide(5, CRITICAL).eligibility is ConversionEligibility.ELIGIBLE


def test_neither_condition_is_not_eligible():
    assert decide(1, NORMAL).eligibility is ConversionEligibility.NOT_ELIGIBLE


def test_criticality_is_matched_case_and_whitespace_insensitively():
    assert decide(5, " critical ").eligibility is ConversionEligibility.ELIGIBLE


def test_the_threshold_is_read_from_policy_not_hardcoded():
    policy = ConversionTriggerPolicy(
        consumption_count_threshold=10, criticality_trigger_tiers=(CRITICAL,)
    )
    assert decide(5, CRITICAL, policy=policy).eligibility is ConversionEligibility.NOT_ELIGIBLE
    assert decide(11, CRITICAL, policy=policy).eligibility is ConversionEligibility.ELIGIBLE


# --- Rule 3: OBSOLETE is never eligible ---------------------------------------


def test_obsolete_with_heavy_use_is_not_eligible():
    decision = decide(50, OBSOLETE)
    assert decision.eligibility is ConversionEligibility.NOT_ELIGIBLE
    assert decision.production_impact is False
    assert "never" in decision.detail


def test_obsolete_is_not_eligible_even_with_a_missing_count():
    """A definite failure decides the verdict; a missing count cannot make an
    OBSOLETE material UNKNOWN."""
    assert decide(None, OBSOLETE).eligibility is ConversionEligibility.NOT_ELIGIBLE


def test_obsolete_is_not_eligible_even_while_tiers_are_unresolved():
    policy = ConversionTriggerPolicy()
    assert policy.criticality_trigger_tiers is None
    assert decide(9, OBSOLETE, policy=policy).eligibility is ConversionEligibility.NOT_ELIGIBLE


def test_obsolete_cannot_be_configured_as_a_qualifying_tier():
    with pytest.raises(ValidationError):
        ConversionTriggerPolicy(criticality_trigger_tiers=(CRITICAL, OBSOLETE))


def test_a_misspelt_tier_is_refused_at_load():
    with pytest.raises(ValidationError):
        ConversionTriggerPolicy(criticality_trigger_tiers=("CRTICAL",))


# --- Rule 1: only Stage 1 OAR material-plants are evaluated --------------------


def test_an_out_of_scope_material_plant_is_never_eligible():
    """A cold-start VB material reaches the OAR recommendation path by its
    history, not its MRP type -- it must not be offered a conversion."""
    decision = decide(10, CRITICAL, scope=ScopeDecision.OUT_OF_SCOPE.value)
    assert decision.eligibility is ConversionEligibility.NOT_ELIGIBLE
    assert decision.trigger is ConversionTrigger.NONE
    assert "OAR" in decision.detail


@pytest.mark.parametrize("scope", [ScopeDecision.UNKNOWN.value, None, "garbage"])
def test_an_undetermined_scope_is_unknown(scope):
    decision = decide(10, CRITICAL, scope=scope)
    assert decision.eligibility is ConversionEligibility.UNKNOWN
    assert decision.trigger is ConversionTrigger.UNKNOWN


def test_the_scope_enum_and_its_stored_string_are_equivalent():
    assert decide(10, CRITICAL, scope=ScopeDecision.IN_SCOPE).eligibility is (
        ConversionEligibility.ELIGIBLE
    )


# --- Rule 4: undetermined inputs are UNKNOWN, never assumed false -------------


def test_missing_consumption_count_with_a_qualifying_tier_is_unknown():
    decision = decide(None, CRITICAL)
    assert decision.eligibility is ConversionEligibility.UNKNOWN
    assert decision.trigger is ConversionTrigger.UNKNOWN
    assert "could not be counted" in decision.detail


@pytest.mark.parametrize("criticality", [None, "", "   "])
def test_missing_criticality_with_enough_use_is_unknown(criticality):
    decision = decide(10, criticality)
    assert decision.eligibility is ConversionEligibility.UNKNOWN
    assert decision.production_impact is None
    assert decision.detail == "Its criticality level is not known."


def test_unrecognised_criticality_is_unknown_not_a_failure():
    decision = decide(10, "HIGH")
    assert decision.eligibility is ConversionEligibility.UNKNOWN
    assert decision.production_impact is None


def test_unresolved_tier_set_is_unknown():
    policy = ConversionTriggerPolicy()
    decision = decide(10, CRITICAL, policy=policy)
    assert decision.eligibility is ConversionEligibility.UNKNOWN
    assert "criticality levels" in decision.detail


def test_unresolved_tier_set_is_reported_as_an_unresolved_policy():
    document = PolicyDocument(conversion_triggers=ConversionTriggerPolicy())
    assert "conversion_criticality_tiers" in document.unresolved_policies()


def test_a_definite_failure_wins_over_missing_data():
    """One condition already false makes the AND false, whatever is missing."""
    assert decide(2, None).eligibility is ConversionEligibility.NOT_ELIGIBLE
    assert decide(None, NORMAL).eligibility is ConversionEligibility.NOT_ELIGIBLE


def test_unknown_lists_every_undetermined_reason():
    decision = decide(None, None, scope=None)
    assert decision.eligibility is ConversionEligibility.UNKNOWN
    assert "on demand" in decision.detail
    assert "could not be counted" in decision.detail
    assert "not known" in decision.detail


# --- Structured evidence and reason labels -------------------------------------


def test_consumption_count_and_threshold_are_structured_on_the_decision():
    decision = decide(7, NORMAL)
    assert decision.consumption_count_12m == 7
    assert decision.consumption_count_threshold == 4


def test_the_decision_has_no_hod_field():
    assert "i13_hod_approved" not in decide(7, CRITICAL)._fields


def test_the_eligible_reason_names_both_conditions():
    decision = decide(7, CRITICAL)
    assert decision.detail == (
        "It was used 7 times in the last 12 months, more than the 4 needed to qualify "
        "for Min-Max ordering. Its criticality is Critical, which qualifies it for "
        "Min-Max ordering."
    )


def test_a_not_eligible_reason_names_only_what_failed():
    assert decide(7, NORMAL).detail == (
        "Its criticality is Normal, which is not one of the levels that qualify "
        "(Critical, Impact)."
    )
    assert decide(2, IMPACT).detail == (
        "It was used 2 times in the last 12 months, which does not exceed the 4 needed "
        "to qualify for Min-Max ordering."
    )


def test_the_obsolete_reason_reads_as_a_plain_sentence():
    assert decide(9, OBSOLETE).detail == (
        "It is classified Obsolete, and such materials are never moved to Min-Max ordering."
    )


def test_a_single_use_is_not_pluralised():
    assert conversion._times(1) == "1 time" and conversion._times(2) == "2 times"
    assert "used 1 time in" in decide(1, CRITICAL).detail


# --- FR-2 demand class is a display signal, never an input ---------------------
#
# The FRS: "the FR-2 demand class as a confidence signal" and "the ADI / CV
# squared class is shown as a supporting regularity and confidence signal,
# not the trigger." evaluate() must therefore have no way to receive it.


def test_evaluate_has_no_demand_class_parameter():
    parameters = inspect.signature(evaluate).parameters
    assert "demand_class" not in parameters
    assert "adi" not in parameters
    assert "cv_squared" not in parameters
