"""Statistics-independent aging, derived from goods-movement history only.

S031 (``MonthlyMovementStatisticSet``) has no usable rows and S032 coverage
is limited in this tenant, so aging is computed directly from
``GoodsMovementItemSet`` (MSEG/MKPF) via the movement-normalisation layer in
``movements.py`` -- never from either LIS statistic set.
"""

import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from app.initiatives.i13.config import AgingThresholds
from app.initiatives.i13.models import AgingBand, AgingResult
from app.initiatives.i13.movements import ISSUE_TYPES, filter_by_window, latest_movement_date, net_quantity

Row = dict[str, Any]


def months_before(reference: date, months: int) -> date:
    """``reference`` shifted back by ``months`` whole calendar months."""
    month_index = reference.month - 1 - months
    year = reference.year + month_index // 12
    month = month_index % 12 + 1
    day = min(reference.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def classify_aging_band(days_since_last_movement: int | None, thresholds: AgingThresholds) -> AgingBand:
    if days_since_last_movement is None:
        # Never moved: at least as aged as "more than slow_max_days".
        return AgingBand.NON_MOVING
    if days_since_last_movement <= thresholds.fast_max_days:
        return AgingBand.FAST
    if days_since_last_movement <= thresholds.slow_max_days:
        return AgingBand.SLOW
    return AgingBand.NON_MOVING


def group_by_material_plant(rows: list[Row]) -> dict[tuple[str, str], list[Row]]:
    grouped: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in rows:
        material = row.get("Matnr")
        plant = row.get("Werks")
        if material is None or plant is None:
            continue
        grouped[(material, plant)].append(row)
    return grouped


def compute_aging(
    material: str,
    plant: str,
    movements: list[Row],
    *,
    current_stock: Decimal | None,
    thresholds: AgingThresholds,
    window_months: int,
    as_of: date,
) -> AgingResult:
    last_movement = latest_movement_date(movements)
    days_since = (as_of - last_movement).days if last_movement else None

    window_start = months_before(as_of, window_months)
    windowed = filter_by_window(movements, start=window_start, end=as_of)
    issue_rows = [row for row in windowed if row.get("Bwart") in ISSUE_TYPES or row.get("Bwart") in {"202", "262"}]
    consumption_count = sum(1 for row in windowed if row.get("Bwart") in ISSUE_TYPES) - sum(
        1 for row in windowed if row.get("Bwart") in {"202", "262"}
    )
    consumption_count = max(consumption_count, 0)
    consumed_qty = net_quantity(issue_rows, ISSUE_TYPES)

    aging_band = classify_aging_band(days_since, thresholds)

    inventory_turns: Decimal | None = None
    inventory_turns_reason: str | None = None
    if current_stock is None:
        inventory_turns_reason = "INSUFFICIENT_HISTORY"
    elif current_stock == 0:
        inventory_turns_reason = "INSUFFICIENT_HISTORY"
    else:
        inventory_turns = consumed_qty / current_stock

    return AgingResult(
        material=material,
        plant=plant,
        last_movement_date=last_movement,
        days_since_last_movement=days_since,
        consumption_count_12m=consumption_count,
        consumed_qty_12m=consumed_qty,
        aging_band=aging_band,
        current_stock=current_stock,
        inventory_turns=inventory_turns,
        inventory_turns_reason=inventory_turns_reason,
    )
