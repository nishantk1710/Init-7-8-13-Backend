"""OAR similarity orchestration.

    Phase 3 cold-start targets + classified candidate population
        -> hard filter -> [two-stage shortlist] -> three dimensions
        -> combined score + evidence -> rank -> Top-K
        -> confidence -> weighted estimate (only from Phase 5 SUCCESS neighbours)

Reads Phase 3 features, Phase 2 staging, and Phase 5 inventory calculations.
Never ``raw_*``. Never recomputes the Phase 3 history gate or OAR rule -- a
target is whatever Phase 3 already routed to NO_HISTORY or COLD_START.

Two ways to the same answer. :func:`evaluate_target` scores every eligible
candidate exactly -- the reference, and what ``I7_OAR_RETRIEVAL_MODE=
exhaustive`` runs. ``oar.engine.evaluate_targets`` (the default) shortlists
first and scores only the shortlist. Both build their results through
:func:`scored_entry` and :func:`finalise_target`, so they can differ only in
which candidates were scored; the tests hold them to identical output.
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
    identity,
    ranking,
    repository,
    scoring,
    structured_similarity,
)
from app.initiatives.i7.oar.retrieval import RetrievalConfig
from app.initiatives.i7.oar.text_similarity import (
    BgeEmbeddingProvider,
    EmbeddingCache,
    EmbeddingProvider,
    EmbeddingStats,
    PrecomputedEmbeddingProvider,
)
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
    RetrievalTally,
    SimilarityStatus,
    StageCounts,
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
    embeddings_generated: int = 0
    """Descriptions embedded by the model in this run -- 0 when every one was
    already stored by an earlier run."""
    embedding_cache_hit_rate: float = 0.0
    scored_fraction: float = 0.0
    """Exact scores computed, as a share of eligible (target, donor) pairs.
    1.0 under exhaustive retrieval."""
    stage_totals: dict[str, int] = field(default_factory=dict)
    """Every ``StageCounts`` field summed over the run's targets, so how many candidates survived each step
    of retrieval is on record for the run as well as for each target."""


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

        text = text_score(
            target.description,
            candidate.description,
            provider,
            embedding_weight=Decimal(str(policy.text_embedding_weight)),
            fuzzy_weight=Decimal(str(policy.text_fuzzy_weight)),
        )
    else:
        # Reported as a missing feature, the same as an absent description, so
        # the stored explanation says the description was not compared.
        text = DimensionScore(
            value=None,
            status=SimilarityStatus.NOT_AVAILABLE_NO_MODEL,
            missing_features=1,
            missing=("description",),
        )

    same_group = bool(
        target.material_group
        and candidate.material_group
        and target.material_group == candidate.material_group
    )
    validate_identity = policy.identity_validation
    combined = scoring.combine(
        struct,
        text,
        business,
        Decimal(str(policy.structural_weight)),
        Decimal(str(policy.text_weight)),
        Decimal(str(policy.business_weight)),
        minimum_evidence_features=policy.minimum_evidence_features,
        identity_evidence_features=tuple(policy.identity_evidence_features),
        same_material_group=same_group,
        # With identity validation on, identity is its own stage below; the combine-time rule is the
        # oar-similarity-8/9 one, kept only for the switch that turns validation off.
        identity_text_floor=(
            Decimal(str(policy.minimum_identity_text_similarity))
            if policy.minimum_identity_text_similarity is not None and not validate_identity
            else None
        ),
    )
    if not validate_identity:
        return combined
    verdict = identity.assess_descriptions(
        target.description,
        candidate.description,
        same_material_group=same_group,
        text_similarity=text.value if text.is_available else None,
        config=identity.config_for(policy),
    )
    return scoring.with_identity(combined, verdict)


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


def scored_entry(
    target: CandidateAttributes,
    candidate: CandidateAttributes,
    combined: CombinedScore,
    inventory_values: dict[tuple[str, str], dict],
) -> tuple[str, str, CombinedScore, bool | None, dict]:
    """One candidate's score plus what ranking and persistence need about it.

    ``same_circuit`` is always ``None``: circuit is not evaluated (see
    ``CandidateAttributes.circuit``), and two missing circuits are never read as
    the same circuit.
    """
    same_group = (
        None
        if target.material_group is None or candidate.material_group is None
        else target.material_group == candidate.material_group
    )
    inventory = inventory_values.get(
        (candidate.sap_material_number, candidate.sap_plant_code)
    )
    return (
        candidate.sap_material_number,
        candidate.sap_plant_code,
        combined,
        None,
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


def finalise_target(
    target: CandidateAttributes,
    scored: list[tuple[str, str, CombinedScore, bool | None, dict]],
    *,
    population_size: int,
    eligible_count: int,
    rejections: dict[str, int],
    low_evidence: int,
    shortlisted: int,
    retrieval: str,
    policy: PolicyDocument,
    tally: RetrievalTally | None = None,
) -> OarResult:
    """Ranking, confidence and the weighted estimate for one target.

    Shared by the exhaustive reference (:func:`evaluate_target`) and the
    two-stage engine (``oar.engine``), so the two can differ only in WHICH
    candidates were scored, never in what happens to the scores.
    """
    if eligible_count == 0:
        return OarResult(
            sap_material_number=target.sap_material_number,
            sap_plant_code=target.sap_plant_code,
            status=OarStatus.NO_ELIGIBLE_NEIGHBORS,
            confidence=OarConfidence.LOW,
            neighbours=(),
            estimate=OarEstimate(
                status=estimate_module.EstimateStatus.NOT_EVALUABLE_NO_NEIGHBORS
            ),
            candidates_considered=population_size,
            eligible_candidates=0,
            rejection_counts=tuple(sorted(rejections.items())),
            best_similarity=None,
            generated_at=datetime.now(timezone.utc),
            detail="no candidate satisfied criticality, active-status and "
            "history hard constraints",
            low_evidence_candidates=0,
            shortlisted_candidates=0,
            retrieval=retrieval,
            stage_counts=StageCounts(
                donor_pool=population_size,
                duplicates_dropped=tally.duplicates_dropped if tally else 0,
                hard_filter_passes=0,
                structure_shortlist=None,
                text_shortlist=None,
                fused_shortlist=None,
                merged_candidates=0,
                identity_conflicts=0,
                identity_insufficient=0,
                identity_passes=0,
                score_floor_passes=0,
                final_neighbours=0,
            ),
        )

    minimum_similarity = Decimal(str(policy.similarity.minimum_similarity))
    scored = ranking.unique_entries(scored)
    neighbours = ranking.select_top_k(
        scored, policy.similarity.maximum_neighbours, minimum_similarity=minimum_similarity
    )
    identity_passes, floor_passes, final = ranking.stage_tally(
        scored, minimum_similarity, policy.similarity.maximum_neighbours
    )
    tally = tally or RetrievalTally()
    scored_conflicts = sum(1 for e in scored if e[2].identity is not None and e[2].identity.conflict)
    scored_insufficient = sum(
        1
        for e in scored
        if e[2].identity is not None and not e[2].identity.passed and not e[2].identity.conflict
    )
    stage_counts = StageCounts(
        donor_pool=population_size,
        duplicates_dropped=tally.duplicates_dropped,
        hard_filter_passes=eligible_count,
        structure_shortlist=tally.structure_shortlist,
        text_shortlist=tally.text_shortlist,
        fused_shortlist=tally.fused_shortlist,
        merged_candidates=len(scored),
        identity_conflicts=(
            tally.identity_conflicts if tally.identity_conflicts is not None else scored_conflicts
        ),
        identity_insufficient=(
            tally.identity_insufficient
            if tally.identity_insufficient is not None
            else scored_insufficient
        ),
        identity_passes=identity_passes,
        score_floor_passes=floor_passes,
        final_neighbours=final,
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
        strategy=policy.similarity.estimator,
        material_difference_ratio=Decimal(str(policy.similarity.estimator_material_difference_ratio)),
    )
    best = max(
        (n.score.combined for n in neighbours if n.score.combined is not None),
        default=None,
    )

    detail = None
    if low_evidence and len(neighbours) < policy.similarity.minimum_neighbours:
        detail = (
            f"{low_evidence} eligible candidate(s) refused for too little evidence "
            "(see SimilarityPolicy.minimum_evidence_features / identity_evidence_features)"
        )

    return OarResult(
        sap_material_number=target.sap_material_number,
        sap_plant_code=target.sap_plant_code,
        status=OarStatus.SUCCESS,
        confidence=grade,
        neighbours=tuple(neighbours),
        estimate=oar_estimate,
        candidates_considered=population_size,
        eligible_candidates=eligible_count,
        rejection_counts=tuple(sorted(rejections.items())),
        best_similarity=best,
        generated_at=datetime.now(timezone.utc),
        detail=detail,
        low_evidence_candidates=low_evidence,
        shortlisted_candidates=shortlisted,
        retrieval=retrieval,
        stage_counts=stage_counts,
        rejected_candidates=ranking.rejected_sample(scored, minimum_similarity),
    )


def dedupe_donors(
    population: list[CandidateAttributes],
) -> tuple[list[CandidateAttributes], int]:
    """One donor per material-plant, the first occurrence kept, and how many were dropped.

    A material-plant is the donor's identity. If a join upstream ever lists one twice, scoring it twice
    would count it twice in the weighted estimate and again in the neighbour list, so it is dropped here
    and the count is reported rather than hidden. A population with no duplicates is returned as it came.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[CandidateAttributes] = []
    for candidate in population:
        key = (candidate.sap_material_number, candidate.sap_plant_code)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    if len(unique) == len(population):
        return population, 0
    return unique, len(population) - len(unique)


