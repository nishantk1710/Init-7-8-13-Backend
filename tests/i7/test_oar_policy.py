"""OAR identification policy tests.

Three properties matter most, and each has burned somebody:

* the rule is *configuration* -- changing it needs no code edit;
* UNKNOWN is not OUT_OF_SCOPE -- 47% of live rows have no MRP type, and folding
  them into "not OAR" drops half the catalogue silently;
* EXTWG is retired -- and enforced, not merely documented.
"""

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.initiatives.i7.contracts import (
    MaterialAttributes,
    MaterialIdentity,
    MaterialPlantKey,
    PlantIdentity,
    ScopeDecision,
)
from app.initiatives.i7.policy import (
    OarPolicy,
    PredicateField,
    PredicateOperator,
    RollupPolicy,
    ScopePredicate,
    current_oar_policy,
)


def attributes(
    *, mrp_type: str | None = None, material_status: str | None = None, plant: str = "1300", **extra
) -> MaterialAttributes:
    return MaterialAttributes(
        key=MaterialPlantKey(
            material=MaterialIdentity(sap_material_number="000000000010000000"),
            plant=PlantIdentity(sap_plant_code=plant),
        ),
        mrp_type=mrp_type,
        material_status=material_status,
        **extra,
    )


# --- The rule in force ------------------------------------------------


@pytest.mark.parametrize("mrp_type", ["ND", "PD"])
def test_oar_when_mrp_type_matches(mrp_type):
    decision = current_oar_policy().evaluate(attributes(mrp_type=mrp_type))
    assert decision is ScopeDecision.IN_SCOPE


def test_not_oar_when_mrp_type_is_planned():
    """VB = normal Min-Max managed material, never OAR."""
    decision = current_oar_policy().evaluate(attributes(mrp_type="VB"))
    assert decision is ScopeDecision.OUT_OF_SCOPE


def test_mrp_type_alone_determines_oar_regardless_of_material_status():
    """The clarified rule is MRP_TYPE in {ND, PD} only -- material_status no
    longer participates in the active OAR predicate."""
    decision = current_oar_policy().evaluate(attributes(mrp_type="ND", material_status="01"))
    assert decision is ScopeDecision.IN_SCOPE


def test_only_one_predicate_in_the_active_rule():
    policy = current_oar_policy()
    assert len(policy.predicates) == 1
    assert policy.predicates[0].field is PredicateField.MRP_TYPE


# --- Unknown handling -------------------------------------------------


def test_missing_mrp_type_is_in_scope_not_unknown():
    """Business-confirmed exception for OAR specifically: a genuinely
    unmaintained MRP type (the staging adapter collapses a blank cell to
    None -- see ScopePredicate.unknown_values's docstring) now counts as
    OAR, the same as ND/PD. This reverses the rule's earlier behaviour
    (blank -> UNKNOWN); see oar.py's module docstring for why."""
    decision = current_oar_policy().evaluate(attributes(mrp_type=None))
    assert decision is ScopeDecision.IN_SCOPE


def test_undocumented_mrp_codes_are_out_of_scope_not_unknown():
    """V1/M0/RP/VI/VH/V2 are present and unmentioned by any ruling. A real value
    that fails the rule is a definite no; only absence is unknown."""
    for code in ("V1", "M0", "RP", "VI", "VH", "V2"):
        assert current_oar_policy().evaluate(
            attributes(mrp_type=code)
        ) is ScopeDecision.OUT_OF_SCOPE


# --- Per material-plant evaluation ------------------------------------


def test_same_material_can_differ_by_plant():
    """DISMM lives on MARC, so scope is a material-plant answer."""
    policy = current_oar_policy()
    at_1300 = policy.evaluate(attributes(mrp_type="PD", material_status="00", plant="1300"))
    at_1200 = policy.evaluate(attributes(mrp_type="VB", material_status="00", plant="1200"))
    assert at_1300 is ScopeDecision.IN_SCOPE
    assert at_1200 is ScopeDecision.OUT_OF_SCOPE


