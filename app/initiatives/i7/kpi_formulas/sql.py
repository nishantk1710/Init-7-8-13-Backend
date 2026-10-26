"""The formulas in force, as SQL.

The Overview counts in the database, so a validated formula is compiled into a SQLAlchemy expression
here rather than evaluated row by row in Python. Nothing in this module builds SQL from text: every
column comes from the fixed maps below, and every operator from the validated tree.
"""

from __future__ import annotations

from typing import Any

from app.initiatives.i7.kpi_formulas.expression import compile_sql
from app.initiatives.i7.kpi_formulas.resolved import Formulas
from app.models.i7_recommendation import Recommendation


def at_risk_sql(
    formulas: Formulas, *, usage_in_window: Any, window_days: Any, lead_time_days: Any, stock_on_hand: Any
) -> Any:
    """True for a judged material-plant that is at risk. The three stages feed each other:
    average daily use -> stock needed -> at risk."""
    columns: dict[str, Any] = {
        "usage_in_window": usage_in_window,
        "window_days": window_days,
        "lead_time_days": lead_time_days,
        "stock_on_hand": stock_on_hand,
    }
    columns["avg_daily_use"] = compile_sql(formulas.parsed["avg_daily_use"], columns)
    columns["stock_needed"] = compile_sql(formulas.parsed["stock_needed"], columns)
    return compile_sql(formulas.parsed["at_risk"], columns)


def recommendation_columns() -> dict[str, Any]:
    """The recommendation values the excess and working-capital formulas may use."""
    return {
        "current_max_stock": Recommendation.current_max_stock,
        "recommended_max_stock": Recommendation.recommended_max_stock,
        "current_rop": Recommendation.current_rop,
        "recommended_rop": Recommendation.recommended_rop,
        "current_safety_stock": Recommendation.current_safety_stock,
        "recommended_safety_stock": Recommendation.recommended_safety_stock,
        "unit_price": Recommendation.unit_price,
    }


def excess_condition_sql(formulas: Formulas) -> Any:
    return compile_sql(formulas.parsed["excess_condition"], recommendation_columns())


def excess_value_sql(formulas: Formulas) -> Any:
    return compile_sql(formulas.parsed["excess_value"], recommendation_columns())


def working_capital_sql(formulas: Formulas) -> Any:
    return compile_sql(formulas.parsed["working_capital_value"], recommendation_columns())
