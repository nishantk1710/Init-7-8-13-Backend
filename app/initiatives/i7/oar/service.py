"""OAR similarity orchestration.

    Phase 3 cold-start targets + classified candidate population
        -> hard filter -> three dimensions -> combined score -> rank -> Top-K
        -> confidence -> weighted estimate (only from Phase 5 SUCCESS neighbours)

Reads Phase 3 features, Phase 2 staging, and Phase 5 inventory calculations.
Never ``raw_*``. Never recomputes the Phase 3 history gate or OAR rule -- a
target is whatever Phase 3 already routed to NO_HISTORY or COLD_START.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.initiatives.i7.contracts import Criticality
from app.initiatives.i7.errors import PolicyNotConfiguredError
from app.initiatives.i7.oar import (
    business_similarity,
    confidence as confidence_module,
    eligibility,
    estimate as estimate_module,
    ranking,
    repository,
    scoring,
    structured_similarity,
)
from app.initiatives.i7.oar.text_similarity import EmbeddingProvider, MiniLmEmbeddingProvider
from app.initiatives.i7.oar.types import (
    ALGORITHM_VERSION,
    CandidateAttributes,
    CombinedScore,
    DimensionScore,
    Neighbour,
    OarConfidence,
    OarEstimate,
    OarResult,
    OarStatus,
    SimilarityStatus,
)
from app.initiatives.i7.policy import PolicyDocument
from app.models.i7_oar import OarNeighbour, OarRun, OarTargetResult

logger = logging.getLogger(__name__)

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

NO_EMBEDDING_MODEL_VERSION = "none"
"""Recorded on the run when the embedding library is unavailable, so a run made
without text similarity is never mistaken for one that had it."""


@dataclass
class OarRunResult:
    run_id: int | None = None
    status: str = STATUS_SUCCEEDED
    targets_evaluated: int = 0
    reused_existing: bool = False
    confidence_counts: dict[str, int] = field(default_factory=dict)
    estimate_status_counts: dict[str, int] = field(default_factory=dict)
    neighbour_count_distribution: dict[str, int] = field(default_factory=dict)
    text_similarity_available: bool = False
    error: str | None = None


def _score_pair(
    target: CandidateAttributes,
    candidate: CandidateAttributes,
    provider: EmbeddingProvider,
    price_range: Decimal | None,
    policy,
) -> CombinedScore:
    struct = structured_similarity.score(target, candidate)
    business = business_similarity.score(target, candidate, price_range)

    if provider.is_available:
        from app.initiatives.i7.oar.text_similarity import score as text_score

        text = text_score(target.description, candidate.description, provider)
    else:
        text = DimensionScore(value=None, status=SimilarityStatus.NOT_AVAILABLE_NO_MODEL)

    return scoring.combine(
        struct,
        text,
        business,
        Decimal(str(policy.structural_weight)),
        Decimal(str(policy.text_weight)),
        Decimal(str(policy.business_weight)),
    )


OAR_TEMPORARY_CRITICALITY = Criticality.NORMAL
"""TEMPORARY development/implementation policy, not a VZI business rule.

The real Criticality x Circuit service-level matrix is unsigned (Solution
Design Rule 1: "THIS MUST COME FROM VEDANTA, NOT FROM CODE"). Each
neighbour's own Phase 5 Safety Stock/ROP/Max already legitimately blocks on
that until Vedanta signs it -- unchanged here, and untouched by this
constant, which affects nothing about how neighbour values are computed.

