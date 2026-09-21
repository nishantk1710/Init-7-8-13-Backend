"""Aging-band classification and grouping shared by every I13 metric that
needs them -- ``movement_metrics.py`` (W3.5), ``watch.py``, and
``reclassification.py`` all import from here rather than each defining their
own copy.

Statistics-independent: everything downstream is derived from goods-movement
history only (S031/S032 are never read for this). This module used to also
hold a full CSV-backed ``compute_aging``/``AgingResult`` pipeline; that was
removed when I13 fully migrated onto Postgres -- see ``movement_metrics.py``
for the one remaining (Postgres-backed) aging computation.
"""

import calendar
from collections import defaultdict
from datetime import date
from typing import Any

from app.initiatives.i13.config import AgingThresholds
from app.initiatives.i13.models import AgingBand

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
