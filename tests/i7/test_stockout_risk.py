"""High Stockout Risk: stock on hand < average daily consumption x lead time.

Two halves, both tested without a database server:

* ``aggregate_issue_history`` -- per material-plant, the newest issue date and the net
  quantity issued in the 90 days ending on the anchor, from movement rows.
* ``stockout_risk_counts`` -- the SQL that classifies each material-plant (at risk,
  not at risk, Insufficient Data, or outside the KPI as Non-moving) and counts.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.initiatives.i7.stockout_risk import CONSUMPTION_WINDOW_DAYS, window_start
from app.initiatives.i7.stockout_risk.counts import stockout_risk_counts
from app.initiatives.i7.stockout_risk.history import aggregate_issue_history
from app.models.i7_features import MaterialFeature
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedStock
from app.models.i7_stockout_risk import StockoutRiskInput

AS_OF = date(2026, 5, 31)
ISSUES = frozenset({"201", "261"})
REVERSALS = frozenset({"202", "262"})


# --- aggregate_issue_history --------------------------------------------------------


def movement(posted, qty="10", *, material="M1", plant="1300", movement_type="201", credit="H"):
    return SimpleNamespace(
        material=material,
        plant=plant,
        posting_date=posted if isinstance(posted, str) else posted.isoformat(),
        quantity=qty,
        movement_type=movement_type,
        debit_credit_ind=credit,
    )


def history(rows, **kwargs):
    return aggregate_issue_history(rows, ISSUES, REVERSALS, as_of=kwargs.pop("as_of", AS_OF), **kwargs)


def test_the_window_is_90_days_ending_on_and_including_the_anchor():
    assert window_start(AS_OF) == date(2026, 3, 3)
    assert (AS_OF - window_start(AS_OF)).days + 1 == CONSUMPTION_WINDOW_DAYS


def test_consumption_in_the_window_is_summed():
    result = history([movement(date(2026, 5, 31), "10"), movement(date(2026, 3, 3), "5")])
    assert result.facts[("M1", "1300")].consumption == Decimal(15)


def test_an_issue_the_day_before_the_window_sets_the_last_issue_date_but_adds_no_consumption():
    result = history([movement(date(2026, 3, 2), "99")])
    facts = result.facts[("M1", "1300")]
    assert facts.last_issue_date == date(2026, 3, 2)
    assert facts.consumption == 0  # a real zero: issued, but not in the window


def test_last_issue_date_is_the_newest_issue_on_or_before_the_anchor():
    result = history([movement(date(2025, 1, 1)), movement(date(2026, 4, 10)), movement(date(2026, 2, 1))])
    assert result.facts[("M1", "1300")].last_issue_date == date(2026, 4, 10)


def test_movements_after_the_anchor_are_ignored_and_counted():
    result = history([movement(date(2026, 4, 1), "10"), movement(date(2026, 9, 8), "500")])
    facts = result.facts[("M1", "1300")]
    assert facts.last_issue_date == date(2026, 4, 1)
    assert facts.consumption == 10
    assert result.skipped == {"after_anchor": 1}


def test_a_material_issued_only_after_the_anchor_is_not_returned():
    assert history([movement(date(2026, 9, 8))]).facts == {}


def test_a_reversal_nets_against_the_issue_it_cancels():
    result = history(
        [
            movement(date(2026, 5, 1), "10", movement_type="201", credit="H"),
            movement(date(2026, 5, 2), "4", movement_type="202", credit="S"),
        ]
    )
    assert result.facts[("M1", "1300")].consumption == 6


def test_reversals_do_not_move_the_last_issue_date():
    result = history(
        [
            movement(date(2026, 4, 1), "10", movement_type="261"),
            movement(date(2026, 5, 20), "10", movement_type="262", credit="S"),
        ]
    )
    assert result.facts[("M1", "1300")].last_issue_date == date(2026, 4, 1)


def test_consumption_never_goes_below_zero():
    result = history(
        [
            movement(date(2025, 12, 1), "50"),  # the issue, long before the window
            movement(date(2026, 5, 1), "50", movement_type="202", credit="S"),  # its reversal, inside it
        ]
    )
    assert result.facts[("M1", "1300")].consumption == 0


def test_a_material_with_only_reversals_has_not_been_issued():
    assert history([movement(date(2026, 5, 1), movement_type="202", credit="S")]).facts == {}


def test_other_movement_types_neither_consume_nor_set_the_last_issue_date():
    assert history([movement(date(2026, 5, 1), "100", movement_type="101")]).facts == {}


def test_material_plants_are_kept_apart():
    result = history([movement(date(2026, 5, 1), "10", plant="1300"), movement(date(2026, 5, 1), "7", plant="1500")])
    assert result.facts[("M1", "1300")].consumption == 10
    assert result.facts[("M1", "1500")].consumption == 7


def test_plants_outside_the_scope_are_dropped():
    result = history(
        [movement(date(2026, 5, 1), plant="1300"), movement(date(2026, 5, 1), plant="3000")],
        plant_scope=frozenset({"1300"}),
    )
    assert list(result.facts) == [("M1", "1300")]


def test_bad_rows_are_skipped_and_counted_not_swallowed():
    result = history(
        [
            movement(date(2026, 5, 1), material=" "),
            movement("not-a-date"),
            movement(date(2026, 5, 1), qty="abc"),
            movement(date(2026, 5, 1), "10"),
        ]
    )
    assert result.facts[("M1", "1300")].consumption == 10
    assert result.skipped == {"missing_material_or_plant": 1, "invalid_date": 1, "invalid_quantity": 1}


# --- stockout_risk_counts -----------------------------------------------------------


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (Recommendation, MaterialFeature, StagedStock, StockoutRiskInput):
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def recommendation(material, plant="1300", lead_time="10"):
    return Recommendation(
        recommendation_id=f"REC-{material}-{plant}",
        sap_material_number=material,
        sap_plant_code=plant,
        feature_run_id=1,
        forecast_run_id=1,
        inventory_run_id=1,
        oar_run_id=1,
        policy_id="p",
        policy_version=1,
        formula_version="f",
        status="READY_FOR_REVIEW",
        impact_status="X",
        is_oar=False,
        lead_time_days=None if lead_time is None else Decimal(lead_time),
        generated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
    )


def feature(material, plant="1300", *, demand_class="SMOOTH", lead_time="10", criticality=None, history="SUFFICIENT"):
    """A feature-store row: where a SMOOTH material-plant's lead time comes from."""
    return MaterialFeature(
        sap_material_number=material,
        sap_plant_code=plant,
        history_status=history,
        data_sufficiency="LIMITED",
        adi_status="OK",
        cv_squared_status="OK",
        oar_scope="OUT_OF_SCOPE",
        feature_run_id=1,
        demand_class=demand_class,
        lead_time_days=None if lead_time is None else Decimal(lead_time),
        criticality=criticality,
    )