# --- Configurability (the point of Phase 1) ---------------------------


def test_rule_change_needs_no_code_change():
    """Adding an MRP type is configuration."""
    widened = OarPolicy(
        predicates=(
            ScopePredicate(
                field=PredicateField.MRP_TYPE,
                operator=PredicateOperator.IN,
                values=("ND", "PD", "V1"),
            ),
        )
    )
    assert widened.evaluate(attributes(mrp_type="V1")) is ScopeDecision.IN_SCOPE
    assert current_oar_policy().evaluate(
        attributes(mrp_type="V1", material_status="00")
    ) is ScopeDecision.OUT_OF_SCOPE


def test_policy_can_express_a_single_predicate_rule():
    """The Solution Design's MRP-only rule remains expressible."""
    design_rule = OarPolicy(
        predicates=(
            ScopePredicate(
                field=PredicateField.MRP_TYPE,
                operator=PredicateOperator.IN,
                values=("PD", "ND"),
            ),
        )
    )
    assert design_rule.evaluate(attributes(mrp_type="ND", material_status="01")) is ScopeDecision.IN_SCOPE


def test_all_operators_evaluate():
    cases = [
        (PredicateOperator.IN, ("ND", "PD"), "ND", ScopeDecision.IN_SCOPE),
        (PredicateOperator.NOT_IN, ("VB",), "ND", ScopeDecision.IN_SCOPE),
        (PredicateOperator.EQUALS, ("ND",), "ND", ScopeDecision.IN_SCOPE),
        (PredicateOperator.NOT_EQUALS, ("ND",), "ND", ScopeDecision.OUT_OF_SCOPE),
        (PredicateOperator.STARTS_WITH, ("N",), "ND", ScopeDecision.IN_SCOPE),
    ]
    for operator, values, raw, expected in cases:
        predicate = ScopePredicate(
            field=PredicateField.MRP_TYPE, operator=operator, values=values
        )
        assert predicate.evaluate(attributes(mrp_type=raw)) is expected


# --- EXTWG retirement, enforced ---------------------------------------


def test_extwg_is_not_an_available_predicate_field():
    """Retirement is enforced by the field simply not being offered."""
    assert "external_material_group" not in {field.value for field in PredicateField}
    with pytest.raises(ValueError):
        PredicateField("external_material_group")


def test_extwg_value_does_not_appear_in_the_active_rule():
    policy = current_oar_policy()
    configured = {value for predicate in policy.predicates for value in predicate.values}
    assert "100" not in configured


def test_extwg_stays_on_the_contract_for_audit():
    """Retired from the rule, kept in the data -- the extract carries it."""
    assert attributes(external_material_group="100").external_material_group == "100"


# --- Unconfirmed state ------------------------------------------------


def test_current_rule_is_not_marked_business_confirmed():
    assert current_oar_policy().confirmed is False


def test_rollup_is_unset_until_the_team_lead_rules():
    assert current_oar_policy().rollup is None


def test_rollup_is_configurable_when_decided():
    policy = OarPolicy(
        predicates=current_oar_policy().predicates, rollup=RollupPolicy.PER_PLANT_ONLY
    )
    assert policy.rollup is RollupPolicy.PER_PLANT_ONLY


# --- Predicate validation ---------------------------------------------


def test_predicate_without_values_rejected():
    with pytest.raises(PydanticValidationError):
        ScopePredicate(field=PredicateField.MRP_TYPE, operator=PredicateOperator.IN, values=())


def test_single_value_operator_rejects_multiple_values():
    with pytest.raises(PydanticValidationError):
        ScopePredicate(
            field=PredicateField.MATERIAL_STATUS,
            operator=PredicateOperator.NOT_EQUALS,
            values=("01", "02"),
        )


def test_policy_requires_at_least_one_predicate():
    """An empty rule would silently make every material OAR."""
    with pytest.raises(PydanticValidationError):
        OarPolicy(predicates=())
