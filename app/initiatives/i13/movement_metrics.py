"""W3.5: statistics-independent aging/movement metrics, from real Postgres
goods-movement history only -- never S031/S032.

This is the Postgres-native sibling of ``aging.py`` (which serves the
CSV-backed gateway used by W6.1/W6.2/WATCH today). It is deliberately a
separate module rather than a change to ``aging.py``: ``aging.py`` is relied
on by ``summary.py``, ``watch.py``, ``reclassification.py`` and
``exceptions.py`` already, classifying on *any* last movement, and changing
that classification basis would silently change all of their behaviour. This
module implements the stricter W3.5 rule instead -- ``aging_band`` is
classified on ``days_since_last_issue`` (goods-issue/consumption only), not
on the date of the last movement of any kind (see ``compute_movement_metrics``
below and the implementation report's "aging basis" note for why the two
existing definitions differ and which one is authoritative is an open
business question, not settled here).

Reuses, rather than reimplements:
  * ``movements.py``'s ``ISSUE_TYPES``/``RECEIPT_TYPES``/``REVERSAL_OF`` --
    the one centralized, already-tested consumption/receipt/reversal rule
    in this repo (see that module's docstring for its own caveats about not
    being a proven-complete enterprise allowlist).
  * ``movements.py``'s reversal-aware ``net_quantity``/``net_event_count``/
    ``filter_by_window``/``latest_movement_date``.
  * ``aging.py``'s ``classify_aging_band`` (a pure function of "how many
    days" against configured thresholds -- it does not care what the days
    represent, so feeding it ``days_since_last_issue`` instead of
    ``days_since_last_movement`` needs no change there) and ``months_before``
    /``group_by_material_plant``.
  * ``config.py``'s ``AgingThresholds`` -- no new configuration for the
    365/730-day bands.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from app.initiatives.i13.aging import classify_aging_band, group_by_material_plant, months_before
from app.initiatives.i13.config import AgingThresholds
from app.initiatives.i13.models import MovementMetrics
from app.initiatives.i13.movements import ISSUE_TYPES, filter_by_window, latest_movement_date, net_event_count, net_quantity
from app.integrations.sap.postgres_movements import PostgresMovementRepository

Row = dict[str, Any]


def compute_movement_metrics(
    material: str,
    plant: str,
    movements: list[Row],
    *,
    current_stock: Decimal | None,
    thresholds: AgingThresholds,
    window_months: int,
    as_of: date,
) -> MovementMetrics:
    """Pure calculation over one material+plant's normalized movement rows.

    ``as_of`` is a required, explicit parameter rather than a call to
    ``date.today()`` inside this function, so a test can pin it and get a
    deterministic result -- see ``tests/i13/test_movement_metrics.py``.
    """
    last_movement_date = latest_movement_date(movements)
    days_since_last_movement = (as_of - last_movement_date).days if last_movement_date else None

    issue_rows = [row for row in movements if row.get("Bwart") in ISSUE_TYPES]
    last_issue_date = latest_movement_date(issue_rows)
    days_since_last_issue = (as_of - last_issue_date).days if last_issue_date else None

    window_start = months_before(as_of, window_months)
    windowed = filter_by_window(movements, start=window_start, end=as_of)
    consumption_count_12m = net_event_count(windowed, ISSUE_TYPES)
    consumption_qty_12m = net_quantity(windowed, ISSUE_TYPES)

    # W3.5 §10: classification is explicitly on days-since-last-ISSUE, not
    # days-since-last-movement -- a pure receipt does not reset the clock.
    # No historical issue at all (last_issue_date is None) must not read as
    # "recent" -- classify_aging_band already treats None as NON_MOVING
    # (see aging.py), which is the honest "no consumption history" outcome,
    # not a silently-assumed FAST.
    aging_band = classify_aging_band(days_since_last_issue, thresholds)

    inventory_turns: Decimal | None = None
    inventory_turns_reason: str | None = None
    if current_stock is None or current_stock == 0:
        # Same reason string aging.py already uses for the equivalent
        # CSV-backed case -- one vocabulary for "denominator unavailable"
        # across both movement-metrics implementations.
        inventory_turns_reason = "INSUFFICIENT_HISTORY"
    else:
        inventory_turns = consumption_qty_12m / current_stock

    return MovementMetrics(
        material=material,
        plant=plant,
        last_movement_date=last_movement_date,
        days_since_last_movement=days_since_last_movement,
        last_issue_date=last_issue_date,
        days_since_last_issue=days_since_last_issue,
        consumption_count_12m=consumption_count_12m,
        consumption_qty_12m=consumption_qty_12m,
        inventory_turns=inventory_turns,
        inventory_turns_reason=inventory_turns_reason,
        aging_band=aging_band,
        calculated_at=datetime.now(timezone.utc),
    )


def compute_all_movement_metrics(
    repository: PostgresMovementRepository,
    *,
    thresholds: AgingThresholds,
    window_months: int,
    as_of: date | None = None,
    material: str | None = None,
    plant: str | None = None,
) -> list[MovementMetrics]:
    """Orchestration: real Postgres movement rows -> grouped by
    (material, plant) -> one ``MovementMetrics`` each.

    ``material``/``plant`` push down to the repository's SQL filter when
    given (the per-item API path); omitted, this computes for every
    (material, plant) combination present in the movement history (the list
    API path) -- the same "load full history, group in Python" shape the
    CSV-backed ``aging.py``/``summary.py`` already use, at a comparable row
    count (~230k movement rows).
    """
    as_of = as_of or date.today()

    movements = repository.get_movement_history(material=material, plant=plant)
    stock_by_key = repository.get_current_stock(material=material, plant=plant)
    grouped = group_by_material_plant(movements)

    results: list[MovementMetrics] = []
    for (row_material, row_plant), rows in sorted(grouped.items()):
        current_stock = stock_by_key.get((row_material, row_plant))
        results.append(
            compute_movement_metrics(
                row_material,
                row_plant,
                rows,
                current_stock=current_stock,
                thresholds=thresholds,
                window_months=window_months,
                as_of=as_of,
            )
        )
    return results