def usage(material, plant="1300", *, days_ago=30, consumption="900", as_of=AS_OF):
    return StockoutRiskInput(
        sap_material_number=material,
        sap_plant_code=plant,
        as_of_date=as_of,
        last_issue_date=as_of - timedelta(days=days_ago),
        consumption_90d=Decimal(consumption),
        window_days=CONSUMPTION_WINDOW_DAYS,
    )


def stock(material, quantity, plant="1300", location="0001"):
    return StagedStock(
        sap_material_number=material,
        sap_plant_code=plant,
        storage_location=location,
        unrestricted_use_stock=None if quantity is None else Decimal(quantity),
        source_table="n_mard",
        staging_run_id=1,
    )


def counts(db, **where):
    db.commit()
    statement = select(Recommendation)
    for column, value in where.items():
        statement = statement.where(getattr(Recommendation, column) == value)
    return stockout_risk_counts(db, statement)


def seed_one(db, *, soh="99", consumption="900", lead_time="10", days_ago=30):
    """A Fast material needing 900 / 90 x 10 = 100 units to cover its lead time."""
    db.add_all([recommendation("M1", lead_time=lead_time), usage("M1", days_ago=days_ago, consumption=consumption)])
    if soh is not None:
        db.add(stock("M1", soh))


def test_stock_below_the_lead_time_requirement_is_at_risk(db):
    seed_one(db, soh="99")
    result = counts(db)
    assert (result.at_risk, result.evaluated, result.insufficient_data) == (1, 1, 0)
    assert result.as_of == AS_OF


def test_stock_exactly_at_the_requirement_is_not_at_risk(db):
    seed_one(db, soh="100")
    assert (counts(db).at_risk, counts(db).evaluated) == (0, 1)


def test_stock_above_the_requirement_is_not_at_risk(db):
    seed_one(db, soh="101")
    assert (counts(db).at_risk, counts(db).evaluated) == (0, 1)


def test_no_consumption_in_the_window_needs_no_stock_so_it_is_not_at_risk(db):
    seed_one(db, soh="0", consumption="0")  # zero stock, but issued earlier and nothing lately
    assert (counts(db).at_risk, counts(db).evaluated, counts(db).insufficient_data) == (0, 1, 0)


def test_stock_is_summed_over_storage_locations(db):
    db.add_all([recommendation("M1"), usage("M1"), stock("M1", "60", location="0001"), stock("M1", "50", location="0002")])
    assert (counts(db).at_risk, counts(db).evaluated) == (0, 1)  # 110 >= 100, though neither location is


