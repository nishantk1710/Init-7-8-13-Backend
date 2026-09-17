"""Recommendation generation orchestration.

    Phase 3 feature store + Phase 4 forecast + Phase 5 inventory + Phase 6 OAR
        -> builder -> i7_recommendation

Reads the latest successful run of each upstream phase in two grouped queries
and assembles one recommendation per material-plant in memory, batching the
insert. No query and no external call happens per material.

**Idempotent by the same pattern as every earlier phase.** A recommendation's
identity is the tuple of upstream run ids plus the policy and formula
versions; the unique constraint on ``i7_recommendation`` makes a repeat run
with unchanged inputs a no-op rather than a duplicate.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.initiatives.i7.recommendations import builder, repository
from app.initiatives.i7.recommendations.builder import BuiltRecommendation
from app.initiatives.i7.recommendations.conversion import HodApprovalLookup
from app.initiatives.i7.recommendations.types import RECOMMENDATION_FORMULA_VERSION
from app.initiatives.i7.policy import PolicyDocument
from app.models.i7_recommendation import Recommendation

logger = logging.getLogger(__name__)

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"


@dataclass
class RecommendationRunResult:
    status: str = STATUS_SUCCEEDED
    recommendations_written: int = 0
    reused_existing: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def _recommendation_id(material: str, plant: str, is_oar: bool) -> str:
    """A stable, human-readable identity -- not a UUID, so a log line or an
    approver's URL names the material-plant directly."""
    kind = "OAR" if is_oar else "STD"
    return f"REC-{kind}-{material}-{plant}"


def _trace_text(entries: tuple) -> str | None:
    if not entries:
        return None
    return ";".join(f"{key}={value}" for key, value in entries)[:2000]


def _factors_text(factors: tuple) -> str | None:
    if not factors:
        return None
    return "\n".join(f"{factor.label}: {factor.detail}" for factor in factors)[:2000]


def _to_row(built: BuiltRecommendation) -> dict[str, Any]:
    conversion = built.conversion
    impact = built.impact

    return {
        "recommendation_id": _recommendation_id(
            built.sap_material_number, built.sap_plant_code, bool(built.is_oar)
        ),
        "sap_material_number": built.sap_material_number,
        "sap_plant_code": built.sap_plant_code,
        "feature_run_id": built.feature_run_id,
        "forecast_run_id": built.forecast_run_id,
        "inventory_run_id": built.inventory_run_id,
        "oar_run_id": built.oar_run_id,
        "policy_id": built.policy_id,
        "policy_version": built.policy_version,
        "formula_version": RECOMMENDATION_FORMULA_VERSION,
        "is_oar": built.is_oar,
        "demand_class": built.demand_class,
        "history_status": built.history_status,
        "criticality": built.criticality,
        "current_safety_stock": built.current_safety_stock,
        "current_rop": built.current_rop,
        "current_max_stock": built.current_max_stock,
        "recommended_safety_stock": built.recommended_safety_stock,
        "recommended_rop": built.recommended_rop,
        "recommended_max_stock": built.recommended_max_stock,
        "baseline_model": built.baseline_model,
        "forecast_rate": built.forecast_rate,
        "lead_time_method": built.lead_time_method,
        "safety_stock_method": built.safety_stock_method,
        "max_stock_strategy": built.max_stock_strategy,
        "confidence": built.confidence,
        "oar_neighbour_count": built.oar_neighbour_count,
        "oar_best_similarity": built.oar_best_similarity,
        "oar_similarity_status": built.oar_similarity_status,
        "oar_estimate_status": built.oar_estimate_status,
        "conversion_eligibility": conversion.eligibility.value if conversion else None,
        "conversion_trigger": conversion.trigger.value if conversion else None,
        "conversion_detail": conversion.detail[:500] if conversion else None,
        "consumption_count_12m": conversion.consumption_count_12m if conversion else None,
        "consumption_count_threshold": (
            conversion.consumption_count_threshold if conversion else None
        ),
        "production_impact": conversion.production_impact if conversion else None,
        "i13_hod_approved": conversion.i13_hod_approved if conversion else None,
        "blocking_reason": (built.blocking_reason or "")[:500] or None,
        "factors_text": _factors_text(built.factors),
        "calculation_trace": _trace_text(built.calculation_trace),
        "impact_status": impact.status.value,
        "safety_stock_delta": impact.safety_stock.delta if impact.safety_stock else None,
        "rop_delta": impact.reorder_point.delta if impact.reorder_point else None,
        "max_stock_delta": impact.maximum_stock.delta if impact.maximum_stock else None,
        "status": built.status.value,
        "chain_index": 0,
        "adjustment_count": 0,
        "current_version": 1,
    }


