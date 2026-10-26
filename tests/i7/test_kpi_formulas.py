"""Editable KPI formulas: the safe language, the defaults, and the SQL the Overview counts with.

None of this needs the database server. The SQL is exercised on in-memory SQLite, which is enough to
prove the compiled expression means the same as the Python reading of it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import Column, Integer, MetaData, Numeric, Table, create_engine, select

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
        "abs(a)",
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


def test_a_name_that_is_not_allowed_says_what_is():
    with pytest.raises(FormulaError, match="a, b, c"):
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
        parse(" + ".join(["a"] * 40), NAMES, "number")


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
    expr = at_risk_sql(
        formulas,
        usage_in_window=_stock.c.usage_in_window,
        window_days=_stock.c.window_days,
        lead_time_days=_stock.c.lead_time_days,
        stock_on_hand=_stock.c.stock_on_hand,
    )
    with engine.connect() as connection:
        got = [bool(r[0]) if r[0] is not None else None for r in connection.execute(select(expr).order_by(_stock.c.id))]
    assert got == [_python_at_risk(formulas, row) for row in ROWS]


def test_the_default_matches_the_rule_the_overview_used_before_it_was_editable(engine):
    """``SOH x window < consumption x lead time`` -- the old, division-free test."""
    formulas = defaults()
    expr = at_risk_sql(
        formulas,
        usage_in_window=_stock.c.usage_in_window,
        window_days=_stock.c.window_days,
        lead_time_days=_stock.c.lead_time_days,
        stock_on_hand=_stock.c.stock_on_hand,
    )
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
    assert expression.MAX_LENGTH == 200
