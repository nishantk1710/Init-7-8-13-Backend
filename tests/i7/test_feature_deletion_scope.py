"""The feature build leaves out anything SAP flags for deletion (LVORM = X).

A material-plant flagged in MARC (plant level) and every material-plant of a
material flagged in MARA (material level) never reach the feature store, which
is the one gate forecasting, inventory, OAR and recommendations all read from.
Run on the real attribute query over an in-memory database; an unknown flag
(NULL) is not "deleted" and passes.
"""

from __future__ import annotations

import pytest
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.features.builder import _ATTRIBUTE_SQL
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant, StagedStock


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (StagedMaterial, StagedMaterialPlant, StagedStock):
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def seed(db, material, *, material_flag, plant_flag, plant="1300"):
    db.add(
        StagedMaterial(
            sap_material_number=material, source_table="n_mara", staging_run_id=1, deletion_flag=material_flag
        )
    )
    db.add(
        StagedMaterialPlant(
            sap_material_number=material, sap_plant_code=plant, source_table="n_marc",
            staging_run_id=1, deletion_flag=plant_flag,
        )
    )
    db.commit()


def built(db) -> list[str]:
    rows = db.execute(text(_ATTRIBUTE_SQL), {"not_deleted": False}).all()
    return sorted(row.sap_material_number for row in rows)


def test_a_plant_flagged_for_deletion_is_left_out(db):
    seed(db, "PLANT-X", material_flag=False, plant_flag=True)
    seed(db, "OK", material_flag=False, plant_flag=False)
    assert built(db) == ["OK"]


def test_a_material_flagged_for_deletion_is_left_out_at_every_plant(db):
    seed(db, "MAT-X", material_flag=True, plant_flag=False, plant="1300")
    db.add(
        StagedMaterialPlant(
            sap_material_number="MAT-X", sap_plant_code="1500", source_table="n_marc",
            staging_run_id=1, deletion_flag=False,
        )
    )
    seed(db, "OK", material_flag=False, plant_flag=False)
    assert built(db) == ["OK"]


def test_both_flags_left_out_and_neither_kept(db):
    seed(db, "BOTH", material_flag=True, plant_flag=True)
    seed(db, "NEITHER", material_flag=False, plant_flag=False)
    assert built(db) == ["NEITHER"]


def test_an_unknown_flag_is_not_deletion(db):
    """No MARC row's flag, or no MARA flag, is "we do not know" -- not "deleted"."""
    seed(db, "UNKNOWN-PLANT", material_flag=False, plant_flag=None)
    seed(db, "UNKNOWN-MATERIAL", material_flag=None, plant_flag=False)
    seed(db, "UNKNOWN-BOTH", material_flag=None, plant_flag=None)
    assert built(db) == ["UNKNOWN-BOTH", "UNKNOWN-MATERIAL", "UNKNOWN-PLANT"]


def test_a_stock_only_material_plant_is_filtered_by_its_material_flag(db):
    """A material-plant known only from stock has no MARC row, but its material's MARA flag still applies."""
    db.add(StagedMaterial(sap_material_number="STOCK-X", source_table="n_mara", staging_run_id=1, deletion_flag=True))
    db.add(StagedMaterial(sap_material_number="STOCK-OK", source_table="n_mara", staging_run_id=1, deletion_flag=False))
    for material in ("STOCK-X", "STOCK-OK"):
        db.add(
            StagedStock(
                sap_material_number=material, sap_plant_code="1500", storage_location="0001",
                source_table="n_mard", staging_run_id=1,
            )
        )
    db.commit()
    assert built(db) == ["STOCK-OK"]