def test_a_longer_lead_time_raises_the_requirement(db):
    seed_one(db, soh="150", lead_time="20")  # needs 200
    assert counts(db).at_risk == 1


@pytest.mark.parametrize("days_ago, judged", [(0, True), (365, True), (366, True), (730, True), (731, False)])
def test_only_fast_and_slow_moving_material_plants_are_judged(db, days_ago, judged):
    # 0-365 Fast, 366-730 Slow, more than 730 Non-moving.
    seed_one(db, soh="1", days_ago=days_ago)
    result = counts(db)
    assert result.evaluated == (1 if judged else 0)
    assert result.at_risk == (1 if judged else 0)
    assert result.insufficient_data == 0  # Non-moving is outside the KPI, not Insufficient Data


def test_a_missing_lead_time_is_insufficient_data_not_safe(db):
    seed_one(db, soh="0", lead_time=None)
    result = counts(db)
    assert (result.at_risk, result.evaluated, result.insufficient_data) == (0, 0, 1)


def test_missing_stock_is_insufficient_data_not_safe(db):
    seed_one(db, soh=None)
    result = counts(db)
    assert (result.at_risk, result.evaluated, result.insufficient_data) == (0, 0, 1)


def test_a_stock_row_with_no_quantity_is_insufficient_data(db):
    db.add_all([recommendation("M1"), usage("M1"), stock("M1", None)])
    result = counts(db)
    assert (result.evaluated, result.insufficient_data) == (0, 1)


def test_insufficient_data_is_only_counted_for_fast_and_slow_material_plants(db):
    seed_one(db, soh=None, lead_time=None, days_ago=900)  # Non-moving: outside the KPI altogether
    result = counts(db)
    assert (result.at_risk, result.evaluated, result.insufficient_data) == (0, 0, 0)


def test_never_issued_in_a_plant_whose_movements_were_loaded_is_non_moving(db):
    db.add_all([recommendation("M1"), usage("M1"), stock("M1", "500")])
    db.add_all([recommendation("NEVER"), stock("NEVER", "0")])  # same plant, no usage row
    result = counts(db)
    assert (result.evaluated, result.insufficient_data) == (1, 0)  # NEVER is left out, not flagged


def test_a_plant_with_no_movement_data_at_all_is_insufficient_data(db):
    db.add_all([recommendation("M1", plant="1300"), usage("M1", plant="1300"), stock("M1", "500")])
    db.add_all([recommendation("G1", plant="1500"), stock("G1", "0", plant="1500")])  # nothing loaded for 1500
    result = counts(db)
    assert (result.at_risk, result.evaluated, result.insufficient_data) == (0, 1, 1)


def test_the_percentage_denominator_leaves_out_insufficient_data(db):
    db.add_all(
        [
            recommendation("RISK"), usage("RISK"), stock("RISK", "10"),
            recommendation("SAFE"), usage("SAFE"), stock("SAFE", "1000"),
            recommendation("NODATA", lead_time=None), usage("NODATA"), stock("NODATA", "1"),
        ]
    )
    result = counts(db)
    assert (result.at_risk, result.evaluated, result.insufficient_data) == (1, 2, 1)


def test_the_counts_follow_the_filter_the_caller_applies(db):
    db.add_all(
        [
            recommendation("A", plant="1300"), usage("A", plant="1300"), stock("A", "1", plant="1300"),
            recommendation("B", plant="1500"), usage("B", plant="1500"), stock("B", "1", plant="1500"),
        ]
    )
    assert counts(db).at_risk == 2
    assert counts(db, sap_plant_code="1500").at_risk == 1


def test_it_reports_not_computed_not_zero_when_the_usage_table_is_empty(db):
    db.add_all([recommendation("M1"), stock("M1", "0")])
    result = counts(db)
    assert result.as_of is None
    assert (result.at_risk, result.evaluated, result.insufficient_data) == (0, 0, 0)


def test_the_label_date_is_the_one_the_table_was_built_with(db):
    built = date(2026, 6, 30)
    db.add_all([recommendation("M1"), usage("M1", as_of=built), stock("M1", "1")])
    assert counts(db).as_of == built


def test_the_band_is_measured_from_the_anchor_the_table_was_built_with(db):
    built = date(2026, 6, 30)
    db.add_all([recommendation("M1"), usage("M1", days_ago=731, as_of=built), stock("M1", "1")])
    assert counts(db).evaluated == 0


