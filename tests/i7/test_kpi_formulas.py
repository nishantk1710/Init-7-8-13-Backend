"""Editable KPI formulas: the safe language, the defaults, and the SQL the Overview counts with.

None of this needs the database server. The SQL is exercised on in-memory SQLite, which is enough to
prove the compiled expression means the same as the Python reading of it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import Column, Integer, MetaData, Numeric, Table, create_engine, literal_column, select

from app.initiatives.i7.kpi_formulas import expression
from app.initiatives.i7.kpi_formulas.definitions import DEFAULTS, FIELDS_BY_KEY
from app.initiatives.i7.kpi_formulas.expression import FormulaError, compile_sql, evaluate, parse, to_text
from app.initiatives.i7.kpi_formulas.presentation import formula_lines
from app.initiatives.i7.kpi_formulas.resolved import FormulaValidationError, defaults, validate
from app.initiatives.i7.kpi_formulas.sql import at_risk_sql

NAMES = {"a", "b", "c"}


# --- The language: what is accepted -----------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    ["a + b", "a * (b - c)", "a / b", "-a + 1", "a < b", "a * 2 >= b / 3", "a < b and b < c", "not a < b", "a ÷ b"],
)
def test_valid_formulas_are_accepted(source):
    expect = "condition" if any(op in source for op in "<>") else "number"
    assert parse(source, NAMES, expect).source


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os').system('x')",
        "a.__class__",
        "a[0]",
        "round(a)",
        "a ** 2",
        "a // b",
        "a % b",
        "'text'",
        "True",
        "lambda: 1",
        "[a, b]",
        "(x := 1)",
        "a if b else c",
        "a < b < c",
        "a == b",
        "a != b",
        "a in b",
        "z + 1",
    ],
)
def test_anything_outside_the_language_is_refused(source):
    with pytest.raises(FormulaError):
        parse(source, NAMES, "condition" if "<" in source or "==" in source or "in" in source else "number")


def test_a_name_that_is_not_allowed_says_to_pick_from_the_list():
    with pytest.raises(FormulaError, match="Pick one from the list"):
        parse("a + zzz", NAMES, "number")


def test_a_formula_must_give_what_its_field_needs():
    with pytest.raises(FormulaError, match="number"):
        parse("a < b", NAMES, "number")
    with pytest.raises(FormulaError, match="comparison"):
        parse("a + b", NAMES, "condition")


def test_mixing_numbers_and_conditions_is_refused():
    with pytest.raises(FormulaError):
        parse("(a < b) + 1", NAMES, "number")
    with pytest.raises(FormulaError):
        parse("a and b", NAMES, "condition")


@pytest.mark.parametrize("source", ["", "   ", "a +", "a + (", "1e999", "a * 99999999999"])
def test_empty_broken_and_absurd_formulas_are_refused(source):
    with pytest.raises(FormulaError):
        parse(source, NAMES, "number")


def test_length_and_size_are_capped():
    with pytest.raises(FormulaError, match="too long"):
        parse("a + " * 80 + "a", NAMES, "number")
    with pytest.raises(FormulaError, match="complicated"):
        parse(" + ".join(["a"] * 50), NAMES, "number")


def test_the_card_symbols_are_accepted():
    assert evaluate(parse("a × b ÷ c", NAMES, "number"), {"a": Decimal(6), "b": Decimal(4), "c": Decimal(3)}) == 8


# --- Reading a formula: Python -----------------------------------------------------------------------


def test_python_reading():
    p = parse("a * (b - c)", NAMES, "number")
    assert evaluate(p, {"a": Decimal(3), "b": Decimal(10), "c": Decimal(4)}) == 18


def test_a_missing_input_is_no_value_never_zero():
    p = parse("a + b", NAMES, "number")
    assert evaluate(p, {"a": Decimal(1), "b": None}) is None
    assert evaluate(parse("a < b", NAMES, "condition"), {"a": None, "b": Decimal(1)}) is None


def test_division_by_zero_is_no_value_not_an_error():
    assert evaluate(parse("a / b", NAMES, "number"), {"a": Decimal(1), "b": Decimal(0)}) is None


def test_and_or_follow_three_valued_logic():
    p = parse("a < b and b < c", NAMES, "condition")
    assert evaluate(p, {"a": Decimal(1), "b": Decimal(2), "c": Decimal(3)}) is True
    assert evaluate(p, {"a": Decimal(5), "b": Decimal(2), "c": None}) is False  # one side false settles it
    assert evaluate(p, {"a": Decimal(1), "b": Decimal(2), "c": None}) is None


# --- Reading a formula: text -------------------------------------------------------------------------


def test_text_uses_card_symbols_and_labels():
    p = parse("a / b * (c + 1)", NAMES, "number")
    assert to_text(p, {"a": "units used", "b": "days"}) == "units used ÷ days × (c + 1)"


def test_text_keeps_the_brackets_that_change_the_answer():
    assert to_text(parse("a - (b - c)", NAMES, "number")) == "a - (b - c)"
    assert to_text(parse("(a + b) * c", NAMES, "number")) == "(a + b) × c"
    assert to_text(parse("a * b + c", NAMES, "number")) == "a × b + c"


# --- The defaults are today's rules ------------------------------------------------------------------


def test_every_default_is_valid():
    assert defaults().is_default


def test_default_cards_read_as_they_always_did():
    lines = formula_lines(defaults())
    assert lines["stockout"] == [
        "Average daily use = units used in the last 90 days ÷ 90",
        "Stock needed = average daily use × lead time (days)",
        "At risk when stock on hand < stock needed",
    ]
    assert lines["excess"][0] == "Excess candidate when current max stock > recommended max stock"
    assert lines["working_capital"] == [
        "Working capital impact = unit price × (current reorder point - recommended reorder point)"
    ]


def test_a_changed_window_is_written_into_the_card():
    lines = formula_lines(validate({"window_days": 60}))
    assert lines["stockout"][0] == "Average daily use = units used in the last 60 days ÷ 60"


# --- validate(): numbers, ranges, reporting every problem ---------------------------------------------


def test_numbers_are_range_checked():
    with pytest.raises(FormulaValidationError) as caught:
        validate({"window_days": 1, "lookback_days": 999999})
    assert set(caught.value.errors) == {"window_days", "lookback_days"}


def test_every_problem_is_reported_not_just_the_first():
    with pytest.raises(FormulaValidationError) as caught:
        validate({"at_risk": "stock_on_hand +", "excess_value": "nope * 2", "bogus": 1})
    assert set(caught.value.errors) == {"at_risk", "excess_value", "bogus"}


def test_a_stage_may_only_use_what_comes_before_it():
    with pytest.raises(FormulaValidationError) as caught:
        validate({"avg_daily_use": "stock_needed * 2"})  # stock_needed is computed *from* it
    assert "avg_daily_use" in caught.value.errors


def test_settings_reject_text_and_booleans():
    for bad in ("abc", True, 1.5, None):
        with pytest.raises(FormulaValidationError):
            validate({"window_days": bad})


def test_defaults_are_filled_in_for_missing_fields():
    assert validate({"window_days": 60}).values == {**DEFAULTS, "window_days": 60}


# --- SQL means what the Python reading means -----------------------------------------------------------

_metadata = MetaData()
_stock = Table(
    "stock",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("usage_in_window", Numeric(18, 3)),
    Column("window_days", Integer),
    Column("lead_time_days", Numeric(18, 3)),
    Column("stock_on_hand", Numeric(18, 3)),
)

#: (usage in window, window days, lead time, stock on hand)
ROWS = [
    (900, 90, 30, 100),  # needs 300, has 100: at risk
    (900, 90, 30, 300),  # exactly the stock needed: not at risk
    (900, 90, 30, 400),
    (110500, 90, 15, 10000),  # the card's own example: 1,227.8 / day x 15 = 18,417 > 10,000
    (1, 90, 365, 3),  # a small daily average that must not round to zero
    (0, 90, 30, 0),  # no use, no stock: needs 0, has 0: not at risk
]


@pytest.fixture(scope="module")
def engine():
    engine = create_engine("sqlite://")
    _metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            _stock.insert(),
            [
                {"id": i, "usage_in_window": u, "window_days": w, "lead_time_days": lt, "stock_on_hand": s}
                for i, (u, w, lt, s) in enumerate(ROWS)
            ],
        )
    return engine


def _columns():
    return {
        "usage_in_window": _stock.c.usage_in_window,
        "window_days": _stock.c.window_days,
        "lead_time_days": _stock.c.lead_time_days,
        "stock_on_hand": _stock.c.stock_on_hand,
    }


def _python_at_risk(formulas, row):
    u, w, lt, s = row
    values = {
        "usage_in_window": Decimal(u),
        "window_days": Decimal(w),
        "lead_time_days": Decimal(lt),
        "stock_on_hand": Decimal(s),
    }
    for key in ("avg_daily_use", "stock_needed"):
        values[key] = evaluate(formulas.parsed[key], values)
    return evaluate(formulas.parsed["at_risk"], values)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"at_risk": "stock_on_hand <= stock_needed"},
        {"stock_needed": "avg_daily_use * (lead_time_days + 7)"},
        {"at_risk": "stock_on_hand < stock_needed * 1.2 and lead_time_days > 10"},
    ],
)
def test_the_sql_agrees_with_the_python_reading(engine, overrides):
    formulas = validate(overrides)
    expr = at_risk_sql(formulas, _columns())
    with engine.connect() as connection:
        got = [bool(r[0]) if r[0] is not None else None for r in connection.execute(select(expr).order_by(_stock.c.id))]
    assert got == [_python_at_risk(formulas, row) for row in ROWS]


def test_the_default_matches_the_rule_the_overview_used_before_it_was_editable(engine):
    """``SOH x window < consumption x lead time`` -- the old, division-free test."""
    formulas = defaults()
    expr = at_risk_sql(formulas, _columns())
    old = [s * w < u * lt for (u, w, lt, s) in ROWS]
    with engine.connect() as connection:
        new = [bool(r[0]) for r in connection.execute(select(expr).order_by(_stock.c.id))]
    assert new == old


def test_a_zero_divisor_in_sql_is_unknown_not_an_error(engine):
    p = parse("usage_in_window / (window_days - window_days)", {"usage_in_window", "window_days"}, "number")
    with engine.connect() as connection:
        values = connection.execute(
            select(compile_sql(p, {"usage_in_window": _stock.c.usage_in_window, "window_days": _stock.c.window_days}))
        ).scalars().all()
    assert values and all(v is None for v in values)


def test_sql_server_never_gets_a_bare_numeric_cast():
    """A bare ``CAST(x AS NUMERIC)`` is NUMERIC(18,0) on SQL Server: it would round a fractional divisor.
    SQLite cannot show this, so the statement is compiled for SQL Server and read."""
    from sqlalchemy.dialects import mssql

    p = parse("a / b", {"a", "b"}, "number")
    sql = str(select(compile_sql(p, {"a": _stock.c.usage_in_window, "b": _stock.c.lead_time_days})).compile(
        dialect=mssql.dialect()
    ))
    assert "AS NUMERIC)" not in sql
    assert sql.count("NUMERIC(38, 12)") == 2


def test_only_declared_fields_exist():
    assert set(DEFAULTS) == set(FIELDS_BY_KEY)
    assert expression.MAX_LENGTH == 300


# --- The versions table is missing (the migration has not been run) -----------------------------------


@pytest.fixture
def empty_database():
    from sqlalchemy.orm import Session

    engine = create_engine("sqlite://")  # no tables at all
    with Session(engine) as session:
        yield session


def test_a_missing_table_does_not_break_reading(empty_database):
    from app.initiatives.i7.kpi_formulas import service
    from app.initiatives.i7.kpi_formulas.resolved import load_active

    assert service.schema_ready(empty_database) is False
    assert service.applying_version(empty_database) is None
    assert service.history(empty_database) == []
    assert load_active(empty_database).is_default


def test_a_missing_table_is_a_clear_refusal_to_save_not_a_crash(empty_database):
    from app.initiatives.i7.kpi_formulas import service

    with pytest.raises(service.NotSetUp, match="migration"):
        service.save_and_apply(empty_database, {"window_days": 60}, "someone")


# --- Invented formulas: the data list, functions, and adoption -----------------------------------------


def test_every_catalogue_name_is_unique_within_its_data_set_and_not_a_function_or_stage():
    from app.initiatives.i7.kpi_formulas.data_catalog import CATALOG, HELPER_LABELS

    for data_set, items in CATALOG.items():
        names = [i.name for i in items]
        assert len(names) == len(set(names)), f"duplicate name in {data_set}"
        assert not set(names) & set(expression.FUNCTIONS), data_set
        assert not set(names) & set(HELPER_LABELS), data_set


def test_every_catalogue_entry_points_at_a_real_column():
    from app.initiatives.i7.kpi_formulas.data_catalog import RECOMMENDATION_ITEMS, STOCKOUT_ITEMS
    from app.models.i7_features import MaterialFeature
    from app.models.i7_recommendation import Recommendation
    from app.models.i7_stockout_risk import StockoutRiskInput

    models = {"recommendation": Recommendation, "feature": MaterialFeature, "usage": StockoutRiskInput}
    for item in (*RECOMMENDATION_ITEMS, *STOCKOUT_ITEMS):
        if item.source == "stock":
            continue  # summed from the staged stock by the caller, not a column
        assert hasattr(models[item.source], item.attribute), (item.name, item.source, item.attribute)
        assert item.label and item.description and item.group and item.unit, item.name


def test_a_formula_may_use_any_fact_in_its_data_set():
    from app.initiatives.i7.kpi_formulas.data_catalog import names_of

    for name in sorted(names_of("recommendation")):
        validate({"excess_condition": f"{name} > 0"})
    for name in sorted(names_of("stockout")):
        validate({"at_risk": f"{name} > 0"})


def test_a_fact_from_another_data_set_is_refused():
    with pytest.raises(FormulaValidationError) as caught:
        validate({"excess_condition": "usage_in_window > 0"})  # a stockout fact, not a recommendation one
    assert "excess_condition" in caught.value.errors
    with pytest.raises(FormulaValidationError):
        validate({"adoption_rate": "unit_price / 2"})


def test_invented_formulas_compile_for_sql_server_over_the_real_tables():
    """Every fact, in every position, must compile -- the correlated feature-store sub-selects included."""
    from sqlalchemy.dialects import mssql

    from app.initiatives.i7.kpi_formulas.sql import excess_condition_sql, excess_value_sql, working_capital_sql

    formulas = validate(
        {
            "excess_condition": "(current_rop + adi + history_months) > max(cv_squared, 1) * 2 "
            "and stock_on_hand >= abs(current_rop - recommended_rop)",
            "excess_value": "unit_price * max(current_max_stock - recommended_max_stock, 0)",
            "working_capital_value": "min(unit_price, 100) * (average_demand - demand_std_dev)",
        }
    )
    for build in (excess_condition_sql, excess_value_sql, working_capital_sql):
        sql = str(select(build(formulas)).compile(dialect=mssql.dialect()))
        assert "AS NUMERIC)" not in sql
    # unused facts cost nothing: a formula over its own columns adds no feature-store sub-select
    plain = str(select(excess_condition_sql(defaults())).compile(dialect=mssql.dialect()))
    assert "i7_material_feature" not in plain
    assert "i7_material_feature" in str(select(excess_condition_sql(formulas)).compile(dialect=mssql.dialect()))


@pytest.mark.parametrize(
    ("source", "values", "expected"),
    [
        ("max(a, b)", {"a": 3, "b": 7}, 7),
        ("min(a, b)", {"a": 3, "b": 7}, 3),
        ("abs(a - b)", {"a": 3, "b": 7}, 4),
        ("max(a - b, 0)", {"a": 3, "b": 7}, 0),
        ("min(a, 5) * max(b, 1)", {"a": 9, "b": 0}, 5),
    ],
)
def test_functions_agree_between_python_and_sql(engine, source, values, expected):
    p = parse(source, {"a", "b"}, "number")
    assert evaluate(p, {k: Decimal(v) for k, v in values.items()}) == expected
    columns = {k: literal_column(str(v)) for k, v in values.items()}
    with engine.connect() as connection:
        got = connection.execute(select(compile_sql(p, columns))).scalar()
    assert Decimal(str(got)) == expected


def test_a_missing_value_makes_min_max_abs_unknown_not_the_other_one():
    assert evaluate(parse("max(a, b)", {"a", "b"}, "number"), {"a": Decimal(1), "b": None}) is None
    assert evaluate(parse("abs(a)", {"a"}, "number"), {"a": None}) is None


@pytest.mark.parametrize(
    "source", ["pow(a, 2)", "max(a)", "max(a, b, a)", "abs(a, b)", "max(a=1, b=2)", "a.max(b)", "sum(a, b)"]
)
def test_only_min_max_abs_with_the_right_number_of_values_are_functions(source):
    with pytest.raises(FormulaError):
        parse(source, NAMES, "number")


def test_functions_print_as_written():
    assert to_text(parse("max(a - b, 0) / c", NAMES, "number")) == "max(a - b, 0) ÷ c"


def test_the_stockout_rule_judges_on_the_facts_it_uses():
    from app.initiatives.i7.kpi_formulas.sql import stockout_used_names

    assert stockout_used_names(defaults()) == {"usage_in_window", "window_days", "lead_time_days", "stock_on_hand"}
    # no lead time anywhere in the rule -> lead time is not required to judge
    only_stock = validate({"at_risk": "stock_on_hand < avg_daily_use * 30"})
    assert stockout_used_names(only_stock) == {"stock_on_hand", "usage_in_window", "window_days"}
    # a fact the helpers use counts, even though at_risk never names it
    via_helper = validate({"stock_needed": "avg_daily_use * planned_delivery_days"})
    assert "planned_delivery_days" in stockout_used_names(via_helper)
    assert stockout_used_names(validate({"at_risk": "adi > 3"})) == {"adi"}


def test_adoption_defaults_are_the_rule_the_card_always_used():
    f = defaults()
    for adopted, partially, not_adopted in [(10, 5, 5), (0, 0, 0), (3, 0, 0), (0, 0, 7), (12, 7, 1)]:
        values = {
            "adopted": Decimal(adopted),
            "partially_adopted": Decimal(partially),
            "not_adopted": Decimal(not_adopted),
        }
        rate = evaluate(f.parsed["adoption_rate"], values)
        known = adopted + partially + not_adopted
        if known == 0:
            assert rate is None  # nothing observed is "not yet measured", never 0%
        else:
            assert round(float(rate * 100), 1) == round((adopted + partially) / known * 100, 1)
        assert evaluate(f.parsed["adopted_card"], values) == adopted


def test_adoption_can_be_changed():
    f = validate({"adoption_rate": "adopted / tracked", "adopted_card": "adopted + partially_adopted"})
    values = {"adopted": Decimal(10), "partially_adopted": Decimal(5), "not_adopted": Decimal(5), "tracked": Decimal(50)}
    assert evaluate(f.parsed["adoption_rate"], values) == Decimal("0.2")
    assert evaluate(f.parsed["adopted_card"], values) == 15
    assert formula_lines(f)["adoption"] == [
        "Adoption rate = adopted ÷ all tracked",
        "Adopted card counts = adopted + partially adopted",
    ]
