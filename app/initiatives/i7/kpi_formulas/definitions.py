"""What on the Overview can be edited, and what it is today.

The defaults here are the rules the Overview used before any of this was editable, written in the
formula language of :mod:`app.initiatives.i7.kpi_formulas.expression`. With no saved version the Overview
uses exactly these, so nothing changes until someone applies an edit.

A formula may use **any fact in the data catalogue of its row type** (:mod:`.data_catalog`), plus the
helper values built by the formulas before it. Nothing about which facts a formula uses is fixed: the
default at-risk rule happens to use stock on hand, usage and lead time; an invented one may use anything
listed.

What is **not** editable, deliberately:

* the data-quality guard of High Stockout Risk -- a material-plant is judged only when the facts its
  formula uses are present and usable (a lead time of 0 is SAP's "not maintained", a negative stock or a
  blank storage location is not a stock figure). A formula decides who is *at risk* among the judged
  ones; it cannot turn a missing figure into "safe";
* the population (latest recommendation per material-plant, smooth demand left out).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.initiatives.i7.kpi_formulas import data_catalog
from app.initiatives.i7.kpi_formulas.data_catalog import ADOPTION as ADOPTION_DATA
from app.initiatives.i7.kpi_formulas.data_catalog import RECOMMENDATION, STOCKOUT as STOCKOUT_DATA

FieldKind = Literal["number", "condition", "setting"]


@dataclass(frozen=True)
class FormulaField:
    key: str
    kpi: str
    label: str
    kind: FieldKind
    default: str | int
    help: str
    data_set: str = ""
    """Which data catalogue the formula draws on (``recommendation``, ``stockout`` or ``adoption``)."""
    helpers: tuple[str, ...] = ()
    """Values built by earlier formulas of the same card that this one may also use."""
    minimum: int | None = None
    maximum: int | None = None
    needs_rebuild: bool = False
    """Changing it means the stockout usage table must be rebuilt before the Overview is right."""
    example: str = ""
    """One sentence showing what a formula here could look like."""


def allowed_names(field: FormulaField) -> frozenset[str]:
    """Every name this formula may use: its data set's facts and its helpers."""
    return data_catalog.names_of(field.data_set) | frozenset(field.helpers) if field.data_set else frozenset()


#: Plain-English names for every fact and helper, used on the page and on the information cards.
VARIABLES: dict[str, str] = data_catalog.labels()

STOCKOUT = "stockout"
EXCESS = "excess"
WORKING_CAPITAL = "working_capital"
ADOPTION = "adoption"

KPI_TITLES = {
    STOCKOUT: "High Stockout Risk",
    EXCESS: "Excess Inventory Candidates",
    WORKING_CAPITAL: "Working Capital Impact",
    ADOPTION: "SAP Adoption",
}

#: What each calculation drives on the Overview: the card first, then the charts that repeat its rule.
KPI_CONTROLS: dict[str, tuple[str, ...]] = {
    STOCKOUT: ("High Stockout Risk card", "Stockout Risk Distribution chart", "Stockout risk by plant chart"),
    EXCESS: ("Excess Inventory Candidates card", "Excess inventory by plant chart"),
    WORKING_CAPITAL: ("Working Capital Impact card",),
    ADOPTION: ("Adopted recommendation card", "SAP adoption chart"),
}

KPI_DATA_SETS: dict[str, str] = {
    STOCKOUT: STOCKOUT_DATA,
    EXCESS: RECOMMENDATION,
    WORKING_CAPITAL: RECOMMENDATION,
    ADOPTION: ADOPTION_DATA,
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
        help="How many days of issues 'units used in the window' covers. Changing it rebuilds the usage data.",
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
        data_set=STOCKOUT_DATA,
        help="Units used per day. A helper: the next formulas can use it by name.",
        example="usage_in_window / window_days",
    ),
    FormulaField(
        key="stock_needed",
        kpi=STOCKOUT,
        label="Stock needed",
        kind="number",
        default="avg_daily_use * lead_time_days",
        data_set=STOCKOUT_DATA,
        helpers=("avg_daily_use",),
        help="The stock required to last until a new order arrives. A helper: the next formula can use it.",
        example="avg_daily_use * (lead_time_days + 7)   - add a week of cover",
    ),
    FormulaField(
        key="at_risk",
        kpi=STOCKOUT,
        label="At risk when",
        kind="condition",
        default="stock_on_hand < stock_needed",
        data_set=STOCKOUT_DATA,
        helpers=("avg_daily_use", "stock_needed"),
        help="A comparison that is true for a material that is at risk. Use any data below.",
        example="stock_on_hand < stock_needed * 1.2 and lead_time_days > 10",
    ),
    FormulaField(
        key="excess_condition",
        kpi=EXCESS,
        label="Counts as excess when",
        kind="condition",
        default="current_max_stock > recommended_max_stock",
        data_set=RECOMMENDATION,
        help="A comparison that is true for an excess-inventory candidate. Use any data below.",
        example="current_max_stock > recommended_max_stock * 1.1   - at least 10% over",
    ),
    FormulaField(
        key="excess_value",
        kpi=EXCESS,
        label="Excess value",
        kind="number",
        default="unit_price * (current_max_stock - recommended_max_stock)",
        data_set=RECOMMENDATION,
        help="The money tied up in one excess candidate, added up for the card.",
        example="unit_price * (stock_on_hand - recommended_max_stock)",
    ),
    FormulaField(
        key="working_capital_value",
        kpi=WORKING_CAPITAL,
        label="Working capital impact",
        kind="number",
        default="unit_price * (current_rop - recommended_rop)",
        data_set=RECOMMENDATION,
        help="The working-capital effect of one recommendation, added up for the card. Positive frees cash.",
        example="unit_price * (current_rop - recommended_rop + current_safety_stock - recommended_safety_stock)",
    ),
    FormulaField(
        key="adoption_rate",
        kpi=ADOPTION,
        label="Adoption rate",
        kind="number",
        default="(adopted + partially_adopted) / (adopted + partially_adopted + not_adopted)",
        data_set=ADOPTION_DATA,
        help=(
            "A share between 0 and 1 (0.75 shows as 75%). By default pending ones are left out, so waiting for "
            "SAP does not pull the rate down."
        ),
        example="adopted / tracked   - only fully adopted ones, out of everything tracked",
    ),
    FormulaField(
        key="adopted_card",
        kpi=ADOPTION,
        label="Counts as adopted (the card)",
        kind="number",
        default="adopted",
        data_set=ADOPTION_DATA,
        help="How many recommendations the Adopted card counts.",
        example="adopted + partially_adopted",
    ),
)

FIELDS_BY_KEY: dict[str, FormulaField] = {f.key: f for f in FIELDS}

DEFAULTS: dict[str, str | int] = {f.key: f.default for f in FIELDS}
