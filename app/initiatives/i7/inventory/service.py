"""Inventory calculation orchestration.

    forecast + features + staged POs
        -> lead time -> service level -> safety stock -> ROP -> max stock

Reads the Phase 3 feature store, the Phase 4 forecasts and the Phase 2 staged
purchase orders. Never ``raw_*``.

**Each step gates the next.** No lead time means no safety stock, and no signed
service level means no safety stock either -- and without safety stock there is
no reorder point. Each blocked output carries the status naming what is missing,
so an operator can tell a data gap from a pending business decision.

**Cold-start materials are deferred, not defaulted.** NO_HISTORY and COLD_START
route to the Phase 6 similarity engine; nothing here invents a neighbour, and no
global average stands in.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.initiatives.i7.contracts import Criticality, DemandPattern
from app.initiatives.i7.inventory import (
    lead_time as lead_time_module,
)
from app.initiatives.i7.inventory import max_stock as max_stock_module
from app.initiatives.i7.inventory import monte_carlo, rop as rop_module, safety_stock
from app.initiatives.i7.inventory import service_level as service_level_module
from app.initiatives.i7.inventory import variability as variability_module
from app.initiatives.i7.inventory.lead_time import PurchaseOrderInput
from app.initiatives.i7.inventory.max_stock import MaxStockContext
from app.initiatives.i7.inventory.types import (
    FORMULA_VERSION,
    CalculationStatus,
    DemandVariabilityResult,
    LeadTimeResult,
    MaxStockResult,
    RopResult,
    SafetyStockResult,
    ServiceLevelResult,
)
from app.initiatives.i7.policy import PolicyDocument
from app.models.i7_inventory import InventoryCalculation, InventoryRun

logger = logging.getLogger(__name__)

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

OBSOLETE_TIER = Criticality.OBSOLETE


@dataclass
class InventoryRunResult:
    run_id: int | None = None
    status: str = STATUS_SUCCEEDED
    calculations: int = 0
    reused_existing: bool = False
    safety_stock_status_counts: dict[str, int] = field(default_factory=dict)
    rop_status_counts: dict[str, int] = field(default_factory=dict)
    max_stock_status_counts: dict[str, int] = field(default_factory=dict)
    lead_time_method_counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


# Feature-store rows joined to the baseline forecast of the latest run. Every
# material-plant appears, including those Phase 3 gated out -- they are recorded
# as deferred rather than omitted, so the table answers "what happened to this
# material?" for the whole catalogue.
_INPUT_SQL = """
    SELECT f.sap_material_number,
           f.sap_plant_code,
           f.demand_class,
           f.history_status,
           f.criticality,
           f.total_periods,
           f.non_zero_periods,
           f.mean_demand_all_periods,
           f.std_dev_demand_all_periods,
           f.mean_non_zero_demand,
           f.std_dev_non_zero_demand,
           f.planned_delivery_time_days,
           f.unit_price,
           c.model_name,
           c.forecast_rate,
           c.forecast_unit,
           c.parameters
      FROM i7_material_feature f
      LEFT JOIN i7_forecast c
             ON c.sap_material_number = f.sap_material_number
            AND c.sap_plant_code = f.sap_plant_code
            AND c.forecast_run_id = :forecast_run_id
            AND c.is_champion = true
            AND c.forecast_status = 'SUCCESS'
     ORDER BY f.sap_material_number, f.sap_plant_code
"""
# planned_delivery_time_days and unit_price are read from the feature store
# (f.), not re-joined from i7_staged_material_plant / i7_staged_material: the
# Phase 3 builder already carried both through from staging (see
# features/builder.py's _ATTRIBUTE_SQL), so re-joining staging here would be a
# second, independent read of the same value rather than a second computation
# -- still exactly the thing the feature store exists to avoid.

_PURCHASE_ORDER_SQL = """
    SELECT sap_material_number, sap_plant_code, lead_time_days, is_cancelled
      FROM i7_staged_purchase_order
     WHERE sap_material_number IS NOT NULL
