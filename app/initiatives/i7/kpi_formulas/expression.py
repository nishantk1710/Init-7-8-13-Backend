"""A small, safe formula language for the Overview KPIs.

A user-edited formula is **data, never code**. It is parsed with :mod:`ast` and walked by this module
only; nothing is ever passed to ``eval``/``exec``/``compile``. The grammar is deliberately tiny:

* numbers (``90``, ``1.5``) and the names the caller allows (``stock_on_hand`` ...);
* ``+ - * /``, unary minus, parentheses;
* one comparison (``<  <=  >  >=``), and ``and`` / ``or`` / ``not`` over comparisons;
* ``×`` and ``÷`` are accepted for ``*`` and ``/`` because the information cards print them that way.

Anything else -- calls, attributes, subscripts, strings, ``**``, ``//``, ``%``, chained comparisons, a name
that is not allowed -- is a :class:`FormulaError` carrying a sentence the page can show as it is.

One validated tree has three readers, so the SQL the Overview counts with, the text on the information
card and the worked example can never describe different formulas:

* :func:`compile_sql` -- a SQLAlchemy expression, so counting stays in the database;
* :func:`evaluate` -- the same tree over ``Decimal`` values, for the worked example and preview;
* :func:`to_text` -- the formula as the card prints it.

A division is ``a / NULLIF(b, 0)`` in SQL and "no value" in Python, so a zero divisor makes the figure
missing, never an error and never zero. A missing figure (``NULL`` / ``None``) propagates through
arithmetic and makes a comparison unknown, which the counts treat as "not judged".
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Mapping

from sqlalchemy import Float, Numeric, and_, case, cast, func, literal, not_, or_

MAX_LENGTH = 300
MAX_NODES = 80

#: The only functions a formula may call, and how many values each takes.
FUNCTIONS: dict[str, int] = {"abs": 1, "min": 2, "max": 2}
MAX_CONSTANT = Decimal("1000000000")

Kind = Literal["number", "condition"]

_SYMBOLS = {"×": "*", "÷": "/", "≤": "<=", "≥": ">="}


class FormulaError(ValueError):
    """The formula cannot be used. The message is written for the person who typed it."""


@dataclass(frozen=True)
class Parsed:
    """A formula that has been checked. ``tree`` is the validated :mod:`ast` expression."""

    source: str
    tree: ast.expr
    kind: Kind
    names: frozenset[str]
    """The inputs the formula actually uses."""


def _normalise(source: str) -> str:
    for symbol, plain in _SYMBOLS.items():
        source = source.replace(symbol, plain)
    return source.strip()


def parse(source: str, allowed: frozenset[str] | set[str], expect: Kind) -> Parsed:
    """Check ``source`` and return it, or raise :class:`FormulaError`.

    ``allowed`` is every name the formula may use; ``expect`` is what the formula must produce -- a
    ``number`` (a quantity or a value) or a ``condition`` (true or false).
    """
    if not isinstance(source, str) or not source.strip():
        raise FormulaError("The formula is empty.")
    text = _normalise(source)
    if len(text) > MAX_LENGTH:
        raise FormulaError(f"The formula is too long ({len(text)} characters; the limit is {MAX_LENGTH}).")
    try:
        tree = ast.parse(text, mode="eval").body
    except SyntaxError:
        raise FormulaError("The formula is not valid. Check the brackets and the operators.") from None
    except (ValueError, RecursionError):
        raise FormulaError("The formula is not valid.") from None

    names: set[str] = set()
    nodes = 0

    def walk(node: ast.AST) -> Kind:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES:
            raise FormulaError("The formula is too complicated. Break it into simpler parts.")

        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FormulaError("Only numbers can be written into a formula, not text or true/false.")
            if abs(Decimal(str(value))) > MAX_CONSTANT:
                raise FormulaError(f"The number {value} is too large.")
            return "number"
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name not in FUNCTIONS or node.keywords:
                raise FormulaError("Only min(a, b), max(a, b) and abs(x) can be used as functions.")
            wanted = FUNCTIONS[name]
            if len(node.args) != wanted:
                raise FormulaError(f"{name}() needs {wanted} value{'s' if wanted > 1 else ''}.")
            for argument in node.args:
                _require(walk(argument), "number", f"{name}()")
            return "number"
        if isinstance(node, ast.Name):
            if node.id not in allowed:
                raise FormulaError(f"'{node.id}' is not data this calculation can use. Pick one from the list.")
            names.add(node.id)
            return "number"
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, (ast.USub, ast.UAdd)):
                _require(walk(node.operand), "number", "a minus sign")
                return "number"
            if isinstance(node.op, ast.Not):
                _require(walk(node.operand), "condition", "'not'")
                return "condition"
            raise FormulaError("That operator is not allowed.")
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                raise FormulaError("Only + - * / are allowed. Powers, remainders and whole-number division are not.")
            _require(walk(node.left), "number", "arithmetic")
            _require(walk(node.right), "number", "arithmetic")
            return "number"
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                raise FormulaError("Use one comparison at a time. Join two with 'and' / 'or'.")
            if not isinstance(node.ops[0], (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                raise FormulaError("Compare with <, <=, > or >= only.")
            _require(walk(node.left), "number", "a comparison")
            _require(walk(node.comparators[0]), "number", "a comparison")
            return "condition"
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                _require(walk(value), "condition", "'and' / 'or'")
            return "condition"
        raise FormulaError(
            "Only numbers, the listed values, + - * /, brackets and comparisons (< <= > >=) can be used."
        )

    produced = walk(tree)
    if produced != expect:
        raise FormulaError(
            "This formula must give a number, for example 'a * b'."
            if expect == "number"
            else "This formula must be a comparison that is true or false, for example 'a < b'."
        )
    return Parsed(source=text, tree=tree, kind=expect, names=frozenset(names))


def _require(found: Kind, wanted: Kind, where: str) -> None:
    if found != wanted:
        raise FormulaError(
            f"{where.capitalize()} needs a number on each side, not a true/false comparison."
            if wanted == "number"
            else f"{where.capitalize()} joins comparisons, not plain numbers."
        )


# --- SQL ---------------------------------------------------------------------------------------------


def compile_sql(parsed: Parsed, columns: Mapping[str, Any]) -> Any:
    """The formula as a SQLAlchemy expression over ``columns`` (name -> column or expression)."""

    def build(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return Decimal(str(node.value))
        if isinstance(node, ast.Call):
            args = [literal(a) if isinstance(a, Decimal) else a for a in (build(x) for x in node.args)]
            if node.func.id == "abs":  # type: ignore[union-attr]
                return func.abs(args[0])
            left, right = args
            keep_left = left >= right if node.func.id == "max" else left <= right  # type: ignore[union-attr]
            # Either side unknown -> unknown, never "the other one": a missing figure is not zero.
            return case((or_(left.is_(None), right.is_(None)), None), (keep_left, left), else_=right)
        if isinstance(node, ast.Name):
            return columns[node.id]
        if isinstance(node, ast.UnaryOp):
            operand = build(node.operand)
            if isinstance(node.op, ast.Not):
                return not_(operand)
            return -operand if isinstance(node.op, ast.USub) else operand
        if isinstance(node, ast.BinOp):
            left, right = build(node.left), build(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            # Both sides widened: SQL Server keeps few decimals when it divides one fixed-scale number by
            # another, which would round a small daily average to zero. The divisor is cast here, with its
            # precision, because SQLAlchemy would otherwise add a bare ``CAST(... AS NUMERIC)`` -- which SQL
            # Server reads as NUMERIC(18,0) and so rounds a fractional divisor (a reorder point of 0.5) to 1.
            # SQLite (the unit tests) has no fixed-point type: a NUMERIC cast keeps integers, and integer
            # division would truncate -- so there, and only there, the cast is a float.
            wide = Numeric(38, 12).with_variant(Float(), "sqlite")
            # ``.op("/")`` rather than ``/``: SQLAlchemy's own true-division handling is what adds the bare cast.
            # Precedence 8 is that of ``*`` and ``/``; the default (0) would drop the brackets in ``a / b * c``.
            return cast(left, wide).op("/", precedence=8, return_type=wide)(func.nullif(cast(right, wide), 0))
        if isinstance(node, ast.Compare):
            left, right = build(node.left), build(node.comparators[0])
            op = node.ops[0]
            if isinstance(op, ast.Lt):
                return left < right
            if isinstance(op, ast.LtE):
                return left <= right
            if isinstance(op, ast.Gt):
                return left > right
            return left >= right
        if isinstance(node, ast.BoolOp):
            parts = [build(value) for value in node.values]
            return and_(*parts) if isinstance(node.op, ast.And) else or_(*parts)
        raise FormulaError("The formula is not valid.")  # unreachable for a Parsed tree

    return build(parsed.tree)


# --- Python (worked example, preview) ----------------------------------------------------------------


def evaluate(parsed: Parsed, values: Mapping[str, Decimal | None]) -> Decimal | bool | None:
    """The formula over concrete values. ``None`` means "cannot be worked out" (a missing input or a
    division by zero), mirroring SQL ``NULL``."""

    def run(node: ast.AST) -> Decimal | bool | None:
        if isinstance(node, ast.Constant):
            return Decimal(str(node.value))
        if isinstance(node, ast.Call):
            arguments = [run(x) for x in node.args]
            if any(a is None for a in arguments):
                return None
            if node.func.id == "abs":  # type: ignore[union-attr]
                return abs(arguments[0])  # type: ignore[arg-type]
            return max(arguments) if node.func.id == "max" else min(arguments)  # type: ignore[union-attr,type-var]
        if isinstance(node, ast.Name):
            return values.get(node.id)
        if isinstance(node, ast.UnaryOp):
            operand = run(node.operand)
            if operand is None:
                return None
            if isinstance(node.op, ast.Not):
                return not operand
            return -operand if isinstance(node.op, ast.USub) else operand
        if isinstance(node, ast.BinOp):
            left, right = run(node.left), run(node.right)
            if left is None or right is None:
                return None
            try:
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                return None if right == 0 else left / right
            except InvalidOperation:
                return None
        if isinstance(node, ast.Compare):
            left, right = run(node.left), run(node.comparators[0])
            if left is None or right is None:
                return None
            op = node.ops[0]
            if isinstance(op, ast.Lt):
                return left < right
            if isinstance(op, ast.LtE):
                return left <= right
            if isinstance(op, ast.Gt):
                return left > right
            return left >= right
        if isinstance(node, ast.BoolOp):
            results = [run(value) for value in node.values]
            if isinstance(node.op, ast.And):
                if any(r is False for r in results):
                    return False
                return None if any(r is None for r in results) else True
            if any(r is True for r in results):
                return True
            return None if any(r is None for r in results) else False
        return None

    return run(parsed.tree)


# --- Text --------------------------------------------------------------------------------------------

_PRECEDENCE = {ast.Add: 1, ast.Sub: 1, ast.Mult: 2, ast.Div: 2}
_TEXT_OP = {ast.Add: "+", ast.Sub: "-", ast.Mult: "×", ast.Div: "÷"}
_COMPARE_TEXT = {ast.Lt: "<", ast.LtE: "≤", ast.Gt: ">", ast.GtE: "≥"}


def to_text(parsed: Parsed, labels: Mapping[str, str] | None = None) -> str:
    """The formula as the information card prints it: ``×`` and ``÷``, and ``labels`` (name ->
    plain-English words) in place of the raw names when given."""
    labels = labels or {}

    def show(node: ast.AST, parent: int = 0, right_side: bool = False) -> str:
        if isinstance(node, ast.Constant):
            value = Decimal(str(node.value))
            return f"{value:,.0f}" if value == value.to_integral_value() and abs(value) >= 10000 else format(
                value.normalize(), "f"
            )
        if isinstance(node, ast.Name):
            return labels.get(node.id, node.id)
        if isinstance(node, ast.Call):
            return f"{node.func.id}({', '.join(show(a) for a in node.args)})"  # type: ignore[union-attr]
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return f"not ({show(node.operand)})"
            sign = "-" if isinstance(node.op, ast.USub) else ""
            return f"{sign}{show(node.operand, 3)}"
        if isinstance(node, ast.BinOp):
            level = _PRECEDENCE[type(node.op)]
            text = (
                f"{show(node.left, level)} {_TEXT_OP[type(node.op)]} "
                f"{show(node.right, level, right_side=True)}"
            )
            needs_brackets = level < parent or (right_side and level == parent)
            return f"({text})" if needs_brackets else text
        if isinstance(node, ast.Compare):
            return f"{show(node.left)} {_COMPARE_TEXT[type(node.ops[0])]} {show(node.comparators[0])}"
        if isinstance(node, ast.BoolOp):
            joiner = " and " if isinstance(node.op, ast.And) else " or "
            return joiner.join(show(value) for value in node.values)
        return ""

    return show(parsed.tree)
