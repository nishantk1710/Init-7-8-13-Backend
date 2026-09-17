"""Forecast persistence, checked against a real run.

Real Postgres or skip. These assert the properties that matter for Phase 5 and
for the audit trail: no fabricated rate, no adoption without evidence, and a
model version on every row.
"""

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.forecasting.types import (
    AdoptionStatus,
    BacktestStatus,
    ModelName,
    ModelStatus,
)
from app.models.i7_forecast import Forecast, ForecastRun, SegmentModelDecision

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session


def _run(session) -> bool:
    return session.execute(select(func.count()).select_from(Forecast)).scalar() > 0


# --- Rates are never fabricated ----------------------------------------------


@needs_db
def test_a_rate_exists_only_where_the_model_succeeded(session):
    """An unavailable forecast is absent, never zero -- zero is a prediction."""
    if not _run(session):
        pytest.skip("no forecast run")
    wrong = session.execute(
        select(func.count())
        .select_from(Forecast)
        .where(
            Forecast.forecast_status != ModelStatus.SUCCESS.value,
            Forecast.forecast_rate.isnot(None),
        )
    ).scalar()
    assert wrong == 0


@needs_db
def test_successful_forecasts_always_carry_a_rate(session):
    if not _run(session):
        pytest.skip("no forecast run")
    missing = session.execute(
        select(func.count())
        .select_from(Forecast)
        .where(
            Forecast.forecast_status == ModelStatus.SUCCESS.value,
            Forecast.forecast_rate.is_(None),
        )
    ).scalar()
    assert missing == 0


@needs_db
def test_no_negative_demand_rate(session):
    if not _run(session):
        pytest.skip("no forecast run")
    negative = session.execute(
        select(func.count()).select_from(Forecast).where(Forecast.forecast_rate < 0)
    ).scalar()
    assert negative == 0


# --- Blocked models report why -------------------------------------------------


@needs_db
def test_lightgbm_is_blocked_on_the_unsigned_service_level(session):
    """A quantile model cannot run without a target quantile, and the quantile
    is the service level."""
    if not _run(session):
        pytest.skip("no forecast run")
    statuses = session.execute(
        select(Forecast.forecast_status)
        .where(Forecast.model_name == ModelName.LIGHTGBM.value)
        .distinct()
    ).scalars().all()
    assert ModelStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET.value in statuses


@needs_db
def test_tsb_is_blocked_on_the_unset_obsolescence_trigger(session):
    if not _run(session):
        pytest.skip("no forecast run")
    statuses = session.execute(
        select(Forecast.forecast_status)
        .where(Forecast.model_name == ModelName.TSB.value)
        .distinct()
    ).scalars().all()
    assert ModelStatus.NOT_EVALUABLE_TRIGGER_UNSET.value in statuses


@needs_db
def test_missing_lead_time_is_reported_not_defaulted(session):
    """No 30/60/90-day substitute: the fallback policy is unresolved."""
    if not _run(session):
        pytest.skip("no forecast run")
    rows = session.execute(
        select(func.count())
        .select_from(Forecast)
        .where(
            Forecast.backtest_status
            == BacktestStatus.NOT_EVALUABLE_LEAD_TIME_UNAVAILABLE.value
        )
    ).scalar()
    assert rows > 0
    # Such a row must not carry a horizon it could not have known.
    wrong = session.execute(
        select(func.count())
        .select_from(Forecast)
        .where(
            Forecast.backtest_status
            == BacktestStatus.NOT_EVALUABLE_LEAD_TIME_UNAVAILABLE.value,
            Forecast.horizon_months.isnot(None),
        )
    ).scalar()
    assert wrong == 0


# --- Evidence ---------------------------------------------------------------------


@needs_db
def test_no_backtest_reaches_the_required_origins(session):
    """A 13-month window yields at most 10 origins. If this ever fails, the
    extract grew -- which would be good news worth noticing."""
    if not _run(session):
        pytest.skip("no forecast run")
    complete = session.execute(
        select(func.count())
        .select_from(Forecast)
        .where(Forecast.backtest_status == BacktestStatus.COMPLETE.value)
    ).scalar()
    assert complete == 0


@needs_db
def test_required_origins_recorded_as_twelve(session):
    if not _run(session):
        pytest.skip("no forecast run")
    values = session.execute(
        select(Forecast.required_origins).where(Forecast.origins_evaluated > 0).distinct()
    ).scalars().all()
    assert values == [12]


