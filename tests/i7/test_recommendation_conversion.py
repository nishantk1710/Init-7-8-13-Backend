"""OAR -> Min-Max conversion eligibility.

Two gates -- Stage 1 OAR scope, and never OBSOLETE -- then either trigger is
enough: more than 4 net issues in 6 months, OR a CRITICAL / IMPACT /
INSURANCE tier. Missing inputs are UNKNOWN, never assumed false; a definite
failure is NOT_ELIGIBLE even when another input is missing.
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
INSURANCE = Criticality.INSURANCE.value
NORMAL = Criticality.NORMAL.value
OBSOLETE = Criticality.OBSOLETE.value

ELIGIBLE = ConversionEligibility.ELIGIBLE
NOT_ELIGIBLE = ConversionEligibility.NOT_ELIGIBLE
UNKNOWN = ConversionEligibility.UNKNOWN


def decide(count, criticality, scope=IN_SCOPE, policy=POLICY):
    return evaluate(count, criticality, policy, scope)


# --- The policy in force ------------------------------------------------------


def test_the_policy_in_force_qualifies_critical_impact_and_insurance():
    assert POLICY.criticality_trigger_tiers == (CRITICAL, IMPACT, INSURANCE)
    assert POLICY.consumption_count_threshold == 4


def test_the_policy_in_force_excludes_obsolete():
    assert POLICY.excluded_criticality_tiers == (OBSOLETE,)


def test_the_hod_trigger_is_gone():
    assert not any("hod" in field for field in ConversionTriggerPolicy.model_fields)
    assert not any("hod" in name for name in inspect.signature(evaluate).parameters)
    assert "I13_HOD_APPROVED_REQUEST" not in {member.value for member in ConversionTrigger}


# --- Either trigger on its own is enough --------------------------------------


@pytest.mark.parametrize("tier", [NORMAL, None])
def test_usage_alone_is_eligible(tier):
    decision = decide(5, tier)
    assert decision.eligibility is ELIGIBLE
    assert decision.trigger is ConversionTrigger.CONSUMPTION_FREQUENCY


@pytest.mark.parametrize("tier", [CRITICAL, IMPACT, INSURANCE])
def test_a_qualifying_tier_alone_is_eligible(tier):
    decision = decide(0, tier)
    assert decision.eligibility is ELIGIBLE
    assert decision.trigger is ConversionTrigger.PRODUCTION_IMPACT
    assert decision.production_impact is True


@pytest.mark.parametrize("tier", [CRITICAL, IMPACT, INSURANCE])
def test_a_qualifying_tier_is_eligible_even_with_no_usage_count(tier):
    assert decide(None, tier).eligibility is ELIGIBLE


def test_both_triggers_together_name_both():
    decision = decide(9, CRITICAL)
    assert decision.eligibility is ELIGIBLE
    assert decision.trigger is ConversionTrigger.CONSUMPTION_AND_CRITICALITY


def test_neither_trigger_is_not_eligible():
    decision = decide(2, NORMAL)
    assert decision.eligibility is NOT_ELIGIBLE
    assert decision.trigger is ConversionTrigger.NONE
    assert decision.production_impact is False


def test_exactly_four_issues_does_not_qualify_on_usage():
    """The rule is "more than 4", so 4 itself does not fire."""
    assert decide(4, NORMAL).eligibility is NOT_ELIGIBLE
    assert decide(5, NORMAL).eligibility is ELIGIBLE


def test_criticality_is_matched_case_and_whitespace_insensitively():
    assert decide(0, " insurance ").eligibility is ELIGIBLE


def test_the_threshold_is_read_from_policy_not_hardcoded():
    policy = ConversionTriggerPolicy(consumption_count_threshold=10, criticality_trigger_tiers=(CRITICAL,))
    assert decide(5, NORMAL, policy=policy).eligibility is NOT_ELIGIBLE
    assert decide(11, NORMAL, policy=policy).eligibility is ELIGIBLE


# --- OBSOLETE is never eligible -----------------------------------------------


def test_obsolete_with_heavy_use_is_not_eligible():
    decision = decide(50, OBSOLETE)
    assert decision.eligibility is NOT_ELIGIBLE
    assert decision.trigger is ConversionTrigger.NONE
    assert decision.production_impact is False
    assert decision.detail == (
        "It is classified Obsolete, and such materials are never moved to Min-Max ordering."
    )


def test_obsolete_is_not_eligible_even_with_a_missing_count():
    assert decide(None, OBSOLETE).eligibility is NOT_ELIGIBLE


def test_obsolete_is_not_eligible_even_while_tiers_are_unresolved():
    policy = ConversionTriggerPolicy()
    assert policy.criticality_trigger_tiers is None
    assert decide(9, OBSOLETE, policy=policy).eligibility is NOT_ELIGIBLE


def test_obsolete_cannot_be_configured_as_a_qualifying_tier():
    with pytest.raises(ValidationError):
        ConversionTriggerPolicy(criticality_trigger_tiers=(CRITICAL, OBSOLETE))


def test_a_misspelt_tier_is_refused_at_load():
    with pytest.raises(ValidationError):
        ConversionTriggerPolicy(criticality_trigger_tiers=("CRTICAL",))


# --- Only Stage 1 OAR material-plants ------------------------------------------


def test_an_out_of_scope_material_plant_is_never_eligible():
    decision = decide(10, CRITICAL, scope=ScopeDecision.OUT_OF_SCOPE.value)
    assert decision.eligibility is NOT_ELIGIBLE
    assert "OAR" in decision.detail


@pytest.mark.parametrize("scope", [ScopeDecision.UNKNOWN.value, None, "garbage"])
def test_an_undetermined_scope_is_unknown_even_when_a_trigger_fires(scope):
    decision = decide(10, CRITICAL, scope=scope)
    assert decision.eligibility is UNKNOWN
    assert decision.trigger is ConversionTrigger.UNKNOWN


def test_an_undetermined_scope_with_no_trigger_is_not_eligible():
    """No trigger holds, so the answer is no whatever the scope turns out to be."""
    assert decide(2, NORMAL, scope=None).eligibility is NOT_ELIGIBLE


def test_the_scope_enum_and_its_stored_string_are_equivalent():
    assert decide(10, NORMAL, scope=ScopeDecision.IN_SCOPE).eligibility is ELIGIBLE


# --- Undetermined inputs are UNKNOWN, never assumed false ---------------------


@pytest.mark.parametrize("criticality", [None, "", "   "])
def test_low_usage_and_no_criticality_is_unknown(criticality):
    """The tier might be a qualifying one; without it the question is open."""
    decision = decide(2, criticality)
    assert decision.eligibility is UNKNOWN
    assert decision.production_impact is None
    assert decision.detail == "Its criticality level is not known."


def test_no_usage_count_and_a_non_qualifying_tier_is_unknown():
    decision = decide(None, NORMAL)
    assert decision.eligibility is UNKNOWN
    assert "could not be counted" in decision.detail


def test_unrecognised_criticality_with_low_usage_is_unknown():
    decision = decide(1, "HIGH")
    assert decision.eligibility is UNKNOWN
    assert decision.production_impact is None


def test_unresolved_tier_set_with_low_usage_is_unknown():
    decision = decide(1, CRITICAL, policy=ConversionTriggerPolicy())
    assert decision.eligibility is UNKNOWN
    assert "criticality levels" in decision.detail


def test_unresolved_tier_set_is_reported_as_an_unresolved_policy():
    document = PolicyDocument(conversion_triggers=ConversionTriggerPolicy())
    assert "conversion_criticality_tiers" in document.unresolved_policies()


def test_nothing_known_lists_every_open_question():
    decision = decide(None, None, scope=None)
    assert decision.eligibility is UNKNOWN
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


def test_the_eligible_reason_names_what_qualified():
    assert decide(7, NORMAL).detail == (
        "It was used 7 times in the last 6 months, more than the 4 needed to qualify "
        "for Min-Max ordering."
    )
    assert decide(1, INSURANCE).detail == (
        "Its criticality is Insurance, which qualifies it for Min-Max ordering."
    )
    assert decide(7, CRITICAL).detail == (
        "It was used 7 times in the last 6 months, more than the 4 needed to qualify "
        "for Min-Max ordering. Its criticality is Critical, which qualifies it for "
        "Min-Max ordering."
    )


def test_the_not_eligible_reason_names_both_failed_triggers():
    assert decide(2, NORMAL).detail == (
        "It was used 2 times in the last 6 months, which does not exceed the 4 needed "
        "to qualify for Min-Max ordering. Its criticality is Normal, which is not one of "
        "the levels that qualify (Critical, Impact, Insurance)."
    )


def test_a_single_use_is_not_pluralised():
    assert conversion._times(1) == "1 time" and conversion._times(2) == "2 times"
    assert "used 1 time in" in decide(1, NORMAL).detail


# --- FR-2 demand class is a display signal, never an input ---------------------


def test_evaluate_has_no_demand_class_parameter():
    parameters = inspect.signature(evaluate).parameters
    assert "demand_class" not in parameters
    assert "adi" not in parameters
    assert "cv_squared" not in parameters
