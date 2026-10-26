"""What on the Overview can be edited, and what it is today.

The defaults here are the rules the Overview used before any of this was editable, written in the
formula language of :mod:`app.initiatives.i7.kpi_formulas.expression`. With no saved version the Overview
uses exactly these, so nothing changes until someone applies an edit.

What is **not** editable, deliberately:

* the data-quality guard of High Stockout Risk -- a material-plant is judged only when its usage, stock
  and lead time are all present and usable (a lead time of 0 is SAP's "not maintained", a negative stock
  or a blank storage location is not a stock figure). A formula decides who is *at risk* among the
  judged ones; it cannot turn a missing figure into "safe";
* the population (latest recommendation per material-plant, smooth demand left out) and the SAP
  adoption figures, which count a ledger and have no formula.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FieldKind = Literal["number", "condition", "setting"]


@dataclass(frozen=True)
class FormulaField:
    key: str
    kpi: str
    label: str
    kind: FieldKind
    default: str | int
    help: str
    allowed: tuple[str, ...] = ()
    """Names the formula may use (formula fields only)."""
    minimum: int | None = None
    maximum: int | None = None
    needs_rebuild: bool = False
    """Changing it means the stockout usage table must be rebuilt before the Overview is right."""


#: Plain-English names for every input, used on the page and on the information cards.
VARIABLES: dict[str, str] = {
    "usage_in_window": "units used in the window",
    "window_days": "window days",
    "avg_daily_use": "average daily use",
    "stock_needed": "stock needed",
    "lead_time_days": "lead time (days)",
    "stock_on_hand": "stock on hand",
    "current_max_stock": "current max stock",
    "recommended_max_stock": "recommended max stock",
    "current_rop": "current reorder point",
    "recommended_rop": "recommended reorder point",
    "current_safety_stock": "current safety stock",
    "recommended_safety_stock": "recommended safety stock",
    "unit_price": "unit price",
}

_STOCK_INPUTS = ("usage_in_window", "window_days", "lead_time_days", "stock_on_hand")
_LEVEL_INPUTS = (
    "current_max_stock",
    "recommended_max_stock",
    "current_rop",
    "recommended_rop",
    "current_safety_stock",
    "recommended_safety_stock",
    "unit_price",
)

STOCKOUT = "stockout"
EXCESS = "excess"
WORKING_CAPITAL = "working_capital"

KPI_TITLES = {
    STOCKOUT: "High Stockout Risk",
    EXCESS: "Excess Inventory Candidates",
    WORKING_CAPITAL: "Working Capital Impact",
}

FIELDS: tuple[FormulaField, ...] = (
    FormulaField(
        key="window_days",
        kpi=STOCKOUT,
        label="Usage window (days)",
        kind="setting",
        default=90,
        minimum=7,
        maximum=730,
        needs_rebuild=True,
        help="How many days of issues the usage is taken over. Changing it rebuilds the usage data.",
    ),
    FormulaField(
        key="lookback_days",
        kpi=STOCKOUT,
        label="Only judge materials issued within (days)",
        kind="setting",
        default=730,
        minimum=30,
        maximum=3650,
        help="A material whose last issue is older than this is Non-moving and left out (730 = Fast + Slow).",
    ),
    FormulaField(
        key="avg_daily_use",
        kpi=STOCKOUT,
        label="Average daily use",
        kind="number",
        default="usage_in_window / window_days",
        allowed=("usage_in_window", "window_days"),
        help="Units used per day.",
    ),
    FormulaField(
        key="stock_needed",
        kpi=STOCKOUT,
        label="Stock needed",
        kind="number",
        default="avg_daily_use * lead_time_days",
        allowed=("avg_daily_use", "usage_in_window", "window_days", "lead_time_days"),
        help="The stock required to last until a new order arrives.",
    ),
    FormulaField(
        key="at_risk",
        kpi=STOCKOUT,
        label="At risk when",
        kind="condition",
        default="stock_on_hand < stock_needed",
        allowed=("stock_on_hand", "stock_needed", "avg_daily_use", *_STOCK_INPUTS),
        help="A comparison that is true for a material that is at risk.",
    ),
    FormulaField(
        key="excess_condition",
        kpi=EXCESS,
        label="Counts as excess when",
        kind="condition",
        default="current_max_stock > recommended_max_stock",
        allowed=_LEVEL_INPUTS,
        help="A comparison that is true for an excess-inventory candidate.",
    ),
    FormulaField(
        key="excess_value",
        kpi=EXCESS,
        label="Excess value",
        kind="number",
        default="unit_price * (current_max_stock - recommended_max_stock)",
        allowed=_LEVEL_INPUTS,
        help="The money tied up in one excess candidate, added up for the card.",
    ),
    FormulaField(
        key="working_capital_value",
        kpi=WORKING_CAPITAL,
        label="Working capital impact",
        kind="number",
        default="unit_price * (current_rop - recommended_rop)",
        allowed=_LEVEL_INPUTS,
        help="The working-capital effect of one recommendation, added up for the card. Positive frees cash.",
    ),
)

FIELDS_BY_KEY: dict[str, FormulaField] = {f.key: f for f in FIELDS}

DEFAULTS: dict[str, str | int] = {f.key: f.default for f in FIELDS}
