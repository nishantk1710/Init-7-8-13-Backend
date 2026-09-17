"""Bulk reads of Phase 3/4/5/6 outputs for the recommendation builder.

Two grouped queries, not one per material-plant -- the builder never opens a
session itself, and everything it needs arrives as plain rows already joined
against the latest successful run of each upstream phase.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

_NORMAL_SQL = """
    SELECT f.sap_material_number, f.sap_plant_code, f.demand_class, f.history_status,
           f.criticality, f.non_zero_periods, f.feature_run_id,
           f.baseline_model, c.forecast_rate,
           f.current_safety_stock, f.current_reorder_point, f.current_maximum_stock,
           f.unit_price,
           i.lead_time_method, i.valid_po_count, i.lt_avg_months,
           i.lt_avg_days, i.sigma_lt_days, i.circuit,
           i.service_level, i.z_factor,
           i.safety_stock_status, i.safety_stock_method, i.safety_stock, i.detail,
           i.rop_status, i.rop,
           i.max_stock_status, i.max_stock_strategy, i.max_stock,
           i.inventory_run_id,
           c.forecast_run_id
      FROM i7_material_feature f
      JOIN i7_inventory_calculation i
        ON i.sap_material_number = f.sap_material_number
       AND i.sap_plant_code = f.sap_plant_code
       AND i.inventory_run_id = :inventory_run_id
      LEFT JOIN i7_forecast c
        ON c.sap_material_number = f.sap_material_number
       AND c.sap_plant_code = f.sap_plant_code
       AND c.forecast_run_id = :forecast_run_id
       AND c.is_champion = true
       AND c.forecast_status = 'SUCCESS'
     WHERE f.history_status = 'SUFFICIENT'
"""

_OAR_SQL = """
    SELECT f.sap_material_number, f.sap_plant_code, f.history_status, f.criticality,
           f.non_zero_periods, f.consumption_count_12m, f.demand_class, f.feature_run_id,
           t.status, t.confidence, t.candidates_considered, t.eligible_candidates,
           t.neighbour_count, t.best_similarity,
           t.estimate_status, t.safety_stock AS oar_safety_stock,
           t.rop AS oar_rop, t.max_stock AS oar_max_stock,
           t.oar_run_id,
           f.current_safety_stock, f.current_reorder_point, f.current_maximum_stock,
           f.unit_price
      FROM i7_material_feature f
      JOIN i7_oar_target t
        ON t.sap_material_number = f.sap_material_number
       AND t.sap_plant_code = f.sap_plant_code
       AND t.oar_run_id = :oar_run_id
     WHERE f.history_status <> 'SUFFICIENT'
"""
# current_safety_stock / current_reorder_point / current_maximum_stock /
# unit_price all read from the feature store (f.), not re-joined from
# i7_staged_material_plant / i7_staged_material: Phase 3 already carried every
# one of these through from staging (see features/builder.py's _ATTRIBUTE_SQL),
# so the staging joins here were a second, independent read of values the
# feature store already has.

_UNCOVERED_OAR_SQL = """
    SELECT f.sap_material_number, f.sap_plant_code, f.history_status, f.criticality,
           f.feature_run_id, f.unit_price
      FROM i7_material_feature f
     WHERE f.history_status <> 'SUFFICIENT'
       AND NOT EXISTS (
             SELECT 1 FROM i7_oar_target t
              WHERE t.sap_material_number = f.sap_material_number
                AND t.sap_plant_code = f.sap_plant_code
                AND t.oar_run_id = :oar_run_id
       )
"""


def latest_feature_run(session: Session) -> int | None:
    return session.execute(
        text("select max(id) from i7_feature_run where status = 'succeeded'")
    ).scalar()


def latest_forecast_run(session: Session, feature_run_id: int | None = None) -> int | None:
    """The most recent successful forecast run.

    Scoped to ``feature_run_id`` when given: a forecast run only has rows for
    the material-plants its own feature generation classified as
    ``SUFFICIENT``, so joining a recommendation batch built for feature
    generation N against a forecast run built for an older generation
    silently drops any material that generation N newly classified. Found
    during Phase 9 SIT (test_domain_integration.py) -- the real database has
    outrun forecasting (feature generation 20 exists; forecasting has only
    ever run against generation 8), and picking the global latest forecast
    run regardless of which feature generation the caller is building for
    produced 21 recommendations with a silently-missing forecast_rate.

    Callers that do not pass a ``feature_run_id`` keep the old "global latest"
    behaviour, matching every other ``latest_*_run`` helper here -- this
    parameter is additive, not a change to their contract.
    """
    if feature_run_id is None:
        return session.execute(
            text("select max(id) from i7_forecast_run where status = 'succeeded'")
        ).scalar()
    return session.execute(
        text(
            "select max(id) from i7_forecast_run "
            "where status = 'succeeded' and feature_run_id = :feature_run_id"
        ),
        {"feature_run_id": feature_run_id},
    ).scalar()


def latest_inventory_run(session: Session) -> int | None:
    return session.execute(
        text("select max(id) from i7_inventory_run where status = 'succeeded'")
    ).scalar()


def latest_oar_run(session: Session) -> int | None:
    return session.execute(
        text("select max(id) from i7_oar_run where status = 'succeeded'")
    ).scalar()


def load_normal_inputs(session: Session, inventory_run_id: int, forecast_run_id: int | None):
    """Every SUFFICIENT material-plant joined to its latest Phase 4/5 result."""
    return list(
        session.execute(
            text(_NORMAL_SQL),
            {"inventory_run_id": inventory_run_id, "forecast_run_id": forecast_run_id},
        )
    )


def load_oar_inputs(session: Session, oar_run_id: int):
    """Every cold-start material-plant joined to its Phase 6 result."""
    return list(session.execute(text(_OAR_SQL), {"oar_run_id": oar_run_id}))


def load_uncovered_oar_targets(session: Session, oar_run_id: int):
    """Cold-start material-plants with no Phase 6 result at all -- the feature
    store and OAR runs can drift apart if OAR has not been re-run since the
    last feature build."""
    return list(session.execute(text(_UNCOVERED_OAR_SQL), {"oar_run_id": oar_run_id}))
