"""The monthly KPI snapshots: capture, idempotency, revisions, final snapshots, failures and the migration.

SQLite with only the tables a snapshot reads and writes. The stockout measurement is replaced by the counts it would
return, as in test_quarter_management.py: what is tested is how a snapshot treats that measurement.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import MetaData, create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.reporting import kpi_definitions, snapshots
from app.initiatives.i7.stockout_risk.counts import StockoutRiskCounts
from app.models.i7_features import MaterialFeature
from app.models.i7_kpi_snapshot import STATUS_FINAL, STATUS_OPEN, KpiSnapshot, KpiSnapshotLog
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun
from app.models.i7_stockout_risk import StockoutRiskInput

TABLES = (Recommendation, KpiSnapshot, KpiSnapshotLog, StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun, StockoutRiskInput, MaterialFeature)
OCT = date(2026, 10, 1)
NOV = date(2026, 11, 1)
MID_OCT = date(2026, 10, 21)
EARLY_NOV = date(2026, 11, 3)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in TABLES:
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def at(year, month, day=10):
    return datetime(year, month, day, 9, 0, tzinfo=timezone.utc)


def rec(material, generated_at, *, plant="1300", run=1, price=Decimal("10"), cur_rop=2, rec_rop=12, cur_max=100, rec_max=60,
        cur_ss=1, rec_ss=3):
    return Recommendation(
        recommendation_id=f"REC-{material}-{plant}-{run}", sap_material_number=material, sap_plant_code=plant,
        feature_run_id=run, forecast_run_id=run, inventory_run_id=run, oar_run_id=run, policy_id="p", policy_version=1,
        formula_version="f", status="READY_FOR_REVIEW", impact_status="X", generated_at=generated_at, unit_price=price,
        current_rop=cur_rop, recommended_rop=rec_rop, current_max_stock=cur_max, recommended_max_stock=rec_max,
        current_safety_stock=cur_ss, recommended_safety_stock=rec_ss,
    )


def usage(monkeypatch, as_of, *, at_risk=5, evaluated=50, insufficient=2):
    monkeypatch.setattr(
        kpi_definitions, "stockout_risk_counts",
        lambda session, base, **kw: StockoutRiskCounts(at_risk=at_risk, evaluated=evaluated, insufficient_data=insufficient, as_of=as_of),
    )


@pytest.fixture(autouse=True)
def no_usage(monkeypatch):
    usage(monkeypatch, None, at_risk=0, evaluated=0, insufficient=0)


def logs(db, month=OCT):
    return [row.action for row in db.execute(select(KpiSnapshotLog).where(KpiSnapshotLog.month == month).order_by(KpiSnapshotLog.id)).scalars()]


def only(db, month=OCT) -> KpiSnapshot:
    return db.execute(select(KpiSnapshot).where(KpiSnapshot.month == month)).scalar_one()


# --- capture ---------------------------------------------------------------------------------


def test_capturing_the_current_month_saves_an_open_rolling_reading(db):
    db.add_all([rec("A", at(2026, 10)), rec("B", at(2026, 10))])
    db.commit()
    result = snapshots.capture_month(db, OCT, today=MID_OCT)
    assert result.action == snapshots.CREATED
    row = only(db)
    assert (row.status, row.revision, row.population) == (STATUS_OPEN, 1, 2)
    assert row.cutoff == MID_OCT  # an open month speaks for today, not for its last day
    assert row.measured_on == MID_OCT
    assert row.month_end_valid is False  # a rolling reading is never a month-end figure
    assert row.calc_version == snapshots.CALC_VERSION
    assert logs(db) == ["CREATED"]


def test_a_snapshot_holds_the_headline_figures_and_the_per_plant_payload(db):
    db.add_all([rec("A", at(2026, 10), plant="1300"), rec("B", at(2026, 10), plant="1500")])
    db.commit()
    snapshots.capture_month(db, OCT, today=MID_OCT)
    row = only(db)
    assert row.excess_count == 2  # 100 > 60
    assert row.excess_opportunity == Decimal("800")  # 2 x 10 x 40
    assert row.net_rop_value_impact == Decimal("-200")  # 2 x 10 x (2 - 12)
    assert row.priced_count == 2
    payload = json.loads(row.payload_json)
    assert [p["plant"] for p in payload["plant_positions"]] == [None, "1300", "1500"]
    assert {lvl["key"]: lvl["records"] for lvl in payload["stock_levels"]} == {"safety_stock": 2, "reorder_point": 2, "max_stock": 2}


def test_a_month_that_has_not_started_cannot_be_captured(db):
    with pytest.raises(ValueError, match="has not started"):
        snapshots.capture_month(db, date(2026, 12, 1), today=MID_OCT)
    assert db.execute(select(KpiSnapshot)).scalars().all() == []


# --- idempotency and revisions ---------------------------------------------------------------


def test_capturing_the_same_month_again_with_the_same_figures_changes_nothing(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    snapshots.capture_month(db, OCT, today=MID_OCT)
    again = snapshots.capture_month(db, OCT, today=date(2026, 10, 22))
    assert again.action == snapshots.UNCHANGED
    assert db.execute(select(KpiSnapshot)).scalars().all().__len__() == 1
    row = only(db)
    assert row.revision == 1
    assert row.measured_on == MID_OCT  # not touched: nothing was re-measured as different
    assert logs(db) == ["CREATED"]


def test_new_data_while_the_month_is_open_updates_it_as_a_new_logged_revision(db):
    db.add(rec("A", at(2026, 10, 5)))
    db.commit()
    snapshots.capture_month(db, OCT, today=MID_OCT)
    first = only(db).payload_sha256
    db.add(rec("B", at(2026, 10, 20)))
    db.commit()
    result = snapshots.capture_month(db, OCT, today=date(2026, 10, 22))
    assert result.action == snapshots.UPDATED
    row = only(db)
    assert (row.revision, row.population) == (2, 2)
    assert row.payload_sha256 != first
    log = db.execute(select(KpiSnapshotLog).where(KpiSnapshotLog.action == "UPDATED")).scalar_one()
    assert log.previous_sha256 == first and log.payload_sha256 == row.payload_sha256 and log.revision == 2


# --- the population: the recommendations in force on the last day of the month ------------------


def test_each_month_is_over_the_recommendations_in_force_at_its_end(db):
    db.add_all([
        rec("A", at(2026, 10, 5), run=1, rec_max=60),
        rec("B", at(2026, 10, 6)),
        rec("A", at(2026, 11, 5), run=2, rec_max=200),  # A is regenerated in November, no longer in excess
    ])
    db.commit()
    snapshots.capture_month(db, OCT, today=EARLY_NOV)
    snapshots.capture_month(db, NOV, today=EARLY_NOV)
    october, november = only(db, OCT), only(db, NOV)
    assert (october.population, october.excess_count) == (2, 2)
    # November still counts B (generated in October, in force) and A's newer recommendation.
    assert (november.population, november.excess_count) == (2, 1)


# --- final snapshots -------------------------------------------------------------------------


def test_a_snapshot_captured_after_the_month_ended_is_final_and_valid_for_month_end_if_close_enough(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    result = snapshots.capture_month(db, OCT, today=EARLY_NOV)
    row = only(db)
    assert (result.action, row.status) == (snapshots.CREATED, STATUS_FINAL)
    assert row.cutoff == date(2026, 10, 31)  # a closed month speaks for its last day
    assert row.month_end_valid is True  # measured 3 days after the month ended


def test_a_final_snapshot_measured_long_after_the_month_is_not_a_month_end_figure(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    snapshots.capture_month(db, OCT, today=date(2027, 1, 20))
    row = only(db)
    assert row.status == STATUS_FINAL
    assert row.month_end_valid is False  # 81 days after the month: figures read then are not the month-end position


def test_an_open_snapshot_is_finalised_once_the_month_has_ended_even_with_the_same_figures(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    snapshots.capture_month(db, OCT, today=MID_OCT)
    result = snapshots.capture_month(db, OCT, today=EARLY_NOV)
    row = only(db)
    assert result.action == snapshots.FINALISED
    assert (row.status, row.revision, row.month_end_valid) == (STATUS_FINAL, 2, True)
    assert logs(db) == ["CREATED", "FINALISED"]


def test_a_final_snapshot_is_not_replaced_silently(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    snapshots.capture_month(db, OCT, today=EARLY_NOV)
    before = only(db).payload_sha256
    db.add(rec("B", at(2026, 10, 25)))  # figures differ now
    db.commit()
    result = snapshots.capture_month(db, OCT, today=date(2026, 11, 4))
    assert result.action == snapshots.REFUSED
    row = only(db)
    assert (row.payload_sha256, row.revision, row.population) == (before, 1, 1)  # exactly as it was published
    assert logs(db) == ["CREATED", "REFUSED"]


def test_a_forced_replacement_of_a_final_snapshot_is_logged_with_what_it_replaced(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    snapshots.capture_month(db, OCT, today=EARLY_NOV)
    before = only(db).payload_sha256
    db.add(rec("B", at(2026, 10, 25)))
    db.commit()
    result = snapshots.capture_month(db, OCT, today=date(2026, 11, 4), force=True)
    assert result.action == snapshots.REPLACED_FINAL
    row = only(db)
    assert (row.revision, row.population) == (2, 2)
    log = db.execute(select(KpiSnapshotLog).where(KpiSnapshotLog.action == "REPLACED_FINAL")).scalar_one()
    assert log.previous_sha256 == before and log.payload_sha256 == row.payload_sha256


# --- the stockout reading is kept, and flagged when it cannot be trusted ---------------------------


def test_a_stockout_usage_far_from_the_cutoff_is_kept_but_flagged_stale(db, monkeypatch):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    usage(monkeypatch, date(2026, 5, 31), at_risk=891, evaluated=9916)
    snapshots.capture_month(db, OCT, today=MID_OCT)
    row = only(db)
    assert row.stockout_stale is True
    assert (row.stockout_at_risk, row.stockout_evaluated) == (891, 9916)  # kept for the record
    assert row.stockout_usage_as_of == date(2026, 5, 31)


def test_a_stockout_usage_close_to_the_cutoff_is_trusted_and_a_missing_one_is_stale(db, monkeypatch):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    usage(monkeypatch, date(2026, 10, 20))
    snapshots.capture_month(db, OCT, today=MID_OCT)
    assert only(db).stockout_stale is False
    usage(monkeypatch, None, at_risk=0, evaluated=0, insufficient=0)
    snapshots.capture_month(db, OCT, today=date(2026, 10, 22))
    assert only(db).stockout_stale is True  # never built: not "no risk"


# --- failures --------------------------------------------------------------------------------


def test_a_failed_capture_is_logged_raised_and_leaves_the_previous_snapshot_untouched(db, monkeypatch):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    snapshots.capture_month(db, OCT, today=MID_OCT)
    before = only(db).payload_sha256

    def boom(*args, **kwargs):
        raise RuntimeError("database blip")

    monkeypatch.setattr(snapshots, "compute", boom)
    with pytest.raises(RuntimeError, match="database blip"):
        snapshots.capture_month(db, OCT, today=date(2026, 10, 22))
    assert only(db).payload_sha256 == before
    assert only(db).revision == 1
    failed = db.execute(select(KpiSnapshotLog).where(KpiSnapshotLog.action == "FAILED")).scalar_one()
    assert "database blip" in failed.detail


def test_a_failed_capture_can_simply_be_retried(db, monkeypatch):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    real = snapshots.compute
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first attempt fails")
        return real(*args, **kwargs)

    monkeypatch.setattr(snapshots, "compute", flaky)
    with pytest.raises(RuntimeError):
        snapshots.capture_month(db, OCT, today=MID_OCT)
    assert db.execute(select(KpiSnapshot)).scalars().all() == []  # nothing half-written
    assert snapshots.capture_month(db, OCT, today=MID_OCT).action == snapshots.CREATED
    assert logs(db) == ["FAILED", "CREATED"]


# --- what the nightly job asks for -----------------------------------------------------------


def test_the_nightly_capture_refreshes_this_month_and_finalises_the_last_and_is_then_a_no_op(db):
    db.add(rec("A", at(2026, 10)))
    db.commit()
    first = snapshots.capture_due(db, today=EARLY_NOV)
    assert [(r.month, r.action) for r in first] == [(OCT, snapshots.CREATED), (NOV, snapshots.CREATED)]
    assert only(db, OCT).status == STATUS_FINAL and only(db, NOV).status == STATUS_OPEN
    assert snapshots.capture_due(db, today=EARLY_NOV) == []  # final, and already read today: nothing to do


def test_the_nightly_capture_rolls_over_the_year(db):
    results = snapshots.capture_due(db, today=date(2027, 1, 2))
    assert [r.month for r in results] == [date(2026, 12, 1), date(2027, 1, 1)]


# --- the migration matches the model ---------------------------------------------------------


def _load_migration():
    path = next(Path(__file__).resolve().parents[3].glob("alembic/versions/a3c9e7f15b42_*.py"))
    spec = importlib.util.spec_from_file_location("migration_a3c9e7f15b42", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_creates_exactly_the_tables_and_columns_of_the_models_and_can_be_undone():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        inspector = inspect(connection)
        for model in (KpiSnapshot, KpiSnapshotLog):
            made = {column["name"] for column in inspector.get_columns(model.__tablename__)}
            assert made == {column.name for column in model.__table__.columns}, model.__tablename__
        uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("i7_kpi_snapshot")}
        assert ("month",) in uniques  # one snapshot per month, enforced by the database too
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert not {"i7_kpi_snapshot", "i7_kpi_snapshot_log"} & set(inspect(connection).get_table_names())


def test_the_migration_continues_the_single_alembic_chain():
    migration = _load_migration()
    assert migration.down_revision == "d4e8b2a61c35"