def evaluate_target(
    target: CandidateAttributes,
    candidate_population: list[CandidateAttributes],
    provider: EmbeddingProvider,
    price_range: Decimal | None,
    inventory_values: dict[tuple[str, str], dict],
    policy: PolicyDocument,
) -> OarResult:
    """The full similarity pipeline for one cold-start target, exhaustively.

    Every eligible candidate gets the exact score. This is the reference the
    two-stage engine (``oar.engine.evaluate_targets``) is tested against, and
    what ``I7_OAR_RETRIEVAL_MODE=exhaustive`` runs.
    """
    candidate_population, duplicates = dedupe_donors(candidate_population)
    eligible, rejections = eligibility.filter_candidates(
        target, candidate_population, policy.similarity.minimum_neighbour_history_months
    )

    scored = [
        scored_entry(
            target,
            candidate,
            _score_pair(target, candidate, provider, price_range, policy.similarity),
            inventory_values,
        )
        for candidate in eligible
    ]
    low_evidence = sum(1 for entry in scored if not entry[2].evidence_sufficient)

    return finalise_target(
        target,
        scored,
        population_size=len(candidate_population),
        eligible_count=len(eligible),
        rejections=rejections,
        low_evidence=low_evidence,
        shortlisted=len(eligible),
        retrieval="exhaustive",
        policy=policy,
        tally=RetrievalTally(duplicates_dropped=duplicates),
    )


