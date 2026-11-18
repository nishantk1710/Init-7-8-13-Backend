"""The list's value_change filter: released / tied-up rows are picked in SQL, so every page is covered.

Compiled to T-SQL and read as text, because the real database is not reachable locally."""

import inspect

from sqlalchemy import select
from sqlalchemy.dialects import mssql

from app.api.i7 import recommendations as module
from app.models.i7_recommendation import Recommendation


def _sql(value_change):
    statement = module._apply_filters(
        select(Recommendation.id),
        status=None,
        plant=None,
        material=None,
        demand_class=None,
        is_oar=None,
        confidence=None,
        criticality=None,
        generated_from=None,
        generated_to=None,
        value_change=value_change,
    )
    return str(statement.compile(dialect=mssql.dialect(), compile_kwargs={"literal_binds": True}))


def test_no_value_change_filter_adds_no_condition():
    assert "unit_price" not in _sql(None)


def test_released_keeps_only_a_positive_figure_from_the_columns_own_formula():
    sql = _sql("released")
    # unit_price x (current ROP - recommended ROP), and only where that figure could be worked out.
    assert "unit_price" in sql and "current_rop" in sql and "recommended_rop" in sql
    assert "> 0" in sql and "< 0" not in sql


def test_tied_up_keeps_only_a_negative_figure():
    sql = _sql("tied_up")
    assert "< 0" in sql and "> 0" not in sql


def test_list_and_export_both_take_the_filter():
    for endpoint in (module.list_recommendations, module.export_recommendations):
        assert "value_change" in inspect.signature(endpoint).parameters
