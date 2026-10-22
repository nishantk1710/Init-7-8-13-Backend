"""OAR target-demand sanity check: a proposed ROP against the target's own consumption.

A diagnostic, kept apart from similarity and eligibility. It flags, never rejects; it distinguishes missing or
incomplete history from genuine zero consumption; it reads the observation window from the source data, never
the system date. Expected figures are worked out by hand in each test.

Working for the plausible case: mean 10 a month, sigma 3, lead time 60 days = 60 / 30.4375 = 1.9713 months.
requirement = 10 x 1.9713 + 1.645 x 3 x sqrt(1.9713) = 19.713 + 6.929 = 26.64.
"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.initiatives.i7.oar import demand_check
from app.initiatives.i7.oar.demand_check import DemandCheckStatus as Status
from app.initiatives.i7.oar.demand_check import DemandInputs, evaluate
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.thresholds import DemandSanityPolicy

D = Decimal


def inputs(**overrides) -> DemandInputs:
    base = dict(
        observed_months=12,
        total_demand=D(120),
        mean_monthly=D(10),
        std_monthly=D(3),
        lead_time_days=D(60),
        window_end=date(2026, 7, 1),
    )
    base.update(overrides)
    return DemandInputs(**base)


# --- plausible demand ----------------------------------------------------------------------------------


def test_a_rop_in_line_with_the_targets_demand_is_plausible() -> None:
    check = evaluate(inputs(), 30)

    assert check.status is Status.PLAUSIBLE and check.flagged is False
    assert abs(check.requirement - D("26.64")) < D("0.01")
    assert check.annual_consumption == 120
    assert abs(check.rop_to_requirement - D("1.126")) < D("0.01")
    assert not check.insufficient


def test_a_rop_at_exactly_the_limit_is_not_flagged() -> None:
    limit = int(evaluate(inputs(), 1).requirement * 2)  # the flag is strictly above 2 x requirement

    assert evaluate(inputs(), limit).flagged is False


# --- unusually high ----------------------------------------------------------------------------------


def test_an_unusually_high_rop_is_flagged_with_the_numbers_behind_it() -> None:
    check = evaluate(inputs(), 120)

    assert check.status is Status.HIGH_VS_DEMAND and check.flagged is True
    assert abs(check.rop_to_requirement - D("4.50")) < D("0.01")
    assert "120" in check.detail and "26.6" in check.detail and "60-day" in check.detail


def test_the_filter_example_a_rop_of_17_against_a_quiet_target_is_flagged() -> None:
    """A target consuming 1 a month with little variation, a 30-day lead time: it calls for about 1.7, so a
    ROP of 17 (what the mean produced) is ten times that."""
    check = evaluate(inputs(total_demand=D(12), mean_monthly=D(1), std_monthly=D("0.5"), lead_time_days=D(30)), 17)

    assert check.status is Status.HIGH_VS_DEMAND and check.flagged is True
    check_median = evaluate(
        inputs(total_demand=D(12), mean_monthly=D(1), std_monthly=D("0.5"), lead_time_days=D(30)), 3
    )
    assert check_median.status is Status.PLAUSIBLE


def test_a_rop_above_annual_consumption_is_not_by_itself_a_flag() -> None:
    """Consumption 1 a month, a 365-day lead time: over the lead time it needs 12 plus a buffer of about 2.8.
    A ROP of 13 is above the 12 consumed in a year, and entirely reasonable."""
    check = evaluate(inputs(total_demand=D(12), mean_monthly=D(1), std_monthly=D("0.5"), lead_time_days=D(365)), 13)

    assert check.annual_consumption == 12
    assert check.rop_to_annual > 1
    assert check.status is Status.PLAUSIBLE and check.flagged is False


def test_variability_raises_what_the_demand_calls_for() -> None:
    steady = evaluate(inputs(std_monthly=D(0)), 40)
    lumpy = evaluate(inputs(std_monthly=D(20)), 40)

    assert steady.requirement < lumpy.requirement
    assert steady.status is Status.HIGH_VS_DEMAND  # 40 > 2 x 19.7
    assert lumpy.status is Status.PLAUSIBLE


def test_a_longer_lead_time_raises_what_the_demand_calls_for() -> None:
    assert evaluate(inputs(lead_time_days=D(180)), 40).flagged is False
    assert evaluate(inputs(lead_time_days=D(15)), 40).flagged is True


# --- missing history is not zero consumption ------------------------------------------------------------


@pytest.mark.parametrize("overrides", [
    dict(observed_months=None, total_demand=None, mean_monthly=None, std_monthly=None),
    dict(observed_months=0, total_demand=None, mean_monthly=None, std_monthly=None),
    dict(total_demand=None, mean_monthly=None),
])
def test_no_history_is_insufficient_data_and_never_zero_consumption(overrides) -> None:
    check = evaluate(inputs(**overrides), 40)

    assert check.status is Status.INSUFFICIENT_DATA_NO_HISTORY
    assert check.flagged is False and check.insufficient
    assert "missing data, not zero consumption" in check.detail
    assert check.requirement is None and check.annual_consumption is None  # nothing invented


# --- incomplete window ---------------------------------------------------------------------------------


def test_an_incomplete_window_is_insufficient_data() -> None:
    check = evaluate(inputs(observed_months=8), 500)

    assert check.status is Status.INSUFFICIENT_DATA_INCOMPLETE_WINDOW and not check.flagged
    assert "8 month(s) of history, fewer than the 12-month window" in check.detail
    assert check.requirement is None


def test_an_incomplete_window_of_zero_demand_is_not_read_as_genuine_zero() -> None:
    check = evaluate(inputs(observed_months=6, total_demand=D(0), mean_monthly=D(0), std_monthly=D(0)), 5)

    assert check.status is Status.INSUFFICIENT_DATA_INCOMPLETE_WINDOW
    assert check.flagged is False


def test_a_longer_window_than_the_policys_is_complete() -> None:
    assert evaluate(inputs(observed_months=13), 30).status is Status.PLAUSIBLE


def test_the_window_is_the_policys() -> None:
    policy = DemandSanityPolicy(window_months=24)

    assert evaluate(inputs(observed_months=18), 30, policy).status is Status.INSUFFICIENT_DATA_INCOMPLETE_WINDOW


# --- genuine zero consumption -----------------------------------------------------------------------------


def test_zero_consumption_over_a_complete_window_is_a_real_finding() -> None:
    zero = inputs(total_demand=D(0), mean_monthly=D(0), std_monthly=D(0))

    flagged = evaluate(zero, 5)
    quiet = evaluate(zero, 0)

    assert flagged.status is Status.ZERO_CONSUMPTION and flagged.flagged is True
    assert "no consumption in a complete 12-month window; a ROP of 5 is proposed" in flagged.detail
    assert quiet.status is Status.ZERO_CONSUMPTION and quiet.flagged is False
    assert not flagged.insufficient  # distinct from missing data


def test_zero_consumption_needs_no_lead_time_to_be_known() -> None:
    check = evaluate(inputs(total_demand=D(0), mean_monthly=D(0), std_monthly=D(0), lead_time_days=None), 5)

    assert check.status is Status.ZERO_CONSUMPTION


# --- the other inputs missing ---------------------------------------------------------------------------


def test_a_missing_lead_time_is_insufficient_data() -> None:
    for lead in (None, D(0)):
        check = evaluate(inputs(lead_time_days=lead), 30)
        assert check.status is Status.INSUFFICIENT_DATA_NO_LEAD_TIME and not check.flagged
        assert check.requirement is None


def test_missing_variability_is_insufficient_data_not_zero_variability() -> None:
    check = evaluate(inputs(std_monthly=None), 30)

    assert check.status is Status.INSUFFICIENT_DATA_NO_VARIABILITY and not check.flagged


def test_nothing_to_check_without_a_proposed_rop_or_when_switched_off() -> None:
    assert evaluate(inputs(), None).status is Status.NOT_APPLICABLE
    assert evaluate(inputs(), 30, DemandSanityPolicy(enabled=False)).status is Status.NOT_APPLICABLE


# --- reads the data's window, not the clock ------------------------------------------------------------


def test_the_check_never_reads_the_system_date() -> None:
    source = inspect.getsource(demand_check)

    assert ".today(" not in source and ".now(" not in source and "time.time" not in source


def test_the_window_end_reported_is_the_datas() -> None:
    check = evaluate(inputs(window_end=date(2025, 3, 1)), 30)

    assert check.window_end == date(2025, 3, 1)
    assert dict(check.trace_entries())["demand_check_window_end"] == "2025-03-01"


def test_inputs_come_from_a_recommendation_row() -> None:
    row = SimpleNamespace(
        total_periods=13, total_demand=D(130), mean_demand_all_periods=D(10), std_dev_demand_all_periods=D(3),
        lead_time_days=None, planned_delivery_time_days=60, window_end=date(2026, 7, 1),
    )

    parsed = DemandInputs.from_row(row)

    assert parsed.lead_time_days == 60  # the planned delivery time stands in when no lead time is resolved
    assert parsed.observed_months == 13 and parsed.window_end == date(2026, 7, 1)
    assert DemandInputs.from_row(SimpleNamespace()) == DemandInputs(None, None, None, None, None, None)


# --- in the recommendation, separate from everything else -------------------------------------------------


def recommendation(**row_overrides):
    from app.initiatives.i7.recommendations import builder
    from tests.i7.test_recommendation_detail_completeness import oar_row

    row = oar_row(
        estimate_status="SUCCESS", oar_safety_stock=2, oar_rop=120, oar_max_stock=130,
        total_periods=12, total_demand=D(120), mean_demand_all_periods=D(10),
        std_dev_demand_all_periods=D(3), lead_time_days=D(60), window_end=date(2026, 7, 1),
        **row_overrides,
    )
    return builder.build_oar_recommendation(row, PolicyDocument())


def test_a_flagged_recommendation_carries_the_diagnostic_and_is_otherwise_unchanged() -> None:
    from app.initiatives.i7.recommendations.types import LifecycleStatus

    built = recommendation()
    trace = dict(built.calculation_trace)

    assert trace["demand_check_status"] == "HIGH_VS_DEMAND" and trace["demand_check_flag"] == "true"
    assert trace["demand_check_window_end"] == "2026-07-01"
    # a flag, not a gate: the status and every number stand
    assert built.status is LifecycleStatus.READY_FOR_REVIEW
    assert (built.recommended_safety_stock, built.recommended_rop, built.recommended_max_stock) == (2, 120, 130)


def test_a_recommendation_with_no_consumption_columns_reports_insufficient_data() -> None:
    from app.initiatives.i7.recommendations import builder
    from tests.i7.test_recommendation_detail_completeness import oar_row

    row = oar_row(estimate_status="SUCCESS", oar_safety_stock=2, oar_rop=30, oar_max_stock=40)
    trace = dict(builder.build_oar_recommendation(row, PolicyDocument()).calculation_trace)

    assert trace["demand_check_status"] == "INSUFFICIENT_DATA_NO_HISTORY"
    assert trace["demand_check_flag"] == "false"


def test_a_recommendation_without_an_estimate_has_no_demand_check() -> None:
    from app.initiatives.i7.recommendations import builder
    from tests.i7.test_recommendation_detail_completeness import oar_row

    trace = dict(builder.build_oar_recommendation(oar_row(), PolicyDocument()).calculation_trace)

    assert not any(key.startswith("demand_check") for key in trace)


def test_the_check_is_outside_similarity_scoring_and_eligibility() -> None:
    """It imports neither, and neither imports it: it cannot influence who a neighbour is or what it scores."""
    import app.initiatives.i7.oar.eligibility as eligibility
    import app.initiatives.i7.oar.scoring as scoring
    import app.initiatives.i7.oar.ranking as ranking
    import app.initiatives.i7.oar.retrieval as retrieval

    for module in (eligibility, scoring, ranking, retrieval):
        assert "demand_check" not in inspect.getsource(module), module.__name__
    imports = [
        line for line in inspect.getsource(demand_check).splitlines() if line.startswith(("from ", "import "))
    ]
    for name in ("scoring", "eligibility", "retrieval", "ranking", "identity"):
        assert not any(name in line for line in imports), (name, imports)
