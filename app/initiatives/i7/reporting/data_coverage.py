"""What the report's data does and does not cover.

A management reader should never have to guess whether a number is current. This section reads the dates straight
from the database -- nothing here is a hardcoded cutoff -- and lists, in plain words, every reason a figure on the
page may be missing or old:

* the last goods issue seen by the stockout usage, and the date that usage was built as of;
* the last month with staged SAP consumption, and which of the period's months fall after it;
* when stock on hand was last loaded;
* how many recommendations and material-plants the figures are over;
* and whether the figures are saved month-end snapshots or current calculations.

The known source-coverage gap after about May 2026 is found this way, from the data, not assumed: a month the staged
consumption does not cover is reported as not covered, never as zero consumption or zero risk.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import extract, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.initiatives.i7.kpi_formulas.inventory_value import stock_loaded_at
from app.initiatives.i7.reporting import kpi_definitions
from app.initiatives.i7.reporting.kpi_definitions import MAX_MEASUREMENT_AGE_DAYS
from app.initiatives.i7.reporting.period import exclusive_end_of, month_label, month_state, months_of
from app.models.i7_recommendation import Recommendation
from app.models.i7_stockout_risk import StockoutRiskInput
from app.schemas.i7.reports import (
    AvailabilityStatus,
    CoverageItem,
    DataCoverage,
    ForecastActualSection,
    MonthCoverage,
    PositionFigures,
    QuarterEnd,
    SourceActivitySection,
    SourceMonthStatus,
)

BASIS_CURRENT = (
    "Current calculations made when this report was generated from the recommendations and the staged SAP data. "
    "No saved month-end snapshot is used."
)


def generated_by_month(session: Session, period_start: date, exclusive_end: date) -> dict[tuple[int, int], int]:
    rows = session.execute(
        select(extract("year", Recommendation.generated_at), extract("month", Recommendation.generated_at), func.count())
        .where(Recommendation.generated_at >= period_start, Recommendation.generated_at < exclusive_end)
        .group_by(extract("year", Recommendation.generated_at), extract("month", Recommendation.generated_at))
    ).all()
    return {(int(y), int(m)): int(n) for y, m, n in rows}


def _latest_issue_date(session: Session) -> date | None:
    try:
        return session.execute(select(func.max(StockoutRiskInput.last_issue_date))).scalar()
    except SQLAlchemyError:
        # The usage table does not exist yet (the migration has not been applied): "not built", not an error.
        session.rollback()
        return None


def _stock_loaded_at(session: Session) -> datetime | None:
    return stock_loaded_at(session)


def build_data_coverage(
    session: Session,
    period_start: date,
    period_end: date,
    today: date,
    cutoff: date,
    *,
    source_activity: SourceActivitySection | None,
    position: PositionFigures,
) -> DataCoverage:
    exclusive_end = exclusive_end_of(period_end)
    months = months_of(period_start, period_end)
    # The recommendations with something to change in SAP, one per material-plant: the number the report's other sections show, not
    # every record generated (a recalculated material-plant has a record for each run).
    generated = kpi_definitions.actionable_generated_by_month(session, period_start, exclusive_end)
    tracked = kpi_definitions.actionable_total(session, period_start, exclusive_end)
    staged_by_month = {m.month: m.status for m in (source_activity.months if source_activity else [])}
    evaluated, _ = kpi_definitions.material_plants_assessed(session, period_start, exclusive_end)
    latest_issue = _latest_issue_date(session)
    stock_loaded = _stock_loaded_at(session)
    last_staged_month = source_activity.staged_data_last_month if source_activity else None

    warnings: list[str] = []

    missing = [
        month_label(m)
        for m in months
        if month_state(m, period_end, today) != "not_started"
        and staged_by_month.get(m, SourceMonthStatus.NO_SOURCE_DATA)
        in (SourceMonthStatus.NOT_COVERED, SourceMonthStatus.NO_SOURCE_DATA)
    ]
    if missing:
        ends = f"ends in {month_label(last_staged_month)}" if last_staged_month else "has not been loaded"
        warnings.append(
            f"Staged SAP consumption {ends}, so {', '.join(missing)} "
            f"{'has' if len(missing) == 1 else 'have'} no consumption data. These months are shown as not available, "
            "never as zero consumption."
        )

    stockout = position.stockout
    if stockout.as_of is None:
        warnings.append("The stockout usage has not been built, so stockout risk cannot be judged.")
    elif stockout.stale_note:
        warnings.append(stockout.stale_note)

    if stock_loaded is not None and (cutoff - stock_loaded.date()).days > MAX_MEASUREMENT_AGE_DAYS:
        warnings.append(
            f"Stock on hand was last loaded on {stock_loaded:%d %b %Y}, more than {MAX_MEASUREMENT_AGE_DAYS} days "
            "before this report's cutoff."
        )
    if stock_loaded is None:
        warnings.append("No stock-on-hand load is recorded, so stock on hand cannot be dated.")

    warnings.append(BASIS_CURRENT)

    return DataCoverage(
        latest_consumption_date=latest_issue,
        latest_consumption_month=last_staged_month,
        latest_stock_loaded_at=stock_loaded,
        stockout_usage_as_of=stockout.as_of,
        months=[
            MonthCoverage(
                month=m,
                label=month_label(m),
                consumption_status=staged_by_month.get(m, SourceMonthStatus.NO_SOURCE_DATA),
                recommendations_generated=generated.get((m.year, m.month), 0),
            )
            for m in months
        ],
        eligible_recommendations=tracked,
        eligible_material_plants=evaluated,
        basis="current_calculation",
        warnings=warnings,
    )


# --- Each source, with a plain status ------------------------------------------------------------------------

AVAILABLE, STALE, NOT_AVAILABLE, NOT_COVERED, INSUFFICIENT_HISTORY = (
    "Available", "Stale", "Not available", "Not covered", "Insufficient history",
)


def _day(value: date | datetime | None) -> str | None:
    return None if value is None else f"{value.day} {value:%b} {value.year}"


def coverage_items(
    coverage: DataCoverage, position: PositionFigures, quarter_end: QuarterEnd | None, forecast: ForecastActualSection
) -> list[CoverageItem]:
    """What each important figure rests on, with a status and the reason. One definition, saved with the report, so the
    page, the PDF and the Excel show the same statuses and none re-derives them."""
    stockout = position.stockout
    uncovered = [m for m in coverage.months if m.consumption_status in (SourceMonthStatus.NOT_COVERED, SourceMonthStatus.NO_SOURCE_DATA)]
    currency = position.working_capital_currency or position.currency
    excluded = position.working_capital_excluded_count
    stock_status = (
        NOT_AVAILABLE if coverage.latest_stock_loaded_at is None else (STALE if position.inventory_unavailable_reason else AVAILABLE)
    )
    return [
        CoverageItem(
            key="goods_issue", label="Latest SAP goods issue", value=_day(coverage.latest_consumption_date),
            status=NOT_AVAILABLE if coverage.latest_consumption_date is None else AVAILABLE,
            detail="The stockout usage has not been built." if coverage.latest_consumption_date is None else None,
        ),
        CoverageItem(
            key="consumption_month", label="Latest staged consumption month",
            value=None if coverage.latest_consumption_month is None else f"{coverage.latest_consumption_month:%b %Y}",
            status=NOT_AVAILABLE if coverage.latest_consumption_month is None else (NOT_COVERED if uncovered else AVAILABLE),
            detail=(
                f"{', '.join(m.label for m in uncovered)} {'has' if len(uncovered) == 1 else 'have'} no consumption data: shown as not "
                "available, never as zero."
                if uncovered else None
            ),
        ),
        CoverageItem(
            key="stockout", label="Latest stockout measurement", value=_day(stockout.as_of),
            status=NOT_AVAILABLE if stockout.as_of is None else (STALE if stockout.stale else AVAILABLE),
            detail=(
                stockout.stale_note if stockout.stale
                else "The stockout usage has not been built, so risk cannot be judged. This is not zero risk." if stockout.as_of is None
                else None
            ),
        ),
        CoverageItem(
            key="stock_load", label="Stock on hand last loaded", value=_day(coverage.latest_stock_loaded_at), status=stock_status,
            detail=position.inventory_unavailable_reason
            or ("No stock-on-hand load is recorded." if coverage.latest_stock_loaded_at is None else None),
        ),
        CoverageItem(
            key="recommendation_date", label="Newest recommendation in the figures", value=_day(position.recommendation_reference_date),
            status=AVAILABLE if position.recommendation_reference_date else NOT_AVAILABLE,
            detail=(
                "The SAP values compared against are the ones stored on it." if position.recommendation_reference_date
                else "No recommendation in the period."
            ),
        ),
        CoverageItem(
            key="recommendation_counts", label="Recommendations to adopt · material-plants computed",
            value=f"{coverage.eligible_recommendations:,} · {coverage.eligible_material_plants:,}", status=AVAILABLE,
            detail=(
                "Recommendations of the period with something to change in SAP, one per material-plant (the same number as the SAP adoption "
                "and approval sections), and distinct material-plants with a computed recommendation."
            ),
        ),
        CoverageItem(
            key="snapshot", label="Month-end snapshot",
            value=_day(quarter_end.measured_on) if quarter_end and quarter_end.status == AvailabilityStatus.AVAILABLE else None,
            status=AVAILABLE if quarter_end and quarter_end.status == AvailabilityStatus.AVAILABLE else NOT_AVAILABLE,
            detail=(
                None if quarter_end and quarter_end.status == AvailabilityStatus.AVAILABLE
                else (quarter_end.reason if quarter_end else "This report was made before snapshots existed.")
            ),
        ),
        CoverageItem(
            key="forecast", label="Forecast data",
            value=f"{len(forecast.months)} month{'' if len(forecast.months) == 1 else 's'}" if forecast.status == AvailabilityStatus.AVAILABLE else None,
            status=(
                AVAILABLE if forecast.status == AvailabilityStatus.AVAILABLE
                else NOT_AVAILABLE if forecast.status == AvailabilityStatus.NOT_AVAILABLE else INSUFFICIENT_HISTORY
            ),
            detail=None if forecast.status == AvailabilityStatus.AVAILABLE else forecast.note,
        ),
        CoverageItem(
            key="currency", label="Currency", value=currency, status=AVAILABLE if currency else NOT_AVAILABLE,
            detail=(
                f"{excluded:,} material-plants are left out of working capital (another or no stated currency, no valid price, or no "
                "reorder point). Only ZAR, and NAD at 1:1, are counted; nothing else is converted."
                if excluded > 0 else None
            ),
        ),
    ]