def _trace_text(trace: tuple[tuple[str, str], ...]) -> str | None:
    if not trace:
        return None
    return ";".join(f"{key}={value}" for key, value in trace)[:1000]


def _rejection_summary(outcome: OarResult) -> str | None:
    """``reason=count;...`` -- the eligibility rejections, then the identity stage's refusals.

    Identity refusals are counted over the whole eligible pool, so a target whose neighbours were all
    filters can still show that twelve spray nozzles and a proximity sensor were turned away.
    """
    parts = [f"{reason}={count}" for reason, count in outcome.rejection_counts]
    counts = outcome.stage_counts
    if counts is not None:
        if counts.identity_conflicts:
            parts.append(f"IDENTITY_COMPONENT_CONFLICT={counts.identity_conflicts}")
        if counts.identity_insufficient:
            parts.append(f"IDENTITY_INSUFFICIENT_EVIDENCE={counts.identity_insufficient}")
    return ";".join(parts)[:500] or None


def _target_row(run_id: int, outcome: OarResult) -> dict[str, Any]:
    detail = "; ".join(
        part for part in (outcome.detail, outcome.estimate.detail) if part
    ) or None
    return {
        "oar_run_id": run_id,
        "sap_material_number": outcome.sap_material_number,
        "sap_plant_code": outcome.sap_plant_code,
        "status": outcome.status.value,
        "confidence": outcome.confidence.value,
        "candidates_considered": outcome.candidates_considered,
        "eligible_candidates": outcome.eligible_candidates,
        "neighbour_count": len(outcome.neighbours),
        "best_similarity": outcome.best_similarity,
        "rejection_summary": _rejection_summary(outcome),
        "estimate_status": outcome.estimate.status.value,
        "estimate_label": (
            outcome.estimate.label if outcome.estimate.status.value == "SUCCESS" else None
        ),
        "safety_stock": outcome.estimate.safety_stock,
        "rop": outcome.estimate.rop,
        "max_stock": outcome.estimate.max_stock,
        "inventory_eligible_neighbours": outcome.estimate.inventory_eligible_neighbours,
        "inventory_ineligible_neighbours": outcome.estimate.inventory_ineligible_neighbours,
        "estimate_trace": _trace_text(outcome.estimate.trace),
        "detail": detail[:500] if detail else None,
        "low_evidence_candidates": outcome.low_evidence_candidates,
        "shortlisted_candidates": outcome.shortlisted_candidates,
    }


