"""Re-select the neighbours of individual targets, inside an OAR run that already exists.

A user excluding (or restoring) a donor must change THAT target's neighbours now -- and the next-best
donor must take the freed slot, which the stored neighbour rows cannot supply because they hold only
the winners. So the target goes back through the pipeline the run itself used:

    load target + donors + inventory values + stored embeddings
        -> ``engine.evaluate_targets`` (two-stage retrieval, exact scoring, ``finalise_target``)
        -> if the exclusion left fewer than Top-K neighbours and retrieval was not exhaustive,
           ``service.evaluate_target`` (the exhaustive reference, which scores every eligible donor)

Nothing here scores, ranks or thresholds anything itself. It calls the two paths ``run_oar_similarity``
calls, with the target's exclusions passed to ``finalise_target``, and writes the one target's result
over its rows in the run. The two paths are held to identical output by the engine tests, so falling
back to the reference can only find donors the shortlist missed, never a different answer.

**A refresh is only valid against the data the run was built from.** It reads the current donor and
inventory tables, so :func:`check_run_is_current` refuses -- with a reason -- when the run's feature or
inventory run is no longer the latest, or the policy, algorithm, retrieval configuration or embedding
model differ from the run's. Mixing two generations of inputs in one run's rows would be worse than
declining: the next full run applies the exclusions anyway.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.initiatives.i7.oar import engine as engine_module
from app.initiatives.i7.oar import exclusions as exclusions_module
from app.initiatives.i7.oar import repository, service
from app.initiatives.i7.oar.embedding_store import SqlEmbeddingStore
from app.initiatives.i7.oar.exclusions import DonorKey, TargetKey
from app.initiatives.i7.oar.retrieval import RetrievalConfig
from app.initiatives.i7.oar.text_similarity import EmbeddingProvider, PrecomputedEmbeddingProvider
from app.initiatives.i7.oar.types import ALGORITHM_VERSION, OarResult
from app.initiatives.i7.policy import PolicyDocument
from app.models.i7_oar import OarNeighbour, OarRun, OarTargetResult

logger = logging.getLogger(__name__)


class RefreshError(Exception):
    """A target could not be re-selected. ``code`` is stable; the message says why and what to do."""

    code = "OAR_REFRESH_FAILED"


class RunNotCurrentError(RefreshError):
    code = "OAR_RUN_NOT_CURRENT"


class TargetNotInRunError(RefreshError):
    code = "OAR_TARGET_NOT_IN_RUN"


@dataclass(frozen=True)
class TargetRefresh:
    outcome: OarResult
    used_exhaustive_fallback: bool


def _latest(session: Session, table: str) -> int | None:
    return session.execute(text(f"select max(id) from {table} where status = 'succeeded'")).scalar()


def check_run_is_current(
    session: Session,
    run: OarRun,
    *,
    policy: PolicyDocument,
    provider: EmbeddingProvider,
    config: RetrievalConfig,
) -> None:
    """Raise :class:`RunNotCurrentError` unless a refresh would reproduce what the run did."""
    problems: list[str] = []
    if run.feature_run_id != _latest(session, "i7_feature_run"):
        problems.append("a newer feature run exists")
    if run.inventory_run_id != _latest(session, "i7_inventory_run"):
        problems.append("a newer inventory run exists")
    if run.policy_id != policy.policy_id or run.policy_version != policy.policy_version:
        problems.append("the policy differs from the run's")
    if run.algorithm_version != ALGORITHM_VERSION:
        problems.append("the similarity algorithm version differs from the run's")
    embedding_version = provider.model_version if provider.is_available else service.NO_EMBEDDING_MODEL_VERSION
    if run.embedding_model_version != embedding_version:
        problems.append(
            f"the embedding model here ({embedding_version}) is not the run's ({run.embedding_model_version})"
        )
    if run.retrieval_config is not None and run.retrieval_config != config.describe():
        problems.append("the retrieval configuration differs from the run's")
    if problems:
        raise RunNotCurrentError(
            f"OAR run {run.id} cannot be refreshed in place: {'; '.join(problems)}. "
            "A new OAR run will apply the saved exclusions."
        )


def compute(
    session: Session,
    run: OarRun,
    excluded_by_target: dict[TargetKey, frozenset[DonorKey]],
    *,
    policy: PolicyDocument,
    provider: EmbeddingProvider,
    config: RetrievalConfig,
    settings: Any,
) -> dict[TargetKey, TargetRefresh]:
    """Re-select every target in ``excluded_by_target`` under the exclusions given for it. Writes nothing.

    The exclusions are an argument, not read here, so a caller can ask "what would this target look
    like if D were removed?" before deciding to save the removal.
    """
    keys = list(excluded_by_target)
    check_run_is_current(session, run, policy=policy, provider=provider, config=config)

    targets = repository.load_targets_for(session, keys)
    if not targets:
        return {}
    candidates = repository.load_candidate_population(session)
    price_span = repository.price_range(candidates)
    inventory_values = repository.load_inventory_values(session)

    store = SqlEmbeddingStore(get_sessionmaker()) if settings.i7_oar_embedding_cache else None
    embeddings, _stats = service.prepare_embeddings(
        provider,
        [t.description for t in targets] + [c.description for c in candidates],
        store=store,
        batch_size=settings.i7_oar_embedding_batch_size,
    )
    scoring_provider = PrecomputedEmbeddingProvider(embeddings, provider.model_name, provider.model_version)

    def exhaustive(target) -> OarResult:
        return service.evaluate_target(
            target,
            candidates,
            scoring_provider,
            price_span,
            inventory_values,
            policy,
            excluded=excluded_by_target[(target.sap_material_number, target.sap_plant_code)],
        )

    results: dict[TargetKey, TargetRefresh] = {}
    if config.mode == "exhaustive":
        for target in targets:
            outcome = exhaustive(target)
            results[(outcome.sap_material_number, outcome.sap_plant_code)] = TargetRefresh(outcome, False)
        return results

    top_k = policy.similarity.maximum_neighbours
    for outcome, target in zip(
        engine_module.evaluate_targets(
            targets,
            candidates,
            provider=scoring_provider,
            embeddings=embeddings,
            price_range=price_span,
            inventory_values=inventory_values,
            policy=policy,
            config=config,
            exclusions=excluded_by_target,
        ),
        targets,
    ):
        key = (target.sap_material_number, target.sap_plant_code)
        fell_back = False
        # A shortlist that came up short once donors were left out may simply not have reached the
        # next-best donor. The reference scores every eligible one, so ask it.
        if (
            excluded_by_target[key]
            and len(outcome.neighbours) < top_k
            and outcome.retrieval != "exhaustive"
        ):
            outcome = exhaustive(target)
            fell_back = True
        results[key] = TargetRefresh(outcome, fell_back)
    return results


def persist(session: Session, run_id: int, outcome: OarResult) -> None:
    """Write one target's result over its rows in the run, inside the caller's transaction.

    The target's ``i7_oar_target`` row is updated and its neighbour rows replaced by the re-selected
    list -- the same two shapes a run writes. Neither is committed here.
    """
    target_row = session.execute(
        select(OarTargetResult).where(
            OarTargetResult.oar_run_id == run_id,
            OarTargetResult.sap_material_number == outcome.sap_material_number,
            OarTargetResult.sap_plant_code == outcome.sap_plant_code,
        )
    ).scalar_one_or_none()
    if target_row is None:
        raise TargetNotInRunError(
            f"OAR run {run_id} holds no result for {outcome.sap_material_number}/{outcome.sap_plant_code}."
        )
    for column, value in service._target_row(run_id, outcome).items():
        setattr(target_row, column, value)
    target_row.generated_at = func.now()

    session.execute(
        delete(OarNeighbour).where(
            OarNeighbour.oar_run_id == run_id,
            OarNeighbour.sap_material_number == outcome.sap_material_number,
            OarNeighbour.sap_plant_code == outcome.sap_plant_code,
        )
    )
    session.add_all(
        OarNeighbour(**service._neighbour_row(run_id, outcome, neighbour))
        for neighbour in outcome.neighbours
    )
    session.flush()


def refresh_stale_targets(
    session: Session,
    run_id: int,
    *,
    policy: PolicyDocument,
    provider: EmbeddingProvider,
    config: RetrievalConfig,
    settings: Any,
) -> int:
    """Re-select the targets of ``run_id`` whose stored neighbours do not reflect their exclusions.

    Returns how many were refreshed. Commits on ``session``. Called when a run is reused and before
    recommendations are generated, so a saved decision is honoured even when none of the run's inputs
    changed. Raises :class:`RefreshError` -- it does not carry on -- if a stale target cannot be
    refreshed, because building a recommendation from a result that still contains a donor the user
    removed would quietly undo their decision.
    """
    stale = exclusions_module.stale_targets(session, run_id)
    if not stale:
        return 0
    run = session.get(OarRun, run_id)
    if run is None:
        raise TargetNotInRunError(f"OAR run {run_id} does not exist.")

    logger.info("OAR run %d: %d target(s) hold a stale donor selection; re-selecting them", run_id, len(stale))
    active = exclusions_module.load_active_for(session, stale)
    excluded_by_target = {key: active.get(key, exclusions_module.NO_EXCLUSIONS) for key in stale}
    refreshed = compute(
        session,
        run,
        excluded_by_target,
        policy=policy,
        provider=provider,
        config=config,
        settings=settings,
    )
    for key in stale:
        if key not in refreshed:
            # No longer an OAR target (its scope changed): nothing to re-select, and nothing to repair.
            logger.warning("OAR run %d: %s/%s is no longer an OAR target; skipped", run_id, *key)
            continue
        persist(session, run_id, refreshed[key].outcome)
    session.commit()
    return len(refreshed)
