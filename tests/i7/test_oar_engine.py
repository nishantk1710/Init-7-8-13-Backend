"""OAR engine against the real extract.

Real Postgres or skip. These check the properties that matter operationally:
the candidate population is exactly Phase 3's classified materials, no
inventory value is invented, hard constraints are never relaxed to manufacture
neighbours, and repeated runs are idempotent.
"""

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.oar.types import EstimateStatus, OarConfidence, OarStatus
from app.models.i7_oar import OarNeighbour, OarRun, OarTargetResult

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session


def _ran(session) -> bool:
    return session.execute(select(func.count()).select_from(OarTargetResult)).scalar() > 0


# --- Feature store is the source, not staging, for unit_price -----------------


def test_candidate_queries_read_unit_price_from_the_feature_store_only():
    """No i7_staged_material.unit_price re-read here.

    A regression re-adding ``m.unit_price`` would silently start reading price
    from staging again even though it is already on i7_material_feature.
    """
    from app.initiatives.i7.oar import repository as oar_repository

    for sql in (oar_repository._TARGET_SQL, oar_repository._CANDIDATE_SQL):
        assert "m.unit_price" not in sql
        assert "f.unit_price" in sql


# --- Population -----------------------------------------------------------------


@needs_db
def test_every_cold_start_material_plant_has_a_result(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    latest = session.execute(
        select(OarRun.id).order_by(OarRun.id.desc()).limit(1)
    ).scalar()
    results = session.execute(
        select(func.count())
        .select_from(OarTargetResult)
        .where(OarTargetResult.oar_run_id == latest)
    ).scalar()
    cold_start = session.execute(
        text("select count(*) from i7_material_feature where history_status <> 'SUFFICIENT'")
    ).scalar()
    assert results == cold_start


@needs_db
def test_no_suffient_material_is_treated_as_a_target(session):
    """SUFFICIENT materials belong to Phase 4's forecasting path, not here."""
    if not _ran(session):
        pytest.skip("no OAR run")
    wrong = session.execute(
        text(
            """select count(*) from i7_oar_target t
                 join i7_material_feature f
                   on f.sap_material_number = t.sap_material_number
                  and f.sap_plant_code = t.sap_plant_code
                where f.history_status = 'SUFFICIENT'"""
        )
    ).scalar()
    assert wrong == 0


# --- Hard constraints are never relaxed -----------------------------------------


@needs_db
def test_every_neighbour_shares_the_targets_criticality(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    mismatched = session.execute(
        text(
            """select count(*) from i7_oar_neighbour n
                 join i7_material_feature target
                   on target.sap_material_number = n.sap_material_number
                  and target.sap_plant_code = n.sap_plant_code
                 join i7_material_feature donor
                   on donor.sap_material_number = n.neighbour_material
                  and donor.sap_plant_code = n.neighbour_plant
                where target.criticality is distinct from donor.criticality"""
        )
    ).scalar()
    assert mismatched == 0


@needs_db
def test_every_neighbour_has_at_least_twelve_months_history(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    short = session.execute(
        select(func.count())
        .select_from(OarNeighbour)
        .where(OarNeighbour.history_months < 12)
    ).scalar()
    assert short == 0


@needs_db
def test_no_neighbour_is_inactive(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    inactive = session.execute(
        select(func.count()).select_from(OarNeighbour).where(~OarNeighbour.is_active)
    ).scalar()
    assert inactive == 0


@needs_db
def test_no_neighbour_is_unknown_active_status(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    unknown = session.execute(
        select(func.count()).select_from(OarNeighbour).where(OarNeighbour.is_active.is_(None))
    ).scalar()
    assert unknown == 0


# --- No fabrication -----------------------------------------------------------------


@needs_db
def test_no_inventory_value_is_invented(session):
    """The service-level matrix is unsigned, so nothing succeeded in Phase 5;
    every neighbour must therefore be ineligible to lend a value."""
    if not _ran(session):
        pytest.skip("no OAR run")
    lending = session.execute(
        select(func.count()).select_from(OarNeighbour).where(
            OarNeighbour.inventory_eligible
        )
    ).scalar()
    assert lending == 0


@needs_db
def test_no_estimate_succeeds_while_service_level_is_unsigned(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    succeeded = session.execute(
        select(func.count())
        .select_from(OarTargetResult)
        .where(OarTargetResult.estimate_status == EstimateStatus.SUCCESS.value)
    ).scalar()
    assert succeeded == 0


@needs_db
def test_targets_with_no_neighbours_have_the_no_neighbours_estimate_status(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    wrong = session.execute(
        select(func.count())
        .select_from(OarTargetResult)
        .where(
            OarTargetResult.neighbour_count == 0,
            OarTargetResult.estimate_status != EstimateStatus.NOT_EVALUABLE_NO_NEIGHBORS.value,
        )
    ).scalar()
    assert wrong == 0


@needs_db
def test_no_target_reaches_high_confidence(session):
    """HIGH needs >= 5 neighbours in the same circuit; no circuit data exists
    in this extract, so HIGH is unreachable -- exactly as it should be."""
    if not _ran(session):
        pytest.skip("no OAR run")
    high = session.execute(
        select(func.count())
        .select_from(OarTargetResult)
        .where(OarTargetResult.confidence == OarConfidence.HIGH.value)
    ).scalar()
    assert high == 0


# --- Structural properties ---------------------------------------------------------


@needs_db
def test_no_duplicate_target_rows(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    duplicates = session.execute(
        text(
            """select count(*) from (
                 select oar_run_id, sap_material_number, sap_plant_code
                   from i7_oar_target group by 1,2,3 having count(*) > 1) d"""
        )
    ).scalar()
    assert duplicates == 0


@needs_db
def test_no_duplicate_neighbours(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    duplicates = session.execute(
        text(
            """select count(*) from (
                 select oar_run_id, sap_material_number, sap_plant_code,
                        neighbour_material, neighbour_plant
                   from i7_oar_neighbour group by 1,2,3,4,5 having count(*) > 1) d"""
        )
    ).scalar()
    assert duplicates == 0


@needs_db
def test_neighbour_ranks_are_dense_and_start_at_one(session):
    """Scoped to one run: several runs may coexist (the suite legitimately
    creates more than one OAR run over time), and pooling ranks across runs
    for the same material-plant would make each run's own 1..N restart appear
    as a broken sequence."""
    if not _ran(session):
        pytest.skip("no OAR run")
    latest = session.execute(
        select(OarRun.id).order_by(OarRun.id.desc()).limit(1)
    ).scalar()
    row = session.execute(
        text(
            """select sap_material_number, sap_plant_code, array_agg(rank order by rank)
                 from i7_oar_neighbour
                where oar_run_id = :run_id
                group by 1,2
                having count(*) > 1
                limit 1"""
        ),
        {"run_id": latest},
    ).first()
    if row is None:
        pytest.skip("no target has more than one neighbour")
    ranks = row[2]
    assert ranks == list(range(1, len(ranks) + 1))


@needs_db
def test_a_material_is_never_its_own_neighbour(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    self_matched = session.execute(
        text(
            """select count(*) from i7_oar_neighbour
                where sap_material_number = neighbour_material
                  and sap_plant_code = neighbour_plant"""
        )
    ).scalar()
    assert self_matched == 0


@needs_db
def test_no_eligible_neighbours_status_has_no_neighbour_rows(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    wrong = session.execute(
        text(
            """select count(*) from i7_oar_target t
                where t.status = 'NO_ELIGIBLE_NEIGHBORS'
                  and exists (select 1 from i7_oar_neighbour n
                              where n.oar_run_id = t.oar_run_id
                                and n.sap_material_number = t.sap_material_number
                                and n.sap_plant_code = t.sap_plant_code)"""
        )
    ).scalar()
    assert wrong == 0


# --- Idempotency and provenance ------------------------------------------------------------


@needs_db
def test_repeating_the_run_reuses_it(session):
    from app.initiatives.i7.oar import run_oar_similarity

    session.commit()
    first = run_oar_similarity()
    assert first.status == "succeeded"

    second = run_oar_similarity()
    assert second.reused_existing is True
    assert second.run_id == first.run_id


@needs_db
def test_run_records_the_embedding_model_version(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    run = session.execute(select(OarRun).order_by(OarRun.id.desc()).limit(1)).scalar_one()
    assert run.embedding_model_version
    # No sentence-transformers is installed in this environment, so the run must
    # say so rather than silently proceeding as if text similarity ran.
    assert run.embedding_model_version == "none"
    assert run.embedding_model is None


@needs_db
def test_run_records_the_configured_weights_and_top_k(session):
    if not _ran(session):
        pytest.skip("no OAR run")
    run = session.execute(select(OarRun).order_by(OarRun.id.desc()).limit(1)).scalar_one()
    total = run.structured_weight + run.text_weight + run.business_weight
    assert abs(float(total) - 1.0) < 1e-6
    assert 5 <= run.top_k <= 10
    assert run.minimum_history_months == 12


@needs_db
@pytest.mark.needs_seed_data
def test_raw_tables_are_untouched(session):
    assert session.execute(
        text("select count(*) from pg_tables where schemaname='public' and tablename like 'raw_%'")
    ).scalar() == 27
    assert session.execute(text("select count(*) from raw_mseg")).scalar() == 233145