def test_a_database_without_the_usage_table_reads_as_not_computed_not_as_an_error():
    """A deploy does not run migrations by default. Until the table exists the card says "Not
    available"; the summary, and the Overview with it, must keep working."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (Recommendation, MaterialFeature, StagedStock):  # no StockoutRiskInput
        model.__table__.to_metadata(metadata).indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        result = stockout_risk_counts(session, select(Recommendation))
        assert (result.at_risk, result.evaluated, result.insufficient_data, result.as_of) == (0, 0, 0, None)
        assert session.execute(select(Recommendation)).all() == []  # the session is still usable


# --- SMOOTH material-plants: no recommendation, counted from the feature store ----------------


def test_a_smooth_material_plant_is_counted_without_any_recommendation(db):
    db.add_all([feature("S1"), usage("S1"), stock("S1", "99")])  # needs 100, has 99
    result = counts(db)
    assert (result.at_risk, result.evaluated, result.insufficient_data) == (1, 1, 0)


def test_a_smooth_material_plant_not_short_is_evaluated_but_not_at_risk(db):
    db.add_all([feature("S1"), usage("S1"), stock("S1", "100")])
    result = counts(db)
    assert (result.at_risk, result.evaluated) == (0, 1)


def test_a_smooth_material_plant_with_no_lead_time_is_insufficient_data(db):
    db.add_all([feature("S1", lead_time=None), usage("S1"), stock("S1", "0")])
    result = counts(db)
    assert (result.at_risk, result.evaluated, result.insufficient_data) == (0, 0, 1)


def test_smooth_and_other_material_plants_are_counted_together(db):
    db.add_all(
        [
            recommendation("R1"), usage("R1"), stock("R1", "10"),
            feature("S1"), usage("S1"), stock("S1", "10"),
        ]
    )
    result = counts(db)
    assert (result.at_risk, result.evaluated) == (2, 2)


def test_a_material_plant_with_a_recommendation_is_not_counted_twice(db):
    # Its latest recommendation (written before it was classified SMOOTH) is still in the list, and
    # it is SMOOTH in the feature store too: one material-plant, one count.
    db.add_all([recommendation("M1"), feature("M1"), usage("M1"), stock("M1", "10")])
    assert counts(db).evaluated == 1


def test_only_smooth_material_plants_with_enough_history_are_taken_from_the_feature_store(db):
    db.add_all(
        [
            feature("COLD", history="COLD_START"), usage("COLD"), stock("COLD", "1"),
            feature("ERR", demand_class="ERRATIC"), usage("ERR"), stock("ERR", "1"),
        ]
    )
    # ERRATIC is a recommended class: with no recommendation row it has nothing here either.
    assert (counts(db).evaluated, counts(db).insufficient_data) == (0, 0)


def test_the_plant_filter_narrows_the_smooth_material_plants_too(db):
    db.add_all(
        [
            feature("A", plant="1300"), usage("A", plant="1300"), stock("A", "1", plant="1300"),
            feature("B", plant="1500"), usage("B", plant="1500"), stock("B", "1", plant="1500"),
        ]
    )
    db.commit()
    both = stockout_risk_counts(db, select(Recommendation))
    only_1500 = stockout_risk_counts(db, select(Recommendation).where(Recommendation.sap_plant_code == "1500"), plant="1500")
    assert (both.at_risk, only_1500.at_risk) == (2, 1)


def test_a_filter_only_a_recommendation_has_leaves_no_smooth_material_plant(db):
    db.add_all([feature("S1"), usage("S1"), stock("S1", "1")])
    db.commit()
    base = select(Recommendation).where(Recommendation.status == "READY_FOR_REVIEW")
    assert stockout_risk_counts(db, base, status="READY_FOR_REVIEW").evaluated == 0
    assert stockout_risk_counts(db, select(Recommendation), is_oar=True).evaluated == 0
    assert stockout_risk_counts(db, select(Recommendation), confidence="HIGH").evaluated == 0


def test_the_smooth_demand_filter_keeps_them_and_another_class_drops_them(db):
    db.add_all([feature("S1"), usage("S1"), stock("S1", "1")])
    db.commit()
    assert stockout_risk_counts(db, select(Recommendation), demand_class="SMOOTH").evaluated == 1
    assert stockout_risk_counts(db, select(Recommendation), demand_class="LUMPY").evaluated == 0


# --- the setting --------------------------------------------------------------------


def test_the_anchor_defaults_to_the_last_month_with_normal_volume(monkeypatch):
    monkeypatch.delenv("I7_STOCKOUT_RISK_AS_OF", raising=False)
    assert Settings(_env_file=None).i7_stockout_risk_as_of == date(2026, 5, 31)


def test_the_anchor_can_be_changed(monkeypatch):
    monkeypatch.setenv("I7_STOCKOUT_RISK_AS_OF", "2026-06-30")
    assert Settings(_env_file=None).i7_stockout_risk_as_of == date(2026, 6, 30)


def test_a_blank_anchor_means_use_the_newest_issue_date(monkeypatch):
    monkeypatch.setenv("I7_STOCKOUT_RISK_AS_OF", "")
    assert Settings(_env_file=None).i7_stockout_risk_as_of is None
