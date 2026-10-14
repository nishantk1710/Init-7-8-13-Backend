"""A TSB champion must not be read as if it were an SBA forecast.

Intermittent and lumpy safety stock takes ``p_final`` from the champion forecast
as the smoothed INTERVAL between demands (SBA: 1 or more). TSB's ``p_final`` is
the PROBABILITY that a month has demand (0 to 1). The declining-demand path makes
TSB the champion, so inventory converts it -- interval = 1 / p -- instead of
feeding a probability into an interval formula.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.initiatives.i7.inventory import safety_stock
from app.initiatives.i7.inventory import service as inventory
from app.initiatives.i7.inventory.types import CalculationStatus

Z = Decimal("1.645")
LEAD_TIME = Decimal("2")
MU, SIGMA = Decimal("40"), Decimal("12")


def calculate(model, parameters, demand_class="INTERMITTENT"):
    row = SimpleNamespace(
        model_name=model,
        parameters=parameters,
        mean_non_zero_demand=MU,
        std_dev_non_zero_demand=SIGMA,
    )
    lead = SimpleNamespace(is_available=True, lt_avg_months=LEAD_TIME, sigma_lt_months=Decimal(0))
    service = SimpleNamespace(status=CalculationStatus.SUCCESS, z_factor=Z, service_level=Decimal("0.95"))
    return inventory._safety_stock(row, demand_class, lead, None, service, [], "M1", "1300")


def test_an_sba_champion_is_used_exactly_as_before():
    assert calculate("SBA", "alpha=0.05;p_final=2.0;z_final=40") == safety_stock.compound_poisson(
        Z, LEAD_TIME, Decimal("2.0"), MU, SIGMA
    )


def test_a_tsb_probability_is_converted_to_an_interval_before_the_formula():
    # p = 0.25: demand in a quarter of the months, so one demand every 4 months.
    assert calculate("TSB", "declining_rule=declining-v1;p_final=0.25;z_final=40") == safety_stock.compound_poisson(
        Z, LEAD_TIME, Decimal(4), MU, SIGMA
    )


def test_feeding_the_probability_in_unconverted_would_give_a_different_answer():
    wrong = safety_stock.compound_poisson(Z, LEAD_TIME, Decimal("0.25"), MU, SIGMA)
    assert calculate("TSB", "p_final=0.25") != wrong


def test_a_falling_probability_lowers_the_safety_stock():
    higher = calculate("TSB", "p_final=0.5").safety_stock
    lower = calculate("TSB", "p_final=0.1").safety_stock
    assert lower < higher


def test_a_zero_probability_is_not_evaluable_not_a_division_error():
    result = calculate("TSB", "p_final=0")
    assert result.status is CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND
    assert "zero" in result.detail


def test_a_tsb_row_without_a_probability_is_not_evaluable():
    result = calculate("TSB", "declining_rule=declining-v1")
    assert result.status is CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND


def test_lumpy_demand_gets_the_converted_interval_in_the_simulation_too(monkeypatch):
    seen = {}

    def fake_simulate(service_level, lt, p_final, non_zero, material, plant):
        seen["p_final"] = p_final
        return SimpleNamespace(status=CalculationStatus.SUCCESS)

    monkeypatch.setattr(inventory.monte_carlo, "simulate", fake_simulate)
    calculate("TSB", "p_final=0.25", demand_class="LUMPY")
    assert seen["p_final"] == Decimal(4)
