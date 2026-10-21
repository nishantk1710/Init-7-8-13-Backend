"""The report's monthly trends and quarter-end position, read from the saved KPI snapshots.

**Trends are what was measured at the time.** Each month of the period is looked up in ``i7_kpi_snapshot``; a month with no
snapshot is MISSING and its points are blank -- never rebuilt from today's data and never drawn as zero. A stockout reading
that was too old for its month (``stockout_stale``) is kept in the snapshot but is not a point on the trend: it would plot
a risk that was never measured for that month. Excess and working-capital points do not depend on the usage build and are
shown for every captured month.

**Counts are not summed across months.** A month's stockout count is a position on a date, so the trend shows one value
per month and the quarter-end position is one month's snapshot -- never a total of the three.

**The quarter-end position is the last month's snapshot, and only if it is valid.** Valid means: that month's snapshot
exists, is FINAL (the month had ended when it was captured) and was measured close enough to the month's end to stand for
it. If it is missing or not valid the quarter-end position is NOT_AVAILABLE and says why, and which snapshot is the latest
available; an earlier month is never relabelled as the quarter-end.

**Working capital** is shown three ways, kept apart: the *estimated impact* at each measurement date, the *change in that
estimate* from the previous month (only between two snapshots made with the same calculation version), and *realised
savings* -- which nothing measures, so the report says so instead of leaving the reader to assume the estimate is one.

A database that does not have the snapshot tables yet (the migration is applied on purpose) simply has no snapshots, and
says so: report generation must never fail because of them.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.initiatives.i7.reporting import snapshots
from app.initiatives.i7.reporting.period import month_label, months_of
from app.models.i7_kpi_snapshot import STATUS_FINAL, KpiSnapshot
from app.schemas.i7.reports import (
    AvailabilityStatus,
    PlantPosition,
    PositionFigures,
    QuarterEnd,
    QuarterTrends,
    StockLevelMean,
    TrendMonth,
)

REALISED_SAVINGS_NOTE = (
    "Realised savings are not measured: nothing records stock that was actually released or a purchase that was actually "
    "avoided. The working capital figures are estimates of what the recommendations would change if adopted."
)

LEVEL_LABELS = {"safety_stock": "Safety stock", "reorder_point": "Reorder point", "max_stock": "Maximum stock"}


def _load(session: Session, months: list[date]) -> tuple[dict[date, KpiSnapshot], str | None]:
    """The period's snapshots by month, or none and a note if the tables are not there yet."""
    try:
        return snapshots.load_for_months(session, months), None
    except SQLAlchemyError:
        session.rollback()
        return {}, "The snapshot tables do not exist yet (the migration has not been applied), so no snapshot can be read."


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _trend_month(month: date, row: KpiSnapshot | None) -> TrendMonth:
    if row is None:
        return TrendMonth(
            month=month, label=month_label(month), state="MISSING", measured_on=None, month_end_valid=False,
            stockout_valid=False, note="No snapshot was captured for this month, and a past month cannot be rebuilt.",
        )
    trusted = not row.stockout_stale
    notes = []
    if row.status != STATUS_FINAL:
        notes.append("Month in progress: a rolling reading, not a month-end figure.")
    elif not row.month_end_valid:
        notes.append("Captured too long after the month ended to stand for its end.")
    if row.stockout_stale:
        usage = f"{row.stockout_usage_as_of:%d %b %Y}" if row.stockout_usage_as_of else "never"
        notes.append(f"Stockout risk not shown: its usage was last built {usage}, too far from this month.")
    return TrendMonth(
        month=month, label=month_label(month), state=row.status, measured_on=row.measured_on,
        month_end_valid=bool(row.month_end_valid), stockout_valid=trusted, population=row.population,
        stockout_at_risk=row.stockout_at_risk if trusted else None,
        stockout_evaluated=row.stockout_evaluated if trusted else None,
        stockout_insufficient=row.stockout_insufficient if trusted else None,
        excess_count=row.excess_count, excess_opportunity=_dec(row.excess_opportunity),
        net_rop_value_impact=_dec(row.net_rop_value_impact), currency=row.currency, revision=row.revision,
        calc_version=row.calc_version, note=" ".join(notes) or None,
    )


