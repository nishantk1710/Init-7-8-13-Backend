"""Inventory formulas: lead time, variability, service level, SS, ROP, Max.

The Formula Reference's worked examples are asserted directly -- they are the
best oracle available and they cost nothing to check. Everything else covers the
boundaries and the refusals, which is where a formula quietly goes wrong.

Service-level percentages appearing here are **mathematical fixtures**, used to
verify ``norm.ppf`` and the formulas that consume Z. None is business
configuration; the signed matrix is still unset.
"""

from decimal import Decimal

import pytest

from app.initiatives.i7.contracts import Criticality
from app.initiatives.i7.inventory import (
    monte_carlo,
    rop,
    safety_stock,
    variability,
)
from app.initiatives.i7.inventory import lead_time as lead_time_module
from app.initiatives.i7.inventory import max_stock as max_stock_module
from app.initiatives.i7.inventory import service_level as service_level_module
from app.initiatives.i7.inventory.lead_time import PurchaseOrderInput
from app.initiatives.i7.inventory.max_stock import (
    EoqMaxStockStrategy,
    MaxStockContext,
    NotConfiguredMaxStockStrategy,
    ReviewPeriodMaxStockStrategy,
)
from app.initiatives.i7.inventory.types import (
    DAYS_PER_MONTH,
    CalculationStatus,
    LeadTimeMethod,
)
from app.initiatives.i7.policy import (
    LeadTimePolicy,
    MaxStockStrategy as MaxStockPolicy,
    ServiceLevelKey,
    ServiceLevelPolicy,
)

POLICY = LeadTimePolicy()


def orders(days: list[int | None], cancelled: int = 0) -> list[PurchaseOrderInput]:
    result = [PurchaseOrderInput(day) for day in days]
    result.extend(PurchaseOrderInput(30, is_cancelled=True) for _ in range(cancelled))
    return result


# --- Lead time ---------------------------------------------------------
#
# By business decision, lead time comes from MARC-PLIFZ unconditionally --
# not from PO-to-GR history, and not only as a sub-2-PO fallback. This is a
# deliberate deviation from the Formula Reference's Stage 3 PO-statistics
# methodology; see the module docstring in inventory/lead_time.py. These
# tests cover the current, overridden behaviour.


def test_planned_delivery_time_is_used_regardless_of_po_count():
    """Even with 5+ usable POs, PLIFZ decides the figure, not PO statistics."""
    result = lead_time_module.analyse(orders([111, 125, 130, 115, 120]), 45, POLICY)
    assert result.status is CalculationStatus.LIMITED
    assert result.method is LeadTimeMethod.PLANNED_FALLBACK
    assert result.lt_avg_days == Decimal(45)


def test_month_conversion_uses_30_44():
    """30 or 31 would shift every lead time and so every safety stock."""
    assert DAYS_PER_MONTH == Decimal("30.44")
    result = lead_time_module.analyse([], 30, POLICY)
    assert result.lt_avg_months == Decimal(30) / Decimal("30.44")


def test_one_order_uses_planned_delivery():
    result = lead_time_module.analyse(orders([100]), 45, POLICY)
    assert result.status is CalculationStatus.LIMITED
    assert result.method is LeadTimeMethod.PLANNED_FALLBACK
    assert result.lt_avg_days == Decimal(45)


def test_no_orders_uses_planned_delivery():
    result = lead_time_module.analyse([], 60, POLICY)
    assert result.method is LeadTimeMethod.PLANNED_FALLBACK
    assert result.planned_lt_days == 60


def test_sigma_is_thirty_percent_of_planned():
    """The documented conservative default: sigma_LT = 0.3 x planned."""
    result = lead_time_module.analyse([], 100, POLICY)
    assert result.sigma_lt_days == Decimal(30)


def test_no_planned_time_is_not_evaluable_regardless_of_po_history():
    """No constant is substituted when PLIFZ is absent -- POs no longer help."""
    result = lead_time_module.analyse(orders([111, 125, 130, 115, 120]), None, POLICY)
    assert result.status is CalculationStatus.NOT_EVALUABLE_LEAD_TIME
    assert result.lt_avg_months is None


