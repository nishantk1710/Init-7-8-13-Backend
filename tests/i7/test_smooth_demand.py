"""Smooth demand is sized by SAP, not by I07 (app.initiatives.i7.smooth_demand).

A SMOOTH material-plant is not forecast, gets no calculated ROP / safety stock / max stock, gets
no recommendation, and is not listed on the Recommendations page. Every other class is untouched.
Its current SAP values stand, and the two places that still need its figures -- OAR donors and
the High Stockout Risk card -- take them from SAP, never from a forecast.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.i7 import recommendations as api
from app.initiatives.i7.forecasting.service import _ROUTED_SQL
from app.initiatives.i7.inventory.service import _INPUT_SQL
from app.initiatives.i7.oar import repository as oar_repository
from app.initiatives.i7.recommendations.repository import _NORMAL_SQL
from app.initiatives.i7.smooth_demand import SMOOTH_DEMAND_CLASS
from app.models.i7_features import DecliningDemandEvaluation, MaterialFeature
from app.models.i7_forecast import Forecast
from app.models.i7_inventory import InventoryCalculation
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant, StagedStock
from app.models.i7_stockout_risk import StockoutRiskInput

CLASSES = ("SMOOTH", "ERRATIC", "INTERMITTENT", "LUMPY")


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (
        MaterialFeature,
        Forecast,
        InventoryCalculation,
        StagedMaterialPlant,
        StagedMaterial,
        StagedStock,
        Recommendation,
        DecliningDemandEvaluation,
        StockoutRiskInput,
    ):
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def feature(
    material, demand_class, *, history="SUFFICIENT", sap=(None, None, None), baseline="SES", oar_scope="OUT_OF_SCOPE"
):
    safety, rop, maximum = sap
    return MaterialFeature(
        sap_material_number=material,
        sap_plant_code="1300",
        history_status=history,
        data_sufficiency="LIMITED",
        adi_status="OK",
        cv_squared_status="OK",
        oar_scope=oar_scope,
        feature_run_id=1,
        demand_class=demand_class,
        baseline_model=baseline,
        current_safety_stock=safety,
        current_reorder_point=rop,
        current_maximum_stock=maximum,
    )


def calculation(material, demand_class, status="SUCCESS"):
    return InventoryCalculation(
        inventory_run_id=1,
        sap_material_number=material,
        sap_plant_code="1300",
        demand_class=demand_class,
        lead_time_status="SUCCESS",
        variability_status="SUCCESS",
        service_level_status="SUCCESS",
        safety_stock_status=status,
        rop_status=status,
        max_stock_status=status,
        safety_stock=5,
        rop=9,
        max_stock=20,
    )


def seed_features(db):
    db.add_all([feature(f"M-{name}", name) for name in CLASSES])
    db.commit()


# --- the pipeline leaves SMOOTH out -------------------------------------------------------------


def test_forecasting_routes_every_class_but_smooth(db):
    seed_features(db)
    rows = db.execute(text(_ROUTED_SQL), {"sufficient": "SUFFICIENT", "smooth": SMOOTH_DEMAND_CLASS, "oar": "IN_SCOPE"}).all()
    assert sorted(row.demand_class for row in rows) == ["ERRATIC", "INTERMITTENT", "LUMPY"]


def test_inventory_calculates_for_every_class_but_smooth(db):
    seed_features(db)
    rows = db.execute(text(_INPUT_SQL), {"forecast_run_id": 1, "champion": True, "smooth": SMOOTH_DEMAND_CLASS}).all()
    assert sorted(row.demand_class for row in rows) == ["ERRATIC", "INTERMITTENT", "LUMPY"]


def test_recommendations_are_built_for_every_class_but_smooth(db):
    seed_features(db)
    db.add_all([calculation(f"M-{name}", name) for name in CLASSES])
    db.commit()
    rows = db.execute(
        text(_NORMAL_SQL),
        {"inventory_run_id": 1, "forecast_run_id": 1, "champion": True, "smooth": SMOOTH_DEMAND_CLASS},
    ).all()
    assert sorted(row.demand_class for row in rows) == ["ERRATIC", "INTERMITTENT", "LUMPY"]


def test_erratic_keeps_its_existing_handling(db):
    """Only SMOOTH changed: ERRATIC is still routed, calculated and recommended."""
    db.add_all([feature("E", "ERRATIC"), calculation("E", "ERRATIC")])
    db.commit()
    assert len(db.execute(text(_ROUTED_SQL), {"sufficient": "SUFFICIENT", "smooth": "SMOOTH", "oar": "IN_SCOPE"}).all()) == 1
    assert len(db.execute(text(_INPUT_SQL), {"forecast_run_id": 1, "champion": True, "smooth": "SMOOTH"}).all()) == 1


# --- OAR neighbours lend their current SAP values, whatever their class -----------------------


def test_a_smooth_neighbour_lends_its_current_sap_values(db):
    db.add_all([feature("S", "SMOOTH", sap=(Decimal(7), Decimal(12), Decimal(30)))])
    db.commit()
    values = oar_repository.load_inventory_values(db)
    assert values == {("S", "1300"): {"safety_stock": 7, "rop": 12, "max_stock": 30}}


@pytest.mark.parametrize("demand_class", ["ERRATIC", "INTERMITTENT", "LUMPY"])
def test_every_class_lends_its_current_sap_values(db, demand_class):
    db.add_all([feature("L", demand_class, sap=(Decimal(2), Decimal(4), Decimal(9)))])
    db.commit()
    assert oar_repository.load_inventory_values(db) == {
        ("L", "1300"): {"safety_stock": 2, "rop": 4, "max_stock": 9}
    }


def test_a_neighbour_never_lends_a_calculated_value(db):
    """A successful Phase 5 calculation is ignored: SAP's figure, or nothing."""
    db.add_all([feature("L", "LUMPY", sap=(Decimal(1), Decimal(2), Decimal(3))), calculation("L", "LUMPY")])
    db.commit()
    assert oar_repository.load_inventory_values(db)[("L", "1300")] == {
        "safety_stock": 1, "rop": 2, "max_stock": 3,
    }  # not 5 / 9 / 20


