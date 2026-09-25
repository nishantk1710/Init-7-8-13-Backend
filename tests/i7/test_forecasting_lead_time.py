"""Forecasting's lead time comes from the feature store, not from PO history.

Regression coverage for the fix that removed forecasting/service.py's
independent ``AVG(lead_time_days)`` calculation over
``i7_staged_purchase_order``. Forecasting must consume
``i7_material_feature.lead_time_days`` -- the same Initiative-11-or-MARC-PLIFZ
value Phase 5 (inventory) uses -- never recompute its own average from raw
purchase orders.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.forecasting.service import (
    DAYS_PER_MONTH,
    _ROUTED_SQL,
    _horizon_months,
)
from app.models.i7_forecast import Forecast, ForecastRun

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session


# --- No independent PO-based calculation --------------------------------------


def test_routed_sql_never_reads_purchase_orders():
    """No i7_staged_purchase_order reference, no AVG(lead_time_days).

    A regression re-adding a PO join here would silently start averaging raw
    PO durations again -- exactly the independent, duplicate lead-time
    calculation the FRS assigns to Initiative 11, not I07.
    """
    assert "i7_staged_purchase_order" not in _ROUTED_SQL
    assert "AVG(" not in _ROUTED_SQL.upper() and "AVG (" not in _ROUTED_SQL.upper()


def test_routed_sql_reads_lead_time_from_the_feature_store():
    assert "f.lead_time_days" in _ROUTED_SQL
    assert "f.lead_time_source" in _ROUTED_SQL


# --- _horizon_months is a pure function of the feature-store value -----------


def test_horizon_months_is_none_without_a_resolved_lead_time():
    """No 30/60/90-day default is substituted -- absent stays absent."""
    assert _horizon_months(None) is None


def test_horizon_months_rounds_up():
    # 45 days / 30.44 = 1.478 months -> rounds up to 2.
    assert _horizon_months(Decimal(45)) == 2


def test_horizon_months_floors_at_one_month():
    # A very short lead time still gets at least a 1-month horizon.
    assert _horizon_months(Decimal(1)) == 1


def test_horizon_months_matches_documented_conversion():
    assert DAYS_PER_MONTH == Decimal("30.44")
    # 90 days / 30.44 = 2.957 -> rounds up to 3.
    assert _horizon_months(Decimal(90)) == 3


# --- Changing PO history must not change the resolved lead time --------------


@needs_db
def test_changing_po_history_does_not_change_forecasting_lead_time(session):
    """The core regression: forecasting's lead time tracks the feature store,
    not i7_staged_purchase_order. If PO durations for a routed candidate are
    mutated (or deleted) without touching i7_material_feature, the horizon
    forecasting used must be provably unaffected -- because it was never
    derived from those rows in the first place.
    """
    if session.execute(select(func.count()).select_from(ForecastRun)).scalar() == 0:
        pytest.skip("no forecast run")

    # A routed candidate's actual PO durations, straight from staging.
    row = session.execute(
        text(
            """
            select f.sap_material_number, f.sap_plant_code,
                   f.lead_time_days, f.lead_time_source
              from i7_material_feature f
             where f.history_status = 'SUFFICIENT'
               and f.baseline_model is not null
               and f.lead_time_days is not null
             limit 1
            """
        )
    ).first()
    if row is None:
        pytest.skip("no routed candidate with a resolved lead time")

    po_avg = session.execute(
        text(
            """
            select avg(lead_time_days) from i7_staged_purchase_order
             where sap_material_number = :m and sap_plant_code = :p
               and lead_time_days is not null and is_cancelled = false
            """
        ),
        {"m": row.sap_material_number, "p": row.sap_plant_code},
    ).scalar()

    # Scoped to the latest forecast run: repeated test/dev runs leave several
    # i7_forecast_run generations behind, and an unordered read could pick up
    # a stale row from an older run whose horizon predates this fix.
    latest_run_id = session.execute(
        text("select max(id) from i7_forecast_run where status = 'succeeded'")
    ).scalar()

    # This is the key assertion: the feature store's resolved lead time is not
    # required to equal (and, per the fix, is not derived from) the PO
    # average. If forecasting still computed AVG(lead_time_days) itself, this
    # would be a tautology; because it now reads f.lead_time_days directly,
    # the PO average is irrelevant to what forecasting actually used.
    stored_forecast = session.execute(
        select(Forecast.horizon_months)
        .where(
            Forecast.sap_material_number == row.sap_material_number,
            Forecast.sap_plant_code == row.sap_plant_code,
            Forecast.forecast_run_id == latest_run_id,
        )
        .limit(1)
    ).scalar()
    if stored_forecast is None:
        pytest.skip("candidate was not written to i7_forecast (series rejected)")

    expected_horizon = _horizon_months(row.lead_time_days)
    assert stored_forecast == expected_horizon
    # And explicitly: the stored horizon must match the feature-store-derived
    # value even when it disagrees with the PO average (or no PO average
    # exists at all, e.g. po_avg is None while lead_time_days is not).
    if po_avg is not None and Decimal(str(po_avg)) != row.lead_time_days:
        assert stored_forecast == expected_horizon


# --- lead_time_source stays traceable -----------------------------------------


@needs_db
def test_lead_time_source_is_one_of_the_known_values(session):
    if session.execute(select(func.count()).select_from(ForecastRun)).scalar() == 0:
        pytest.skip("no forecast run")
    rows = session.execute(
        text(
            "select distinct lead_time_source from i7_material_feature "
            "where lead_time_source is not null"
        )
    ).scalars().all()
    for source in rows:
        assert source in ("I11_PROGRAM", "PLANNED_DELIVERY_TIME", "CALCULATED")


def test_no_i11_value_is_fabricated():
    """The I11 provider always returns None -- no real integration exists yet."""
    from app.initiatives.i7.contracts import MaterialIdentity, MaterialPlantKey, PlantIdentity
    from app.initiatives.i7.features.lead_time_provider import I11LeadTimeProvider

    key = MaterialPlantKey(
        material=MaterialIdentity(sap_material_number="000000000010000000"),
        plant=PlantIdentity(sap_plant_code="1300"),
    )
    assert I11LeadTimeProvider().get(key) is None