def test_zero_or_negative_planned_time_is_not_evaluable():
    result = lead_time_module.analyse([], 0, POLICY)
    assert result.status is CalculationStatus.NOT_EVALUABLE_LEAD_TIME


def test_cancelled_orders_are_still_counted_for_observability():
    """PO counts are retained in the result even though they no longer drive it."""
    result = lead_time_module.analyse(orders([100] * 5, cancelled=3), 45, POLICY)
    assert result.valid_po_count == 5
    assert result.excluded_cancelled_count == 3
    assert result.lt_avg_days == Decimal(45)


def test_po_count_is_observability_only_and_does_not_change_the_method():
    """Whether 0 or 10 usable POs exist, the method and figure are unchanged."""
    few = lead_time_module.analyse(orders([100]), 45, POLICY)
    many = lead_time_module.analyse(orders([100, 110, 120, 115, 105, 130]), 45, POLICY)
    assert few.method is many.method is LeadTimeMethod.PLANNED_FALLBACK
    assert few.lt_avg_days == many.lt_avg_days == Decimal(45)


# --- Demand variability ---------------------------------------------------


def test_sigma_d_includes_zero_periods():
    """"Include zeros -- they represent real zero-demand months.\""""
    with_zeros = variability.analyse([Decimal(6), Decimal(0), Decimal(6), Decimal(0)])
    assert with_zeros.zero_period_count == 2
    assert with_zeros.d_avg == Decimal(3)
    assert with_zeros.sigma_d > 0


def test_variability_needs_two_periods():
    result = variability.analyse([Decimal(5)])
    assert result.status is CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND
    assert result.sigma_d is None


def test_variability_with_no_history():
    assert variability.analyse([]).status is CalculationStatus.NOT_EVALUABLE_NO_HISTORY


def test_constant_demand_has_zero_variability():
    """A genuine zero, not a missing value."""
    result = variability.analyse([Decimal(5)] * 4)
    assert result.sigma_d == 0


# --- Demand variability from the feature store -------------------------------
#
# inventory/service.py reads mean_demand_all_periods / std_dev_demand_all_periods
# straight off MaterialFeature via from_feature, rather than recomputing them
# from i7_staged_consumption via analyse(). These tests pin from_feature's own
# behaviour and its agreement with analyse() on every status boundary.


def test_from_feature_matches_analyse_for_the_same_series():
    """Same series, same statistics, whichever function reads it."""
    series = [Decimal(6), Decimal(0), Decimal(6), Decimal(0)]
    direct = variability.analyse(series)
    from_store = variability.from_feature(
        total_periods=4,
        non_zero_periods=2,
        mean_demand_all_periods=direct.d_avg,
        std_dev_demand_all_periods=direct.sigma_d,
    )
    assert from_store.status == direct.status
    assert from_store.d_avg == direct.d_avg
    assert from_store.sigma_d == direct.sigma_d
    assert from_store.n_periods == direct.n_periods
    assert from_store.zero_period_count == direct.zero_period_count


def test_from_feature_no_history():
    result = variability.from_feature(0, 0, None, None)
    assert result.status is CalculationStatus.NOT_EVALUABLE_NO_HISTORY


def test_from_feature_insufficient_periods():
    result = variability.from_feature(1, 1, Decimal(5), None)
    assert result.status is CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND
    assert result.sigma_d is None


def test_from_feature_missing_std_dev_is_insufficient_even_with_enough_periods():
    """NULL std_dev_all_periods (Phase 3 could not compute it) blocks, not 0."""
    result = variability.from_feature(4, 2, Decimal(3), None)
    assert result.status is CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND


def test_from_feature_success():
    result = variability.from_feature(12, 8, Decimal("5.83"), Decimal("1.37"))
    assert result.status is CalculationStatus.SUCCESS
    assert result.n_periods == 12
    assert result.zero_period_count == 4
    assert result.d_avg == Decimal("5.83")
    assert result.sigma_d == Decimal("1.37")


# --- Service level and Z ----------------------------------------------------