def test_a_calculation_alone_does_not_make_a_donor(db):
    db.add_all([feature("L", "LUMPY"), calculation("L", "LUMPY")])
    db.commit()
    assert oar_repository.load_inventory_values(db) == {}


@pytest.mark.parametrize(
    "sap",
    [
        (Decimal(7), Decimal(12), None),
        (None, None, None),
        (Decimal(0), Decimal(12), Decimal(30)),
        (Decimal(7), Decimal(0), Decimal(30)),
        (Decimal(7), Decimal(12), Decimal(0)),
    ],
)
def test_a_neighbour_needs_all_three_values_above_zero(db, sap):
    db.add_all([feature("X", "LUMPY", sap=sap)])
    db.commit()
    assert oar_repository.load_inventory_values(db) == {}


def test_a_fractional_value_is_rounded_up_never_down_to_zero(db):
    db.add_all([feature("F", "LUMPY", sap=(Decimal("0.5"), Decimal("2.1"), Decimal("4")))])
    db.commit()
    assert oar_repository.load_inventory_values(db)[("F", "1300")] == {
        "safety_stock": 1, "rop": 3, "max_stock": 4,
    }


def test_a_cold_start_row_is_not_a_donor(db):
    db.add_all([feature("S", "SMOOTH", history="COLD_START", sap=(Decimal(1), Decimal(2), Decimal(3)))])
    db.commit()
    assert oar_repository.load_inventory_values(db) == {}


