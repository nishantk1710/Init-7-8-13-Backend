"""Working Capital Impact in ZAR: MBEW's price and WAERS, ZAR and NAD (1:1) only, one calculation for the Overview and the report.

SQLite with only the tables the calculation reads, so it runs without a database.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.kpi_formulas.resolved import load_active
from app.initiatives.i7.kpi_formulas.working_capital import REPORTING_CURRENCY, working_capital_impact
from app.initiatives.i7.reporting import kpi_definitions
from app.initiatives.i7.stockout_risk.counts import StockoutRiskCounts
from app.models.i7_features import MaterialFeature
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun
from app.models.i7_stockout_risk import StockoutRiskInput

TABLES = (Recommendation, StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun, StockoutRiskInput, MaterialFeature)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(
        kpi_definitions, "stockout_risk_counts",
        lambda session, base, **kw: StockoutRiskCounts(at_risk=0, evaluated=0, insufficient_data=0, as_of=None),
    )
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in TABLES:
        model.__table__.to_metadata(metadata).indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def add(session, material, *, currency="ZAR", price="100", current="1", recommended="2", staged=True):
    """One material-plant: its recommendation, and (unless ``staged`` is False) the staged price row that states the currency."""
    session.add(
        Recommendation(
            recommendation_id=f"REC-{material}", sap_material_number=material, sap_plant_code="1300",
            feature_run_id=1, forecast_run_id=1, inventory_run_id=1, oar_run_id=1, policy_id="p", policy_version=1,
            formula_version="f", status="READY_FOR_REVIEW", impact_status="AVAILABLE", is_oar=False,
            unit_price=Decimal(price) if price is not None else None,
            current_rop=Decimal(current) if current is not None else None,
            recommended_rop=Decimal(recommended) if recommended is not None else None,
            generated_at=datetime(2026, 10, 14, tzinfo=timezone.utc), updated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
            chain_index=0, adjustment_count=0, current_version=1,
        )
    )
    if staged:
        session.add(
            StagedMaterialPlant(
                sap_material_number=material, sap_plant_code="1300",
                unit_price=Decimal(price) if price is not None else None, currency=currency,
                staging_run_id=1, source_table="n_mbew",
            )
        )
    session.commit()


def impact(session):
    return working_capital_impact(session, select(Recommendation), load_active(session))


def test_zar_counts_as_it_is(db):
    add(db, "A", currency="ZAR", price="100", current="5", recommended="2")  # 100 x (5 - 2)

    result = impact(db)

    assert result.amount == Decimal("300")
    assert result.currency == REPORTING_CURRENCY == "ZAR"
    assert (result.included_count, result.excluded_count) == (1, 0)


def test_nad_counts_one_for_one_with_zar(db):
    add(db, "A", currency="ZAR", price="100", current="5", recommended="2")
    add(db, "B", currency="NAD", price="10", current="5", recommended="2")  # 10 NAD x 3 = 30 ZAR

    result = impact(db)

    assert result.amount == Decimal("330")
    assert (result.included_count, result.excluded_count) == (2, 0)


@pytest.mark.parametrize("currency", ["USD", "EUR", "AUD"])
def test_another_currency_is_left_out_not_converted(db, currency):
    add(db, "A", currency="ZAR", price="100", current="5", recommended="2")
    add(db, "B", currency=currency, price="10", current="5", recommended="2")

    result = impact(db)

    assert result.amount == Decimal("300")
    assert (result.included_count, result.excluded_count) == (1, 1)


def test_a_price_with_no_stated_currency_is_left_out(db):
    add(db, "A", currency=None, price="100")

    result = impact(db)

    assert result.amount is None  # nothing included: not 0
    assert (result.included_count, result.excluded_count) == (0, 1)


def test_a_material_plant_with_no_staged_price_row_is_left_out(db):
    add(db, "A", staged=False)

    result = impact(db)

    assert result.amount is None
    assert (result.included_count, result.excluded_count) == (0, 1)


@pytest.mark.parametrize("price", [None, "0", "0.01", "1.00"])
def test_a_missing_or_placeholder_price_is_left_out(db, price):
    add(db, "A", currency="ZAR", price=price)

    result = impact(db)

    assert result.amount is None
    assert (result.included_count, result.excluded_count) == (0, 1)


def test_a_price_just_above_the_placeholder_floor_counts(db):
    add(db, "A", currency="ZAR", price="1.01", current="3", recommended="1")

    assert impact(db).amount == Decimal("2.02")


@pytest.mark.parametrize("missing", ["current", "recommended"])
def test_a_missing_reorder_point_is_left_out(db, missing):
    add(db, "A", currency="ZAR", **{missing: None})

    result = impact(db)

    assert result.amount is None
    assert (result.included_count, result.excluded_count) == (0, 1)


def test_a_higher_reorder_point_ties_cash_up_and_a_lower_one_releases_it(db):
    add(db, "UP", currency="ZAR", price="100", current="1", recommended="2")  # -100: one more unit on the shelf
    assert impact(db).amount == Decimal("-100")

    add(db, "DOWN", currency="ZAR", price="100", current="5", recommended="2")  # +300: three fewer units
    assert impact(db).amount == Decimal("200")


def test_an_unchanged_reorder_point_is_a_real_zero(db):
    add(db, "A", currency="ZAR", price="100", current="3", recommended="3")

    result = impact(db)

    assert result.amount == 0 and result.amount is not None
    assert result.included_count == 1


def test_the_counts_cover_the_whole_population(db):
    add(db, "ZAR1", currency="ZAR")
    add(db, "NAD1", currency="NAD")
    add(db, "USD1", currency="USD")
    add(db, "NONE1", currency=None)
    add(db, "BADPRICE", currency="ZAR", price="1.00")

    result = impact(db)

    assert (result.included_count, result.excluded_count, result.population) == (2, 3, 5)


def test_the_report_and_the_overview_calculation_agree(db):
    """The Quarterly Report's position and the shared calculation the Overview uses are one figure."""
    add(db, "A", currency="ZAR", price="100", current="5", recommended="2")
    add(db, "B", currency="NAD", price="10", current="1", recommended="4")
    add(db, "C", currency="USD", price="999", current="1", recommended="9")

    shared = impact(db)
    position = kpi_definitions.position_figures(db, None, date(2027, 1, 1), date(2026, 10, 21))

    assert position.net_rop_value_impact == shared.amount == Decimal("270")  # 300 + (-30)
    assert position.priced_count == shared.included_count == 2
    assert position.working_capital_excluded_count == shared.excluded_count == 1
    assert position.working_capital_currency == "ZAR"
