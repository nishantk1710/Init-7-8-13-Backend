"""The Quarterly Report's source-activity section: a quarter's consumption by the month it happened in.

The bug this closes: every report section counts an I07 output by when it was *generated*, so Q3 2026 --
whose recommendations were all created in October -- reported zeros although the July to September SAP
movements are staged. The section reads ``i7_staged_consumption.period`` instead and leaves every
output-based count alone.

SQLite with only the tables the section reads, so these run without a database.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.reporting.period import resolve_quarter
from app.initiatives.i7.reporting.source_activity import build_source_activity, month_starts
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedConsumption
from app.schemas.i7.reports import AvailabilityStatus, QuarterlyReport, SourceMonthStatus


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (StagedConsumption, Recommendation):
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    return engine


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session


def consumption(material, period, quantity="10", *, plant="1300", issues=1, reversals=0):
    return StagedConsumption(
        sap_material_number=material, sap_plant_code=plant, period=period, quantity=Decimal(quantity),
        unit_of_measure="EA", movement_count=issues + reversals, issue_count=issues, reversal_count=reversals,
        staging_run_id=1, source_table="n_mseg",
    )


def recommendation(material, generated_at, plant="1300"):
    return Recommendation(
        recommendation_id=f"REC-{material}-{plant}-{generated_at:%m%d}", sap_material_number=material,
        sap_plant_code=plant, feature_run_id=1, forecast_run_id=1, inventory_run_id=1, oar_run_id=1,
        policy_id="p", policy_version=1, formula_version="f", status="READY_FOR_REVIEW", impact_status="X",
        generated_at=generated_at,
    )


def section(db, quarter, in_period=0):
    start, end = resolve_quarter(quarter)
    return build_source_activity(db, start, end, recommendations_in_period=in_period)


def by_month(result):
    return {m.month: m for m in result.months}


JUL, AUG, SEP = date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)
OCT, NOV, DEC = date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)
OCTOBER_RUN = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


def seed_q3_with_october_outputs(db):
    """July to September movements, and recommendations generated only in October."""
    db.add_all([
        consumption("M1", JUL, "10", issues=3), consumption("M2", JUL, "5", issues=2),
        consumption("M1", AUG, "4", issues=1, reversals=1),
        consumption("M1", SEP, "7", issues=2), consumption("M3", SEP, "2", plant="1500", issues=1),
        recommendation("M1", OCTOBER_RUN), recommendation("M2", OCTOBER_RUN),
    ])
    db.commit()


# --- 1. Q3 with source data and October outputs -----------------------------------------------------


def test_q3_shows_its_three_source_months_although_the_recommendations_were_created_in_october(db):
    seed_q3_with_october_outputs(db)
    result = section(db, "Q3 2026", in_period=0)
    months = by_month(result)
    assert [m.month for m in result.months] == [JUL, AUG, SEP]
    assert all(m.status is SourceMonthStatus.AVAILABLE for m in result.months)
    assert (months[JUL].material_plants_with_movements, months[JUL].issue_transactions) == (2, 5)
    assert (months[AUG].material_plants_with_movements, months[AUG].reversal_transactions) == (1, 1)
    assert months[SEP].material_plants_with_movements == 2 and months[SEP].materials_with_movements == 2


def test_the_output_counts_stay_at_their_real_generation_dates(db):
    seed_q3_with_october_outputs(db)
    result = section(db, "Q3 2026", in_period=0)
    assert result.recommendations_generated_in_period == 0
    assert result.latest_recommendation_generated_at is not None
    assert result.latest_recommendation_generated_at.month == 10
    assert "No recommendations were generated inside this period" in result.output_note
    assert "2026-10-14" in result.output_note and "not counted in this period" in result.output_note


# --- 2. The October recommendations are untouched ----------------------------------------------------


def test_building_the_section_never_backdates_duplicates_or_changes_a_recommendation(db):
    seed_q3_with_october_outputs(db)

    def fingerprint():
        return db.execute(
            select(func.count(), func.min(Recommendation.generated_at), func.max(Recommendation.generated_at),
                   func.sum(Recommendation.id))
        ).one()

    before = fingerprint()
    section(db, "Q3 2026")
    section(db, "Q4 2026", in_period=2)
    assert fingerprint() == before
    assert db.execute(select(func.count()).select_from(Recommendation).where(
        Recommendation.generated_at < datetime(2026, 10, 1, tzinfo=timezone.utc))).scalar_one() == 0


# --- 3. Q4 ------------------------------------------------------------------------------------------


def test_q4_selects_october_to_december_and_none_of_q3(db):
    seed_q3_with_october_outputs(db)
    db.add_all([consumption("M1", OCT, issues=4), consumption("M2", NOV, issues=1), consumption("M2", DEC, issues=6)])
    db.commit()
    result = section(db, "Q4 2026", in_period=2)
    months = by_month(result)
    assert [m.month for m in result.months] == [OCT, NOV, DEC]
    assert months[OCT].issue_transactions == 4 and months[DEC].issue_transactions == 6
    assert result.totals.issue_transactions == 11  # July to September's 8 movements are not in it
    assert result.recommendations_generated_in_period == 2
    assert "2 recommendations were generated inside this period" in result.output_note


# --- 4. Partial source data -------------------------------------------------------------------------


def test_a_month_inside_the_staged_span_with_no_rows_is_a_confirmed_zero(db):
    db.add_all([consumption("M1", JUL), consumption("M1", SEP)])  # nothing in August
    db.commit()
    august = by_month(section(db, "Q3 2026"))[AUG]
    assert august.status is SourceMonthStatus.NO_ACTIVITY
    assert (august.material_plants_with_movements, august.issue_transactions) == (0, 0)


def test_months_outside_the_staged_span_are_not_covered_and_are_not_reported_as_zero(db):
    db.add_all([consumption("M1", JUL), consumption("M1", AUG)])  # the data ends in August
    months = by_month(section(db, "Q3 2026"))
    assert months[SEP].status is SourceMonthStatus.NOT_COVERED
    assert months[SEP].material_plants_with_movements is None and months[SEP].issue_transactions is None

    q4 = section(db, "Q4 2026")
    assert all(m.status is SourceMonthStatus.NOT_COVERED for m in q4.months)
    assert all(m.issue_transactions is None for m in q4.months)
    assert q4.totals.months_not_covered == 3 and q4.totals.months_available == 0


def test_months_before_the_first_staged_month_are_not_covered(db):
    db.add_all([consumption("M1", SEP)])
    months = by_month(section(db, "Q3 2026"))
    assert months[JUL].status is SourceMonthStatus.NOT_COVERED and months[AUG].status is SourceMonthStatus.NOT_COVERED
    assert months[SEP].status is SourceMonthStatus.AVAILABLE


# --- 5. Empty quarter -------------------------------------------------------------------------------


def test_nothing_staged_at_all_reports_no_source_data_without_error_or_invented_numbers(db):
    result = section(db, "Q3 2026")
    assert [m.status for m in result.months] == [SourceMonthStatus.NO_SOURCE_DATA] * 3
    assert all(m.material_plants_with_movements is None for m in result.months)
    assert result.staged_data_first_month is None and result.staged_data_last_month is None
    assert result.totals.material_plants_with_movements == 0 and result.totals.issue_transactions == 0
    assert result.totals.months_not_covered == 3
    assert result.latest_recommendation_generated_at is None
    assert result.output_note == "No recommendations have been generated yet."


def test_a_quarter_with_data_only_elsewhere_reports_every_month_as_not_covered(db):
    db.add_all([consumption("M1", date(2025, 1, 1)), consumption("M1", date(2025, 3, 1))])
    result = section(db, "Q3 2026")
    assert all(m.status is SourceMonthStatus.NOT_COVERED for m in result.months)


# --- 6. Quarter boundaries --------------------------------------------------------------------------


def test_the_first_day_of_the_quarter_is_included_and_the_exclusive_end_is_not(db):
    db.add_all([
        consumption("A", date(2026, 6, 1), issues=100),  # the previous quarter
        consumption("B", JUL, issues=1),                 # the first day of Q3: in
        consumption("C", SEP, issues=2),                 # the last month of Q3: in
        consumption("D", OCT, issues=1000),              # 2026-10-01 is the exclusive end: out
    ])
    db.commit()
    result = section(db, "Q3 2026")
    assert result.totals.issue_transactions == 3
    assert by_month(result)[JUL].material_plants_with_movements == 1
    assert OCT not in by_month(result)


def test_the_period_end_is_inclusive_to_its_last_day_for_every_quarter_of_a_year(db):
    db.add_all([consumption("M", date(2027, month, 1)) for month in range(1, 13)])
    db.commit()
    for quarter, expected in [("Q1 2027", 3), ("Q2 2027", 3), ("Q3 2027", 3), ("Q4 2027", 3)]:
        assert section(db, quarter).totals.months_available == expected


# --- 7. Plant and material grouping -----------------------------------------------------------------


def test_one_material_in_two_plants_is_two_material_plants_and_one_material(db):
    db.add_all([consumption("M1", JUL, plant="1300"), consumption("M1", JUL, plant="1500")])
    db.commit()
    july = by_month(section(db, "Q3 2026"))[JUL]
    assert july.material_plants_with_movements == 2 and july.materials_with_movements == 1


def test_a_material_plant_active_in_every_month_counts_once_in_the_quarter_total(db):
    db.add_all([consumption("M1", JUL), consumption("M1", AUG), consumption("M1", SEP)])
    db.commit()
    result = section(db, "Q3 2026")
    assert [m.material_plants_with_movements for m in result.months] == [1, 1, 1]
    assert result.totals.material_plants_with_movements == 1  # not 3
    assert result.totals.materials_with_movements == 1
    assert result.totals.issue_transactions == 3  # transactions, unlike material-plants, do add up


def test_only_a_net_positive_month_counts_as_demand(db):
    db.add_all([
        consumption("M1", JUL, "10"),
        consumption("M2", JUL, "0", issues=1, reversals=1),     # netted to zero
        consumption("M3", JUL, "-5", issues=0, reversals=1),    # a reversal outweighed the issues
    ])
    db.commit()
    july = by_month(section(db, "Q3 2026"))[JUL]
    assert july.material_plants_with_movements == 3 and july.material_plants_with_demand == 1
    assert section(db, "Q3 2026").totals.material_plants_with_demand_in_any_month == 1


# --- 8. Compatibility -------------------------------------------------------------------------------


def test_a_report_stored_before_version_1_1_still_validates_without_the_section():
    """Validating an empty payload fails on the many required sections -- but never on source_activity,
    which is optional, so an older stored report (no such key) still loads."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        QuarterlyReport.model_validate({})
    missing = {error["loc"][0] for error in excinfo.value.errors()}
    assert "source_activity" not in missing
    assert {"metadata", "executive_summary", "limitations"} <= missing  # the required ones are still required