def test_the_candidate_population_is_exactly_the_materials_with_values(db):
    """Candidates and lent values share one condition, so no neighbour can be
    found that has nothing to lend."""
    db.add_all(
        [
            feature("YES", "LUMPY", sap=(Decimal(1), Decimal(2), Decimal(3))),
            feature("ZERO", "LUMPY", sap=(Decimal(0), Decimal(2), Decimal(3))),
            feature("BLANK", "ERRATIC"),
            feature("COLD", "SMOOTH", history="COLD_START", sap=(Decimal(1), Decimal(2), Decimal(3))),
        ]
    )
    db.commit()
    candidates = {(c.sap_material_number, c.sap_plant_code) for c in oar_repository.load_candidate_population(db)}
    assert candidates == {("YES", "1300")}
    assert candidates == set(oar_repository.load_inventory_values(db))


# --- the Recommendations page and its summary do not show SMOOTH --------------------------------


def recommendation(material, demand_class):
    return Recommendation(
        recommendation_id=f"REC-{material}",
        sap_material_number=material,
        sap_plant_code="1300",
        feature_run_id=1,
        forecast_run_id=1,
        inventory_run_id=1,
        oar_run_id=1,
        policy_id="p",
        policy_version=1,
        formula_version="f",
        status="READY_FOR_REVIEW",
        impact_status="X",
        is_oar=False,
        demand_class=demand_class,
        generated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
    )


def seed_recommendations(db):
    db.add_all([recommendation("S", "SMOOTH"), recommendation("E", "ERRATIC"), recommendation("U", None)])
    db.commit()


def listing(db, **overrides):
    arguments = dict(
        session=db, page=1, page_size=100, sort="generated_at", sort_desc=False, status=None, plant=None,
        material=None, demand_class=None, is_oar=None, confidence=None, criticality=None,
        generated_from=None, generated_to=None, demand_type=None,
    )
    arguments.update(overrides)
    return api.list_recommendations(**arguments)


def summary(db, **overrides):
    arguments = dict(
        session=db, status=None, plant=None, material=None, demand_class=None, is_oar=None,
        confidence=None, criticality=None,
    )
    arguments.update(overrides)
    return api.get_recommendation_summary(**arguments)


def test_the_list_leaves_out_smooth_recommendations_but_keeps_the_rest(db):
    seed_recommendations(db)
    shown = sorted(item.material for item in listing(db).items)
    assert shown == ["E", "U"]  # a row with no demand class is not SMOOTH
    assert listing(db).total == 2


def test_asking_the_list_for_smooth_by_name_returns_nothing(db):
    seed_recommendations(db)
    assert listing(db, demand_class="SMOOTH").items == []


def test_the_summary_does_not_count_smooth_recommendations(db):
    seed_recommendations(db)
    assert summary(db).total == 2
    assert summary(db, plant="1300").total == 2


def test_the_summary_still_counts_smooth_material_plants_for_stockout_risk(db):
    # Usage built as of 31 May: a Smooth material-plant with 99 on hand against a need of 100.
    from datetime import date

    db.add_all(
        [
            feature("S", "SMOOTH"),
            StockoutRiskInput(
                sap_material_number="S", sap_plant_code="1300", as_of_date=date(2026, 5, 31),
                last_issue_date=date(2026, 5, 1), consumption_90d=Decimal(900), window_days=90,
            ),
            StagedStock(
                sap_material_number="S", sap_plant_code="1300", storage_location="0001",
                unrestricted_use_stock=Decimal(99), source_table="n_mard", staging_run_id=1,
            ),
        ]
    )
    seed_features_lead_time(db, "S", "10")
    result = summary(db)
    assert result.total == 0  # no recommendation for it
    assert (result.critical_stockout_risk_count, result.stockout_risk_evaluated_count) == (1, 1)


def seed_features_lead_time(db, material, lead_time):
    row = db.query(MaterialFeature).filter_by(sap_material_number=material).one()
    row.lead_time_days = Decimal(lead_time)
    db.commit()


