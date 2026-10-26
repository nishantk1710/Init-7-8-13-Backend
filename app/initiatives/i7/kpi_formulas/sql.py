"""The formulas in force, as SQL.

The Overview counts in the database, so a validated formula is compiled into a SQLAlchemy expression
here rather than evaluated row by row in Python. Nothing in this module builds SQL from text: every
column comes from the reviewed catalogue (:mod:`.data_catalog`), and every operator from the validated tree.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.initiatives.i7.kpi_formulas.data_catalog import recommendation_columns, stockout_columns
from app.initiatives.i7.kpi_formulas.expression import compile_sql
from app.initiatives.i7.kpi_formulas.resolved import Formulas

_STAGES = ("avg_daily_use", "stock_needed")


def stockout_used_names(formulas: Formulas) -> frozenset[str]:
    """The stored facts the at-risk rule depends on, following its helpers back to the data they use.

    A material-plant is judged only when *these* are present and usable -- so a rule that never mentions
    lead time does not drop materials for want of one, and a rule that does is never applied to a
    lead time of zero.
    """
    pending = set(formulas.parsed["at_risk"].names)
    seen: set[str] = set()
    facts: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in _STAGES:
            pending |= set(formulas.parsed[name].names)
        else:
            facts.add(name)
    return frozenset(facts)


def at_risk_sql(formulas: Formulas, columns: Mapping[str, Any]) -> Any:
    """True for a judged material-plant that is at risk. ``columns`` is the stockout data set
    (:func:`.data_catalog.stockout_columns`); the stages feed each other: average daily use -> stock needed ->
    at risk. Mutates ``columns`` with the two stage values, so pass a fresh mapping."""
    columns["avg_daily_use"] = compile_sql(formulas.parsed["avg_daily_use"], columns)
    columns["stock_needed"] = compile_sql(formulas.parsed["stock_needed"], columns)
    return compile_sql(formulas.parsed["at_risk"], columns)


def stockout_rule(formulas: Formulas, *, usage: Any, feature: Any, on_hand: Any) -> tuple[Any, Any]:
    """``(rule, evaluated)`` for the stockout population: the at-risk rule, and whether the material-plant could be
    judged at all. ``on_hand`` is the summed-stock subquery (columns ``on_hand`` and ``blank_locations``).

    "Could be judged" means every stored fact *the rule uses* is present and usable: a rule that never
    mentions lead time does not drop a material for lacking one, and a rule that does is never applied to a
    lead time of zero (SAP's "not maintained") or a stock figure that is negative or has a blank storage
    location. The usage guard is unconditional -- the usage table is the population. Any other fact the rule
    uses (demand history, SAP planning values) only has to exist: a missing one makes the rule unknown, and
    ``rule OR NOT rule`` is true only when it is known either way.
    """
    from sqlalchemy import and_, not_, or_

    used = stockout_used_names(formulas)
    rule = at_risk_sql(formulas, stockout_columns(usage=usage, feature=feature, on_hand=on_hand.c.on_hand))
    guards = [usage.window_days > 0, usage.consumption_90d >= 0]
    if "lead_time_days" in used:
        guards.append(feature.lead_time_days > 0)
    if "stock_on_hand" in used:
        guards.append(
            and_(on_hand.c.on_hand.is_not(None), on_hand.c.on_hand >= 0, on_hand.c.blank_locations == 0)
        )
    return rule, and_(*guards, or_(rule, not_(rule)))


def excess_condition_sql(formulas: Formulas) -> Any:
    return compile_sql(formulas.parsed["excess_condition"], recommendation_columns())


def excess_value_sql(formulas: Formulas) -> Any:
    return compile_sql(formulas.parsed["excess_value"], recommendation_columns())


def working_capital_sql(formulas: Formulas) -> Any:
    return compile_sql(formulas.parsed["working_capital_value"], recommendation_columns())