What this constant unblocks is narrower: whether the OAR *estimate itself*
should even attempt to borrow from neighbours, a decision that today reads
the *global* ``policy.service_level.is_configured`` boolean (true only once
the whole matrix is signed, or the dev-mock fixture is loaded). Resolving
that boolean at a fixed, fake criticality (NORMAL) instead lets the OAR gate
track "is a service level resolvable at all" for development purposes,
without deciding -- and without ever overwriting -- any material's actual
criticality. Delete this constant and delete
``_oar_service_level_configured`` the day a real Vedanta OAR service-level
policy exists; ``evaluate_target`` would then go back to reading
``policy.service_level.is_configured`` directly, or whatever the real rule
turns out to require.
"""


def _oar_service_level_configured(policy: PolicyDocument) -> bool:
    """Whether the OAR estimate may proceed past its service-level gate.

    TEMPORARY: resolves the signed (or dev-mock, see
    ``policy.dev_fixtures.default_policy``) service-level matrix at a fixed
    NORMAL criticality, purely to decide whether *some* service level is
    resolvable at all -- this never reads, writes, or overrides any
    material's actual ``MaterialFeature.criticality``, and computes no
    Z-score itself (``service_level_for`` returns a service-level fraction;
    Z is derived downstream, in Phase 5's own ``service_level.resolve()``,
    exactly as before). If the matrix is unsigned, or signed but silent on
    NORMAL, this returns ``False`` and the OAR estimate reports
    ``NOT_EVALUABLE_SERVICE_LEVEL_UNSET`` -- the same honest block as today,
    never a fabricated success.
    """
    if not policy.service_level.is_configured:
        return False
    try:
        policy.service_level.service_level_for(OAR_TEMPORARY_CRITICALITY)
    except PolicyNotConfiguredError:
        return False
    return True


def evaluate_target(
    target: CandidateAttributes,
    candidate_population: list[CandidateAttributes],
    provider: EmbeddingProvider,
    price_range: Decimal | None,
    inventory_values: dict[tuple[str, str], dict],
    policy: PolicyDocument,
) -> OarResult:
    """The full similarity pipeline for one cold-start target."""
    eligible, rejections = eligibility.filter_candidates(
        target, candidate_population, policy.similarity.minimum_neighbour_history_months
    )

    if not eligible:
        return OarResult(
            sap_material_number=target.sap_material_number,
            sap_plant_code=target.sap_plant_code,
            status=OarStatus.NO_ELIGIBLE_NEIGHBORS,
            confidence=OarConfidence.LOW,
            neighbours=(),
            estimate=OarEstimate(
                status=estimate_module.EstimateStatus.NOT_EVALUABLE_NO_NEIGHBORS
            ),
            candidates_considered=len(candidate_population),
            eligible_candidates=0,
            rejection_counts=tuple(sorted(rejections.items())),
            best_similarity=None,
            generated_at=datetime.now(timezone.utc),
            detail="no candidate satisfied criticality, active-status and "
            "history hard constraints",
        )

    scored: list[tuple[str, str, CombinedScore, bool | None, dict]] = []
    for candidate in eligible:
        combined = _score_pair(target, candidate, provider, price_range, policy.similarity)
        same_circuit = (
            None
            if target.circuit is None or candidate.circuit is None
            else target.circuit == candidate.circuit
        )
        same_group = (
            None
            if target.material_group is None or candidate.material_group is None
            else target.material_group == candidate.material_group
        )
        inventory = inventory_values.get(
            (candidate.sap_material_number, candidate.sap_plant_code)
        )
        scored.append(
            (
                candidate.sap_material_number,
                candidate.sap_plant_code,
                combined,
                same_circuit,
                {
                    "same_material_group": same_group,
                    "criticality": candidate.criticality,
                    "history_months": candidate.history_months,
                    "is_active": candidate.is_active,
                    "safety_stock": inventory["safety_stock"] if inventory else None,
                    "rop": inventory["rop"] if inventory else None,
                    "max_stock": inventory["max_stock"] if inventory else None,
                    "inventory_eligible": inventory is not None,
                },
            )
        )

    minimum_similarity = Decimal(str(policy.similarity.minimum_similarity))
    neighbours = ranking.select_top_k(
        scored, policy.similarity.maximum_neighbours, minimum_similarity=minimum_similarity
    )
    grade = confidence_module.grade(
        neighbours,
        policy.confidence.oar_high_minimum_neighbours,
        Decimal(str(policy.confidence.oar_high_minimum_similarity)),
        policy.confidence.oar_medium_minimum_neighbours,
        Decimal(str(policy.confidence.oar_medium_minimum_similarity)),
    )
    oar_estimate = estimate_module.calculate(
        list(neighbours),
        _oar_service_level_configured(policy),
        minimum_neighbours=policy.similarity.minimum_neighbours,
        minimum_similarity=minimum_similarity,
    )
    best = max(
        (n.score.combined for n in neighbours if n.score.combined is not None),
        default=None,
    )

    return OarResult(
        sap_material_number=target.sap_material_number,
        sap_plant_code=target.sap_plant_code,
        status=OarStatus.SUCCESS,
        confidence=grade,
        neighbours=tuple(neighbours),
        estimate=oar_estimate,
        candidates_considered=len(candidate_population),
        eligible_candidates=len(eligible),
        rejection_counts=tuple(sorted(rejections.items())),
        best_similarity=best,
        generated_at=datetime.now(timezone.utc),
    )


def _trace_text(trace: tuple[tuple[str, str], ...]) -> str | None:
    if not trace:
        return None
    return ";".join(f"{key}={value}" for key, value in trace)[:1000]


def run_oar_similarity(
    policy: PolicyDocument | None = None,
    *,
    provider: EmbeddingProvider | None = None,
    force: bool = False,
) -> OarRunResult:
    """Compute similarity-based neighbours and estimates for every cold-start
    material-plant.

    Idempotent: a run is identified by feature run, inventory run, policy and
    embedding-model version, matching the pattern of every earlier phase.

    ``policy`` defaults to the dev-fixture-aware default (see
    ``app.initiatives.i7.policy.dev_fixtures.default_policy``), the same
    fallback ``run_forecasting`` and ``run_inventory_calculations`` already
    use, rather than a bare ``PolicyDocument()``. Without this, a dev run
    with ``I7_DEV_MOCK_SERVICE_LEVEL`` set produced Phase 5 donor values that
    Phase 6 then refused to borrow: ``_oar_service_level_configured`` read an
    unconfigured matrix off a policy the flag had never reached, and every
    target reported ``NOT_EVALUABLE_SERVICE_LEVEL_UNSET`` while its
    neighbours' own numbers sat in ``i7_inventory_calculation``. Production
    is unaffected -- with neither dev flag set, ``default_policy()`` returns
    exactly the ``PolicyDocument()`` this line used to construct.
    """
    from app.initiatives.i7.policy.dev_fixtures import default_policy

    policy = policy or default_policy()
    provider = provider or MiniLmEmbeddingProvider()
    session_factory = get_sessionmaker()

    embedding_version = provider.model_version if provider.is_available else (
        NO_EMBEDDING_MODEL_VERSION
    )

    with session_factory() as session:
        feature_run_id = session.execute(
            text("select max(id) from i7_feature_run where status = 'succeeded'")
        ).scalar()
        inventory_run_id = session.execute(
            text("select max(id) from i7_inventory_run where status = 'succeeded'")
        ).scalar()

        from sqlalchemy import select

        existing = session.execute(
            select(OarRun).where(
                OarRun.feature_run_id == feature_run_id,
                OarRun.inventory_run_id == inventory_run_id,
                OarRun.policy_id == policy.policy_id,
                OarRun.policy_version == policy.policy_version,
                OarRun.algorithm_version == ALGORITHM_VERSION,
                OarRun.embedding_model_version == embedding_version,
                OarRun.status == STATUS_SUCCEEDED,
            )
        ).scalar_one_or_none()

        if existing is not None and not force:
            logger.info("OAR run: inputs unchanged since run %d, reusing it", existing.id)
            return OarRunResult(
                run_id=existing.id,
                status=STATUS_SUCCEEDED,
                targets_evaluated=existing.targets_evaluated,
                reused_existing=True,
                text_similarity_available=embedding_version != NO_EMBEDDING_MODEL_VERSION,
            )

        run = OarRun(
            status="running",
            feature_run_id=feature_run_id,
            inventory_run_id=inventory_run_id,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            algorithm_version=ALGORITHM_VERSION,
            embedding_model=provider.model_name if provider.is_available else None,
            embedding_model_version=embedding_version,
            structured_weight=Decimal(str(policy.similarity.structural_weight)),
            text_weight=Decimal(str(policy.similarity.text_weight)),
            business_weight=Decimal(str(policy.similarity.business_weight)),
            top_k=policy.similarity.maximum_neighbours,
            minimum_history_months=policy.similarity.minimum_neighbour_history_months,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    result = OarRunResult(
        run_id=run_id, text_similarity_available=provider.is_available
    )

    try:
        with session_factory() as session:
            logger.info("OAR run %d: loading targets and candidates", run_id)
            targets = repository.load_targets(session)
            candidates = repository.load_candidate_population(session)
            price_span = repository.price_range(candidates)
            inventory_values = repository.load_inventory_values(session, inventory_run_id)

            logger.info(
                "OAR run %d: %d targets, %d candidates in the donor population",
                run_id,
                len(targets),
                len(candidates),
            )

            target_rows: list[dict[str, Any]] = []
            neighbour_rows: list[dict[str, Any]] = []

            for target in targets:
                outcome = evaluate_target(
                    target, candidates, provider, price_span, inventory_values, policy
                )

                bucket = (
                    "0"
                    if len(outcome.neighbours) == 0
                    else "1-2"
                    if len(outcome.neighbours) <= 2
                    else "3-4"
                    if len(outcome.neighbours) <= 4
                    else "5+"
                )
                result.neighbour_count_distribution[bucket] = (
                    result.neighbour_count_distribution.get(bucket, 0) + 1
                )
                result.confidence_counts[outcome.confidence.value] = (
                    result.confidence_counts.get(outcome.confidence.value, 0) + 1
                )
                result.estimate_status_counts[outcome.estimate.status.value] = (
                    result.estimate_status_counts.get(outcome.estimate.status.value, 0) + 1
                )

                target_rows.append(
                    {
                        "oar_run_id": run_id,
                        "sap_material_number": outcome.sap_material_number,
                        "sap_plant_code": outcome.sap_plant_code,
                        "status": outcome.status.value,
                        "confidence": outcome.confidence.value,
                        "candidates_considered": outcome.candidates_considered,
                        "eligible_candidates": outcome.eligible_candidates,
                        "neighbour_count": len(outcome.neighbours),
                        "best_similarity": outcome.best_similarity,
                        "rejection_summary": ";".join(
                            f"{reason}={count}" for reason, count in outcome.rejection_counts
                        )[:500]
                        or None,
                        "estimate_status": outcome.estimate.status.value,
                        "estimate_label": (
                            outcome.estimate.label
                            if outcome.estimate.status.value == "SUCCESS"
                            else None
                        ),
                        "safety_stock": outcome.estimate.safety_stock,
                        "rop": outcome.estimate.rop,
                        "max_stock": outcome.estimate.max_stock,
                        "inventory_eligible_neighbours": (
                            outcome.estimate.inventory_eligible_neighbours
                        ),
                        "inventory_ineligible_neighbours": (
                            outcome.estimate.inventory_ineligible_neighbours
                        ),
                        "estimate_trace": _trace_text(outcome.estimate.trace),
                        "detail": outcome.detail or outcome.estimate.detail,
                    }
                )

                for neighbour in outcome.neighbours:
                    neighbour_rows.append(
                        {
                            "oar_run_id": run_id,
                            "sap_material_number": outcome.sap_material_number,
                            "sap_plant_code": outcome.sap_plant_code,
                            "neighbour_material": neighbour.material,
                            "neighbour_plant": neighbour.plant,
                            "rank": neighbour.rank,
                            "combined_similarity": neighbour.score.combined,
                            "structured_similarity": neighbour.score.structured.value,
                            "text_similarity": neighbour.score.text.value,
                            "business_similarity": neighbour.score.business.value,
                            "score_completeness": neighbour.score.score_completeness,
                            "same_circuit": neighbour.same_circuit,
                            "same_material_group": neighbour.same_material_group,
                            "criticality": neighbour.criticality,
                            "history_months": neighbour.history_months,
                            "is_active": neighbour.is_active,
                            "safety_stock": neighbour.safety_stock,
                            "rop": neighbour.rop,
                            "max_stock": neighbour.max_stock,
                            "inventory_eligible": neighbour.inventory_eligible,
                        }
                    )

            session.bulk_insert_mappings(OarTargetResult, target_rows)
            session.bulk_insert_mappings(OarNeighbour, neighbour_rows)
            result.targets_evaluated = len(target_rows)
            session.commit()

        with session_factory() as session:
            stored = session.get(OarRun, run_id)
            stored.status = STATUS_SUCCEEDED
            stored.targets_evaluated = result.targets_evaluated
            stored.finished_at = datetime.now(timezone.utc)
            session.commit()

        logger.info(
            "OAR run %d: %d targets; confidence %s; estimates %s",
            run_id,
            result.targets_evaluated,
            result.confidence_counts,
            result.estimate_status_counts,
        )
        return result

    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("OAR run %d failed: %s", run_id, detail)
        result.status = STATUS_FAILED
        result.error = detail
        try:
            with session_factory() as session:
                stored = session.get(OarRun, run_id)
                stored.status = STATUS_FAILED
                stored.error = detail[:4000]
                stored.finished_at = datetime.now(timezone.utc)
                session.commit()
        except Exception:
            logger.exception("OAR run %d: could not record the failure either", run_id)
        return result
