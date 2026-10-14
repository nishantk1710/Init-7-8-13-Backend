"""Overview numbers that must be the portfolio's, not a page's or a viewer's.

* The SAP adoption summary counts every recommendation with a value -- not only
  the ones someone opened -- and everything without SAP change evidence is
  pending.
* The Forecast vs Actual chart is summed from the champion backtests, per month,
  optionally for one plant.

SQLite with the tables these read; the SAP change-history reads are faked.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.dialects import mssql
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.i7 import adoption as adoption_api
from app.api.i7 import recommendations as recommendations_api
from app.models.i7_forecast import Forecast, ForecastBacktestPath, ForecastRun
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation, SapAdoptionResult


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (Recommendation, SapAdoptionResult, ApprovalLedgerEntry, ForecastRun, Forecast, ForecastBacktestPath):
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()  # the models declare some indexes twice; SQLite refuses that
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


# --- SAP adoption: real counts -----------------------------------------------------------


def rec(material, plant="1300", *, run=1, rop=10, generated=datetime(2026, 10, 14, tzinfo=timezone.utc)):
    return Recommendation(
        recommendation_id=f"REC-STD-{material}-{plant}",
        sap_material_number=material,
        sap_plant_code=plant,
        feature_run_id=run,
        forecast_run_id=run,
        inventory_run_id=run,
        oar_run_id=run,
        policy_id="p",
        policy_version=1,
        formula_version="f",
        status="READY_FOR_REVIEW",
        impact_status="X",
        recommended_rop=rop,
        generated_at=generated,
    )


def ledger(material, status, plant="1300"):
    return SapAdoptionResult(
        recommendation_id=f"REC-STD-{material}-{plant}",
        status=status,
        evaluated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
    )


def summary(db, plant=None):
    return adoption_api.get_adoption_summary(session=db, plant=plant)


def test_with_no_sap_evidence_every_recommendation_with_values_is_pending(db):
    db.add_all([rec("M1"), rec("M2"), rec("M3", "1500")])
    db.commit()
    result = summary(db)
    assert result.total_evaluated == 3
    assert result.unknown_count == 3 and result.adopted_count == result.partially_adopted_count == 0
    assert result.not_adopted_count == 0
    assert result.adoption_rate_percentage is None  # not 0 %: nothing is known yet
    assert [(s.status, s.count) for s in result.by_status] == [
        ("ADOPTED", 0), ("PARTIALLY_ADOPTED", 0), ("NOT_ADOPTED", 0), ("UNKNOWN", 3),
    ]


def test_the_count_does_not_depend_on_anyone_having_opened_the_adoption_page(db):
    db.add_all([rec(f"M{i}") for i in range(5)])
    db.commit()  # no ledger rows at all: nobody has viewed anything
    assert summary(db).total_evaluated == 5


def test_recommendations_without_a_value_are_not_tracked(db):
    blank = rec("M2")
    blank.recommended_rop = None
    db.add_all([rec("M1"), blank])
    db.commit()
    assert summary(db).total_evaluated == 1


def test_a_regenerated_recommendation_is_counted_once(db):
    db.add_all([
        rec("M1", run=1, generated=datetime(2026, 10, 12, tzinfo=timezone.utc)),
        rec("M1", run=2, generated=datetime(2026, 10, 14, tzinfo=timezone.utc)),
    ])
    db.commit()
    assert summary(db).total_evaluated == 1


def test_the_plant_filter_really_filters(db):
    db.add_all([rec("M1", "1300"), rec("M2", "1300"), rec("M3", "1500")])
    db.commit()
    assert summary(db, "1300").total_evaluated == 2
    assert summary(db, "1500").total_evaluated == 1
    assert summary(db, "9999").total_evaluated == 0


def test_known_results_come_from_the_ledger_and_the_rest_are_pending(db):
    db.add_all([rec("M1"), rec("M2"), rec("M3"), rec("M4")])
    db.add_all([ledger("M1", "ADOPTED"), ledger("M2", "PARTIALLY_ADOPTED"), ledger("M3", "NOT_ADOPTED")])
    db.commit()
    result = summary(db)
    assert (result.adopted_count, result.partially_adopted_count, result.not_adopted_count) == (1, 1, 1)
    assert result.unknown_count == 1 and result.total_evaluated == 4
    assert result.adoption_rate_percentage == 66.7  # 2 of the 3 with evidence


def test_an_unknown_ledger_row_is_the_same_as_no_row(db):
    db.add_all([rec("M1"), rec("M2")])
    db.add(ledger("M1", "UNKNOWN"))
    db.commit()
    result = summary(db)
    assert result.unknown_count == 2 and result.total_evaluated == 2


def test_ledger_rows_for_recommendations_outside_the_scope_are_ignored(db):
    db.add_all([rec("M1", "1300"), rec("M2", "1500")])
    db.add_all([ledger("M1", "ADOPTED"), ledger("M2", "ADOPTED", "1500"), ledger("GONE", "ADOPTED")])
    db.commit()
    assert summary(db, "1300").adopted_count == 1
    assert summary(db).adopted_count == 2  # the row for a recommendation that no longer exists is not counted


def test_the_four_counts_always_add_up_to_the_total(db):
    db.add_all([rec(f"M{i}") for i in range(6)])
    db.add_all([ledger("M0", "ADOPTED"), ledger("M1", "NOT_ADOPTED")])
    db.commit()
    result = summary(db)
    assert sum(s.count for s in result.by_status) == result.total_evaluated == 6


def test_the_summary_reads_no_sap_change_history(db, monkeypatch):
    """The change-history tables are large and unindexed; the summary stays fast by not touching them."""
    db.add_all([rec("M1")])
    db.commit()

    def boom(*args, **kwargs):
        raise AssertionError("the summary must not evaluate recommendations against SAP change history")

    monkeypatch.setattr(adoption_api, "_build_view", boom)
    assert summary(db).total_evaluated == 1


# --- Forecast vs Actual: summed from the champion backtests --------------------------------


def path(material, plant, period, predicted, actual, *, run=1, step=1):
    return ForecastBacktestPath(
        forecast_run_id=run, sap_material_number=material, sap_plant_code=plant, model_name="SBA",
        origin_period=date(2025, 12, 1), horizon_step=step, forecast_period=period,
        predicted=Decimal(predicted), actual=Decimal(actual),
    )


def champion(material, plant, *, run=1, status="SUCCESS", model="SBA"):
    return Forecast(
        forecast_run_id=run, sap_material_number=material, sap_plant_code=plant, demand_class="LUMPY",
        history_status="SUFFICIENT", model_name=model, model_version=f"{model.lower()}-1", is_champion=True,
        forecast_status=status, backtest_status="COMPLETE",
    )


def seed_forecasts(db):
    db.add(ForecastRun(status="succeeded", policy_id="p", policy_version=1))
    db.add_all([
        champion("A", "1300"), champion("B", "1300"), champion("C", "1500"),
        champion("T", "1300", model="TSB"),  # the declining-demand path: a champion with no backtest
    ])
    jan, feb, mar = date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)
    db.add_all([
        path("A", "1300", jan, 10, 12), path("A", "1300", feb, 20, 18), path("A", "1300", mar, 30, 33),
        path("B", "1300", jan, 1, 2), path("B", "1300", feb, 2, 3),
        path("C", "1500", jan, 100, 90), path("C", "1500", feb, 200, 210), path("C", "1500", mar, 300, 330),
        path("A", "1300", jan, 999, 999, step=2),  # a later horizon step: must not be added in
    ])
    db.commit()


def fva(db, **kwargs):
    return recommendations_api.get_forecast_vs_actual(session=db, plant=kwargs.get("plant"), months=kwargs.get("months", 12))


def test_each_month_sums_predicted_and_actual_across_the_champion_paths(db):
    seed_forecasts(db)
    result = fva(db)
    by_month = {p.forecast_period: (p.predicted, p.actual, p.material_count) for p in result.points}
    assert by_month[date(2026, 1, 1)] == (Decimal(111), Decimal(104), 3)  # A + B + C
    assert by_month[date(2026, 2, 1)] == (Decimal(222), Decimal(231), 3)
    assert by_month[date(2026, 3, 1)] == (Decimal(330), Decimal(363), 2)  # B has no March path
    assert [p.forecast_period for p in result.points] == sorted(by_month)


def test_only_the_first_horizon_step_is_summed(db):
    seed_forecasts(db)
    january = next(p for p in fva(db).points if p.forecast_period == date(2026, 1, 1))
    assert january.predicted == 111  # not 111 + 999


def test_one_plant_only(db):
    seed_forecasts(db)
    result = fva(db, plant="1300")
    january = next(p for p in result.points if p.forecast_period == date(2026, 1, 1))
    assert (january.predicted, january.actual, january.material_count) == (Decimal(11), Decimal(14), 2)
    assert fva(db, plant="1500").materials_with_history_count == 1
    assert fva(db, plant="9999").points == ()


def test_the_caption_counts_say_how_many_materials_have_history(db):
    seed_forecasts(db)
    result = fva(db)
    assert result.materials_with_history_count == 3  # A, B, C
    assert result.materials_in_scope_count == 4  # plus T, forecast with TSB and not backtested by design
    assert fva(db, plant="1300").materials_in_scope_count == 3


def test_only_the_latest_months_are_returned(db):
    seed_forecasts(db)
    result = fva(db, months=2)
    assert [p.forecast_period for p in result.points] == [date(2026, 2, 1), date(2026, 3, 1)]


def test_a_newer_run_that_has_not_succeeded_is_ignored(db):
    seed_forecasts(db)
    db.add(ForecastRun(status="running", policy_id="p", policy_version=1))
    db.add(path("Z", "1300", date(2026, 4, 1), 5, 5, run=2))
    db.commit()
    result = fva(db)
    assert date(2026, 4, 1) not in [p.forecast_period for p in result.points]


def test_with_no_forecast_run_the_answer_is_empty_not_an_error(db):
    result = fva(db)
    assert result.points == () and result.materials_with_history_count == 0 and result.materials_in_scope_count == 0


# --- SQL Server: the production database -------------------------------------------------------
#
# SQLite accepts `col IS 1`; SQL Server does not. A query with that in it passes every
# SQLite test and fails with a 500 in production (the Forecast vs Actual endpoint did).
# So the SQL these endpoints run is also compiled for SQL Server and checked.

BOOLEAN_IS = re.compile(r"\bIS\s+[01]\b", re.IGNORECASE)


def as_sql_server(statement) -> str:
    return str(statement.compile(dialect=mssql.dialect(), compile_kwargs={"literal_binds": True}))


def test_every_statement_the_forecast_endpoint_runs_is_valid_on_sql_server(db):
    seed_forecasts(db)
    seen: list[str] = []
    real_execute = db.execute

    def recording_execute(statement, *args, **kwargs):
        seen.append(as_sql_server(statement))
        return real_execute(statement, *args, **kwargs)

    db.execute = recording_execute  # type: ignore[method-assign]
    fva(db, plant="1300")
    assert len(seen) >= 4
    assert not [sql for sql in seen if BOOLEAN_IS.search(sql)], seen


@pytest.mark.parametrize("kind", ["oar", "declining", "normal"])
def test_the_type_filter_is_valid_on_sql_server(kind):
    from sqlalchemy import select

    statement = recommendations_api._apply_filters(
        select(Recommendation), status=None, plant=None, material=None, demand_class=None, is_oar=None,
        confidence=None, criticality=None, generated_from=None, generated_to=None, demand_type=kind,
    )
    assert not BOOLEAN_IS.search(as_sql_server(statement))


def test_the_api_modules_never_write_a_boolean_is_comparison():
    import pathlib

    offenders = [
        f"{path.name}:{number}"
        for path in pathlib.Path("app/api").rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\.is_\((True|False)\)", line)
    ]
    assert offenders == [], "use the boolean column itself (renders `= 1` on SQL Server): " + ", ".join(offenders)
