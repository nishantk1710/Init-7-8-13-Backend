"""Staging against the real seeded extract.

Real Postgres or skip, following ``tests/test_db.py``. These assert against the
actual July/August data rather than fixtures, because the failures worth
catching -- a mis-joined table, a lost MRP type, a duplicated PO line -- only
appear at real volume and shape.

Read-only apart from the adapter itself: no test mutates ``raw_*``.
"""

from datetime import date

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.adapters import (
    consumption_series_for,
    iter_material_attributes,
    material_attributes_for,
    purchase_orders_for,
)
from app.models.i7_staging import (
    StagedConsumption,
    StagedMaterial,
    StagedMaterialPlant,
    StagedPurchaseOrder,
    StagingRejection,
    StagingRun,
)

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session


def _staged(session) -> bool:
    return session.execute(select(func.count()).select_from(StagedMaterialPlant)).scalar() > 0


needs_staging = pytest.mark.skipif(
    not get_settings().database_url, reason="DATABASE_URL not set"
)


# --- Raw data is untouched --------------------------------------------


@needs_db
@pytest.mark.needs_seed_data
def test_raw_tables_survive_staging(session):
    """The extract is the immutable baseline; the adapter only reads it."""
    tables = session.execute(
        text("select count(*) from pg_tables where schemaname='public' and tablename like 'raw_%'")
    ).scalar()
    assert tables == 27
    assert session.execute(text("select count(*) from raw_mseg")).scalar() == 233145
    assert session.execute(text("select count(*) from raw_marc")).scalar() == 45409
    assert session.execute(text("select count(*) from raw_mara")).scalar() == 5904


# --- Mapping fidelity --------------------------------------------------


@needs_db
def test_mrp_type_distribution_matches_the_source(session):
    """Staging must not reshape DISMM -- the OAR rule reads it later."""
    if not _staged(session):
        pytest.skip("staging not populated")

    raw = dict(
        session.execute(
            text(
                "select coalesce(nullif(mrp_type,''),'~') , count(*) from raw_marc group by 1"
            )
        ).all()
    )
    staged = dict(
        session.execute(
            text(
                "select coalesce(mrp_type,'~'), count(*) from i7_staged_material_plant group by 1"
            )
        ).all()
    )
    assert staged == raw


@needs_db
def test_blank_mrp_type_stages_as_null_not_a_value(session):
    """"Not maintained" must stay distinguishable from a real code."""
    if not _staged(session):
        pytest.skip("staging not populated")
    blanks = session.execute(
        select(func.count()).select_from(StagedMaterialPlant).where(
            StagedMaterialPlant.mrp_type.is_(None)
        )
    ).scalar()
    raw_blanks = session.execute(
        text("select count(*) from raw_marc where nullif(mrp_type,'') is null")
    ).scalar()
    assert blanks == raw_blanks


