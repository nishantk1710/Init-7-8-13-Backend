"""OAR scope evaluation at material-plant grain.

Since 21 Nov 2026 the rule is the MRP type (DISMM) alone: ND or PD is OAR, and a
blank MRP type is OUT_OF_SCOPE. The MRP controller (DISPO) is not read.
"""

import pytest

from app.initiatives.i7.contracts import (
    MaterialAttributes,
    MaterialIdentity,
    MaterialPlantKey,
    PlantIdentity,
    ScopeDecision,
)
from app.initiatives.i7.features import assess_oar_scope
from app.initiatives.i7.features.oar_scope import ROLLUP_NOT_CONFIGURED
from app.initiatives.i7.policy import (
    OarPolicy,
    PredicateField,
    PredicateOperator,
    RollupPolicy,
    ScopePredicate,
    current_oar_policy,
)

POLICY = current_oar_policy()


def attributes(
    *,
    mrp_type=None,
    material_status=None,
    plant="1300",
    material="000000000010000000",
    mrp_controller=None,
    **extra,
) -> MaterialAttributes:
    return MaterialAttributes(
        key=MaterialPlantKey(
            material=MaterialIdentity(sap_material_number=material),
            plant=PlantIdentity(sap_plant_code=plant),
        ),
        mrp_type=mrp_type,
        mrp_controller=mrp_controller,
        material_status=material_status,
        **extra,
    )


# --- The documented cases ------------------------------------------------


@pytest.mark.parametrize(
    "mrp_type,expected",
    [
        ("PD", ScopeDecision.IN_SCOPE),
        ("ND", ScopeDecision.IN_SCOPE),
        ("VB", ScopeDecision.OUT_OF_SCOPE),
        # A blank/unmaintained MRP type is not OAR.
        (None, ScopeDecision.OUT_OF_SCOPE),
    ],
)
def test_documented_scope_cases(mrp_type, expected):
    assessment = assess_oar_scope(attributes(mrp_type=mrp_type), POLICY)
    assert assessment.scope is expected


def test_material_status_no_longer_participates_in_the_active_rule():
    """The clarified rule is MRP_TYPE in {ND, PD} only. An obsolete PD material
    is still OAR -- material_status was removed from the active predicate
    because no explicit requirement was confirmed to keep it."""
    assert (
        assess_oar_scope(attributes(mrp_type="PD", material_status="01"), POLICY).scope
        is ScopeDecision.IN_SCOPE
    )


# --- Unknown handling -----------------------------------------------------


def test_missing_mrp_type_is_out_of_scope_not_unknown():
    """A blank MRP type (the staging adapter collapses a blank cell to None) is
    not OAR -- a definite no, not UNKNOWN."""
    assessment = assess_oar_scope(attributes(mrp_type=None), POLICY)
    assert assessment.scope is ScopeDecision.OUT_OF_SCOPE
    assert "mrp_type" in assessment.reason


def test_extwg_is_not_an_active_predicate():
    """Retired as an OAR identifier."""
    assert "external_material_group" not in {field.value for field in PredicateField}
    configured = {value for predicate in POLICY.predicates for value in predicate.values}
    assert "100" not in configured


# --- The MRP controller (DISPO) is not read ----------------------------------


@pytest.mark.parametrize("mrp_controller", [None, "", "OAR", "K01"])
@pytest.mark.parametrize(
    "mrp_type,expected",
    [("PD", ScopeDecision.IN_SCOPE), ("ND", ScopeDecision.IN_SCOPE), ("VB", ScopeDecision.OUT_OF_SCOPE)],
)
def test_the_controller_does_not_change_the_verdict(mrp_type, mrp_controller, expected):
    assessment = assess_oar_scope(
        attributes(mrp_type=mrp_type, mrp_controller=mrp_controller), POLICY
    )
    assert assessment.scope is expected


def test_the_reason_names_the_failing_predicate_when_only_the_type_fails():
    assessment = assess_oar_scope(attributes(mrp_type="VB"), POLICY)
    assert "mrp_type" in assessment.reason
    assert "mrp_controller" not in assessment.reason
