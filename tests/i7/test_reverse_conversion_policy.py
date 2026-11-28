"""The Min-Max -> OAR policy: the approved values, and the guards on changing them."""

import pytest
from pydantic import ValidationError

from app.initiatives.i7.policy.reverse_conversion import (
    ReverseConversionPolicy,
    current_reverse_conversion_policy,
)


def test_current_policy_carries_the_approved_values():
    policy = current_reverse_conversion_policy()
    assert policy.mrp_types == ("VM", "VB")
    assert policy.plants == ("1300", "1500")
    assert policy.protected_criticality_tiers == ("CRITICAL", "IMPACT", "INSURANCE")
    assert policy.obsolete_criticality_tier == "OBSOLETE"
    assert policy.window_months == 12
    assert policy.zero_use_window_months == 24
    assert policy.minimum_history_span_months == 12
    assert policy.maximum_lead_time_days == 90
    assert policy.recommended_max_issues == 2
    assert policy.recommended_max_months == 2
    assert policy.maximum_months_since_last_movement == 9
    assert policy.frequent_issues_above == 4
    assert policy.frequent_months_above == 4
    assert policy.new_material_age_months == 24
    assert policy.new_material_minimum_lifetime_months == 3


def test_policy_has_a_version_and_exposes_its_parameters():
    policy = current_reverse_conversion_policy()
    assert policy.policy_version == "reverse-conversion-v2"
    parameters = policy.parameters()
    assert parameters["policy_version"] == "reverse-conversion-v2"
    assert parameters["recommended_max_issues"] == 2
    assert parameters["frequent_issues_above"] == 4
    assert parameters["maximum_lead_time_days"] == 90


def test_policy_is_immutable():
    policy = current_reverse_conversion_policy()
    with pytest.raises(ValidationError):
        policy.recommended_max_issues = 5  # type: ignore[misc]


def test_a_misspelt_protected_tier_is_refused():
    # Otherwise it would protect nothing and silently let protected materials be recommended.
    with pytest.raises(ValidationError):
        ReverseConversionPolicy(protected_criticality_tiers=("CRITCAL",))


def test_the_obsolete_tier_cannot_also_be_protected():
    with pytest.raises(ValidationError):
        ReverseConversionPolicy(protected_criticality_tiers=("CRITICAL", "OBSOLETE"))


def test_a_recommended_cap_above_the_frequent_cut_off_is_refused():
    with pytest.raises(ValidationError):
        ReverseConversionPolicy(recommended_max_issues=6, frequent_issues_above=4)
    with pytest.raises(ValidationError):
        ReverseConversionPolicy(recommended_max_months=6, frequent_months_above=4)


def test_the_zero_use_window_must_be_twice_the_latest_window():
    with pytest.raises(ValidationError):
        ReverseConversionPolicy(window_months=12, zero_use_window_months=18)
    # Consistent, accepted (the recency limit has to sit inside the shorter window too).
    ReverseConversionPolicy(window_months=6, zero_use_window_months=12, maximum_months_since_last_movement=5)


def test_the_recency_limit_must_sit_inside_the_latest_window():
    with pytest.raises(ValidationError):
        ReverseConversionPolicy(maximum_months_since_last_movement=12)