@needs_db
def test_origins_evaluated_never_exceeds_available(session):
    if not _run(session):
        pytest.skip("no forecast run")
    wrong = session.execute(
        select(func.count())
        .select_from(Forecast)
        .where(Forecast.origins_evaluated > Forecast.available_origins)
    ).scalar()
    assert wrong == 0


# --- Segment decisions ---------------------------------------------------------------


@needs_db
def test_no_challenger_is_adopted_without_sufficient_origins(session):
    """The central honesty property of Phase 4."""
    if not _run(session):
        pytest.skip("no forecast run")
    adopted = session.execute(
        select(SegmentModelDecision).where(
            SegmentModelDecision.adoption_status == AdoptionStatus.CHALLENGER_ELIGIBLE.value
        )
    ).scalars().all()
    for decision in adopted:
        assert decision.origins_evaluated >= decision.required_origins


@needs_db
def test_every_segment_decision_states_a_reason(session):
    if not _run(session):
        pytest.skip("no forecast run")
    decisions = session.execute(select(SegmentModelDecision)).scalars().all()
    assert decisions
    for decision in decisions:
        assert decision.decision_reason


@needs_db
def test_decisions_are_per_material_plant_not_per_segment(session):
    """FRS Section 3.1 / FR-3: model selection is per material by backtest.

    ``segment_key`` must hold ``"{material}/{plant}"``, one row per
    material-plant per run -- never a demand-class name pooling many
    material-plants into a single decision.

    Scoped to the latest run: earlier runs in this shared dev database
    predate the per-material-plant fix and legitimately still hold the old
    per-segment-class keys, which is history, not a regression."""
    if not _run(session):
        pytest.skip("no forecast run")
    latest_run_id = session.execute(select(func.max(ForecastRun.id))).scalar()
    keys = session.execute(
        select(SegmentModelDecision.segment_key).where(
            SegmentModelDecision.forecast_run_id == latest_run_id
        )
    ).scalars().all()
    assert keys
    assert not set(keys) & {"SMOOTH", "ERRATIC", "INTERMITTENT", "LUMPY"}
    assert all(key.count("/") == 1 for key in keys)


@needs_db
def test_exactly_one_champion_per_material_plant(session):
    """``is_champion`` is the flag inventory/recommendations actually read;
    exactly one model per material-plant per run must carry it."""
    if not _run(session):
        pytest.skip("no forecast run")
    rows = session.execute(
        select(
            Forecast.forecast_run_id,
            Forecast.sap_material_number,
            Forecast.sap_plant_code,
            func.count(),
        )
        .where(Forecast.is_champion.is_(True))
        .group_by(
            Forecast.forecast_run_id, Forecast.sap_material_number, Forecast.sap_plant_code
        )
    ).all()
    assert rows
    for _, _, _, count in rows:
        assert count == 1


# --- Provenance -------------------------------------------------------------------------


@needs_db
def test_every_forecast_carries_a_model_version(session):
    if not _run(session):
        pytest.skip("no forecast run")
    missing = session.execute(
        select(func.count()).select_from(Forecast).where(Forecast.model_version.is_(None))
    ).scalar()
    assert missing == 0


@needs_db
def test_model_versions_are_implementation_ids_not_semantic(session):
    """No invented ``v1.3``: nothing here maintains a release contract."""
    if not _run(session):
        pytest.skip("no forecast run")
    versions = session.execute(select(Forecast.model_version).distinct()).scalars().all()
    for version in versions:
        assert not version.startswith("v")


@needs_db
def test_run_records_the_policy_and_quantile(session):
    if not _run(session):
        pytest.skip("no forecast run")
    run = session.execute(
        select(ForecastRun).order_by(ForecastRun.id.desc()).limit(1)
    ).scalar_one()
    assert run.policy_id and run.policy_version >= 1
    # NULL because the service-level matrix is unsigned.
    assert run.target_quantile is None


@needs_db
def test_no_inventory_parameter_is_stored(session):
    """Phase 4 forecasts demand. Safety stock, ROP and maximum are Phase 5."""
    columns = {column.name for column in Forecast.__table__.columns}
    for forbidden in ("safety_stock", "reorder_point", "maximum_stock", "rop", "eoq"):
        assert forbidden not in columns