# --- Routing by MRP type: OAR (ND / PD / blank) is sized from similar materials -------------------
#
# Stage 1's oar_scope decides the path, not history: an OAR material with a full history is not
# forecast and takes the OAR path; a VB / other one is forecast, or not evaluable when it has too
# little history -- there is no similar-materials fallback for it.


def test_forecasting_skips_an_oar_material_even_with_full_history(db):
    db.add_all([feature("OAR", "INTERMITTENT", oar_scope="IN_SCOPE"), feature("VB", "INTERMITTENT")])
    db.commit()
    rows = db.execute(text(_ROUTED_SQL), {"sufficient": "SUFFICIENT", "smooth": SMOOTH_DEMAND_CLASS, "oar": "IN_SCOPE"}).all()
    assert [row.sap_material_number for row in rows] == ["VB"]


def test_every_oar_material_is_an_oar_target_and_no_vb_one_is(db):
    db.add_all(
        [
            feature("OAR-HISTORY", "INTERMITTENT", oar_scope="IN_SCOPE"),
            feature("OAR-SMOOTH", "SMOOTH", oar_scope="IN_SCOPE"),
            feature("OAR-COLD", "UNCLASSIFIED", history="COLD_START", oar_scope="IN_SCOPE"),
            feature("VB-COLD", "UNCLASSIFIED", history="COLD_START"),
            feature("VB-HISTORY", "INTERMITTENT"),
        ]
    )
    db.commit()
    targets = sorted(t.sap_material_number for t in oar_repository.load_targets(db))
    assert targets == ["OAR-COLD", "OAR-HISTORY", "OAR-SMOOTH"]


def test_the_standard_path_takes_vb_materials_only_cold_start_ones_included(db):
    """An OAR material never reaches the forecast-based recommendation, however much
    history it has; a VB cold-start one does, to be listed as not evaluable."""
    db.add_all(
        [
            feature("OAR", "INTERMITTENT", oar_scope="IN_SCOPE"),
            calculation("OAR", "INTERMITTENT"),
            feature("VB", "INTERMITTENT"),
            calculation("VB", "INTERMITTENT"),
            feature("VB-COLD", "UNCLASSIFIED", history="COLD_START"),
            calculation("VB-COLD", "UNCLASSIFIED", status="NOT_EVALUABLE_NO_HISTORY"),
        ]
    )
    db.commit()
    rows = db.execute(
        text(_NORMAL_SQL),
        {"inventory_run_id": 1, "forecast_run_id": 1, "champion": True, "smooth": SMOOTH_DEMAND_CLASS},
    ).all()
    assert sorted(row.sap_material_number for row in rows) == ["VB", "VB-COLD"]


def test_a_smooth_oar_recommendation_is_listed(db):
    """SAP's values stand for a SMOOTH VB material, so its stale row stays hidden -- but
    a SMOOTH OAR material is sized from similar materials, and that is a real row."""
    oar_smooth = recommendation("OS", "SMOOTH")
    oar_smooth.is_oar = True
    db.add_all([oar_smooth, recommendation("VS", "SMOOTH"), recommendation("E", "ERRATIC")])
    db.commit()
    assert sorted(item.material for item in listing(db).items) == ["E", "OS"]


# --- The OAR list can be filtered by Min-Max conversion decision ------------------------------------


def test_the_list_filters_by_min_max_conversion_decision(db):
    rows = []
    for material, decision in (("ELIG", "ELIGIBLE"), ("NOT", "NOT_ELIGIBLE"), ("NONE", None)):
        rec = recommendation(material, "INTERMITTENT")
        rec.is_oar = decision is not None
        rec.conversion_eligibility = decision
        rows.append(rec)
    db.add_all(rows)
    db.commit()

    def shown(**filters):
        return sorted(item.material for item in listing(db, **filters).items)

    assert shown(conversion_eligibility="ELIGIBLE") == ["ELIG"]
    assert shown(conversion_eligibility="NOT_ELIGIBLE") == ["NOT"]
    assert shown() == ["ELIG", "NONE", "NOT"]  # no filter: everything