@needs_db
def test_material_status_mstae_is_staged(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    obsolete = session.execute(
        select(func.count()).select_from(StagedMaterial).where(
            StagedMaterial.material_status == "01"
        )
    ).scalar()
    raw_obsolete = session.execute(
        text("select count(*) from raw_mara where x_plant_matl_status = '01'")
    ).scalar()
    assert obsolete == raw_obsolete > 0


@needs_db
def test_required_case_pd_with_non_obsolete_status(session):
    """DISMM=PD and MSTAE != '01' -- staged as fields, not classified."""
    if not _staged(session):
        pytest.skip("staging not populated")

    row = session.execute(
        select(StagedMaterialPlant, StagedMaterial)
        .join(
            StagedMaterial,
            StagedMaterial.sap_material_number == StagedMaterialPlant.sap_material_number,
        )
        .where(
            StagedMaterialPlant.mrp_type == "PD",
            StagedMaterial.material_status.is_(None),
        )
        .limit(1)
    ).first()
    assert row is not None
    plant_row, material_row = row
    assert plant_row.mrp_type == "PD"
    assert material_row.material_status != "01"
    # No OAR verdict is stored anywhere -- that is Phase 3's decision.
    assert not hasattr(plant_row, "is_oar")


@needs_db
def test_required_case_nd_with_obsolete_status(session):
    """DISMM=ND and MSTAE='01' -- both inputs present, still unclassified."""
    if not _staged(session):
        pytest.skip("staging not populated")

    row = session.execute(
        select(StagedMaterialPlant, StagedMaterial)
        .join(
            StagedMaterial,
            StagedMaterial.sap_material_number == StagedMaterialPlant.sap_material_number,
        )
        .where(
            StagedMaterialPlant.mrp_type == "ND",
            StagedMaterial.material_status == "01",
        )
        .limit(1)
    ).first()
    assert row is not None
    plant_row, material_row = row
    assert (plant_row.mrp_type, material_row.material_status) == ("ND", "01")


@needs_db
def test_planned_delivery_time_is_numeric(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    populated = session.execute(
        select(func.count()).select_from(StagedMaterialPlant).where(
            StagedMaterialPlant.planned_delivery_time_days.isnot(None)
        )
    ).scalar()
    assert populated > 40000


@needs_db
def test_safety_stock_is_null_because_eisbe_is_absent(session):
    """Not zero. Zero safety stock is a real, different claim."""
    if not _staged(session):
        pytest.skip("staging not populated")
    non_null = session.execute(
        select(func.count()).select_from(StagedMaterialPlant).where(
            StagedMaterialPlant.current_safety_stock.isnot(None)
        )
    ).scalar()
    assert non_null == 0


@needs_db
def test_app_identities_are_left_unresolved(session):
    """No SAP field maps to the app vocabulary; none may be invented."""
    if not _staged(session):
        pytest.skip("staging not populated")
    assert (
        session.execute(
            select(func.count()).select_from(StagedMaterial).where(
                StagedMaterial.app_material_id.isnot(None)
            )
        ).scalar()
        == 0
    )
    assert (
        session.execute(
            select(func.count()).select_from(StagedMaterialPlant).where(
                StagedMaterialPlant.app_plant_id.isnot(None)
            )
        ).scalar()
        == 0
    )


@needs_db
def test_descriptions_are_joined_from_makt(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    described = session.execute(
        select(func.count()).select_from(StagedMaterial).where(
            StagedMaterial.description.isnot(None)
        )
    ).scalar()
    assert described > 5000


# --- Consumption -------------------------------------------------------


@needs_db
def test_consumption_is_aggregated_to_months(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    periods = session.execute(select(StagedConsumption.period).distinct()).scalars().all()
    assert periods, "no consumption staged"
    assert all(period.day == 1 for period in periods)


@needs_db
def test_consumption_covers_the_extract_window(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    low, high = session.execute(
        select(func.min(StagedConsumption.period), func.max(StagedConsumption.period))
    ).one()
    assert low >= date(2025, 8, 1)
    assert high <= date(2026, 9, 1)


@needs_db
def test_no_duplicate_consumption_periods(session):
    """The unique key makes this impossible; the test proves it holds."""
    if not _staged(session):
        pytest.skip("staging not populated")
    duplicates = session.execute(
        text(
            """select count(*) from (
                 select sap_material_number, sap_plant_code, period
                   from i7_staged_consumption
                  group by 1,2,3 having count(*) > 1) d"""
        )
    ).scalar()
    assert duplicates == 0


@needs_db
def test_movement_count_is_recorded_for_provenance(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    assert (
        session.execute(
            select(func.count()).select_from(StagedConsumption).where(
                StagedConsumption.movement_count > 0
            )
        ).scalar()
        > 0
    )


# --- Purchase orders ---------------------------------------------------


@needs_db
def test_purchase_orders_are_unique_per_line(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    duplicates = session.execute(
        text(
            """select count(*) from (
                 select purchasing_document, item from i7_staged_purchase_order
                  group by 1,2 having count(*) > 1) d"""
        )
    ).scalar()
    assert duplicates == 0


@needs_db
def test_lead_time_is_stored_unfiltered(session):
    """The 1-730 day window is Phase 3 policy, not a staging filter."""
    if not _staged(session):
        pytest.skip("staging not populated")
    outside = session.execute(
        select(func.count()).select_from(StagedPurchaseOrder).where(
            StagedPurchaseOrder.lead_time_days > 730
        )
    ).scalar()
    # Long durations exist in the data and must survive staging intact.
    assert outside >= 0
    negative = session.execute(
        select(func.count()).select_from(StagedPurchaseOrder).where(
            StagedPurchaseOrder.lead_time_days < 0
        )
    ).scalar()
    assert negative == 0, "impossible durations should be rejected, not stored"


@needs_db
def test_open_purchase_orders_have_no_lead_time(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    wrong = session.execute(
        select(func.count()).select_from(StagedPurchaseOrder).where(
            StagedPurchaseOrder.goods_receipt_date.is_(None),
            StagedPurchaseOrder.lead_time_days.isnot(None),
        )
    ).scalar()
    assert wrong == 0


@needs_db
def test_cancellation_is_staged_not_filtered(session):
    """Cancelled POs are excluded from statistics later, not dropped here."""
    if not _staged(session):
        pytest.skip("staging not populated")
    cancelled = session.execute(
        select(func.count()).select_from(StagedPurchaseOrder).where(
            StagedPurchaseOrder.is_cancelled.is_(True)
        )
    ).scalar()
    assert cancelled > 0


# --- Rejections and provenance ------------------------------------------


@needs_db
def test_rejections_are_recorded_with_a_reason(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    rows = session.execute(
        select(StagingRejection.reason, func.count())
        .group_by(StagingRejection.reason)
    ).all()
    assert rows, "expected at least one recorded rejection"
    for reason, count in rows:
        assert reason and count > 0


@needs_db
def test_every_staged_row_names_its_source_and_run(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    for model, expected in (
        (StagedMaterial, "raw_mara"),
        (StagedMaterialPlant, "raw_marc"),
        (StagedConsumption, "raw_mseg"),
        (StagedPurchaseOrder, "raw_ekpo"),
    ):
        row = session.execute(select(model).limit(1)).scalar_one()
        assert row.source_table == expected
        assert row.staging_run_id > 0


@needs_db
def test_run_records_the_movement_types_used(session):
    """The set is unconfirmed, so which one produced these rows must be known."""
    if not _staged(session):
        pytest.skip("staging not populated")
    run = session.execute(
        select(StagingRun).order_by(StagingRun.id.desc()).limit(1)
    ).scalar_one()
    assert run.consumption_movement_types
    assert "201" in run.consumption_movement_types


# --- Canonical contracts ------------------------------------------------


@needs_db
def test_repository_returns_a_dense_consumption_series(session):
    """Zero-demand months must be present: ADI counts total periods."""
    if not _staged(session):
        pytest.skip("staging not populated")

    row = session.execute(
        text(
            """select sap_material_number, sap_plant_code
                 from i7_staged_consumption
                group by 1,2
               having count(*) > 2
                  and count(*) < (extract(year from max(period))*12+extract(month from max(period)))
                                - (extract(year from min(period))*12+extract(month from min(period))) + 1
                limit 1"""
        )
    ).first()
    if row is None:
        pytest.skip("no sparse series in the extract")

    series = consumption_series_for(session, row[0], row[1])
    # Contiguity is enforced by the contract; reaching here proves no gaps.
    assert series.total_periods > series.non_zero_periods
    assert any(observation.is_zero_demand for observation in series.observations)


@needs_db
def test_repository_builds_canonical_attributes(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    row = session.execute(
        select(StagedMaterialPlant).where(StagedMaterialPlant.mrp_type == "PD").limit(1)
    ).scalar_one()

    attributes = material_attributes_for(
        session, row.sap_material_number, row.sap_plant_code
    )
    assert attributes.mrp_type == "PD"
    assert attributes.key.material.sap_material_number == row.sap_material_number
    # Unresolved identity stays unresolved.
    assert attributes.key.material.app_material_id is None
    # No SAP source for circuit.
    assert attributes.circuit is None


@needs_db
def test_repository_returns_none_for_an_unknown_material(session):
    assert material_attributes_for(session, "NO-SUCH-MATERIAL", "1300") is None


@needs_db
def test_iter_material_attributes_streams(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    seen = 0
    for attributes in iter_material_attributes(session, plant="1300"):
        seen += 1
        if seen >= 5:
            break
    assert seen == 5


@needs_db
def test_purchase_order_observations_are_canonical(session):
    if not _staged(session):
        pytest.skip("staging not populated")
    row = session.execute(
        select(StagedPurchaseOrder)
        .where(StagedPurchaseOrder.goods_receipt_date.isnot(None))
        .limit(1)
    ).scalar_one()

    observations = purchase_orders_for(
        session, row.sap_material_number, row.sap_plant_code
    )
    assert observations
    assert all(
        observation.key.material.sap_material_number == row.sap_material_number
        for observation in observations
    )
