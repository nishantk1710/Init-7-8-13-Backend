"""Safety stock is DEACTIVATED, not removed -- for stock items and for OAR.

``PolicyDocument.safety_stock`` carries two switches (``stock_item_enabled`` and ``oar_enabled``). The deployed
default is both off (``Settings`` through ``default_policy()``); the model default is on, so every existing caller
keeps the previous behaviour. These tests pin both sides:

* **off**: stock items get ``ROP = ceil(rate x LT)`` and ``Max = ceil(rate x (LT + T))`` with no SS recommendation
  and no SS-related blocker; OAR gets no SS estimate and a Max floor of ROP; nothing is recorded as a zero;
* **on**: the previous formulas and statuses come back unchanged (the code was kept, so reactivation is a setting);
* the categories are independent of each other, run identities differ by setting, Adjust works without SS, and a
  missing SS never reaches adoption as a zero.

No database is needed.
"""

import math
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.initiatives.i7.contracts.enums import Criticality
from app.initiatives.i7.inventory import calculate_one, max_stock as max_stock_module, rop as rop_module
from app.initiatives.i7.inventory.lead_time import PurchaseOrderInput
from app.initiatives.i7.inventory.types import (
    FORMULA_VERSION,
    STOCK_NO_SAFETY_STOCK_FORMULA_VERSION,
    CalculationStatus,
    formula_version_for,
)
from app.initiatives.i7.oar import estimate, service as oar_service
from app.initiatives.i7.oar.recalculate import PolicyChangedError, check_policy_matches_run
from app.initiatives.i7.oar.types import ALGORITHM_VERSION, NO_SAFETY_STOCK_SUFFIX, algorithm_version_for
from app.initiatives.i7.policy import PolicyDocument, ServiceLevelKey, ServiceLevelPolicy
from app.initiatives.i7.policy.dev_fixtures import default_policy
from app.initiatives.i7.policy.thresholds import SafetyStockPolicy
from app.initiatives.i7.policy.unresolved import MaxStockStrategy
from app.initiatives.i7.recommendations import builder, ledger
from app.initiatives.i7.recommendations.adoption import evaluate_parameter_adoption
from app.initiatives.i7.recommendations.types import (
    RECOMMENDATION_FORMULA_VERSION,
    AdoptionStatus,
    LifecycleStatus,
    recommendation_formula_version_for,
)
from app.initiatives.i7.oar import repository as oar_repository
from tests.i7.test_consumption_window import normal_row
from tests.i7.test_inventory_engine import DEMAND, FIVE_ORDERS, row
from tests.i7.test_oar_blend_path import oar_row
from tests.i7.test_oar_similarity import neighbour_with

DISABLED = CalculationStatus.NOT_APPLICABLE_SAFETY_STOCK_DISABLED
REVIEW_PERIOD = MaxStockStrategy(strategy="review_period", review_period_months=((Criticality.NORMAL, 1.0),))
SIGNED = ServiceLevelPolicy(matrix=tuple((ServiceLevelKey(criticality=tier), 0.95) for tier in Criticality))


def off(**kw) -> PolicyDocument:
    """Stock-item safety stock deactivated (OAR as given)."""
    oar = kw.pop("oar", False)
    return PolicyDocument(safety_stock=SafetyStockPolicy(stock_item_enabled=False, oar_enabled=oar), **kw)


def on(**kw) -> PolicyDocument:
    return PolicyDocument(safety_stock=SafetyStockPolicy(stock_item_enabled=True, oar_enabled=True), **kw)


LT = Decimal(120) / Decimal("30.44")  # row() plans 120 days; DAYS_PER_MONTH is 30.44


def ceil_(value: Decimal) -> int:
    return math.ceil(value)


# ======================================================================= policy and defaults


def test_the_model_default_is_the_previous_behaviour():
    """A bare PolicyDocument() (and every existing direct caller) still has safety stock on."""
    assert PolicyDocument().safety_stock == SafetyStockPolicy(stock_item_enabled=True, oar_enabled=True)