@pytest.mark.parametrize(
    "level,expected",
    [("0.85", 1.04), ("0.90", 1.28), ("0.95", 1.65), ("0.98", 2.05), ("0.99", 2.33), ("0.995", 2.58)],
)
def test_z_factor_matches_the_documented_table(level, expected):
    """Formula Reference Stage 4B. Mathematical validation, not configuration."""
    assert abs(float(service_level_module.z_factor(Decimal(level))) - expected) < 0.01


@pytest.mark.parametrize("invalid", ["0", "1", "-0.5", "1.5"])
def test_z_factor_rejects_invalid_probabilities(invalid):
    """norm.ppf(0) is -inf and norm.ppf(1) is +inf; neither may reach a formula."""
    with pytest.raises(ValueError):
        service_level_module.z_factor(Decimal(invalid))


def test_unsigned_matrix_blocks():
    result = service_level_module.resolve(
        ServiceLevelPolicy(), Criticality.CRITICAL, "Milling"
    )
    assert result.status is CalculationStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET
    assert result.z_factor is None


def test_signed_matrix_resolves_and_derives_z():
    policy = ServiceLevelPolicy(
        matrix=((ServiceLevelKey(criticality=Criticality.CRITICAL), 0.98),)
    )
    result = service_level_module.resolve(policy, Criticality.CRITICAL, None)
    assert result.status is CalculationStatus.SUCCESS
    assert abs(float(result.z_factor) - 2.05) < 0.01


def test_missing_criticality_blocks_the_lookup():
    """Criticality is the matrix key; defaulting a tier would pick a service
    level by accident."""
    policy = ServiceLevelPolicy(
        matrix=((ServiceLevelKey(criticality=Criticality.NORMAL), 0.95),)
    )
    result = service_level_module.resolve(policy, None, None)
    assert result.status is CalculationStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET


# --- Safety stock, Path A ------------------------------------------------------


def test_documented_normal_safety_stock_example():
    """Stage 5 Path A: D_avg=5.83 sigma_D=1.37 LT=3.99 sigma_LT=0.246 Z=2.05
    -> term1 7.49, term2 2.06, SS 6.33 -> 7."""
    result = safety_stock.normal(
        Decimal("2.05"), Decimal("3.99"), Decimal("1.37"), Decimal("5.83"), Decimal("0.246")
    )
    trace = dict(result.trace)
    assert abs(float(trace["term_1"]) - 7.49) < 0.01
    assert abs(float(trace["term_2"]) - 2.06) < 0.01
    assert abs(float(result.raw_safety_stock) - 6.33) < 0.01
    assert result.safety_stock == 7


def test_safety_stock_rounds_up_not_to_nearest():
    """6.33 -> 7. Rounding to nearest would give 6 and under-protect."""
    result = safety_stock.normal(
        Decimal("2.05"), Decimal("3.99"), Decimal("1.37"), Decimal("5.83"), Decimal("0.246")
    )
    assert result.safety_stock == 7


def test_safety_stock_trace_is_complete():
    result = safety_stock.normal(
        Decimal("2.05"), Decimal("3.99"), Decimal("1.37"), Decimal("5.83"), Decimal("0.246")
    )
    keys = dict(result.trace).keys()
    assert {"z_factor", "term_1", "term_2", "raw_safety_stock", "rounding_method"} <= keys


def test_negative_input_is_an_error_not_a_clamp():
    result = safety_stock.normal(
        Decimal("2.05"), Decimal("-1"), Decimal("1.37"), Decimal("5.83"), Decimal("0.246")
    )
    assert result.status is CalculationStatus.CALCULATION_ERROR
    assert result.safety_stock is None


# --- Safety stock, Path B --------------------------------------------------------


