"""Read-only SAP adoption reconciliation.

No provider exists today (no staged CDHDR/CDPOS), so the interface-only path
matters as much as the reconciliation logic itself: absent evidence must never
read as a proven negative.
"""

from app.initiatives.i7.recommendations.adoption import (
    evaluate_conversion_adoption,
    evaluate_parameter_adoption,
)
from app.initiatives.i7.recommendations.types import AdoptionStatus


class FixedSapState:
    def __init__(self, state: dict | None):
        self._state = state

    def current_state(self, material, plant):
        return self._state


def test_exact_conversion_match_is_adopted():
    provider = FixedSapState({"mrp_type": "VB", "minbe": "70", "mabst": "100"})
    result = evaluate_conversion_adoption("M1", "1300", "VB", provider)
    assert result.status is AdoptionStatus.ADOPTED


def test_one_field_differing_is_partially_adopted():
    """MRP type and MINBE match the conversion's expectation (any populated
    value); MABST was never populated, so the conversion is only partial."""
    provider = FixedSapState({"mrp_type": "VB", "minbe": "60", "mabst": ""})
    result = evaluate_conversion_adoption("M1", "1300", "VB", provider)
    assert result.status is AdoptionStatus.PARTIALLY_ADOPTED
    assert "mrp_type" in result.matched_fields
    assert "mabst_populated" in result.mismatched_fields


def test_no_baseline_evidence_is_unknown_not_not_adopted():
    """The central rule: absent evidence is never read as a proven negative."""
    result = evaluate_conversion_adoption("M1", "1300", "VB", FixedSapState(None))
    assert result.status is AdoptionStatus.UNKNOWN


def test_default_provider_has_no_staged_evidence():
    """No CDHDR/CDPOS staging exists yet; the default provider must say so."""
    result = evaluate_conversion_adoption("M1", "1300", "VB", None)
    assert result.status is AdoptionStatus.UNKNOWN


def test_wrong_mrp_type_with_populated_fields_is_partially_adopted():
    provider = FixedSapState({"mrp_type": "PD", "minbe": "70", "mabst": "100"})
    result = evaluate_conversion_adoption("M1", "1300", "VB", provider)
    assert result.status is AdoptionStatus.PARTIALLY_ADOPTED


def test_nothing_matches_is_not_adopted():
    """A genuinely observed, unchanged state -- distinct from no evidence."""
    provider = FixedSapState({"mrp_type": "PD", "minbe": "", "mabst": ""})
    result = evaluate_conversion_adoption("M1", "1300", "VB", provider)
    assert result.status is AdoptionStatus.NOT_ADOPTED


def test_zero_values_are_not_populated():
    provider = FixedSapState({"mrp_type": "VB", "minbe": "0", "mabst": "0.00"})
    result = evaluate_conversion_adoption("M1", "1300", "VB", provider)
    assert result.status is AdoptionStatus.PARTIALLY_ADOPTED


def test_expected_fields_are_recorded():
    result = evaluate_conversion_adoption("M1", "1300", "VB", FixedSapState(None))
    assert ("mrp_type", "VB") in result.expected


# --- Parameter (normal-path) adoption --------------------------------------


def test_parameter_exact_match_is_adopted():
    provider = FixedSapState({"eisbe": "19", "minbe": "32", "mabst": "29"})
    result = evaluate_parameter_adoption("M1", "1300", 19, 32, 29, provider)
    assert result.status is AdoptionStatus.ADOPTED


def test_parameter_one_field_differing_is_partially_adopted():
    provider = FixedSapState({"eisbe": "19", "minbe": "40", "mabst": "29"})
    result = evaluate_parameter_adoption("M1", "1300", 19, 32, 29, provider)
    assert result.status is AdoptionStatus.PARTIALLY_ADOPTED


def test_parameter_no_evidence_is_unknown():
    result = evaluate_parameter_adoption("M1", "1300", 19, 32, 29, FixedSapState(None))
    assert result.status is AdoptionStatus.UNKNOWN


def test_no_approved_values_at_all_is_unknown():
    """Nothing to reconcile against is a different case from no SAP evidence."""
    result = evaluate_parameter_adoption("M1", "1300", None, None, None, FixedSapState({}))
    assert result.status is AdoptionStatus.UNKNOWN
