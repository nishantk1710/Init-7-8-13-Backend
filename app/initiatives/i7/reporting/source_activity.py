"""The Quarterly Report's source-activity section: what happened in the quarter, by month, from the
staged SAP consumption -- not from when I07 generated its outputs.

**Why this exists.** Every other section of the report counts an I07 output by its own timestamp
(``generated_at``, ``computed_at``). A quarter in which nothing was generated -- Q3 2026, when
every recommendation was created in October -- therefore reports zeros even though the SAP
movements of July, August and September are staged. This section reads those movements by the
month they happened in (``i7_staged_consumption.period``) and leaves the output counts alone.

**Source.** ``i7_staged_consumption``: one row per material-plant-month (``period`` is the first
day of the month, unique with material and plant), holding the net issued quantity and the
``movement_count`` / ``issue_count`` / ``reversal_count`` that staging already derived with the
project's movement-type rules (issues 201/261, reversals 202/262). Nothing is re-derived here.
Rows are never stored for a month with no movement, which is why a missing month needs the
coverage rule below.

**Rules.**
* The period is half-open: ``period >= period_start`` and ``period < period_end + 1 day``, the
  convention the other sections use.
* Every figure is aggregated in SQL. The table has one row per material-plant-month, so nothing
  is joined and nothing can be counted twice.
* A month is AVAILABLE when rows exist; NO_ACTIVITY (a confirmed zero) when it lies between the
  first and last month the staged data covers and has no rows; NOT_COVERED when it lies outside
  that span (not delivered or not yet ingested -- never reported as zero); NO_SOURCE_DATA when
  nothing is staged at all.
* Quantities are not reported. The staged quantities currently inherit the SAP number-format
  issue (a quantity written ``12.000`` is read as 12,000), so a transaction count is given
  instead: it needs no unit of measure and no number parsing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import Integer, case, func, select
from sqlalchemy.orm import Session

from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedConsumption
from app.schemas.i7.reports import (
    AvailabilityStatus,
    SourceActivityMonth,
    SourceActivitySection,
    SourceActivityTotals,
    SourceMonthStatus,
)

QUANTITY_NOTE = (
    "Consumption quantities are not reported for the staged source data yet: they are withheld until the "
    "SAP number-format correction is applied. Transaction counts are given instead; they do not depend "
    "on a unit of measure or on how a quantity was read."
)


def month_starts(period_start: date, period_end: date) -> list[date]:
    """The first day of every calendar month from ``period_start`` to ``period_end`` inclusive."""
    months: list[date] = []
    current = date(period_start.year, period_start.month, 1)
    while current <= period_end:
        months.append(current)
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return months


def _exclusive_end(period_end: date) -> date:
    return period_end + timedelta(days=1)


def _output_note(in_period: int, latest: datetime | None) -> str:
    if latest is None:
        return "No recommendations have been generated yet."
    stamp = latest.strftime("%Y-%m-%d")
    if in_period == 0:
        return (
            f"No recommendations were generated inside this period. The latest were generated on {stamp}; "
            "they are a current-state snapshot and are not counted in this period."
        )
    return (
        f"{in_period:,} recommendations were generated inside this period. The latest were generated on {stamp}."
    )


def build_source_activity(
    session: Session, period_start: date, period_end: date, *, recommendations_in_period: int
) -> SourceActivitySection:
    """The section for ``[period_start, period_end]``. Four small aggregate queries, no row loading."""
    exclusive_end = _exclusive_end(period_end)

    coverage_first, coverage_last = session.execute(
        select(func.min(StagedConsumption.period), func.max(StagedConsumption.period))
    ).one()

    monthly = {
        row.period: row
        for row in session.execute(
            select(
                StagedConsumption.period.label("period"),
                func.count().label("material_plants"),
                func.sum(case((StagedConsumption.quantity > 0, 1), else_=0)).cast(Integer).label("with_demand"),
                func.count(func.distinct(StagedConsumption.sap_material_number)).label("materials"),
                func.coalesce(func.sum(StagedConsumption.issue_count), 0).label("issues"),
                func.coalesce(func.sum(StagedConsumption.reversal_count), 0).label("reversals"),
            )
            .where(StagedConsumption.period >= period_start, StagedConsumption.period < exclusive_end)
            .group_by(StagedConsumption.period)
        )
    }

    months: list[SourceActivityMonth] = []
    for month in month_starts(period_start, period_end):
        row = monthly.get(month)
        if row is not None:
            months.append(
                SourceActivityMonth(
                    month=month,
                    status=SourceMonthStatus.AVAILABLE,
                    material_plants_with_movements=row.material_plants,
                    material_plants_with_demand=row.with_demand or 0,
                    materials_with_movements=row.materials,
                    issue_transactions=int(row.issues),
                    reversal_transactions=int(row.reversals),
                )
            )
            continue

        if coverage_first is None:
            status = SourceMonthStatus.NO_SOURCE_DATA
        elif coverage_first <= month <= coverage_last:
            status = SourceMonthStatus.NO_ACTIVITY
        else:
            status = SourceMonthStatus.NOT_COVERED
        confirmed_zero = status is SourceMonthStatus.NO_ACTIVITY
        zero_or_none = 0 if confirmed_zero else None
        months.append(
            SourceActivityMonth(
                month=month,
                status=status,
                material_plants_with_movements=zero_or_none,
                material_plants_with_demand=zero_or_none,
                materials_with_movements=zero_or_none,
                issue_transactions=zero_or_none,
                reversal_transactions=zero_or_none,
            )
        )

    in_window = (StagedConsumption.period >= period_start, StagedConsumption.period < exclusive_end)
    distinct_pairs = (
        select(StagedConsumption.sap_material_number, StagedConsumption.sap_plant_code)
        .where(*in_window)
        .group_by(StagedConsumption.sap_material_number, StagedConsumption.sap_plant_code)
        .subquery()
    )
    demand_pairs = (
        select(StagedConsumption.sap_material_number, StagedConsumption.sap_plant_code)
        .where(*in_window, StagedConsumption.quantity > 0)
        .group_by(StagedConsumption.sap_material_number, StagedConsumption.sap_plant_code)
        .subquery()
    )
    distinct_materials = session.execute(
        select(func.count(func.distinct(StagedConsumption.sap_material_number))).where(*in_window)
    ).scalar_one()

    totals = SourceActivityTotals(
        material_plants_with_movements=session.execute(select(func.count()).select_from(distinct_pairs)).scalar_one(),
        material_plants_with_demand_in_any_month=session.execute(
            select(func.count()).select_from(demand_pairs)
        ).scalar_one(),
        materials_with_movements=distinct_materials,
        issue_transactions=sum(m.issue_transactions or 0 for m in months),
        reversal_transactions=sum(m.reversal_transactions or 0 for m in months),
        months_available=sum(1 for m in months if m.status is SourceMonthStatus.AVAILABLE),
        months_no_activity=sum(1 for m in months if m.status is SourceMonthStatus.NO_ACTIVITY),
        months_not_covered=sum(
            1 for m in months if m.status in (SourceMonthStatus.NOT_COVERED, SourceMonthStatus.NO_SOURCE_DATA)
        ),
    )

    latest_recommendation = session.execute(select(func.max(Recommendation.generated_at))).scalar_one()

    return SourceActivitySection(
        months=months,
        totals=totals,
        staged_data_first_month=coverage_first,
        staged_data_last_month=coverage_last,
        quantity_status=AvailabilityStatus.NOT_AVAILABLE,
        quantity_note=QUANTITY_NOTE,
        latest_recommendation_generated_at=latest_recommendation,
        recommendations_generated_in_period=recommendations_in_period,
        output_note=_output_note(recommendations_in_period, latest_recommendation),
    )
