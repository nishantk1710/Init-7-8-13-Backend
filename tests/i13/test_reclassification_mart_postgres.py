"""W6.5 integration tests: the persisted reclassification-evidence mart
against real Postgres data -- proving refresh is idempotent and free of
duplicate material+plant rows.

Skipped outright when no ``DATABASE_URL`` is configured (same pattern as
the other Postgres-gated I13 test files).
"""

from datetime import date

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i13.config import build_i13_config
from app.initiatives.i13.reclassification_mart import (
    get_reclassification_candidate,
    refresh_reclassification_mart,
)
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.models.i13_reclassification import ReclassificationCandidateMart
from tests.i13.conftest import FakeCriticalitySource, FakeHodJustificationProvider

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


def _refresh(session, *, material: str | None = None, plant: str | None = None):
    config = build_i13_config(get_settings())
    movement_repo = PostgresMovementRepository(session)
    scope_index = fetch_material_scope_index(session, material=material, plant=plant)
    return refresh_reclassification_mart(
        session,
        movement_repo,
        scope_index,
        config,
        material=material,
        plant=plant,
        as_of=date.today(),
        criticality_source=FakeCriticalitySource(),
        hod_provider=FakeHodJustificationProvider(),
    )


@needs_db
@pytest.mark.needs_seed_data
def test_refresh_is_idempotent_across_two_consecutive_runs() -> None:
    with get_sessionmaker()() as session:
        first = _refresh(session, plant="1300")
        session.commit()
        rows_after_first = session.execute(
            select(func.count()).select_from(ReclassificationCandidateMart).where(
                ReclassificationCandidateMart.plant == "1300"
            )
        ).scalar_one()

        second = _refresh(session, plant="1300")
        session.commit()
        rows_after_second = session.execute(
            select(func.count()).select_from(ReclassificationCandidateMart).where(
                ReclassificationCandidateMart.plant == "1300"
            )
        ).scalar_one()

    assert len(first) > 0
    assert len(first) == len(second) == rows_after_first == rows_after_second


@needs_db
@pytest.mark.needs_seed_data
def test_mart_has_no_duplicate_material_plant_rows() -> None:
    with get_sessionmaker()() as session:
        _refresh(session, plant="1300")
        session.commit()
        duplicates = session.execute(
            select(
                ReclassificationCandidateMart.material, ReclassificationCandidateMart.plant, func.count()
            )
            .where(ReclassificationCandidateMart.plant == "1300")
            .group_by(ReclassificationCandidateMart.material, ReclassificationCandidateMart.plant)
            .having(func.count() > 1)
        ).all()
    assert duplicates == []


@needs_db
@pytest.mark.needs_seed_data
def test_partial_refresh_does_not_touch_other_plants_rows() -> None:
    """A plant-scoped refresh's DELETE is itself plant-scoped -- it must not
    clear rows a previous refresh wrote for a different plant."""
    from sqlalchemy import text

    with get_sessionmaker()() as session:
        other_plant = session.execute(
            text("SELECT DISTINCT TOP 1 plant FROM raw_marc WHERE mrp_type IN ('ND', 'PD') AND plant <> '1300'")
        ).scalar_one_or_none()
        if other_plant is None:
            pytest.skip("no second real OAR plant available in this dataset")

        _refresh(session, plant=other_plant)
        session.commit()
        before = session.execute(
            select(func.count()).select_from(ReclassificationCandidateMart).where(
                ReclassificationCandidateMart.plant == other_plant
            )
        ).scalar_one()

        _refresh(session, plant="1300")
        session.commit()
        after = session.execute(
            select(func.count()).select_from(ReclassificationCandidateMart).where(
                ReclassificationCandidateMart.plant == other_plant
            )
        ).scalar_one()

    assert before > 0
    assert after == before


@needs_db
@pytest.mark.needs_seed_data
def test_get_reclassification_candidate_reads_a_persisted_row() -> None:
    with get_sessionmaker()() as session:
        candidates = _refresh(session, plant="1300")
        session.commit()
        assert candidates, "expected at least one OAR candidate row at plant 1300"
        sample = candidates[0]

        row = get_reclassification_candidate(session, sample.material, sample.plant)
    assert row is not None
    assert row.material == sample.material
    assert row.plant == sample.plant
    assert row.candidate_flag == sample.candidate_flag