def test_the_section_survives_a_json_round_trip_unchanged(db):
    from app.schemas.i7.reports import SourceActivitySection

    seed_q3_with_october_outputs(db)
    original = section(db, "Q3 2026")
    restored = SourceActivitySection.model_validate_json(original.model_dump_json())
    assert restored == original


def test_quantities_are_withheld_with_a_reason_and_no_quantity_field_exists(db):
    db.add_all([consumption("M1", JUL, "99999")])
    db.commit()
    result = section(db, "Q3 2026")
    assert result.quantity_status is AvailabilityStatus.NOT_AVAILABLE
    assert "number-format correction" in result.quantity_note
    dumped = result.model_dump_json()
    assert "99999" not in dumped
    assert "quantity\":" not in dumped.replace("quantity_status", "").replace("quantity_note", "")


# --- 9. Database-side aggregation --------------------------------------------------------------------


def test_the_section_aggregates_in_sql_and_never_loads_the_movement_rows(db, engine):
    db.add_all([consumption(f"M{n}", JUL, issues=1) for n in range(200)])
    db.commit()
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    section(db, "Q3 2026")
    staged = [s for s in statements if "i7_staged_consumption" in s]
    assert 1 <= len(staged) <= 6  # a handful of aggregate queries, not one per row or per month
    for sql in staged:
        lowered = sql.lower()
        assert "count(" in lowered or "min(" in lowered or "sum(" in lowered
        assert "i7_staged_consumption.id" not in lowered  # no full-row select
    assert not [s for s in statements if "join" in s.lower()]


