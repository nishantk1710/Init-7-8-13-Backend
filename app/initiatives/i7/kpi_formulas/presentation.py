"""The formulas in force, as the page and the information cards show them."""

from __future__ import annotations

from app.initiatives.i7.kpi_formulas import expression
from app.initiatives.i7.kpi_formulas.definitions import (
    EXCESS,
    KPI_TITLES,
    STOCKOUT,
    VARIABLES,
    WORKING_CAPITAL,
)
from app.initiatives.i7.kpi_formulas.resolved import Formulas


def formula_lines(formulas: Formulas) -> dict[str, list[str]]:
    """Each KPI's formula as plain lines, with the window written out ("units used in the last 90
    days"), the way the information card's *How it is calculated* prints it."""
    window = formulas.window_days
    labels = dict(VARIABLES)
    labels["usage_in_window"] = f"units used in the last {window} days"

    def text(key: str) -> str:
        parsed = formulas.parsed[key]
        shown = dict(labels)
        shown["window_days"] = str(window)
        return expression.to_text(parsed, shown)

    return {
        STOCKOUT: [
            f"Average daily use = {text('avg_daily_use')}",
            f"Stock needed = {text('stock_needed')}",
            f"At risk when {text('at_risk')}",
        ],
        EXCESS: [
            f"Excess candidate when {text('excess_condition')}",
            f"Excess value = {text('excess_value')}",
        ],
        WORKING_CAPITAL: [f"Working capital impact = {text('working_capital_value')}"],
    }


__all__ = ["KPI_TITLES", "formula_lines"]