"""
# Retained: PO count/cancellation observability in lead_time.analyse() has no
# feature-store equivalent (MaterialFeature stores only purchase_order_count,
# not per-PO cancellation status), and analyse()'s actual lead-time figure
# does not depend on this input -- see inventory/lead_time.py's module
# docstring.

_CONSUMPTION_SQL = """
    SELECT sap_material_number, sap_plant_code, quantity
      FROM i7_staged_consumption
     ORDER BY sap_material_number, sap_plant_code, period
"""
# Retained for LUMPY's Monte Carlo path only (monte_carlo.simulate below), which
# samples from the empirical non-zero-demand distribution -- something
# MaterialFeature genuinely does not store (only mean/std-dev aggregates).
# Everything that IS a feature-store aggregate (mean_demand_all_periods,
# std_dev_demand_all_periods) is read from f. in _INPUT_SQL instead; see
# variability_module.from_feature.


def _parse_parameters(text_value: str | None) -> dict[str, str]:
    """``key=value;key=value`` back into a mapping."""
    if not text_value:
        return {}
    pairs = {}
    for chunk in text_value.split(";"):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            pairs[key] = value
    return pairs


def _trace_text(trace: tuple[tuple[str, str], ...]) -> str | None:
    if not trace:
        return None
    return ";".join(f"{key}={value}" for key, value in trace)[:1000]


def _criticality(raw: str | None) -> Criticality | None:
    if raw is None:
        return None
    try:
        return Criticality(raw.strip().upper())
    except ValueError:
        return None


def calculate_one(
    row: Any,
    orders: list[PurchaseOrderInput],
    policy: PolicyDocument,
    demand_values: list[Decimal] | None = None,
) -> dict[str, Any]:
    """The full calculation chain for one material-plant."""
    material = row.sap_material_number
    plant = row.sap_plant_code
    demand_class = row.demand_class
    criticality = _criticality(row.criticality)

    blank_lead = LeadTimeResult(status=CalculationStatus.NOT_EVALUABLE_LEAD_TIME)
    blank_var = DemandVariabilityResult(status=CalculationStatus.NOT_EVALUABLE_NO_HISTORY)
    blank_service = ServiceLevelResult(
        status=CalculationStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET
    )

    # --- Cold start: Phase 6's problem, not this one -------------------
    if demand_class == DemandPattern.UNCLASSIFIED.value:
        deferred = CalculationStatus.DEFERRED_TO_OAR
        return _row(
            material, plant, demand_class, row, blank_lead, blank_var, blank_service,
            SafetyStockResult(
                status=deferred,
                detail="no usable demand history; parameters come from the OAR "
                "similarity engine (Phase 6)",
            ),
            RopResult(status=deferred),
            MaxStockResult(status=deferred),
        )

    # --- Lead time -------------------------------------------------------
    lead = lead_time_module.analyse(orders, row.planned_delivery_time_days, policy.lead_time)

    # --- Demand variability: all periods, zeros included -----------------
    # Read from the feature store, not recomputed from i7_staged_consumption --
    # Phase 3 already produced mean_demand_all_periods / std_dev_demand_all_periods
    # over the identical densified series and formula. See variability.from_feature.
    variability = variability_module.from_feature(
        row.total_periods,
        row.non_zero_periods,
        row.mean_demand_all_periods,
        row.std_dev_demand_all_periods,
    )

    # --- Service level: the business gate --------------------------------
    service = service_level_module.resolve(policy.service_level, criticality, None)

    # --- Obsolete materials get no safety stock --------------------------
    # Checked after lead time and variability so those are still recorded: the
    # data is useful even where no recommendation follows.
    if criticality is OBSOLETE_TIER:
        not_applicable = CalculationStatus.NOT_APPLICABLE_OBSOLETE
        return _row(
            material, plant, demand_class, row, lead, variability, service,
            SafetyStockResult(
                status=not_applicable,
                detail="material is classified OBSOLETE; I07 does not recommend "
                "safety stock for it",
            ),
            RopResult(status=not_applicable),
            MaxStockResult(status=not_applicable),
        )

    # --- Safety stock ------------------------------------------------------
    ss = _safety_stock(row, demand_class, lead, variability, service, demand_values, material, plant)

    # --- Reorder point ------------------------------------------------------
    if ss.status is not CalculationStatus.SUCCESS or ss.safety_stock is None:
        rop_result = RopResult(status=ss.status, detail="blocked by safety stock")
    elif row.forecast_rate is None:
        rop_result = RopResult(
            status=CalculationStatus.NOT_EVALUABLE_INVALID_FORECAST,
            detail="no selected demand forecast",
        )
    else:
        rop_result = rop_module.calculate(
            Decimal(str(row.forecast_rate)), lead.lt_avg_months, ss.safety_stock
        )

    # --- Maximum stock -------------------------------------------------------
    strategy = max_stock_module.strategy_for(policy.max_stock)
    review_period = _review_period(policy, criticality)
    max_result = strategy.calculate(
        MaxStockContext(
            safety_stock=ss.safety_stock,
            rop=rop_result.rop,
            forecast_rate=Decimal(str(row.forecast_rate)) if row.forecast_rate else None,
            criticality=criticality.value if criticality else None,
            unit_price=Decimal(str(row.unit_price)) if row.unit_price else None,
            ordering_cost=policy.max_stock.ordering_cost,
            holding_cost_rate=policy.max_stock.holding_cost_rate,
            review_period_months=review_period,
        )
    )

    return _row(
        material, plant, demand_class, row, lead, variability, service, ss, rop_result, max_result
    )


def _review_period(policy: PolicyDocument, criticality: Criticality | None) -> Decimal | None:
    """The agreed review period for a tier, if one has been signed."""
    if criticality is None:
        return None
    for tier, months in policy.max_stock.review_period_months:
        if tier is criticality:
            return Decimal(str(months))
    return None


def _safety_stock(
    row: Any,
    demand_class: str,
    lead: LeadTimeResult,
    variability: DemandVariabilityResult,
    service: ServiceLevelResult,
    demand_values: list[Decimal] | None,
    material: str,
    plant: str,
) -> SafetyStockResult:
    """Route to the formula the demand class calls for, once inputs allow."""
    if not lead.is_available:
        return SafetyStockResult(
            status=CalculationStatus.NOT_EVALUABLE_LEAD_TIME,
            detail=lead.detail or "no usable lead time",
        )
    if service.status is not CalculationStatus.SUCCESS or service.z_factor is None:
        return SafetyStockResult(
            status=CalculationStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET,
            detail=service.detail,
        )

    z = service.z_factor
    lt_months = lead.lt_avg_months
    # A single PO yields a mean but no sample deviation. Treated as zero
    # variability only here, where the alternative is discarding a usable mean;
    # the LIMITED/WARNING status on the lead-time result carries the caveat.
    sigma_lt = lead.sigma_lt_months if lead.sigma_lt_months is not None else Decimal(0)

    if demand_class in (DemandPattern.SMOOTH.value, DemandPattern.ERRATIC.value):
        if variability.status is not CalculationStatus.SUCCESS:
            return SafetyStockResult(
                status=variability.status, detail="demand variability unavailable"
            )
        return safety_stock.normal(
            z, lt_months, variability.sigma_d, variability.d_avg, sigma_lt
        )

    parameters = _parse_parameters(row.parameters)
    raw_p_final = parameters.get("p_final")
    if raw_p_final is None:
        return SafetyStockResult(
            status=CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND,
            detail="no SBA smoothed interval available from the selected forecast",
        )
    p_final = Decimal(raw_p_final)

    if row.mean_non_zero_demand is None or row.std_dev_non_zero_demand is None:
        return SafetyStockResult(
            status=CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND,
            detail="non-zero demand statistics unavailable",
        )

    mu_nz = Decimal(str(row.mean_non_zero_demand))
    sigma_nz = Decimal(str(row.std_dev_non_zero_demand))

    # LUMPY prefers the simulation: its lead-time demand distribution has a long
    # right tail, and the service-level quantile is read from exactly that tail,
    # where a normal approximation is least trustworthy.
    if demand_class == DemandPattern.LUMPY.value and service.service_level is not None:
        non_zero = [value for value in (demand_values or []) if value > 0]
        simulated = monte_carlo.simulate(
            service.service_level, lt_months, p_final, non_zero, material, plant
        )
        if simulated.status is CalculationStatus.SUCCESS:
            return simulated
        # Fall through to the closed form when the simulation cannot run, and
        # say so rather than silently switching method.
        result = safety_stock.compound_poisson(z, lt_months, p_final, mu_nz, sigma_nz)
        return result._replace(
            detail=f"Monte Carlo unavailable ({simulated.detail}); compound-Poisson used"
        )

    return safety_stock.compound_poisson(z, lt_months, p_final, mu_nz, sigma_nz)


def _row(
    material: str,
    plant: str,
    demand_class: str,
    row: Any,
    lead: LeadTimeResult,
    variability: DemandVariabilityResult,
    service: ServiceLevelResult,
    ss: SafetyStockResult,
    rop_result: RopResult,
    max_result: MaxStockResult,
) -> dict[str, Any]:
    """Flatten one calculation into a persistable row."""
    return {
        "sap_material_number": material,
        "sap_plant_code": plant,
        "demand_class": demand_class,
        "selected_model": row.model_name,
        "forecast_rate": row.forecast_rate,
        "forecast_unit": row.forecast_unit,
        "lead_time_status": lead.status.value,
        "lead_time_method": lead.method.value if lead.method else None,
        "po_count": lead.po_count,
        "valid_po_count": lead.valid_po_count,
        "excluded_cancelled_count": lead.excluded_cancelled_count,
        "excluded_lt_error_count": lead.excluded_lt_error_count,
        "outlier_count": lead.outlier_count,
        "lt_avg_days": lead.lt_avg_days,
        "lt_avg_months": lead.lt_avg_months,
        "sigma_lt_days": lead.sigma_lt_days,
        "sigma_lt_months": lead.sigma_lt_months,
        "planned_lt_days": lead.planned_lt_days,
        "variability_status": variability.status.value,
        "n_periods": variability.n_periods,
        "zero_period_count": variability.zero_period_count,
        "d_avg": variability.d_avg,
        "sigma_d": variability.sigma_d,
        "service_level_status": service.status.value,
        "service_level": service.service_level,
        "z_factor": service.z_factor,
        "criticality": service.criticality,
        "circuit": service.circuit,
        "safety_stock_status": ss.status.value,
        "safety_stock_method": ss.method,
        "raw_safety_stock": ss.raw_safety_stock,
        "safety_stock": ss.safety_stock,
        "safety_stock_trace": _trace_text(ss.trace),
        "rop_status": rop_result.status.value,
        "expected_lead_time_demand": rop_result.expected_lead_time_demand,
        "raw_rop": rop_result.raw_rop,
        "rop": rop_result.rop,
        "rop_trace": _trace_text(rop_result.trace),
        "max_stock_status": max_result.status.value,
        "max_stock_strategy": max_result.strategy,
        "raw_max_stock": max_result.raw_max_stock,
        "max_stock": max_result.max_stock,
        "max_stock_trace": _trace_text(max_result.trace),
        "detail": (ss.detail or lead.detail or service.detail or None),
    }


def run_inventory_calculations(
    policy: PolicyDocument | None = None, *, force: bool = False
) -> InventoryRunResult:
    """Calculate inventory parameters for every material-plant.

    Idempotent by construction: a run is identified by its feature, forecast,
    policy and formula versions, so repeating with the same inputs reuses the
    existing run rather than writing a second copy.

    ``policy`` defaults to the dev-fixture-aware default (see
    ``app.initiatives.i7.policy.dev_fixtures.default_policy``): an empty,
    unsigned :class:`PolicyDocument` everywhere the
    ``I7_DEV_MOCK_SERVICE_LEVEL`` env var is unset (every environment
    including production), or one carrying the development-only mock
    Criticality -> Service Level matrix when that flag is explicitly set.
    """
    from app.initiatives.i7.policy.dev_fixtures import default_policy

    policy = policy or default_policy()
    session_factory = get_sessionmaker()

    with session_factory() as session:
        feature_run_id = session.execute(
            text("select max(id) from i7_feature_run where status = 'succeeded'")
        ).scalar()
        forecast_run_id = session.execute(
            text("select max(id) from i7_forecast_run where status = 'succeeded'")
        ).scalar()

        existing = session.execute(
            select(InventoryRun).where(
                InventoryRun.feature_run_id == feature_run_id,
                InventoryRun.forecast_run_id == forecast_run_id,
                InventoryRun.policy_id == policy.policy_id,
                InventoryRun.policy_version == policy.policy_version,
                InventoryRun.formula_version == FORMULA_VERSION,
                InventoryRun.status == STATUS_SUCCEEDED,
            )
        ).scalar_one_or_none()

        if existing is not None and not force:
            logger.info(
                "inventory run: inputs unchanged since run %d, reusing it", existing.id
            )
            return InventoryRunResult(
                run_id=existing.id,
                status=STATUS_SUCCEEDED,
                calculations=existing.calculations_written,
                reused_existing=True,
            )

        strategy = max_stock_module.strategy_for(policy.max_stock)
        run = InventoryRun(
            status="running",
            feature_run_id=feature_run_id,
            forecast_run_id=forecast_run_id,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            formula_version=FORMULA_VERSION,
            service_level_configured=policy.service_level.is_configured,
            max_stock_strategy=policy.max_stock.strategy,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    result = InventoryRunResult(run_id=run_id)

    try:
        with session_factory() as session:
            logger.info("inventory run %d: loading purchase orders", run_id)
            orders_by_key: dict[tuple[str, str], list[PurchaseOrderInput]] = defaultdict(list)
            for row in session.execute(text(_PURCHASE_ORDER_SQL)).yield_per(5000):
                orders_by_key[(row.sap_material_number, row.sap_plant_code)].append(
                    PurchaseOrderInput(row.lead_time_days, row.is_cancelled)
                )

            # Only LUMPY's Monte Carlo path needs raw per-period demand (see
            # _CONSUMPTION_SQL's comment); everything else reads the feature
            # store's already-computed aggregates via variability_module.from_feature.
            logger.info("inventory run %d: loading demand for Monte Carlo", run_id)
            demand_by_key: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
            for row in session.execute(text(_CONSUMPTION_SQL)).yield_per(5000):
                demand_by_key[(row.sap_material_number, row.sap_plant_code)].append(
                    row.quantity
                )

            logger.info("inventory run %d: calculating", run_id)
            rows: list[dict[str, Any]] = []
            for row in session.execute(
                text(_INPUT_SQL), {"forecast_run_id": forecast_run_id}
            ).yield_per(5000):
                key = (row.sap_material_number, row.sap_plant_code)
                calculation = calculate_one(
                    row, orders_by_key.get(key, []), policy, demand_by_key.get(key, [])
                )
                calculation["inventory_run_id"] = run_id
                rows.append(calculation)

                for counter, value in (
                    (result.safety_stock_status_counts, calculation["safety_stock_status"]),
                    (result.rop_status_counts, calculation["rop_status"]),
                    (result.max_stock_status_counts, calculation["max_stock_status"]),
                ):
                    counter[value] = counter.get(value, 0) + 1
                method = calculation["lead_time_method"] or "NONE"
                result.lead_time_method_counts[method] = (
                    result.lead_time_method_counts.get(method, 0) + 1
                )

            session.bulk_insert_mappings(InventoryCalculation, rows)
            result.calculations = len(rows)
            session.commit()

        with session_factory() as session:
            stored = session.get(InventoryRun, run_id)
            stored.status = STATUS_SUCCEEDED
            stored.calculations_written = result.calculations
            stored.finished_at = datetime.now(timezone.utc)
            session.commit()

        logger.info(
            "inventory run %d: %d calculations; safety stock %s",
            run_id,
            result.calculations,
            result.safety_stock_status_counts,
        )
        return result

    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("inventory run %d failed: %s", run_id, detail)
        result.status = STATUS_FAILED
        result.error = detail
        try:
            with session_factory() as session:
                stored = session.get(InventoryRun, run_id)
                stored.status = STATUS_FAILED
                stored.error = detail[:4000]
                stored.finished_at = datetime.now(timezone.utc)
                session.commit()
        except Exception:
            logger.exception("inventory run %d: could not record the failure either", run_id)
        return result
