"""The consumption history in a recommendation's detail is the series the models see.

Dense (a month with no movement is a zero), floored at zero (demand is never
negative), and the latest months only. It used to be the staged rows as they
were: only the months that had movements, with a month netted below zero shown
as negative consumption -- which drew a curve between two distant points.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.i7 import recommendations as api
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedConsumption, StagedMaterial, StagedMaterialPlant


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (Recommendation, StagedConsumption, StagedMaterial, StagedMaterialPlant):
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def consumption(material, period, quantity, plant="1300"):
    return StagedConsumption(
        sap_material_number=material, sap_plant_code=plant, period=period, quantity=Decimal(quantity),
        unit_of_measure="EA", staging_run_id=1, source_table="n_mseg",
    )


def recommendation(material, plant="1300"):
    return Recommendation(
        recommendation_id=f"REC-{material}-{plant}", sap_material_number=material, sap_plant_code=plant,
        feature_run_id=1, forecast_run_id=1, inventory_run_id=1, oar_run_id=1, policy_id="p",
        policy_version=1, formula_version="f", status="READY_FOR_REVIEW", impact_status="X",
        generated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
    )


def history(db, material="M1"):
    return api.get_recommendation(f"REC-{material}-1300", db).demand.consumption_history


def seed_window(db):
    """M1 moves in Aug (+1000), Sep (returns outweigh issues: -1000), Oct (+500); another
    material makes the extract's window run Jan to Dec 2025."""
    db.add_all([
        recommendation("M1"),
        consumption("M1", date(2025, 8, 1), "1000"),
        consumption("M1", date(2025, 9, 1), "-1000"),
        consumption("M1", date(2025, 10, 1), "500"),
        consumption("OTHER", date(2025, 1, 1), "1"),
        consumption("OTHER", date(2025, 12, 1), "1"),
    ])
    db.commit()


def test_every_month_of_the_window_is_present_with_zeros_for_months_without_movement(db):
    seed_window(db)
    entries = history(db)
    assert [e.period for e in entries] == [date(2025, m, 1) for m in range(1, 13)]
    by_month = {e.period.month: e.quantity for e in entries}
    assert by_month[1] == 0 and by_month[7] == 0 and by_month[11] == 0 and by_month[12] == 0


def test_a_month_with_more_returns_than_issues_is_zero_not_negative(db):
    seed_window(db)
    by_month = {e.period.month: e.quantity for e in history(db)}
    assert by_month[8] == 1000 and by_month[9] == 0 and by_month[10] == 500
    assert all(e.quantity >= 0 for e in history(db))


def test_only_the_latest_24_months_are_returned(db):
    db.add(recommendation("M1"))
    for index in range(36):  # three years of monthly movements
        year, month = 2023 + index // 12, index % 12 + 1
        db.add(consumption("M1", date(year, month, 1), "10"))
    db.commit()
    entries = history(db)
    assert len(entries) == 24
    assert entries[0].period == date(2024, 1, 1) and entries[-1].period == date(2025, 12, 1)


def test_a_material_with_no_staged_consumption_gets_an_empty_history_not_a_guess(db):
    db.add_all([recommendation("M1"), consumption("OTHER", date(2025, 1, 1), "1")])
    db.commit()
    assert history(db) == ()


# --- Sign convention ---------------------------------------------------------
# Staging nets issues (+) against reversals (-) per month. A reversal that posts
# after the issue it cancels leaves a later month negative; demand is not
# negative, so every view of the series -- the models' and the chart's -- floors
# that month at zero. The service must agree with the feature builder, not just
# with itself.


def test_the_service_agrees_with_the_feature_builders_densification(db):
    from app.initiatives.i7.adapters.repository import consumption_series_for

    seed_window(db)
    series = consumption_series_for(db, "M1", "1300")
    assert [(o.period, o.quantity) for o in series.observations] == [
        (e.period, e.quantity) for e in history(db)
    ]


def test_a_net_negative_month_between_normal_months_does_not_dip_below_zero(db):
    db.add(recommendation("M1"))
    for month, quantity in ((1, "40"), (2, "-15"), (3, "25"), (4, "-0.5")):
        db.add(consumption("M1", date(2025, month, 1), quantity))
    db.commit()
    assert [(e.period.month, e.quantity) for e in history(db)] == [(1, 40), (2, 0), (3, 25), (4, 0)]


def test_the_service_returns_plain_month_quantity_pairs_oldest_first(db):
    from app.initiatives.i7.recommendations.consumption_history import monthly_consumption_history

    seed_window(db)
    pairs = monthly_consumption_history(db, "M1", "1300", months=3)
    assert pairs == ((date(2025, 10, 1), Decimal("500")), (date(2025, 11, 1), Decimal("0")),
                     (date(2025, 12, 1), Decimal("0")))


def test_another_plants_movements_are_not_mixed_in(db):
    seed_window(db)
    db.add(consumption("M1", date(2025, 8, 1), "999", plant="1500"))
    db.commit()
    assert {e.period.month: e.quantity for e in history(db)}[8] == 1000