# --- 10. Dynamic periods ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quarter,first,count",
    [("Q3 2026", date(2026, 7, 1), 3), ("Q4 2026", date(2026, 10, 1), 3), ("Q1 2027", date(2027, 1, 1), 3),
     ("Q2 2027", date(2027, 4, 1), 3)],
)
def test_the_three_months_are_derived_from_the_quarter_not_hardcoded(quarter, first, count):
    start, end = resolve_quarter(quarter)
    months = month_starts(start, end)
    assert months[0] == first and len(months) == count
    assert all(m.day == 1 for m in months)


def test_a_december_quarter_does_not_run_into_the_next_year():
    start, end = resolve_quarter("Q4 2026")
    assert month_starts(start, end) == [date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)]


# --- 11. The real database is SQL Server ------------------------------------------------------------


def test_every_query_compiles_for_sql_server_without_an_unsupported_construct(db):
    import re

    from sqlalchemy.dialects import mssql

    db.add_all([consumption("M1", JUL), consumption("M1", AUG)])
    db.commit()
    dialect = mssql.dialect()
    compiled: list[str] = []
    real = db.execute

    def recording(statement, *args, **kw):
        compiled.append(str(statement.compile(dialect=dialect, compile_kwargs={"literal_binds": True})))
        return real(statement, *args, **kw)

    db.execute = recording  # type: ignore[method-assign]
    section(db, "Q3 2026")
    joined = "\n".join(compiled)
    assert len(compiled) >= 4
    assert not re.search(r"\bIS 1\b", joined)            # SQL Server has no "IS 1"
    assert not re.search(r"row_number", joined, re.I)    # no window function over the table
    assert "COUNT(DISTINCT" in joined.upper()