def test_documented_compound_poisson_example():
    """Stage 5 Path B: p=1.68 mu_nz=5.57 sigma_nz=1.56 LT=3.99 Z=2.05
    -> lambda 2.375, E[d2] 33.459, Var 79.47, SS 18.27 -> 19."""
    result = safety_stock.compound_poisson(
        Decimal("2.05"), Decimal("3.99"), Decimal("1.68"), Decimal("5.57"), Decimal("1.56")
    )
    trace = dict(result.trace)
    assert abs(float(trace["lambda"]) - 2.375) < 0.001
    assert abs(float(trace["second_moment"]) - 33.459) < 0.01
    assert abs(float(trace["variance_ltd"]) - 79.47) < 0.05
    assert abs(float(result.raw_safety_stock) - 18.27) < 0.01
    assert result.safety_stock == 19


def test_compound_poisson_refuses_a_zero_interval():
    """lambda = LT / p_final would divide by zero."""
    result = safety_stock.compound_poisson(
        Decimal("2.05"), Decimal("3.99"), Decimal(0), Decimal("5.57"), Decimal("1.56")
    )
    assert result.status is CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND


# --- Monte Carlo ------------------------------------------------------------------


NON_ZERO = [Decimal(x) for x in (5, 40, 8, 120, 15, 60)]


def test_monte_carlo_runs_ten_thousand_simulations():
    result = monte_carlo.simulate(
        Decimal("0.95"), Decimal("3.99"), Decimal("2.5"), NON_ZERO, "M1", "1300"
    )
    assert dict(result.trace)["simulation_count"] == "10000"


def test_monte_carlo_is_deterministic():
    """Identical inputs must give an identical answer, or a recommendation
    becomes irreproducible the moment anyone asks how it was reached."""
    first = monte_carlo.simulate(
        Decimal("0.95"), Decimal("3.99"), Decimal("2.5"), NON_ZERO, "M1", "1300"
    )
    second = monte_carlo.simulate(
        Decimal("0.95"), Decimal("3.99"), Decimal("2.5"), NON_ZERO, "M1", "1300"
    )
    assert first.raw_safety_stock == second.raw_safety_stock


def test_monte_carlo_seed_is_stable_across_processes():
    """Derived by hash, not Python's randomised built-in hash()."""
    assert monte_carlo.derive_seed("M1", "1300") == monte_carlo.derive_seed("M1", "1300")


def test_different_materials_get_different_streams():
    first = monte_carlo.simulate(
        Decimal("0.95"), Decimal("3.99"), Decimal("2.5"), NON_ZERO, "M1", "1300"
    )
    second = monte_carlo.simulate(
        Decimal("0.95"), Decimal("3.99"), Decimal("2.5"), NON_ZERO, "M2", "1300"
    )
    assert first.raw_safety_stock != second.raw_safety_stock


def test_monte_carlo_quantile_exceeds_the_mean():
    result = monte_carlo.simulate(
        Decimal("0.95"), Decimal("3.99"), Decimal("2.5"), NON_ZERO, "M1", "1300"
    )
    trace = dict(result.trace)
    assert float(trace["simulated_quantile"]) > float(trace["simulated_mean"])
    assert result.safety_stock > 0


def test_higher_service_level_needs_more_safety_stock():
    low = monte_carlo.simulate(
        Decimal("0.90"), Decimal("3.99"), Decimal("2.5"), NON_ZERO, "M1", "1300"
    )
    high = monte_carlo.simulate(
        Decimal("0.99"), Decimal("3.99"), Decimal("2.5"), NON_ZERO, "M1", "1300"
    )
    assert high.raw_safety_stock > low.raw_safety_stock


@pytest.mark.parametrize(
    "sizes,interval",
    [([Decimal(5)], Decimal("2.5")), ([], Decimal("2.5")), (NON_ZERO, Decimal(0))],
)
def test_monte_carlo_edge_cases_are_explicit(sizes, interval):
    result = monte_carlo.simulate(
        Decimal("0.95"), Decimal("3.99"), interval, sizes, "M1", "1300"
    )
    assert result.status is CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND
    assert result.safety_stock is None


# --- ROP --------------------------------------------------------------------------


def test_documented_rop_example():
    """Stage 6: forecast 5.83 x LT 3.99 = 23.26, + SS 7 = 30.26 -> 31."""
    result = rop.calculate(Decimal("5.83"), Decimal("3.99"), 7)
    assert abs(float(result.expected_lead_time_demand) - 23.26) < 0.01
    assert abs(float(result.raw_rop) - 30.26) < 0.01
    assert result.rop == 31


