"""scripts.i7_progress reports the stage, the done/left counts and any LVORM leftovers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models.i7_features import FeatureBuildRun, MaterialFeature
from app.models.i7_forecast import Forecast, ForecastRun
from app.models.i7_inventory import InventoryCalculation, InventoryRun
from app.models.i7_oar import OarRun, OarTargetResult
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant, StagingRun
from scripts import i7_progress

MODELS = (
    StagingRun, FeatureBuildRun, MaterialFeature, ForecastRun, Forecast, InventoryRun,
    InventoryCalculation, OarRun, OarTargetResult, Recommendation, StagedMaterial, StagedMaterialPlant,
)
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in MODELS:
        model.__table__.to_metadata(metadata).indexes.clear()
    metadata.create_all(engine)
    monkeypatch.setattr(i7_progress, "_process_running", lambda: True)
    with Session(engine) as session:
        yield session


def test_an_empty_database_reports_nothing_started(db, tmp_path):
    text = i7_progress.render(i7_progress.collect(db, str(tmp_path / "none.log")))
    assert "pipeline process: RUNNING" in text
    assert "no stage is running" in text
    assert text.count("not started") == 6


def feature(material, **overrides):
    values = dict(
        sap_material_number=material, sap_plant_code="1300", history_status="SUFFICIENT",
        data_sufficiency="LIMITED", adi_status="OK", cv_squared_status="OK", oar_scope="OUT_OF_SCOPE",
        feature_run_id=1, demand_class="INTERMITTENT", baseline_model="SBA",
    )
    values.update(overrides)
    return MaterialFeature(**values)


def test_a_half_finished_run_shows_the_stage_and_what_is_left(db, tmp_path):
    db.add_all([
        StagingRun(source="test", status="succeeded", started_at=NOW, finished_at=NOW, materials_staged=10, material_plants_staged=20,
                   stock_staged=30, consumption_staged=40, purchase_orders_staged=50),
        FeatureBuildRun(status="succeeded", started_at=NOW, finished_at=NOW, features_built=5, policy_id="p", policy_version=1),
        ForecastRun(status="running", started_at=NOW, policy_id="p", policy_version=1, feature_run_id=1),
        feature("F1"), feature("F2"),
        feature("O1", oar_scope="IN_SCOPE"), feature("O2", oar_scope="IN_SCOPE"), feature("O3", oar_scope="IN_SCOPE"),
        Forecast(forecast_run_id=1, sap_material_number="F1", sap_plant_code="1300", demand_class="INTERMITTENT",
                 history_status="SUFFICIENT", model_name="SBA", model_version="v1", forecast_status="SUCCESS", backtest_status="NOT_RUN", pinball_status="NOT_RUN", fill_rate_status="NOT_RUN", holding_cost_status="NOT_RUN"),
    ])
    db.commit()
    text = i7_progress.render(i7_progress.collect(db, str(tmp_path / "none.log")))
    assert "5 of 5 steps saved" in text
    assert "1 of 2 material-plants forecast (50%, 1 left)" in text
    assert "NOW: 3 Forecasting" in text


def test_ordered_on_demand_materials_are_not_counted_as_still_to_forecast(db, tmp_path):
    """OAR material-plants are not forecast, so they are not "left" for forecasting."""
    db.add_all([
        FeatureBuildRun(status="succeeded", started_at=NOW, finished_at=NOW, features_built=2, policy_id="p", policy_version=1),
        ForecastRun(status="running", started_at=NOW, policy_id="p", policy_version=1, feature_run_id=1),
        feature("F1"), feature("O1", oar_scope="IN_SCOPE"),
    ])
    db.commit()
    assert "0 of 1 material-plants forecast" in i7_progress.render(i7_progress.collect(db, str(tmp_path / "none.log")))


def test_the_deleted_material_check_counts_leftovers(db, tmp_path):
    db.add_all([
        feature("OK"), feature("GONE"),
        StagedMaterial(sap_material_number="OK", source_table="n_mara", staging_run_id=1, deletion_flag=False),
        StagedMaterial(sap_material_number="GONE", source_table="n_mara", staging_run_id=1, deletion_flag=True),
        StagedMaterialPlant(sap_material_number="OK", sap_plant_code="1300", source_table="n_marc", staging_run_id=1, deletion_flag=False),
        StagedMaterialPlant(sap_material_number="GONE", sap_plant_code="1300", source_table="n_marc", staging_run_id=1, deletion_flag=False),
    ])
    db.commit()
    report = i7_progress.collect(db, str(tmp_path / "none.log"))
    assert report["deleted"] == {"plant": 0, "material": 1}


def test_the_recommendation_stage_reads_the_explanation_progress_from_the_log(db, tmp_path):
    log = tmp_path / "run.log"
    log.write_text("x\nrecommendation explanations: 500 of 18,000 done (AI_GENERATED 480, DETERMINISTIC_FALLBACK 20)\n".replace("18,000", "18000"))
    text = i7_progress.render(i7_progress.collect(db, str(log)))
    assert "500 of 18,000 explanations written (AI_GENERATED 480, DETERMINISTIC_FALLBACK 20)" in text
    assert "NOW: 6 Recommendations" in text


def test_every_count_compiles_for_sql_server(db, tmp_path, monkeypatch):
    """The progress report runs against Azure SQL, not the SQLite these tests use. SQLite
    accepts ``IS 1`` where SQL Server fails with "Incorrect syntax near '1'", so compile each
    count for SQL Server and look for what it rejects."""
    from sqlalchemy.dialects import mssql

    compiled: list[str] = []

    def record(session, statement):
        compiled.append(str(statement.compile(dialect=mssql.dialect())))
        return 1  # nonzero, so the report also runs the deleted-material check

    monkeypatch.setattr(i7_progress, "_count", record)
    db.add_all([
        StagingRun(source="test", status="succeeded", started_at=NOW),
        FeatureBuildRun(status="succeeded", started_at=NOW, policy_id="p", policy_version=1),
        ForecastRun(status="running", started_at=NOW, policy_id="p", policy_version=1),
        InventoryRun(status="succeeded", started_at=NOW, policy_id="p", policy_version=1, formula_version="f"),
        OarRun(status="running", started_at=NOW, policy_id="p", policy_version=1, algorithm_version="a", embedding_model_version="e", structured_weight=0.35, text_weight=0.3, business_weight=0.35, top_k=5, minimum_history_months=12, retrieval_config="c"),
    ])
    db.commit()
    i7_progress.collect(db, str(tmp_path / "none.log"))
    assert any("deletion_flag" in sql for sql in compiled), "the deleted-material check did not run"
    for sql in compiled:
        assert " IS 1" not in sql and " IS true" not in sql.lower() and " IS 0" not in sql, sql