def generate_recommendations(
    policy: PolicyDocument | None = None,
    *,
    hod_lookup: HodApprovalLookup | None = None,
) -> RecommendationRunResult:
    """Build recommendations for every material-plant from the latest
    successful upstream runs.

    ``policy`` defaults to the dev-fixture-aware default (see
    ``app.initiatives.i7.policy.dev_fixtures.default_policy``), matching
    every other phase's fallback. The recommended SS/ROP/Max values
    themselves are read from the upstream rows and never depend on this, but
    the builder asks the policy two questions it would otherwise answer
    wrongly in a dev run: whether the service level is configured (which
    decides the blocking reason and the explanation factors) and the holding
    cost rate (used by the expected-impact calculation). Production is
    unaffected -- with neither dev flag set, ``default_policy()`` returns
    exactly the ``PolicyDocument()`` this line used to construct.
    """
    from app.initiatives.i7.policy.dev_fixtures import default_policy

    policy = policy or default_policy()
    session_factory = get_sessionmaker()
    result = RecommendationRunResult()

    try:
        with session_factory() as session:
            feature_run_id = repository.latest_feature_run(session)
            # Scoped to feature_run_id: an unscoped "global latest" forecast
            # run can predate the feature generation being built for and
            # silently miss materials that generation newly classified as
            # SUFFICIENT (Phase 9 SIT finding).
            forecast_run_id = repository.latest_forecast_run(session, feature_run_id)
            inventory_run_id = repository.latest_inventory_run(session)
            oar_run_id = repository.latest_oar_run(session)

            built: list[BuiltRecommendation] = []

            if inventory_run_id is not None:
                for row in repository.load_normal_inputs(
                    session, inventory_run_id, forecast_run_id
                ):
                    built.append(builder.build_normal_recommendation(row, policy, hod_lookup))

            if oar_run_id is not None:
                for row in repository.load_oar_inputs(session, oar_run_id):
                    built.append(builder.build_oar_recommendation(row, policy, hod_lookup))
                for row in repository.load_uncovered_oar_targets(session, oar_run_id):
                    built.append(builder.deferred_recommendation(row, policy))

            rows = [_to_row(item) for item in built]
            for row in rows:
                result.status_counts[row["status"]] = (
                    result.status_counts.get(row["status"], 0) + 1
                )

            written = 0
            reused = 0
            for row in rows:
                existing = session.execute(
                    select(Recommendation.id).where(
                        Recommendation.sap_material_number == row["sap_material_number"],
                        Recommendation.sap_plant_code == row["sap_plant_code"],
                        Recommendation.feature_run_id == row["feature_run_id"],
                        Recommendation.forecast_run_id == row["forecast_run_id"],
                        Recommendation.inventory_run_id == row["inventory_run_id"],
                        Recommendation.oar_run_id == row["oar_run_id"],
                        Recommendation.policy_id == row["policy_id"],
                        Recommendation.policy_version == row["policy_version"],
                        Recommendation.formula_version == row["formula_version"],
                    )
                ).scalar_one_or_none()

                if existing is not None:
                    reused += 1
                    continue

                session.add(Recommendation(**row))
                written += 1

            result.recommendations_written = written
            result.reused_existing = reused
            session.commit()

        logger.info(
            "recommendation generation: %d written, %d reused, statuses %s",
            result.recommendations_written,
            result.reused_existing,
            result.status_counts,
        )
        return result

    except Exception as exc:
        result.status = STATUS_FAILED
        result.error = f"{type(exc).__name__}: {exc}"
        logger.error("recommendation generation failed: %s", result.error)
        return result
