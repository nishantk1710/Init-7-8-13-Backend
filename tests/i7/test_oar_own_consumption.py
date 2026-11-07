"""Consumption-first sizing of an eligible OAR material-plant, after the similarity engine.

Order, for a material-plant that passed the (unchanged) Min-Max conversion rule:

1. the similarity estimate, when it succeeded -- never replaced;
2. otherwise its own consumption over the last complete months, when there is enough of it;
3. otherwise manual review.

No database: the window and the monthly consumption are supplied by monkeypatching the two reads in
``recommendations.own_consumption_path``, and the formulas are checked against hand arithmetic.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from scipy.stats import norm

from app.core.criticality import CriticalityTier
from app.initiatives.i7.features.consumption_window import RunWindow, WindowStatus
from app.initiatives.i7.inventory import lead_time as lead_time_module
from app.initiatives.i7.inventory import own_consumption as oc
from app.initiatives.i7.inventory import service_level as service_level_module
from app.initiatives.i7.inventory.own_consumption import OwnConsumptionOutcome, OwnConsumptionStatus
from app.initiatives.i7.inventory.types import CalculationStatus
from app.initiatives.i7.policy import OwnConsumptionPolicy, PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import load_mock_max_stock_policy, load_mock_service_level_policy
from app.initiatives.i7.policy.unresolved import ServiceLevelKey, ServiceLevelPolicy
from app.initiatives.i7.recommendations import builder, own_consumption_path as path
from app.initiatives.i7.recommendations.types import ConversionEligibility, LifecycleStatus

OCT = date(2026, 10, 1)  # the last complete month; November is in progress and never in the window

POLICY = PolicyDocument(
    service_level=load_mock_service_level_policy(), max_stock=load_mock_max_stock_policy()
)


def monthly(**by_month: int) -> dict[date, Decimal]:
    """``monthly(m05=21, m07=19)`` -> May and July 2026."""
    return {date(2026, int(name[1:]), 1): Decimal(value) for name, value in by_month.items()}


BLEACH = monthly(m05=21, m07=19, m08=15, m09=11)  # 4 active months of 6, 66 units


def lead(days: int | None = 14):
    return lead_time_module.analyse([], days, POLICY.lead_time)


def service(tier: str | None = "NORMAL", policy: PolicyDocument = POLICY):
    return service_level_module.resolve(policy.service_level, builder._criticality_tier(tier), None)


def evaluate(values=BLEACH, *, tier="NORMAL", days=14, last=OCT, policy=POLICY, **kwargs):
    return oc.evaluate(
        monthly=values,
        last_complete_month=last,
        criticality=tier,
        lead=lead(days),
        service=service(tier, policy),
        policy=kwargs.pop("own_policy", policy.own_consumption),
    )


def oar_row(**overrides) -> SimpleNamespace:
    defaults = dict(
        sap_material_number="5000016353",
        sap_plant_code="1300",
        history_status="SUFFICIENT",
        criticality="NORMAL",
        non_zero_periods=84,
        consumption_count_12m=18,  # more than 4: eligible on usage
        demand_class="LUMPY",
        oar_scope="IN_SCOPE",
        feature_run_id=1,
        status="SUCCESS",
        confidence="LOW",
        candidates_considered=1628,
        eligible_candidates=1214,
        neighbour_count=0,
        best_similarity=None,
        estimate_status="NOT_EVALUABLE_NO_NEIGHBORS",
        oar_safety_stock=None,
        oar_rop=None,
        oar_max_stock=None,
        oar_run_id=7,
        current_safety_stock=Decimal(0),
        current_reorder_point=Decimal(0),
        current_maximum_stock=Decimal(0),
        unit_price=Decimal("79"),
        planned_delivery_time_days=14,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def similarity_row(**overrides) -> SimpleNamespace:
    return oar_row(
        status="SUCCESS", confidence="MEDIUM", neighbour_count=4, best_similarity=Decimal("0.85"),
        estimate_status="SUCCESS", oar_safety_stock=Decimal(9), oar_rop=Decimal(20), oar_max_stock=Decimal(40),
        **overrides,
    )


@pytest.fixture
def window(monkeypatch):
    """Supply the verified window and the monthly consumption the path reads."""

    def apply(data: dict, *, status=WindowStatus.USABLE.value, end=OCT):
        monkeypatch.setattr(path, "latest_run_window", lambda session: RunWindow(status, date(2024, 11, 1), end))
        monkeypatch.setattr(path.repository, "load_monthly_consumption", lambda session, first, last: data)

    return apply


def outcomes_for(rows, policy=POLICY):
    return path.evaluate_rows(object(), rows, policy)


def key(row):
    return (row.sap_material_number, row.sap_plant_code)


# --- 1. good consumption, no neighbours: consumption-based --------------------------------------


def test_good_history_and_no_neighbours_is_sized_from_own_consumption(window) -> None:
    window({("5000016353", "1300"): BLEACH})
    row = oar_row()
    outcome = outcomes_for([row])[key(row)]

    built = builder.build_oar_recommendation(row, POLICY, outcome)

    assert built.status is LifecycleStatus.READY_FOR_REVIEW
    assert built.blocking_reason is None
    assert (built.recommended_safety_stock, built.recommended_rop, built.recommended_max_stock) == (7, 12, 18)
    assert built.safety_stock_method == "consumption_based"
    assert built.max_stock_strategy == "rop_plus_lead_time_demand"
    assert built.lead_time_days == Decimal(14)
    assert built.conversion.eligibility is ConversionEligibility.ELIGIBLE  # eligibility is untouched
    trace = dict(built.calculation_trace)
    assert trace["estimation_method"] == "CONSUMPTION_BASED"
    for name in ("consumption_window", "total_consumption", "adc", "monthly_mean", "monthly_std", "cv",
                 "lead_time_days", "lead_time_source", "criticality", "service_level", "z_factor",
                 "safety_days", "safety_stock", "rop", "max_stock", "confidence", "confidence_basis"):
        assert name in trace, name
    assert trace["consumption_window"] == "2026-05-01..2026-10-31"


# --- 2. good history AND neighbours: similarity first, consumption not used ------------------------


def test_a_similarity_estimate_is_never_replaced_by_the_consumption_one(window) -> None:
    window({("5000016353", "1300"): BLEACH})
    row = similarity_row()
    outcome = outcomes_for([row])[key(row)]

    assert outcome.result is None  # the fallback was not needed, and was not run

    built = builder.build_oar_recommendation(row, POLICY, outcome)
    assert (built.recommended_safety_stock, built.recommended_rop, built.recommended_max_stock) == (9, 20, 40)
    assert built.safety_stock_method == "similarity_weighted"
    assert built.confidence == "MEDIUM"  # the engine's own grade
    assert dict(built.calculation_trace)["estimation_method"] == "SIMILARITY_BASED"


# --- 3. insufficient consumption, suitable neighbours: similarity --------------------------------


def test_thin_consumption_with_neighbours_still_uses_similarity(window) -> None:
    window({("5000016353", "1300"): monthly(m07=19)})  # one active month: thin, but similarity still wins
    row = similarity_row()
    built = builder.build_oar_recommendation(row, POLICY, outcomes_for([row])[key(row)])
    assert built.safety_stock_method == "similarity_weighted"
    assert built.recommended_rop == Decimal(20)


# --- 4. thin consumption (1 or 2 active months), no neighbours: sized, graded LOW ---------------------


def test_the_minimum_active_months_rule_is_gone() -> None:
    assert not hasattr(OwnConsumptionStatus, "INSUFFICIENT_HISTORY")
    assert not hasattr(OwnConsumptionPolicy(), "minimum_active_months")


@pytest.mark.parametrize("values, active", [(monthly(m07=120), 1), (monthly(m05=60, m09=60), 2)])
def test_one_or_two_active_months_still_get_values_with_low_confidence(window, values, active) -> None:
    window({("5000050697", "1300"): values})
    row = oar_row(sap_material_number="5000050697")
    outcome = outcomes_for([row])[key(row)]

    assert outcome.result.status is OwnConsumptionStatus.SUCCESS
    assert outcome.result.active_months == active
    built = builder.build_oar_recommendation(row, POLICY, outcome)

    assert built.status is LifecycleStatus.READY_FOR_REVIEW
    assert built.blocking_reason is None
    assert built.recommended_safety_stock is not None and built.recommended_rop is not None
    assert built.recommended_max_stock is not None
    assert built.safety_stock_method == "consumption_based"
    assert built.confidence == "LOW"
    assert dict(built.calculation_trace)["estimation_method"] == "CONSUMPTION_BASED"


def test_history_length_only_grades_confidence_it_never_blocks() -> None:
    steady4 = monthly(m05=10, m06=11, m07=10, m08=11)
    steady6 = monthly(m05=10, m06=11, m07=10, m08=11, m09=10, m10=11)
    rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    thin, four, six = evaluate(monthly(m07=120)), evaluate(steady4), evaluate(steady6)

    assert thin.status is OwnConsumptionStatus.SUCCESS and thin.confidence == "LOW"
    assert four.confidence != "LOW"  # 4 active months of steady demand is fair, not weak
    assert rank[six.confidence] >= rank[four.confidence] > rank[thin.confidence]


# --- 5 and 6. zero consumption on CRITICAL and IMPACT -----------------------------------------------


@pytest.mark.parametrize("tier", ["CRITICAL", "IMPACT"])
def test_a_zero_consumption_spare_gets_no_demand_based_value(window, tier) -> None:
    window({})  # nothing consumed in the window
    row = oar_row(criticality=tier, consumption_count_12m=0, history_status="COLD_START", demand_class="UNCLASSIFIED")
    outcome = outcomes_for([row])[key(row)]

    assert outcome.result.status is OwnConsumptionStatus.ZERO_CONSUMPTION
    assert outcome.result.safety_stock is None and outcome.result.rop is None and outcome.result.max_stock is None

    built = builder.build_oar_recommendation(row, POLICY, outcome)
    assert built.status is LifecycleStatus.NOT_EVALUABLE
    assert built.recommended_rop is None  # zero consumption is not read as zero need
    assert built.blocking_reason.startswith(oc.MANUAL_REVIEW_REASON)
    assert "ZERO_CONSUMPTION" in built.blocking_reason


@pytest.mark.parametrize("tier", ["CRITICAL", "IMPACT"])
def test_a_zero_consumption_spare_with_a_similarity_estimate_keeps_it(window, tier) -> None:
    window({})
    row = similarity_row(criticality=tier, consumption_count_12m=0)
    built = builder.build_oar_recommendation(row, POLICY, outcomes_for([row])[key(row)])
    assert built.safety_stock_method == "similarity_weighted"  # the similarity methodology is untouched


# --- 7. zero-consumption months inside the window -----------------------------------------------


def test_months_with_no_consumption_count_as_zero_in_the_statistics() -> None:
    values = monthly(m05=10, m08=10)  # 10, 0, 0, 10, 0, 0
    result = evaluate(values)

    assert result.monthly_values == tuple(Decimal(v) for v in (10, 0, 0, 10, 0, 0))
    assert result.total_consumption == 20
    assert result.monthly_mean == Decimal(20) / 6
    assert float(result.monthly_std) == pytest.approx(5.163977794943222)  # sample standard deviation
    assert float(result.cv) == pytest.approx(5.163977794943222 / (20 / 6))
    assert result.active_months == 2


def test_a_month_outside_the_verified_window_is_not_read_as_zero(window) -> None:
    window({("5000016353", "1300"): BLEACH}, status=WindowStatus.MISSING_DATA.value)
    row = oar_row()
    outcome = outcomes_for([row])[key(row)]
    assert outcome.result.status is OwnConsumptionStatus.NO_WINDOW  # nothing assumed about unverified months
    assert outcome.result.safety_stock is None


def test_the_month_in_progress_is_not_in_the_window() -> None:
    values = {**BLEACH, date(2026, 11, 1): Decimal(5000)}  # November has begun; it is not complete
    result = evaluate(values)
    assert result.window_end == date(2026, 10, 31)
    assert result.total_consumption == 66  # the 5000 is ignored
    assert date(2026, 11, 1) not in [date(2026, 5 + i, 1) for i in range(6)]


# --- 8. missing lead time ---------------------------------------------------------------------------


def test_a_missing_lead_time_stops_the_estimate_and_is_never_defaulted(window) -> None:
    window({("5000016353", "1300"): BLEACH})
    row = oar_row(planned_delivery_time_days=None)
    outcome = outcomes_for([row])[key(row)]
    assert outcome.result.status is OwnConsumptionStatus.LEAD_TIME_UNAVAILABLE
    built = builder.build_oar_recommendation(row, POLICY, outcome)
    assert built.status is LifecycleStatus.NOT_EVALUABLE and built.recommended_rop is None


# --- 9. a zero monthly mean cannot divide by zero ---------------------------------------------------


def test_a_zero_mean_is_handled_without_dividing_by_zero() -> None:
    result = evaluate({})
    assert result.status is OwnConsumptionStatus.ZERO_CONSUMPTION
    assert result.cv is None and result.safety_days is None and result.adc is None
    explicit_zeros = evaluate({date(2026, 5, 1): Decimal(0)})
    assert explicit_zeros.status is OwnConsumptionStatus.ZERO_CONSUMPTION


# --- 10 and 11. unusually high safety stock is flagged, never changed -----------------------------


def test_safety_days_above_the_limit_are_flagged_and_the_values_are_kept(window) -> None:
    single_burst = monthly(m07=100)  # one active month of six: very erratic
    own = OwnConsumptionPolicy()
    result = evaluate(single_burst, days=21, own_policy=own)

    assert result.safety_days > 45
    assert result.high_safety_stock is True and result.warning == "Unusually high safety stock: review"
    # Not capped: the calculation is exactly the formula.
    adc = Decimal(100) / 184
    assert result.safety_stock == math.ceil(float(adc * result.safety_days))
    assert result.confidence == "LOW"

    policy = POLICY.model_copy(update={"own_consumption": own})
    window({("5000016353", "1300"): single_burst})
    row = oar_row(planned_delivery_time_days=21)
    built = builder.build_oar_recommendation(row, policy, path.evaluate_rows(object(), [row], policy)[key(row)])
    assert built.recommended_safety_stock == result.safety_stock
    assert any("Unusually high safety stock: review" in f.detail for f in built.factors)
    assert dict(built.calculation_trace)["high_safety_stock_warning"] == "Unusually high safety stock: review"


def test_safety_stock_above_three_times_lead_time_demand_is_flagged_on_its_own() -> None:
    own = OwnConsumptionPolicy()
    result = evaluate(monthly(m07=60), days=7, own_policy=own)
    lead_time_demand = result.adc * result.lead_time_days

    assert result.safety_days <= 45  # the days rule alone would not flag it
    assert Decimal(result.safety_stock) > 3 * lead_time_demand
    assert result.high_safety_stock is True


def test_an_ordinary_estimate_is_not_flagged() -> None:
    result = evaluate()
    assert result.high_safety_stock is False and result.warning is None


# --- 12. each tier uses its own, configurable, service level -------------------------------------------


@pytest.mark.parametrize(
    ("tier", "level"), [("NORMAL", 0.85), ("INSURANCE", 0.90), ("IMPACT", 0.95), ("CRITICAL", 0.98)]
)
def test_the_z_factor_follows_the_tiers_service_level(tier, level) -> None:
    result = evaluate(tier=tier)
    assert float(result.service_level) == pytest.approx(level)
    assert float(result.z_factor) == pytest.approx(float(norm.ppf(level)), abs=1e-4)
    expected_days = float(result.z_factor) * float(result.cv) * math.sqrt(14 * 30.44)
    assert float(result.safety_days) == pytest.approx(expected_days, rel=1e-9)


def test_a_higher_tier_gets_more_safety_days_for_the_same_demand() -> None:
    days = {tier: evaluate(tier=tier).safety_days for tier in ("NORMAL", "INSURANCE", "IMPACT", "CRITICAL")}
    assert days["NORMAL"] < days["INSURANCE"] < days["IMPACT"] < days["CRITICAL"]


def test_the_service_levels_are_configuration_not_code() -> None:
    custom = PolicyDocument(
        service_level=ServiceLevelPolicy(matrix=((ServiceLevelKey(criticality=CriticalityTier.NORMAL), 0.60),)),
        max_stock=load_mock_max_stock_policy(),
    )
    low = evaluate(policy=custom)
    assert float(low.service_level) == pytest.approx(0.60)
    assert low.safety_days < evaluate().safety_days


def test_an_unset_service_level_blocks_the_estimate_and_is_never_guessed() -> None:
    result = evaluate(policy=PolicyDocument())  # no matrix signed
    assert result.status is OwnConsumptionStatus.SERVICE_LEVEL_UNSET and result.safety_stock is None


# --- 13. plant-specific criticality, one material at two plants ----------------------------------------


def test_each_plant_is_sized_with_its_own_tier_and_its_own_consumption(window) -> None:
    window({
        ("5000016353", "1300"): monthly(m05=21, m07=19, m08=15, m09=11),
        ("5000016353", "1500"): monthly(m05=40, m06=35, m07=30, m08=38, m09=33),
    })
    normal = oar_row(sap_plant_code="1300", criticality="NORMAL")
    impact = oar_row(sap_plant_code="1500", criticality="IMPACT", consumption_count_12m=20)
    out = outcomes_for([normal, impact])

    a, b = out[key(normal)].result, out[key(impact)].result
    assert a.criticality == "NORMAL" and b.criticality == "IMPACT"
    assert float(a.service_level) == pytest.approx(0.85) and float(b.service_level) == pytest.approx(0.95)
    assert a.total_consumption == 66 and b.total_consumption == 176  # each plant's own units
    assert a.adc != b.adc


# --- 14. calendar days and rounding ------------------------------------------------------------------


def test_the_window_is_the_exact_calendar_days() -> None:
    assert evaluate().window_days == 184  # May to October 2026
    leap = oc.window_months_ending(date(2028, 2, 1), 6)  # September 2027 to February 2028
    start, end = oc.window_bounds(leap)
    assert (start, end) == (date(2027, 9, 1), date(2028, 2, 29))
    assert oc.calendar_days(start, end) == 182  # 30+31+30+31+31+29, the leap day counted
    assert oc.calendar_days(*oc.window_bounds(oc.window_months_ending(date(2027, 2, 1), 6))) == 181


def test_values_are_rounded_up_once_at_the_end() -> None:
    result = evaluate()
    adc = result.total_consumption / 184
    safety_days = result.z_factor * result.cv * Decimal(str(math.sqrt(float(Decimal(14) * Decimal("30.44")))))
    assert result.safety_stock == math.ceil(float(adc * safety_days))
    assert result.rop == math.ceil(float(adc * (Decimal(14) + safety_days)))
    assert result.max_stock == math.ceil(float(Decimal(result.rop) + adc * 14))
    assert result.max_stock >= result.rop >= 0


def test_float_noise_just_above_a_whole_number_does_not_add_a_unit() -> None:
    assert oc._ceil(Decimal("17.0000000001")) == 17
    assert oc._ceil(Decimal("17.000001")) == 18
    assert oc._ceil(Decimal("17")) == 17


# --- who gets an outcome, and the switch -------------------------------------------------------------


def test_only_eligible_oar_material_plants_get_an_outcome(window) -> None:
    window({})
    not_eligible = oar_row(sap_material_number="A", criticality="NORMAL", consumption_count_12m=2)
    obsolete = oar_row(sap_material_number="B", criticality="OBSOLETE", consumption_count_12m=50)
    not_oar = oar_row(sap_material_number="C", oar_scope="OUT_OF_SCOPE")
    eligible = oar_row(sap_material_number="D")
    out = outcomes_for([not_eligible, obsolete, not_oar, eligible])
    assert set(out) == {key(eligible)}


def test_without_an_outcome_the_builder_behaves_as_it_did_before() -> None:
    row = oar_row()
    built = builder.build_oar_recommendation(row, POLICY)
    assert built.status is LifecycleStatus.NOT_EVALUABLE
    assert built.blocking_reason == "NO_SIMILAR_MATERIALS_FOUND"  # unchanged wording, no manual-review text
    assert "estimation_method" not in dict(built.calculation_trace)


def test_the_fallback_is_off_by_default() -> None:
    from app.core.config import Settings

    assert Settings().i7_oar_own_consumption_enabled is False


# --- confidence is separate and never raised by criticality -----------------------------------------


def test_confidence_is_graded_apart_from_the_formulas() -> None:
    result = evaluate()
    assert result.confidence in {"HIGH", "MEDIUM", "LOW"}
    assert "CV" in result.confidence_basis and "months" in result.confidence_basis


def test_a_planned_lead_time_keeps_the_grade_below_high() -> None:
    steady = monthly(m05=20, m06=22, m07=19, m08=21, m09=20, m10=18)  # every month active, steady
    result = evaluate(steady)
    assert result.active_months == 6 and float(result.cv) < 1.0
    assert result.confidence == "MEDIUM"  # the lead time is a plan, not an observation


def test_criticality_does_not_raise_confidence() -> None:
    normal, critical = evaluate(tier="NORMAL"), evaluate(tier="CRITICAL")
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    assert order[critical.confidence] <= order[normal.confidence]


def test_a_high_service_tier_is_held_to_the_stricter_consistency_band() -> None:
    erratic = monthly(m05=5, m06=5, m07=5, m08=5, m09=40)  # 5, 5, 5, 5, 40, 0: CV about 1.48
    normal, impact = evaluate(erratic, tier="NORMAL"), evaluate(erratic, tier="IMPACT")
    assert 1.0 < float(normal.cv) < 2.0
    assert normal.confidence == "MEDIUM" and impact.confidence == "LOW"