def test_documented_intermittent_rop_example():
    """forecast 3.18 x 3.99 = 12.69, + SS 19 = 31.69 -> 32."""
    result = rop.calculate(Decimal("3.18"), Decimal("3.99"), 19)
    assert result.rop == 32


def test_rop_rounds_only_after_the_full_calculation():
    """Rounding E[LTD] first would give a different answer."""
    result = rop.calculate(Decimal("5.83"), Decimal("3.99"), 7)
    assert result.rop == 31


def test_negative_forecast_is_rejected():
    result = rop.calculate(Decimal("-1"), Decimal("3.99"), 7)
    assert result.status is CalculationStatus.NOT_EVALUABLE_INVALID_FORECAST
    assert result.rop is None


# --- Max stock ---------------------------------------------------------------------


def test_no_strategy_configured_returns_not_configured():
    result = NotConfiguredMaxStockStrategy().calculate(
        MaxStockContext(7, 31, Decimal("5.83"), None, None, None, None, None)
    )
    assert result.status is CalculationStatus.NOT_CONFIGURED
    assert result.max_stock is None


def test_policy_without_a_strategy_selects_the_not_configured_one():
    strategy = max_stock_module.strategy_for(MaxStockPolicy())
    assert isinstance(strategy, NotConfiguredMaxStockStrategy)


def test_documented_eoq_example():
    """Stage 7 Option A: D_annual=69.96 S=2000 H=0.25 P=12500 SS=7
    -> EOQ 9.46 -> Max 17."""
    result = EoqMaxStockStrategy().calculate(
        MaxStockContext(
            safety_stock=7,
            rop=31,
            forecast_rate=Decimal("5.83"),
            criticality=None,
            unit_price=Decimal(12500),
            ordering_cost=Decimal(2000),
            holding_cost_rate=Decimal("0.25"),
            review_period_months=None,
        )
    )
    assert abs(float(dict(result.trace)["eoq"]) - 9.46) < 0.01
    assert result.max_stock == 17


def test_eoq_without_cost_data_is_not_evaluable():
    """Ordering cost and holding rate exist in no document and no table."""
    result = EoqMaxStockStrategy().calculate(
        MaxStockContext(7, 31, Decimal("5.83"), None, Decimal(12500), None, None, None)
    )
    assert result.status is CalculationStatus.NOT_EVALUABLE_COST_DATA
    assert result.max_stock is None


def test_documented_review_period_example():
    """Stage 7 Option B: ROP 31 + (5.83 x 3) = 48.49 -> 49."""
    result = ReviewPeriodMaxStockStrategy().calculate(
        MaxStockContext(7, 31, Decimal("5.83"), "CRITICAL", None, None, None, Decimal(3))
    )
    assert abs(float(result.raw_max_stock) - 48.49) < 0.01
    assert result.max_stock == 49


def test_review_period_without_t_is_not_configured():
    result = ReviewPeriodMaxStockStrategy().calculate(
        MaxStockContext(7, 31, Decimal("5.83"), "CRITICAL", None, None, None, None)
    )
    assert result.status is CalculationStatus.NOT_CONFIGURED
    assert result.max_stock is None


def test_there_is_no_two_times_rop_fallback():
    """The Solution Design prohibits it by name."""
    import inspect

    source = inspect.getsource(max_stock_module)
    assert "2 * rop" not in source.lower().replace(" ", " ")
    for context in (
        MaxStockContext(7, 31, Decimal("5.83"), None, None, None, None, None),
        MaxStockContext(None, None, None, None, None, None, None, None),
    ):
        result = NotConfiguredMaxStockStrategy().calculate(context)
        assert result.max_stock is None


def test_unknown_strategy_name_does_not_fall_back_to_a_working_formula():
    """A typo in configuration must not silently produce numbers."""
    strategy = max_stock_module.strategy_for(MaxStockPolicy(strategy="typo"))
    assert isinstance(strategy, NotConfiguredMaxStockStrategy)
