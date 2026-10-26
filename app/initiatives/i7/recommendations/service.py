"""Recommendation generation orchestration.

    Phase 3 feature store + Phase 4 forecast + Phase 5 inventory + Phase 6 OAR
        -> builder -> i7_recommendation

Reads the latest successful run of each upstream phase in two grouped queries
and assembles one recommendation per material-plant in memory, batching the
insert. No query happens per material. The one external call per material is
the explanation text (``rationale.generate_rationale``, an AI call when a
provider is configured); those run ``I7_RATIONALE_CONCURRENCY`` at a time,
with no database connection held open while they wait.

**The forecast run is pinned to the selected inventory run, not independently
re-derived.** ``forecast_run_id`` is read from
``i7_inventory_run.forecast_run_id`` -- the exact forecast run the selected
inventory calculation was actually built from -- rather than from
``latest_forecast_run(latest feature_run_id)``. The two can drift apart once
the feature store has advanced past the last executed forecast run: an
independent "latest forecast run for the latest feature generation" lookup
then returns ``None`` even though the selected inventory run's own forecast
join succeeded and already produced real SS/ROP values (Part 28 audit
finding). Pinning to the inventory run's own value means the recommendation's
forecast join can never disagree with the run its SS/ROP were actually
computed from.

**Idempotent by the same pattern as every earlier phase.** A recommendation's
identity is the tuple of upstream run ids plus the policy and formula
versions; the unique constraint on ``i7_recommendation`` makes a repeat run
with unchanged inputs a no-op rather than a duplicate.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.recommendations import builder, rationale, repository
from app.initiatives.i7.recommendations.builder import BuiltRecommendation
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


#: How often the explanation step reports progress, in recommendations.
RATIONALE_PROGRESS_EVERY = 500


def _generate_rationales(
    built: list[BuiltRecommendation], workers: int
) -> list[rationale.RationaleResult]:
    """One explanation per recommendation, in the same order, ``workers`` at a time.

    Each call is an independent HTTP request to the AI provider, so they run
    side by side; ``generate_rationale`` never raises (any failure becomes the
    template text). Progress is logged so a long run can be followed.
    """
    results: list[rationale.RationaleResult | None] = [None] * len(built)
    sources: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="i7-rationale") as pool:
        futures = {
            pool.submit(rationale.generate_rationale, item): index
            for index, item in enumerate(built)
        }
        for done, future in enumerate(as_completed(futures), start=1):
            outcome = future.result()
            results[futures[future]] = outcome
            sources[outcome.source] = sources.get(outcome.source, 0) + 1
            if done % RATIONALE_PROGRESS_EVERY == 0 or done == len(built):
                logger.info(
                    "recommendation explanations: %d of %d done (%s)",
                    done,
                    len(built),
                    ", ".join(f"{name} {count}" for name, count in sorted(sources.items())),
                )
    return results  # type: ignore[return-value]  # every slot is filled above


def _to_row(
    built: BuiltRecommendation, rationale_result: rationale.RationaleResult
) -> dict[str, Any]:
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
        "circuit": built.circuit,
        "unit_price": built.unit_price,
        "lead_time_days": built.lead_time_days,
        "lead_time_variance_days": built.lead_time_variance_days,
        "service_level": built.service_level,
        "z_factor": built.z_factor,
        "conversion_eligibility": conversion.eligibility.value if conversion else None,
        "conversion_trigger": conversion.trigger.value if conversion else None,
        "conversion_detail": conversion.detail[:500] if conversion else None,
        "consumption_count_12m": conversion.consumption_count_12m if conversion else None,
        "consumption_count_threshold": (
            conversion.consumption_count_threshold if conversion else None
        ),
        "production_impact": conversion.production_impact if conversion else None,
        "blocking_reason": (built.blocking_reason or "")[:500] or None,
        "factors_text": _factors_text(built.factors),
        "calculation_trace": _trace_text(built.calculation_trace),
        "rationale_text": rationale_result.text,
        "rationale_source": rationale_result.source,
        "impact_status": impact.status.value,
        "safety_stock_delta": impact.safety_stock.delta if impact.safety_stock else None,
        "rop_delta": impact.reorder_point.delta if impact.reorder_point else None,
        "max_stock_delta": impact.maximum_stock.delta if impact.maximum_stock else None,
        "status": built.status.value,
        "chain_index": 0,
        "adjustment_count": 0,
        "current_version": 1,
    }


def generate_recommendations(policy: PolicyDocument | None = None) -> RecommendationRunResult:
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
        # Three steps, and the explanations sit between two sessions on
        # purpose: they are slow external calls (hours one at a time), and a
        # connection held idle that long is dropped by Azure SQL, which used to
        # fail the whole run at the very end.
        with session_factory() as session:
            inventory_run_id = repository.latest_inventory_run(session)
            oar_run_id = repository.latest_oar_run(session)
            if oar_run_id is not None:
                # A donor a user removed from a material must not come back because the OAR run was
                # reused: re-select any target whose stored neighbours predate the decision, and bring
                # its unsubmitted recommendation into line. Free when nothing has been excluded. Raises
                # (failing this run, loudly) rather than build from a selection that undoes the decision.
                # Imported here: oar_exclusions imports this module for the row mapping.
                from app.initiatives.i7.recommendations import oar_exclusions

                oar_exclusions.apply_exclusions_before_generation(session, oar_run_id, policy)
            # Pinned to the selected inventory run's own forecast_run_id, not
            # independently re-derived from the latest feature run: the two
            # can drift apart once the feature store has advanced past the
            # last executed forecast run, at which point an independent
            # "latest forecast run for the latest feature run" lookup returns
            # None even though the selected inventory run's own forecast join
            # succeeded and already produced real SS/ROP values (Part 28
            # audit finding). Reading the inventory run's own forecast_run_id
            # instead means this can never disagree with the run SS/ROP were
            # actually computed from.
            forecast_run_id = (
                repository.forecast_run_for_inventory_run(session, inventory_run_id)
                if inventory_run_id is not None
                else None
            )

            built: list[BuiltRecommendation] = []

            if inventory_run_id is not None:
                for row in repository.load_normal_inputs(
                    session, inventory_run_id, forecast_run_id
                ):
                    built.append(builder.build_normal_recommendation(row, policy))

            if oar_run_id is not None:
                for row in repository.load_oar_inputs(session, oar_run_id):
                    built.append(builder.build_oar_recommendation(row, policy))
                for row in repository.load_uncovered_oar_targets(session, oar_run_id):
                    built.append(builder.deferred_recommendation(row, policy))

        workers = get_settings().i7_rationale_concurrency
        logger.info(
            "recommendation generation: %d built, writing explanations %d at a time",
            len(built),
            workers,
        )
        rationales = _generate_rationales(built, workers)

        rows = [_to_row(item, outcome) for item, outcome in zip(built, rationales)]
        for row in rows:
            result.status_counts[row["status"]] = (
                result.status_counts.get(row["status"], 0) + 1
            )

        with session_factory() as session:
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
