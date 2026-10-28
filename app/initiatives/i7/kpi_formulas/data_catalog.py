"""Everything a user may use in a formula: the data the platform holds about each material-plant.

A formula is written over *names*. Each name here is one numeric fact -- a stored column, or a figure
summed from one -- with a plain label and a sentence saying what it is. The page lists them, grouped, so
a user can see what is available instead of guessing.

**Why a list and not "any column".** A name is only usable if it appears here, and each entry points at
one specific, reviewed column. A user can combine them however they like, but cannot reach a column nobody
has looked at (a password hash, a free-text field, another module's table) by typing its name. Adding a
fact to the catalogue is a one-line change here.

Three kinds of row a formula can be about, because the facts that exist differ:

* ``recommendation`` -- one recommendation (its current and recommended levels, price, lead time) plus the
  feature-store facts about the same material-plant. Used by Excess Inventory and Working Capital.
* ``stockout`` -- one Fast or Slow moving material-plant: its usage, stock on hand, lead time and the same
  feature-store facts. Used by High Stockout Risk.
* ``adoption`` -- the portfolio's adoption counts. Used by SAP Adoption.

A fact that is missing for a row (NULL) makes any formula using it "cannot be worked out" for that row:
the row is left out, never read as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from sqlalchemy import select

RECOMMENDATION = "recommendation"
STOCKOUT = "stockout"
ADOPTION = "adoption"

Source = Literal["recommendation", "feature", "usage", "stock", "adoption"]


@dataclass(frozen=True)
class DataItem:
    name: str
    label: str
    """Plain words, written to read inside a sentence: "stock on hand", "current reorder point"."""
    description: str
    group: str
    unit: str
    """``quantity``, ``money``, ``days``, ``count``, ``ratio`` or ``number`` -- shown beside the name."""
    source: Source
    column: str = ""
    """The attribute on the source model; the name itself when blank."""

    @property
    def attribute(self) -> str:
        return self.column or self.name


def _rec(name: str, label: str, description: str, group: str, unit: str, column: str = "") -> DataItem:
    return DataItem(name, label, description, group, unit, "recommendation", column)


def _feat(name: str, label: str, description: str, group: str, unit: str, column: str = "") -> DataItem:
    return DataItem(name, label, description, group, unit, "feature", column)


# --- Facts from the feature store, the same for a recommendation and a stockout row ---------------------

_FEATURE_FACTS: tuple[DataItem, ...] = (
    _feat("unit_price", "unit price", "The replacement price of one unit at this plant.", "Price", "money"),
    _feat(
        "average_demand",
        "average demand",
        "Average demand per month over the whole history, months with no demand counted as zero.",
        "Demand history",
        "quantity",
        "mean_demand_all_periods",
    ),
    _feat(
        "average_nonzero_demand",
        "average demand when it occurs",
        "Average demand per month, counting only the months that had demand.",
        "Demand history",
        "quantity",
        "mean_non_zero_demand",
    ),
    _feat(
        "demand_std_dev",
        "demand spread",
        "How much monthly demand varies (standard deviation, zeros included).",
        "Demand history",
        "quantity",
        "std_dev_demand_all_periods",
    ),
    _feat("total_demand", "total demand", "All demand recorded over the history.", "Demand history", "quantity"),
    _feat(
        "adi",
        "months between demands",
        "Average number of months between one demand and the next (ADI). Higher means more intermittent.",
        "Demand history",
        "number",
    ),
    _feat(
        "cv_squared",
        "demand variability",
        "How uneven the size of demands is (CV squared). Higher means lumpier.",
        "Demand history",
        "number",
    ),
    _feat("history_months", "months of history", "How many months of history exist.", "Demand history", "count"),
    _feat(
        "consumption_count_12m",
        "issues in the last 12 months",
        "How many goods issues (less reversals) in the last 12 months.",
        "Demand history",
        "count",
    ),
    _feat(
        "purchase_order_count",
        "purchase orders",
        "How many purchase-order lines have both dates, used for lead times.",
        "Demand history",
        "count",
    ),
    _feat(
        "sap_safety_stock",
        "safety stock in SAP",
        "The safety stock held in SAP today.",
        "Planning values in SAP",
        "quantity",
        "current_safety_stock",
    ),
    _feat(
        "sap_reorder_point",
        "reorder point in SAP",
        "The reorder point held in SAP today.",
        "Planning values in SAP",
        "quantity",
        "current_reorder_point",
    ),
    _feat(
        "sap_max_stock",
        "maximum stock in SAP",
        "The maximum stock held in SAP today.",
        "Planning values in SAP",
        "quantity",
        "current_maximum_stock",
    ),
    _feat(
        "planned_delivery_days",
        "planned delivery time (SAP)",
        "SAP's planned delivery time for this material at this plant, in days.",
        "Planning values in SAP",
        "days",
        "planned_delivery_time_days",
    ),
    _feat(
        "quality_inspection_stock",
        "stock in quality inspection",
        "Stock held back for quality inspection.",
        "Other stock",
        "quantity",
    ),
    _feat("blocked_stock", "blocked stock", "Stock that is blocked.", "Other stock", "quantity"),
    _feat("stock_in_transfer", "stock in transfer", "Stock moving between locations.", "Other stock", "quantity"),
    _feat("restricted_use_stock", "restricted-use stock", "Stock with restricted use.", "Other stock", "quantity"),
    _feat("returns_stock", "returns stock", "Stock returned and not yet placed.", "Other stock", "quantity"),
)

RECOMMENDATION_ITEMS: tuple[DataItem, ...] = (
    _rec(
        "current_safety_stock",
        "current safety stock",
        "The safety stock in SAP when the recommendation was made.",
        "Current in SAP",
        "quantity",
    ),
    _rec(
        "current_rop",
        "current reorder point",
        "The reorder point in SAP when the recommendation was made.",
        "Current in SAP",
        "quantity",
    ),
    _rec(
        "current_max_stock",
        "current max stock",
        "The maximum stock in SAP when the recommendation was made.",
        "Current in SAP",
        "quantity",
    ),
    _rec(
        "recommended_safety_stock",
        "recommended safety stock",
        "The safety stock Spares AI recommends.",
        "Recommended by Spares AI",
        "quantity",
    ),
    _rec(
        "recommended_rop",
        "recommended reorder point",
        "The reorder point Spares AI recommends.",
        "Recommended by Spares AI",
        "quantity",
    ),
    _rec(
        "recommended_max_stock",
        "recommended max stock",
        "The maximum stock Spares AI recommends.",
        "Recommended by Spares AI",
        "quantity",
    ),
    _rec(
        "forecast_rate",
        "forecast demand",
        "The demand rate Spares AI forecast for this material-plant.",
        "Recommended by Spares AI",
        "quantity",
    ),
    _rec(
        "service_level",
        "service level",
        "The service level the safety stock was sized for.",
        "Recommended by Spares AI",
        "ratio",
    ),
    _rec("z_factor", "z factor", "The factor that service level translates to.", "Recommended by Spares AI", "number"),
    _rec(
        "lead_time_days",
        "lead time (days)",
        "How long a new order takes to arrive, as used for this recommendation.",
        "Price and lead time",
        "days",
    ),
    _rec(
        "lead_time_variance_days",
        "lead time variation (days)",
        "How much that lead time varies.",
        "Price and lead time",
        "days",
    ),
    _rec("unit_price", "unit price", "The replacement price of one unit at this plant.", "Price and lead time", "money"),
    _rec(
        "oar_neighbour_count",
        "similar materials used",
        "For an OAR material: how many similar materials its size was based on.",
        "OAR evidence",
        "count",
    ),
    _rec(
        "oar_best_similarity",
        "best similarity",
        "For an OAR material: how alike its closest similar material is (0 to 1).",
        "OAR evidence",
        "ratio",
    ),
    DataItem(
        "stock_on_hand",
        "stock on hand",
        "Unrestricted-use stock summed over all storage locations.",
        "Stock position",
        "quantity",
        "feature",
        "current_stock",
    ),
    *(f for f in _FEATURE_FACTS if f.name not in {"unit_price", "consumption_count_12m"}),
    _feat(
        "consumption_count_12m",
        "issues in the last 12 months",
        "How many goods issues (less reversals) in the last 12 months.",
        "Demand history",
        "count",
    ),
)

STOCKOUT_ITEMS: tuple[DataItem, ...] = (
    DataItem(
        "usage_in_window",
        "units used in the window",
        "Net quantity issued in the usage window (issues less reversals, never below zero).",
        "Usage",
        "quantity",
        "usage",
        "consumption_90d",
    ),
    DataItem(
        "window_days", "window days", "How many days the usage window covers.", "Usage", "days", "usage", "window_days"
    ),
    DataItem(
        "stock_on_hand",
        "stock on hand",
        "Unrestricted-use stock summed over all storage locations.",
        "Stock position",
        "quantity",
        "stock",
        "on_hand",
    ),
    DataItem(
        "lead_time_days",
        "lead time (days)",
        "How long a new order takes to arrive (the resolved lead time in the feature store).",
        "Price and lead time",
        "days",
        "feature",
        "lead_time_days",
    ),
    *_FEATURE_FACTS,
)

ADOPTION_ITEMS: tuple[DataItem, ...] = (
    DataItem("adopted", "adopted", "Recommendations SAP has fully taken up.", "Adoption counts", "count", "adoption"),
    DataItem(
        "partially_adopted",
        "partially adopted",
        "Recommendations where only some values changed in SAP.",
        "Adoption counts",
        "count",
        "adoption",
    ),
    DataItem(
        "not_adopted", "not adopted", "Recommendations where SAP changed nothing.", "Adoption counts", "count", "adoption"
    ),
    DataItem(
        "pending",
        "pending",
        "Recommendations SAP has no change evidence for yet.",
        "Adoption counts",
        "count",
        "adoption",
    ),
    DataItem(
        "tracked",
        "all tracked",
        "Every recommendation that has something to adopt.",
        "Adoption counts",
        "count",
        "adoption",
    ),
)

CATALOG: dict[str, tuple[DataItem, ...]] = {
    RECOMMENDATION: RECOMMENDATION_ITEMS,
    STOCKOUT: STOCKOUT_ITEMS,
    ADOPTION: ADOPTION_ITEMS,
}

#: Values a formula can build up as it goes, so a later one can use an earlier one. Not stored data.
HELPER_LABELS: dict[str, str] = {
    "avg_daily_use": "average daily use",
    "stock_needed": "stock needed",
}

HELPER_DESCRIPTIONS: dict[str, str] = {
    "avg_daily_use": "The result of the 'Average daily use' formula above.",
    "stock_needed": "The result of the 'Stock needed' formula above.",
}


def names_of(data_set: str) -> frozenset[str]:
    return frozenset(item.name for item in CATALOG[data_set])


def labels() -> dict[str, str]:
    """name -> plain label, over every data set and helper. A name means the same thing wherever it is used,
    so one map serves them all."""
    found: dict[str, str] = dict(HELPER_LABELS)
    for items in CATALOG.values():
        for item in items:
            found.setdefault(item.name, item.label)
    return found


class LazyColumns(dict):
    """name -> SQL expression, built only when a formula asks for it.

    A formula that uses three names costs three lookups, not the whole catalogue. A feature-store fact is a
    correlated sub-select, so building the unused ones would add a sub-select per row for nothing.
    """

    def __init__(self, resolvers: dict[str, Callable[[], Any]]) -> None:
        super().__init__()
        self._resolvers = resolvers

    def __missing__(self, name: str) -> Any:
        value = self._resolvers[name]()  # KeyError for a name nobody declared
        self[name] = value
        return value


def _feature_fact(attribute: str, material: Any, plant: Any) -> Any:
    """One feature-store column of the same material-plant, as a correlated scalar sub-select. One row per
    material-plant (``uq_i7_material_feature_key``), so it never returns two."""
    from app.models.i7_features import MaterialFeature

    return (
        select(getattr(MaterialFeature, attribute))
        .where(
            MaterialFeature.sap_material_number == material,
            MaterialFeature.sap_plant_code == plant,
        )
        .scalar_subquery()
    )


def recommendation_columns() -> LazyColumns:
    """The recommendation data set over ``Recommendation``: its own columns directly, the feature-store facts
    as sub-selects correlated on the recommendation's material and plant."""
    from app.models.i7_recommendation import Recommendation

    def resolver(item: DataItem) -> Callable[[], Any]:
        if item.source == "recommendation":
            return lambda: getattr(Recommendation, item.attribute)
        return lambda: _feature_fact(item.attribute, Recommendation.sap_material_number, Recommendation.sap_plant_code)

    return LazyColumns({item.name: resolver(item) for item in RECOMMENDATION_ITEMS})


def stockout_columns(*, usage: Any, feature: Any, on_hand: Any) -> LazyColumns:
    """The stockout data set over the usage table, with the feature table and the summed stock already joined
    by the caller (``on_hand`` is the summed-stock column)."""

    def resolver(item: DataItem) -> Callable[[], Any]:
        if item.source == "usage":
            return lambda: getattr(usage, item.attribute)
        if item.source == "stock":
            return lambda: on_hand
        return lambda: getattr(feature, item.attribute)

    return LazyColumns({item.name: resolver(item) for item in STOCKOUT_ITEMS})
