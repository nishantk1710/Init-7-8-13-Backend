"""W6.3 integration tests: the persisted WATCH utilisation mart against real
Postgres data -- proving the refresh is idempotent, OAR-scoped by default,
and free of the specific data-quality defects the FRS calls out (negative
unissued quantity, duplicate material+plant rows).

Skipped outright when no ``DATABASE_URL`` is configured (same pattern as the
other Postgres-gated I13 test files).
"""

from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i13.config import build_i13_config
from app.initiatives.i13.models import MaterialScope
from app.initiatives.i13.watch_mart import refresh_watch_metrics_mart
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.models.i13_watch_mart import WatchMetricMart

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


def _refresh(session, *, material: str | None = None, plant: str | None = None, oar_only: bool = True):
    config = build_i13_config(get_settings())
    data_dir = Path(get_settings().i13_data_dir)
    movement_repo = PostgresMovementRepository(session)
    procurement_repo = PostgresProcurementRepository(session)
    reservation_repo = PostgresReservationRepository(session)
    scope_index = fetch_material_scope_index(session, material=material, plant=plant)
    result = refresh_watch_metrics_mart(
        session,
        movement_repo,
        procurement_repo,
        reservation_repo,
        scope_index,
        config,
        data_dir,
        material=material,
        plant=plant,
        oar_only=oar_only,
    )
    session.commit()
    return result


@needs_db
@pytest.mark.needs_seed_data
def test_refresh_is_idempotent_across_two_consecutive_runs() -> None:
    with get_sessionmaker()() as session:
        first = _refresh(session, plant="1300")
        rows_after_first = session.execute(
            select(func.count()).select_from(WatchMetricMart).where(WatchMetricMart.plant == "1300")
        ).scalar_one()

        second = _refresh(session, plant="1300")
        rows_after_second = session.execute(
            select(func.count()).select_from(WatchMetricMart).where(WatchMetricMart.plant == "1300")
        ).scalar_one()

    assert first.row_count > 0
    assert first.row_count == second.row_count == rows_after_first == rows_after_second


@needs_db
@pytest.mark.needs_seed_data
def test_refresh_defaults_to_oar_only() -> None:
    with get_sessionmaker()() as session:
        _refresh(session, plant="1300")
        scopes = session.execute(
            select(WatchMetricMart.material_scope).where(WatchMetricMart.plant == "1300").distinct()
        ).scalars().all()
    assert scopes
    assert set(scopes) == {MaterialScope.OAR.value}


@needs_db
@pytest.mark.needs_seed_data
def test_mart_has_no_duplicate_material_plant_rows() -> None:
    with get_sessionmaker()() as session:
        _refresh(session, plant="1300")
        duplicates = session.execute(
            select(WatchMetricMart.material, WatchMetricMart.plant, func.count())
            .where(WatchMetricMart.plant == "1300")
            .group_by(WatchMetricMart.material, WatchMetricMart.plant)
            .having(func.count() > 1)
        ).all()
    assert duplicates == []


@needs_db
@pytest.mark.needs_seed_data
def test_mart_never_has_negative_unissued_quantity() -> None:
    with get_sessionmaker()() as session:
        _refresh(session, plant="1300")
        negative = session.execute(
            select(func.count())
            .select_from(WatchMetricMart)
            .where(WatchMetricMart.plant == "1300", WatchMetricMart.gr_not_issued_outstanding_quantity < 0)
        ).scalar_one()
    assert negative == 0


@needs_db
@pytest.mark.needs_seed_data
def test_partial_refresh_does_not_touch_other_plants_rows() -> None:
    """A plant-scoped refresh's ``DELETE`` is itself plant-scoped -- it must
    not clear rows a previous refresh wrote for a different plant."""
    with get_sessionmaker()() as session:
        other_plant = session.execute(
            text("SELECT DISTINCT TOP 1 plant FROM raw_marc WHERE mrp_type IN ('ND', 'PD') AND plant <> '1300'")
        ).scalar_one_or_none()
        if other_plant is None:
            pytest.skip("no second real OAR plant available in this dataset")

        _refresh(session, plant=other_plant)
        before = session.execute(
            select(func.count()).select_from(WatchMetricMart).where(WatchMetricMart.plant == other_plant)
        ).scalar_one()

        _refresh(session, plant="1300")
        after = session.execute(
            select(func.count()).select_from(WatchMetricMart).where(WatchMetricMart.plant == other_plant)
        ).scalar_one()

    assert before > 0
    assert after == before
