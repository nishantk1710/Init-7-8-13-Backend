"""OAR scope evaluation at material-plant grain.

The cases in section 16 of the phase brief are asserted verbatim. The one that
matters most is ``DISMM=NULL -> UNKNOWN``: a blank MRP type means "not
maintained", and reporting that as OUT_OF_SCOPE would quietly remove the
material from the OAR population with nothing to show it happened.
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
    *, mrp_type=None, material_status=None, plant="1300", material="000000000010000000", **extra
) -> MaterialAttributes:
    return MaterialAttributes(
        key=MaterialPlantKey(
            material=MaterialIdentity(sap_material_number=material),
            plant=PlantIdentity(sap_plant_code=plant),
        ),
        mrp_type=mrp_type,
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
        (None, ScopeDecision.UNKNOWN),
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


def test_blank_mrp_type_is_unknown():
    assert (
        assess_oar_scope(attributes(mrp_type=""), POLICY).scope is ScopeDecision.UNKNOWN
    )


def test_missing_mrp_type_is_unknown():
    assert (
        assess_oar_scope(attributes(mrp_type=None), POLICY).scope is ScopeDecision.UNKNOWN
    )


def test_unknown_reason_names_the_missing_field():
    assessment = assess_oar_scope(attributes(mrp_type=None), POLICY)
    assert "mrp_type" in assessment.reason


def test_excluded_reason_names_the_failing_predicate():
    assessment = assess_oar_scope(attributes(mrp_type="VB"), POLICY)
    assert "mrp_type" in assessment.reason


def test_in_scope_reason_is_explanatory():
    assessment = assess_oar_scope(attributes(mrp_type="PD"), POLICY)
    assert assessment.reason


# --- Per-plant semantics --------------------------------------------------


def test_the_same_material_can_differ_by_plant():
    """DISMM lives on MARC, so scope is a material-plant answer."""
    at_1300 = assess_oar_scope(
        attributes(mrp_type="PD", material_status="02", plant="1300"), POLICY
    )
    at_1200 = assess_oar_scope(
        attributes(mrp_type="VB", material_status="02", plant="1200"), POLICY
    )
    assert at_1300.scope is ScopeDecision.IN_SCOPE
    assert at_1200.scope is ScopeDecision.OUT_OF_SCOPE


# --- Roll-up stays unresolved ----------------------------------------------


def test_rollup_is_not_configured_by_default():
    """per-plant-only is the likely direction but still a guess, and a stored
    guess is indistinguishable from a ruling."""
    assessment = assess_oar_scope(attributes(mrp_type="PD", material_status="02"), POLICY)
    assert assessment.rollup_status == ROLLUP_NOT_CONFIGURED


def test_rollup_is_reported_once_configured():
    policy = OarPolicy(predicates=POLICY.predicates, rollup=RollupPolicy.PER_PLANT_ONLY)
    assessment = assess_oar_scope(attributes(mrp_type="PD", material_status="02"), policy)
    assert assessment.rollup_status == "per-plant-only"


# --- Configuration drives the rule -----------------------------------------


def test_widening_the_mrp_set_is_a_config_change():
    widened = OarPolicy(
        predicates=(
            ScopePredicate(
                field=PredicateField.MRP_TYPE,
                operator=PredicateOperator.IN,
                values=("ND", "PD", "V1"),
            ),
        )
    )
    assert assess_oar_scope(attributes(mrp_type="V1"), widened).scope is ScopeDecision.IN_SCOPE
    assert (
        assess_oar_scope(attributes(mrp_type="V1", material_status="02"), POLICY).scope
        is ScopeDecision.OUT_OF_SCOPE
    )


def test_single_predicate_policy_ignores_material_status():
    """The Solution Design's MRP-only rule stays expressible."""
    design_rule = OarPolicy(
        predicates=(
            ScopePredicate(
                field=PredicateField.MRP_TYPE,
                operator=PredicateOperator.IN,
                values=("PD", "ND"),
            ),
        )
    )
    assert (
        assess_oar_scope(attributes(mrp_type="ND", material_status="01"), design_rule).scope
        is ScopeDecision.IN_SCOPE
    )


def test_no_mrp_value_is_hardcoded_in_the_scope_service():
    """The rule lives in configuration; this module only explains the verdict."""
    import inspect

    from app.initiatives.i7.features import oar_scope

    source = inspect.getsource(oar_scope)
    for node in ("'ND'", '"ND"', "'PD'", '"PD"', "'01'", '"01"'):
        assert node not in source


def test_extwg_is_not_an_active_predicate():
    """Retired as an OAR identifier."""
    assert "external_material_group" not in {field.value for field in PredicateField}
    configured = {value for predicate in POLICY.predicates for value in predicate.values}
    assert "100" not in configured