def test_the_deployed_default_is_both_off_and_each_switch_is_independent(monkeypatch):
    for name in ("I7_STOCK_ITEM_SAFETY_STOCK_ENABLED", "I7_OAR_SAFETY_STOCK_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    try:
        policy = default_policy()
        assert (policy.safety_stock.stock_item_enabled, policy.safety_stock.oar_enabled) == (False, False)

        monkeypatch.setenv("I7_STOCK_ITEM_SAFETY_STOCK_ENABLED", "true")
        get_settings.cache_clear()
        policy = default_policy()
        assert (policy.safety_stock.stock_item_enabled, policy.safety_stock.oar_enabled) == (True, False)

        monkeypatch.delenv("I7_STOCK_ITEM_SAFETY_STOCK_ENABLED")
        monkeypatch.setenv("I7_OAR_SAFETY_STOCK_ENABLED", "true")
        get_settings.cache_clear()
        policy = default_policy()
        assert (policy.safety_stock.stock_item_enabled, policy.safety_stock.oar_enabled) == (False, True)
    finally:
        get_settings.cache_clear()


# ======================================================================= stock item: ROP and Max


def test_rop_without_safety_stock_is_the_rounded_up_lead_time_demand():
    result = rop_module.calculate(Decimal("10.2"), Decimal("1.1"), None)
    assert result.status is CalculationStatus.SUCCESS
    assert result.raw_rop == Decimal("11.22") and result.rop == 12
    assert dict(result.trace)["safety_stock"] == "not_used"


def test_rop_with_a_safety_stock_is_unchanged():
    """The previous formula, kept: 11.22 + 3 = 14.22 -> 15."""
    assert rop_module.calculate(Decimal("10.2"), Decimal("1.1"), 3).rop == 15


def test_a_zero_safety_stock_is_a_value_and_none_is_not_used():
    assert rop_module.calculate(Decimal("10.2"), Decimal("1.1"), 0).rop == 12  # same figure, different meaning
    assert dict(rop_module.calculate(Decimal("10.2"), Decimal("1.1"), 0).trace)["safety_stock"] == "0"


def test_review_period_max_uses_the_unrounded_reorder_point():
    """rate 10.2, LT 1.1, T 1: the formula is ceil(10.2 x 2.1) = ceil(21.42) = 22; adding the rounded ROP gives 23."""
    rop = rop_module.calculate(Decimal("10.2"), Decimal("1.1"), None)
    context = dict(
        safety_stock=None, rop=rop.rop, forecast_rate=Decimal("10.2"), criticality="NORMAL",
        unit_price=Decimal(1), ordering_cost=None, holding_cost_rate=None, review_period_months=Decimal(1),
    )
    exact = max_stock_module.ReviewPeriodMaxStockStrategy().calculate(
        max_stock_module.MaxStockContext(**context, raw_rop=rop.raw_rop)
    )
    legacy = max_stock_module.ReviewPeriodMaxStockStrategy().calculate(max_stock_module.MaxStockContext(**context))
    assert exact.max_stock == 22
    assert legacy.max_stock == 23  # the legacy path (no raw_rop) is untouched
    assert dict(exact.trace)["raw_rop"] == "11.22"


def test_rop_never_exceeds_max_for_a_range_of_inputs():
    """ceil(x) <= ceil(x + y) for y >= 0 -- with T > 0 and rate, LT >= 0 the formulas cannot cross."""
    for rate in ("0", "0.01", "1", "5.83", "10.2", "134.782401"):
        for lt in ("0.01", "0.229961", "1.1", "3.9422"):
            for t in ("0.5", "1", "3"):
                r = Decimal(rate)
                rop = rop_module.calculate(r, Decimal(lt), None)
                mx = max_stock_module.ReviewPeriodMaxStockStrategy().calculate(
                    max_stock_module.MaxStockContext(
                        None, rop.rop, r, "NORMAL", Decimal(1), None, None, Decimal(t), raw_rop=rop.raw_rop
                    )
                )
                assert rop.rop <= mx.max_stock, (rate, lt, t)
                assert mx.max_stock == ceil_(r * (Decimal(lt) + Decimal(t)))


def test_a_zero_forecast_gives_zero_for_both():
    rop = rop_module.calculate(Decimal(0), Decimal("1.1"), None)
    mx = max_stock_module.ReviewPeriodMaxStockStrategy().calculate(
        max_stock_module.MaxStockContext(None, rop.rop, Decimal(0), "NORMAL", Decimal(1), None, None, Decimal(1), raw_rop=rop.raw_rop)
    )
    assert (rop.rop, mx.max_stock) == (0, 0)


def test_the_stock_item_calculation_is_exactly_the_agreed_formulas():
    policy = off(max_stock=REVIEW_PERIOD)
    result = calculate_one(row("ERRATIC"), FIVE_ORDERS, policy, DEMAND)
    rate = Decimal("5.83")
    assert result["safety_stock_status"] == DISABLED.value
    assert result["safety_stock"] is None and result["raw_safety_stock"] is None
    assert result["rop_status"] == CalculationStatus.SUCCESS.value
    assert result["rop"] == ceil_(rate * LT)
    assert result["max_stock_status"] == CalculationStatus.SUCCESS.value
    assert result["max_stock"] == ceil_(rate * (LT + 1))
    assert result["rop"] <= result["max_stock"]
    assert result["max_stock_strategy"] == "review_period"


def test_safety_stock_failures_and_a_missing_service_level_no_longer_block_a_stock_item():
    """No service-level matrix at all: previously NOT_EVALUABLE_SERVICE_LEVEL_UNSET for every row. Now irrelevant."""
    result = calculate_one(row("ERRATIC"), FIVE_ORDERS, off(max_stock=REVIEW_PERIOD), DEMAND)
    assert result["rop_status"] == CalculationStatus.SUCCESS.value
    # the same row with safety stock on is still blocked, exactly as before
    blocked = calculate_one(row("ERRATIC"), FIVE_ORDERS, on(), DEMAND)
    assert blocked["rop_status"] == CalculationStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET.value


def test_intermittent_without_sba_parameters_is_no_longer_blocked_by_the_safety_stock_inputs():
    """INTERMITTENT needs the smoothed interval only for safety stock; ROP and Max do not."""
    result = calculate_one(row("INTERMITTENT", parameters=None), FIVE_ORDERS, off(max_stock=REVIEW_PERIOD), DEMAND)
    assert result["rop_status"] == CalculationStatus.SUCCESS.value
    assert result["safety_stock_status"] == DISABLED.value


def test_lumpy_is_handled_without_the_monte_carlo_path():
    result = calculate_one(row("LUMPY", parameters=None), FIVE_ORDERS, off(max_stock=REVIEW_PERIOD), DEMAND)
    assert result["rop_status"] == CalculationStatus.SUCCESS.value and result["safety_stock"] is None


# ---- the legitimate blockers still block (the lead-time check moved from the SS step to the ROP step)


def test_a_missing_lead_time_still_blocks():
    result = calculate_one(row("ERRATIC", planned_days=None), [], off(max_stock=REVIEW_PERIOD), DEMAND)
    assert result["rop_status"] == CalculationStatus.NOT_EVALUABLE_LEAD_TIME.value
    assert result["rop"] is None and result["max_stock"] is None
    # what blocked it is the lead time -- not "safety stock is not part of the workflow", not the service level
    assert "safety stock" not in (result["detail"] or "").lower()
    assert "service level" not in (result["detail"] or "").lower()


def test_a_non_positive_lead_time_still_blocks():
    result = calculate_one(row("ERRATIC", planned_days=0), [], off(max_stock=REVIEW_PERIOD), DEMAND)
    assert result["rop_status"] == CalculationStatus.NOT_EVALUABLE_LEAD_TIME.value


def test_a_missing_forecast_still_blocks():
    result = calculate_one(row("ERRATIC", forecast_rate=None), FIVE_ORDERS, off(max_stock=REVIEW_PERIOD), DEMAND)
    assert result["rop_status"] == CalculationStatus.NOT_EVALUABLE_INVALID_FORECAST.value


def test_a_negative_forecast_still_blocks():
    result = calculate_one(row("ERRATIC", forecast_rate=Decimal(-1)), FIVE_ORDERS, off(max_stock=REVIEW_PERIOD), DEMAND)
    assert result["rop_status"] == CalculationStatus.NOT_EVALUABLE_INVALID_FORECAST.value


def test_obsolete_still_gets_nothing():
    result = calculate_one(row("ERRATIC", criticality="OBSOLETE"), FIVE_ORDERS, off(max_stock=REVIEW_PERIOD), DEMAND)
    assert result["rop_status"] == CalculationStatus.NOT_APPLICABLE_OBSOLETE.value


def test_no_history_still_blocks():
    result = calculate_one(row("UNCLASSIFIED"), FIVE_ORDERS, off(max_stock=REVIEW_PERIOD), DEMAND)
    assert result["rop_status"] == CalculationStatus.NOT_EVALUABLE_NO_HISTORY.value


def test_an_oar_material_is_still_deferred_to_the_oar_engine():
    result = calculate_one(row("LUMPY", oar_scope="IN_SCOPE"), FIVE_ORDERS, off(max_stock=REVIEW_PERIOD), DEMAND)
    assert result["rop_status"] == CalculationStatus.DEFERRED_TO_OAR.value


# ---- SAP overrides (Smooth / Erratic): ROP and Max as before; SS is not copied across


def test_sap_values_still_override_rop_and_max_but_safety_stock_is_not_copied():
    smooth = row(
        "SMOOTH", current_safety_stock=Decimal(50), current_reorder_point=Decimal(80), current_maximum_stock=Decimal(200)
    )
    result = calculate_one(smooth, FIVE_ORDERS, off(max_stock=REVIEW_PERIOD), DEMAND)
    assert result["rop_status"] == CalculationStatus.SUCCESS_FROM_CURRENT_SAP_VALUE.value and result["rop"] == 80
    assert result["max_stock_status"] == CalculationStatus.SUCCESS_FROM_CURRENT_SAP_VALUE.value
    assert result["max_stock"] == 200
    assert result["safety_stock_status"] == DISABLED.value and result["safety_stock"] is None
    assert smooth.current_safety_stock == Decimal(50)  # SAP's value is untouched
    assert "rop_exceeds_max" not in (result["max_stock_trace"] or "")


def test_with_safety_stock_on_the_sap_override_still_copies_it():
    smooth = row(
        "SMOOTH", current_safety_stock=Decimal(50), current_reorder_point=Decimal(80), current_maximum_stock=Decimal(200)
    )
    result = calculate_one(smooth, FIVE_ORDERS, on(), DEMAND)
    assert result["safety_stock_status"] == CalculationStatus.SUCCESS_FROM_CURRENT_SAP_VALUE.value
    assert result["safety_stock"] == 50


def test_a_sap_rop_above_a_calculated_max_is_marked_not_hidden_and_nothing_is_changed():
    """Decision A is open: the figures and the status stay as they are; the row is only flagged."""
    result = calculate_one(
        row("ERRATIC", current_reorder_point=Decimal(850)), FIVE_ORDERS, off(max_stock=REVIEW_PERIOD), DEMAND
    )
    assert result["rop"] == 850 and result["rop_status"] == CalculationStatus.SUCCESS_FROM_CURRENT_SAP_VALUE.value
    assert result["max_stock_status"] == CalculationStatus.SUCCESS.value
    assert result["rop"] > result["max_stock"]
    assert f"rop_exceeds_max={850} > {result['max_stock']}" in result["max_stock_trace"]


# ---- feature switch: on restores the previous behaviour


def test_with_the_switch_on_the_previous_formulas_and_statuses_are_unchanged():
    policy = on(service_level=SIGNED, max_stock=REVIEW_PERIOD)
    result = calculate_one(row("ERRATIC"), FIVE_ORDERS, policy, DEMAND)
    assert result["safety_stock_status"] == CalculationStatus.SUCCESS.value and result["safety_stock"] is not None
    assert result["rop"] == ceil_(Decimal("5.83") * LT + Decimal(result["safety_stock"]))
    # Max adds the ROUNDED reorder point, as it always did
    assert result["max_stock"] == ceil_(Decimal(result["rop"]) + Decimal("5.83"))
    assert "raw_rop" not in (result["max_stock_trace"] or "")


# ======================================================================= run identity


def test_run_identities_differ_by_setting_and_the_legacy_one_is_unchanged():
    assert formula_version_for(on()) == FORMULA_VERSION == "i07-formula-2"
    assert formula_version_for(off()) == STOCK_NO_SAFETY_STOCK_FORMULA_VERSION == "i07-formula-3"
    assert algorithm_version_for(PolicyDocument()) == ALGORITHM_VERSION
    assert algorithm_version_for(off(oar=False)) == ALGORITHM_VERSION + NO_SAFETY_STOCK_SUFFIX
    assert algorithm_version_for(off(oar=True)) == ALGORITHM_VERSION


def test_recommendation_rows_are_keyed_per_category_so_one_switch_does_not_rewrite_the_other():
    stock_off_oar_on = PolicyDocument(safety_stock=SafetyStockPolicy(stock_item_enabled=False, oar_enabled=True))
    assert recommendation_formula_version_for(False, stock_off_oar_on) == RECOMMENDATION_FORMULA_VERSION + "+stock-no-ss"
    assert recommendation_formula_version_for(True, stock_off_oar_on) == RECOMMENDATION_FORMULA_VERSION  # OAR untouched
    all_off = PolicyDocument(safety_stock=SafetyStockPolicy(stock_item_enabled=False, oar_enabled=False))
    assert recommendation_formula_version_for(True, all_off) == RECOMMENDATION_FORMULA_VERSION + "+oar-no-ss"
    assert recommendation_formula_version_for(None, all_off).endswith("+stock-no-ss")
    assert recommendation_formula_version_for(False, PolicyDocument()) == RECOMMENDATION_FORMULA_VERSION
    assert len(RECOMMENDATION_FORMULA_VERSION + "+stock-no-ss") <= 64  # the column's width


def test_the_monte_carlo_seed_does_not_move_when_the_switch_changes():
    """The seed still derives from FORMULA_VERSION, so switching safety stock back on reproduces the same draws."""
    from app.initiatives.i7.inventory import monte_carlo

    assert monte_carlo.derive_seed("M", "1300") == monte_carlo.derive_seed("M", "1300", FORMULA_VERSION)


def test_removing_a_similar_material_works_on_a_run_made_under_either_safety_stock_setting():
    """A run that still holds safety stock figures (made before the switch) must not block a donor removal: the
    recalculation uses its stored neighbours and the current setting. Only a different policy id or version is refused."""
    with_ss = SimpleNamespace(id=7, algorithm_version=ALGORITHM_VERSION, policy_id="i07-default", policy_version=1)
    without_ss = SimpleNamespace(
        id=8, algorithm_version=ALGORITHM_VERSION + NO_SAFETY_STOCK_SUFFIX, policy_id="i07-default", policy_version=1
    )
    older = SimpleNamespace(id=9, algorithm_version="oar-similarity-old", policy_id="i07-default", policy_version=1)
    for run in (with_ss, without_ss, older):
        for policy in (PolicyDocument(), off(oar=False)):
            check_policy_matches_run(run, policy)  # no error either way
    wrong = SimpleNamespace(id=10, algorithm_version=ALGORITHM_VERSION, policy_id="i07-default", policy_version=999)
    with pytest.raises(PolicyChangedError):
        check_policy_matches_run(wrong, off(oar=False))


def test_a_donor_removal_on_a_run_with_safety_stock_gives_rop_and_max_without_it():
    """The stored neighbours carry safety stock (the run was made before the switch); recalculating under the
    current setting drops it from the estimate and from the Max floor."""
    stored = [neighbour_with(f"0.{90 - i}", ss, rop, mx) for i, (ss, rop, mx) in enumerate([(5, 20, 8), (6, 22, 9), (7, 24, 10)])]
    grade, recalculated, best = oar_service.summarise_neighbours(stored[:2] + stored[2:], off(oar=False))
    assert recalculated.status.value == "SUCCESS"
    assert recalculated.safety_stock is None
    assert recalculated.max_stock == recalculated.rop  # weighted Max (below ROP) is floored at ROP, not ROP + SS


# ======================================================================= builder: stock item


DISABLED_ROW = dict(
    safety_stock_status=DISABLED.value, safety_stock_method=None, safety_stock=None, service_level=None, z_factor=None
)


def test_a_stock_item_is_ready_for_review_without_a_safety_stock():
    built = builder.build_normal_recommendation(normal_row(**DISABLED_ROW), PolicyDocument())
    assert built.status is LifecycleStatus.READY_FOR_REVIEW and built.blocking_reason is None
    assert built.recommended_safety_stock is None  # not 0
    assert built.recommended_rop == Decimal(9) and built.recommended_max_stock == Decimal(14)
    assert built.impact.safety_stock is None or built.impact.safety_stock.delta is None  # nothing invented
    assert "Blocked" not in [factor.label for factor in built.factors]  # no service level is needed any more
    assert "safety_stock" not in dict(built.calculation_trace)


def test_a_stock_item_blocked_by_its_rop_names_the_rop_not_safety_stock():
    built = builder.build_normal_recommendation(
        normal_row(**DISABLED_ROW, rop_status="NOT_EVALUABLE_LEAD_TIME", rop=None, detail="no usable lead time"),
        PolicyDocument(),
    )
    assert built.status is LifecycleStatus.NOT_EVALUABLE
    assert built.blocking_reason == "no usable lead time"
    assert "safety" not in built.blocking_reason.lower()
    assert dict(built.calculation_trace)["blocked_status"] == "NOT_EVALUABLE_LEAD_TIME"


def test_a_row_calculated_with_safety_stock_is_judged_exactly_as_before():
    ready = builder.build_normal_recommendation(normal_row(), PolicyDocument())
    assert ready.status is LifecycleStatus.READY_FOR_REVIEW and ready.recommended_safety_stock == Decimal(5)
    blocked = builder.build_normal_recommendation(
        normal_row(safety_stock_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET", safety_stock=None, rop_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET", rop=None),
        PolicyDocument(),
    )
    assert blocked.status is LifecycleStatus.NOT_EVALUABLE
    assert "safety stock status=" in blocked.blocking_reason


# ======================================================================= OAR


def _three(ss=(1, 2, 3), rop=(10, 20, 30), mx=(40, 50, 60)):
    return [neighbour_with(f"0.{90 - i}", ss[i], rop[i], mx[i]) for i in range(3)]


def test_oar_without_safety_stock_aggregates_none_and_floors_max_at_rop():
    donors = _three(ss=(1000, 2000, 3000), rop=(10, 20, 30), mx=(5, 6, 7))
    off_result = estimate.calculate(donors, True, include_safety_stock=False)
    assert off_result.status.value == "SUCCESS"
    assert off_result.safety_stock is None  # the donors' safety stock is not aggregated
    assert off_result.max_stock >= off_result.rop  # ROP is the floor
    trace = dict(off_result.trace)
    assert trace["max_stock_floor"] == str(off_result.rop) and trace["max_stock_floor_reason"] == "MAX_RAISED_TO_ROP"
    assert trace["safety_stock_status"] == "not_recommended"
    assert "safety_stock_raw" not in trace and "safety_stock_numerator" not in trace
    # no donor safety stock in the floor: with SS 1000-3000 the old floor would be far above this
    assert off_result.max_stock == off_result.rop


def test_oar_rop_and_the_weighted_max_are_unchanged_by_the_switch():
    donors = _three(rop=(10, 20, 30), mx=(100, 120, 140))
    legacy = estimate.calculate(donors, True)
    new = estimate.calculate(donors, True, include_safety_stock=False)
    assert new.rop == legacy.rop
    assert new.max_stock == legacy.max_stock  # weighted Max (120) is above both floors here
    assert legacy.safety_stock is not None and new.safety_stock is None
    assert new.inventory_eligible_neighbours == legacy.inventory_eligible_neighbours  # donor selection untouched
    assert new.qualifying_neighbours == legacy.qualifying_neighbours


def test_oar_with_the_switch_on_keeps_the_old_floor_of_rop_plus_safety_stock():
    legacy = estimate.calculate(_three(ss=(4, 4, 4), rop=(23, 23, 23), mx=(2, 2, 2)), True)
    assert (legacy.rop, legacy.safety_stock, legacy.max_stock) == (23, 4, 27)


def test_oar_max_floor_helper_in_both_modes():
    assert estimate.apply_max_floor(5, 99, 3, include_safety_stock=False)[0] == 5
    assert estimate.apply_max_floor(5, 99, 9, include_safety_stock=False)[0] == 9
    assert estimate.apply_max_floor(5, None, 3, include_safety_stock=False)[0] == 5  # SS is not looked at
    assert estimate.apply_max_floor(None, None, 3, include_safety_stock=False)[0] == 3  # missing ROP: not invented
    assert estimate.apply_max_floor(2, 1, 2)[0] == 3  # the previous behaviour


def test_oar_run_and_recalculation_share_the_switch_through_one_function():
    donors = _three()
    _, off_estimate, _ = oar_service.summarise_neighbours(donors, off(oar=False))
    _, on_estimate, _ = oar_service.summarise_neighbours(donors, on())
    assert off_estimate.safety_stock is None and on_estimate.safety_stock is not None
    assert off_estimate.rop == on_estimate.rop


# ======================================================================= builder: OAR


def test_an_oar_recommendation_carries_no_new_safety_stock_and_is_ready():
    built = builder.build_oar_recommendation(oar_row(), off(oar=False))
    assert built.status is LifecycleStatus.READY_FOR_REVIEW
    assert built.recommended_safety_stock is None  # whatever the stored OAR run holds (here 1)
    assert built.recommended_rop == Decimal(36) and built.recommended_max_stock == Decimal(48)
    assert built.safety_stock_method is None
    assert "safety_stock" not in dict(built.calculation_trace)


def test_an_oar_recommendation_with_the_switch_on_is_as_before():
    built = builder.build_oar_recommendation(oar_row(), on())
    assert built.recommended_safety_stock == Decimal(1) and built.safety_stock_method == "similarity_weighted"
    assert dict(built.calculation_trace)["safety_stock"] == "1"


def test_the_oar_switch_does_not_touch_stock_items_and_vice_versa():
    """Stock item off + OAR on: the OAR row keeps its SS; OAR off + stock item on: the stock row keeps its."""
    stock_off_oar_on = PolicyDocument(safety_stock=SafetyStockPolicy(stock_item_enabled=False, oar_enabled=True))
    assert builder.build_oar_recommendation(oar_row(), stock_off_oar_on).recommended_safety_stock == Decimal(1)
    stock_on_oar_off = PolicyDocument(safety_stock=SafetyStockPolicy(stock_item_enabled=True, oar_enabled=False))
    assert builder.build_normal_recommendation(normal_row(), stock_on_oar_off).recommended_safety_stock == Decimal(5)


# ======================================================================= Adjust (no database)


def _recommendation(**overrides):
    values = dict(
        recommendation_id="REC-X", current_version=1, is_oar=False,
        recommended_rop=Decimal(31), recommended_safety_stock=None, recommended_max_stock=Decimal(166),
        current_rop=Decimal(850), current_safety_stock=Decimal(650), current_max_stock=Decimal(1440),
        rop_delta=None, safety_stock_delta=None, max_stock_delta=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _Session:
    def __init__(self):
        self.added = []

    def execute(self, statement):
        return SimpleNamespace(scalar=lambda: None)

    def add(self, obj):
        self.added.append(obj)


def test_adjust_without_a_safety_stock_checks_rop_against_max_only():
    ledger._validate_adjustment(_recommendation(), ledger.AdjustedValues(rop=40, safety_stock=None, max_stock=170))
    with pytest.raises(ledger.WorkflowError, match="reorder point <= maximum stock"):
        ledger._validate_adjustment(_recommendation(), ledger.AdjustedValues(rop=200, safety_stock=None, max_stock=170))


def test_adjust_with_a_safety_stock_keeps_the_previous_rule():
    ledger._validate_adjustment(_recommendation(), ledger.AdjustedValues(rop=30, safety_stock=15, max_stock=36))
    with pytest.raises(ledger.WorkflowError, match="safety stock <= reorder point <= maximum stock"):
        ledger._validate_adjustment(_recommendation(), ledger.AdjustedValues(rop=10, safety_stock=15, max_stock=36))


def test_adjust_still_needs_rop_and_max_and_refuses_a_no_op_and_negatives():
    with pytest.raises(ledger.WorkflowError, match="needs the new reorder point and maximum stock"):
        ledger._validate_adjustment(_recommendation(), None)
    with pytest.raises(ledger.WorkflowError, match="same as the current"):
        ledger._validate_adjustment(_recommendation(), ledger.AdjustedValues(rop=31, safety_stock=None, max_stock=166))
    with pytest.raises(ledger.WorkflowError, match="negative"):
        ledger._validate_adjustment(_recommendation(), ledger.AdjustedValues(rop=-1, safety_stock=None, max_stock=166))


def test_adjust_without_a_safety_stock_never_writes_a_zero_and_leaves_a_stored_value_alone():
    for stored in (None, Decimal(14)):  # no recommendation, or a historical one
        rec = _recommendation(recommended_safety_stock=stored)
        session = _Session()
        ledger._apply_adjustment(session, rec, ledger.AdjustedValues(rop=40, safety_stock=None, max_stock=170), "U", "c")
        assert (rec.recommended_rop, rec.recommended_max_stock) == (40, 170)
        assert rec.recommended_safety_stock == stored  # None stays None, 14 stays 14 -- never 0
        assert rec.safety_stock_delta is None
        assert rec.rop_delta == Decimal(40) - Decimal(850) and rec.max_stock_delta == Decimal(170) - Decimal(1440)
        assert session.added[0].recommended_safety_stock == stored  # the history snapshot keeps what was replaced
        assert rec.current_safety_stock == Decimal(650)  # SAP's value is untouched


def test_adjust_with_a_safety_stock_still_writes_it():
    rec = _recommendation(recommended_safety_stock=Decimal(14))
    ledger._apply_adjustment(_Session(), rec, ledger.AdjustedValues(rop=30, safety_stock=15, max_stock=36), "U", "c")
    assert rec.recommended_safety_stock == 15 and rec.safety_stock_delta == Decimal(15) - Decimal(650)


def test_the_adjust_summary_names_only_what_was_adjusted():
    rec = _recommendation()
    assert ledger._adjustment_summary(rec, ledger.AdjustedValues(40, None, 170)) == "[ROP 31 -> 40, Max 166 -> 170]"
    assert "Safety stock" in ledger._adjustment_summary(rec, ledger.AdjustedValues(40, 5, 170))


def test_a_manual_conversion_request_no_longer_needs_a_safety_stock():
    """An OAR material that is NOT_ELIGIBLE has none now; the approver reviews ROP and Max."""
    assert ledger._has_recommended_values(_recommendation(recommended_safety_stock=None))
    assert not ledger._has_recommended_values(_recommendation(recommended_rop=None))
    assert not ledger._has_recommended_values(_recommendation(recommended_max_stock=None))


# ======================================================================= adoption


class _Sap:
    def __init__(self, state):
        self._state = state

    def current_state(self, material, plant):
        return self._state


def test_a_recommendation_without_a_safety_stock_adopts_on_rop_and_max():
    """SAP's own safety stock (999 here) is irrelevant: it is not compared, not counted as a mismatch."""
    result = evaluate_parameter_adoption("M", "1300", None, 31, 166, _Sap({"eisbe": "999", "minbe": "31", "mabst": "166"}))
    assert result.status is AdoptionStatus.ADOPTED
    assert [name for name, _ in result.expected] == ["reorder_point", "maximum_stock"]
    assert "safety_stock" not in result.matched_fields and "safety_stock" not in result.mismatched_fields


def test_rop_and_max_still_decide_partial_adoption_without_a_safety_stock():
    result = evaluate_parameter_adoption("M", "1300", None, 31, 166, _Sap({"eisbe": "0", "minbe": "31", "mabst": "999"}))
    assert result.status is AdoptionStatus.PARTIALLY_ADOPTED


def test_a_recommendation_that_still_has_a_safety_stock_is_checked_on_all_three():
    result = evaluate_parameter_adoption("M", "1300", 19, 32, 29, _Sap({"eisbe": "0", "minbe": "32", "mabst": "29"}))
    assert result.status is AdoptionStatus.PARTIALLY_ADOPTED and "safety_stock" in result.mismatched_fields


# ======================================================================= OAR donors: safety stock is not a condition


class _RecordingSession:
    """Records the SQL it is given and returns canned rows."""

    def __init__(self, rows=()):
        self.statements: list[str] = []
        self._rows = list(rows)

    def execute(self, statement):
        self.statements.append(str(statement))
        return iter(self._rows)


def _donor_row(material, safety_stock, rop=10, max_stock=20):
    return SimpleNamespace(
        sap_material_number=material, sap_plant_code="1300", current_safety_stock=safety_stock,
        current_reorder_point=Decimal(rop), current_maximum_stock=Decimal(max_stock),
    )


def test_the_donor_filter_has_no_safety_stock_condition_and_keeps_the_others():
    for sql in (oar_repository._CANDIDATE_SQL, oar_repository._DONOR_VALUES_SQL):
        assert "current_safety_stock" not in sql.replace("f.current_safety_stock,", "")  # only ever loaded, never filtered
        assert "f.current_safety_stock >" not in sql
        # the ROP, max stock and history conditions are exactly as they were
        assert "f.history_status = 'SUFFICIENT'" in sql
        assert "f.current_reorder_point > 0" in sql
        assert "f.current_maximum_stock > 0" in sql


def test_a_donor_without_a_safety_stock_lends_none_not_zero_and_keeps_its_rop_and_max():
    rows = [_donor_row("A", None, rop=12, max_stock=30), _donor_row("B", Decimal(0)), _donor_row("C", Decimal("2.4"))]
    values = oar_repository.load_inventory_values(_RecordingSession(rows))
    assert values[("A", "1300")] == {"safety_stock": None, "rop": 12, "max_stock": 30}
    assert values[("B", "1300")]["safety_stock"] == 0  # a real zero stays a zero
    assert values[("C", "1300")]["safety_stock"] == 3  # rounded up, as before
    assert values[("C", "1300")]["rop"] == 10 and values[("C", "1300")]["max_stock"] == 20


def test_a_donor_with_no_safety_stock_still_counts_for_the_estimate():
    """It is eligible (it lends ROP and max stock) and drags nothing down: safety stock is not aggregated at all."""
    donors = [neighbour_with("0.90", None, 10, 40), neighbour_with("0.80", 0, 20, 50), neighbour_with("0.70", None, 30, 60)]
    result = estimate.calculate(donors, True, include_safety_stock=False)
    assert result.status.value == "SUCCESS"
    assert result.inventory_eligible_neighbours == 3
    assert result.safety_stock is None and (result.rop, result.max_stock) == (20, 50)


def test_a_blend_peer_without_a_safety_stock_is_still_used_and_changes_nothing_else():
    """A peer's safety stock is not a condition for the OAR blend either: ROP and max stock decide whether it is used,
    and the blended ROP and Max are the same with or without the peers' safety stock."""
    from app.initiatives.i7.oar.blend import BlendInput, BlendStatus, PeerInput as Peer, estimate as blend_estimate
    from app.initiatives.i7.policy import OarBlendPolicy

    base = dict(monthly=[1, 0, 2, 0, 0, 0, 5, 0, 0, 0, 0, 0], n_events=6, lead_time_days=21)
    with_ss = [Peer("a", "1500", 0.85, 7, 1, 9, 9), Peer("b", "1300", 0.85, 17, 3, 19, 19)]
    without = [Peer("a", "1500", 0.85, 7, None, 9, 9), Peer("b", "1300", 0.85, 17, None, 19, 19)]
    config = OarBlendPolicy()
    expected = blend_estimate(BlendInput(peers=with_ss, **base), config, 7)
    result = blend_estimate(BlendInput(peers=without, **base), config, 7)
    assert result.status is BlendStatus.BLENDED and result.used_peer_count == 2
    assert (result.rop, result.max_stock) == (expected.rop, expected.max_stock)
    assert result.ss_peer is None and result.ss == math.ceil(result.ss_own)  # nothing to take from the peers
    # a peer still needs a ROP and a max stock
    unusable = [Peer("a", "1500", 0.85, 0, 1, 9, 9), Peer("b", "1300", 0.85, 17, 3, None, 19)]
    assert blend_estimate(BlendInput(peers=unusable, **base), config, 7).status is BlendStatus.OWN_ONLY


# ======================================================================= default review period: T = 1 month


def test_the_default_policy_calculates_max_with_a_one_month_review_period_and_no_dev_flag(monkeypatch):
    monkeypatch.delenv("I7_DEV_MOCK_MAX_STOCK", raising=False)
    monkeypatch.delenv("I7_STOCK_ITEM_SAFETY_STOCK_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        policy = default_policy()
    finally:
        get_settings.cache_clear()
    assert policy.safety_stock.stock_item_enabled is False
    result = calculate_one(row("ERRATIC"), FIVE_ORDERS, policy, DEMAND)
    rate = Decimal("5.83")
    assert result["max_stock_status"] == CalculationStatus.SUCCESS.value
    assert result["max_stock_strategy"] == "review_period"
    assert result["max_stock"] == ceil_(rate * (LT + 1))  # T = 1
    assert result["rop"] == ceil_(rate * LT) and result["rop"] <= result["max_stock"]