def _with_changes(points: list[TrendMonth], rows: dict[date, KpiSnapshot]) -> list[TrendMonth]:
    """Fill in the change in the working-capital estimate from the previous month, where both months have one and were
    made with the same calculation -- otherwise the change is not available, not zero."""
    out: list[TrendMonth] = []
    previous: TrendMonth | None = None
    for point in points:
        change = None
        if (
            previous is not None
            and point.net_rop_value_impact is not None
            and previous.net_rop_value_impact is not None
            and point.calc_version == previous.calc_version
        ):
            change = point.net_rop_value_impact - previous.net_rop_value_impact
        out.append(point.model_copy(update={"net_rop_change_from_previous": change}))
        previous = point
    return out


def _quarter_end(months: list[date], rows: dict[date, KpiSnapshot], note: str | None) -> tuple[QuarterEnd, PositionFigures | None, list[PlantPosition]]:
    last = months[-1]
    label = month_label(last)
    row = rows.get(last)

    def unavailable(reason: str) -> QuarterEnd:
        latest = max((m for m in rows if rows[m] is not None), default=None)
        return QuarterEnd(
            status=AvailabilityStatus.NOT_AVAILABLE, month=last, label=label, snapshot_state="MISSING" if row is None else row.status,
            measured_on=row.measured_on if row else None, reason=reason,
            latest_available_month=latest, latest_available_label=month_label(latest) if latest else None,
            latest_available_measured_on=rows[latest].measured_on if latest else None,
        )

    if note:
        return unavailable(note), None, []
    if row is None:
        return unavailable(f"No snapshot was captured for {label}, and a past month cannot be rebuilt, so there is no quarter-end position."), None, []
    if row.status != STATUS_FINAL:
        return unavailable(f"{label} has not ended: its snapshot is a rolling reading measured on {row.measured_on:%d %b %Y}, not a quarter-end position."), None, []
    if not row.month_end_valid:
        end = snapshots.month_end_of(last)
        days = (row.measured_on - end).days
        return unavailable(
            f"The {label} snapshot was measured on {row.measured_on:%d %b %Y}, {days} days after the month ended: too late to stand for the "
            "quarter-end position."
        ), None, []

    payload = json.loads(row.payload_json)
    plants = [PlantPosition(plant=p["plant"], figures=PositionFigures.model_validate(p["figures"])) for p in payload["plant_positions"]]
    position = plants[0].figures
    levels = [
        StockLevelMean(
            key=lvl["key"], label=LEVEL_LABELS.get(lvl["key"], lvl["key"]), aggregation=lvl["aggregation"], records=lvl["records"],
            current=_dec(lvl["current"]), recommended=_dec(lvl["recommended"]),
        )
        for lvl in payload["stock_levels"]
    ]
    return (
        QuarterEnd(
            status=AvailabilityStatus.AVAILABLE, month=last, label=label, snapshot_state=row.status, measured_on=row.measured_on,
            reason=None, population=row.population, stock_levels=levels,
        ),
        position,
        plants,
    )


def build_trends(
    session: Session, period_start: date, period_end: date
) -> tuple[QuarterTrends, PositionFigures | None, list[PlantPosition]]:
    """The trends and quarter-end position, and -- only when the quarter-end snapshot is valid -- the position figures and
    per-plant positions it holds, for the page to draw in place of the current calculation."""
    months = months_of(period_start, period_end)
    rows, note = _load(session, months)
    points = _with_changes([_trend_month(m, rows.get(m)) for m in months], rows)
    quarter_end, position, plants = _quarter_end(months, rows, note)
    captured = [p.label for p in points if p.state != "MISSING"]
    missing = [p.label for p in points if p.state == "MISSING"]
    notes = [REALISED_SAVINGS_NOTE]
    if note:
        notes.insert(0, note)
    elif missing:
        notes.insert(0, f"No saved snapshot for {', '.join(missing)}." + (f" Captured: {', '.join(captured)}." if captured else ""))
    return QuarterTrends(months=points, quarter_end=quarter_end, notes=notes), position, plants