def _neighbour_row(run_id: int, outcome: OarResult, neighbour: Neighbour) -> dict[str, Any]:
    return {
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
        "score_explanation": scoring.explain(neighbour.score),
    }


def prepare_embeddings(
    provider: EmbeddingProvider,
    descriptions,
    *,
    store=None,
    batch_size: int = 64,
) -> tuple[dict | None, EmbeddingStats | None]:
    """One vector per unique description for this run, or ``None`` without a model.

    Memory first, then ``store`` (normally ``SqlEmbeddingStore``), then the
    model in bounded batches for whatever is left.
    """
    if not getattr(provider, "is_available", True):
        return None, None
    cache = EmbeddingCache(provider, store=store, batch_size=batch_size)
    vectors = cache.ensure(descriptions)
    return vectors, cache.stats


def run_oar_similarity(
    policy: PolicyDocument | None = None,
    *,
    provider: EmbeddingProvider | None = None,
    force: bool = False,
    config: RetrievalConfig | None = None,
) -> OarRunResult:
    """Compute similarity-based neighbours and estimates for every cold-start
    material-plant.

    Idempotent: a run is identified by feature run, inventory run, policy,
    algorithm version, embedding-model version and retrieval configuration,
    matching the pattern of every earlier phase.

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

    ``config`` defaults to the ``I7_OAR_*`` settings. Two-stage retrieval
    (``oar.engine``) is the default; ``exhaustive`` scores every eligible
    candidate exactly, as before.
    """
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.initiatives.i7.oar import engine as engine_module
    from app.initiatives.i7.oar.embedding_store import SqlEmbeddingStore
    from app.initiatives.i7.policy.dev_fixtures import default_policy

    settings = get_settings()
    policy = policy or default_policy()
    provider = provider or BgeEmbeddingProvider(batch_size=settings.i7_oar_embedding_batch_size)
    config = config or RetrievalConfig.from_settings(settings)
    session_factory = get_sessionmaker()

    if provider.is_available:
        logger.info(
            "OAR run: text similarity ON (%s; %.2f x embedding cosine + %.2f x token_set_ratio)",
            provider.model_name,
            policy.similarity.text_embedding_weight,
            policy.similarity.text_fuzzy_weight,
        )
        if not policy.similarity.text_floors_calibrated:
            logger.warning(
                "OAR run: the identity text floors (%s other-group, %s same-group) were set for a MiniLM "
                "cosine and have NOT been calibrated for the current text score (BGE + fuzzy). The identity "
                "gate is provisional: see SimilarityPolicy.text_floors_calibrated and "
                "scripts.oar_identity_review floors.",
                policy.similarity.minimum_identity_text_similarity,
                policy.similarity.same_group_minimum_text_similarity,
            )
    else:
        logger.warning(
            "OAR run: text similarity is OFF (the BGE model or rapidfuzz is not available) -- neighbours are "
            "scored on material group, unit of measure and price only, never on what the description says. "
            "Install requirements-oar-text.txt to switch it on."
        )
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

        existing = session.execute(
            select(OarRun).where(
                OarRun.feature_run_id == feature_run_id,
                OarRun.inventory_run_id == inventory_run_id,
                OarRun.policy_id == policy.policy_id,
                OarRun.policy_version == policy.policy_version,
                OarRun.algorithm_version == ALGORITHM_VERSION,
                OarRun.embedding_model_version == embedding_version,
                OarRun.retrieval_config == config.describe(),
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
            retrieval_config=config.describe(),
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
            inventory_values = repository.load_inventory_values(session)

        logger.info(
            "OAR run %d: %d targets, %d candidates in the donor population (%s)",
            run_id,
            len(targets),
            len(candidates),
            config.describe(),
        )

        store = (
            SqlEmbeddingStore(session_factory) if settings.i7_oar_embedding_cache else None
        )
        embeddings, embedding_stats = prepare_embeddings(
            provider,
            [t.description for t in targets] + [c.description for c in candidates],
            store=store,
            batch_size=settings.i7_oar_embedding_batch_size,
        )
        if embedding_stats is not None:
            logger.info(
                "OAR run %d: embeddings %d unique, %d from memory, %d from the store, "
                "%d generated in %d batch(es)",
                run_id,
                embedding_stats.unique,
                embedding_stats.memory_hits,
                embedding_stats.store_hits,
                embedding_stats.generated,
                embedding_stats.batches,
            )
            result.embeddings_generated = embedding_stats.generated
            result.embedding_cache_hit_rate = embedding_stats.hit_rate
        result.text_similarity_available = embeddings is not None

        scoring_provider = PrecomputedEmbeddingProvider(
            embeddings, provider.model_name, provider.model_version
        )

        stats = engine_module.EngineStats()
        if config.mode == "exhaustive":
            outcomes = (
                evaluate_target(
                    target, candidates, scoring_provider, price_span, inventory_values, policy
                )
                for target in targets
            )
        else:
            outcomes = engine_module.evaluate_targets(
                targets,
                candidates,
                provider=scoring_provider,
                embeddings=embeddings,
                price_range=price_span,
                inventory_values=inventory_values,
                policy=policy,
                config=config,
                stats=stats,
            )

        write_batch = max(1, settings.i7_oar_write_batch_size)
        with session_factory() as session:
            target_rows: list[dict[str, Any]] = []
            neighbour_rows: list[dict[str, Any]] = []

            for outcome in outcomes:
                count = len(outcome.neighbours)
                bucket = "0" if count == 0 else "1-2" if count <= 2 else "3-4" if count <= 4 else "5+"
                result.neighbour_count_distribution[bucket] = (
                    result.neighbour_count_distribution.get(bucket, 0) + 1
                )
                result.confidence_counts[outcome.confidence.value] = (
                    result.confidence_counts.get(outcome.confidence.value, 0) + 1
                )
                result.estimate_status_counts[outcome.estimate.status.value] = (
                    result.estimate_status_counts.get(outcome.estimate.status.value, 0) + 1
                )
                if outcome.stage_counts is not None:
                    for name, value in outcome.stage_counts._asdict().items():
                        if value is not None:
                            result.stage_totals[name] = result.stage_totals.get(name, 0) + value
                target_rows.append(_target_row(run_id, outcome))
                neighbour_rows.extend(
                    _neighbour_row(run_id, outcome, neighbour) for neighbour in outcome.neighbours
                )
                result.targets_evaluated += 1

                # Written in batches, inside the one transaction committed below:
                # memory stays bounded by the batch, and a failure part-way still
                # leaves no partial run visible.
                if len(target_rows) >= write_batch:
                    session.bulk_insert_mappings(OarTargetResult, target_rows)
                    session.bulk_insert_mappings(OarNeighbour, neighbour_rows)
                    target_rows, neighbour_rows = [], []

            if target_rows:
                session.bulk_insert_mappings(OarTargetResult, target_rows)
                session.bulk_insert_mappings(OarNeighbour, neighbour_rows)
            session.commit()

        result.scored_fraction = stats.scored_fraction if config.mode != "exhaustive" else 1.0

        with session_factory() as session:
            stored = session.get(OarRun, run_id)
            stored.status = STATUS_SUCCEEDED
            stored.targets_evaluated = result.targets_evaluated
            if embeddings is None:
                # The provider said it was available but produced nothing; record
                # the run as made without text similarity, which is what it was.
                stored.embedding_model = None
                stored.embedding_model_version = NO_EMBEDDING_MODEL_VERSION
            stored.finished_at = datetime.now(timezone.utc)
            session.commit()

        logger.info(
            "OAR run %d: %d targets; exact scores for %.1f%% of eligible pairs; "
            "confidence %s; estimates %s",
            run_id,
            result.targets_evaluated,
            100 * result.scored_fraction,
            result.confidence_counts,
            result.estimate_status_counts,
        )
        logger.info("OAR run %d: candidates per stage, summed over targets: %s", run_id, result.stage_totals)
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
