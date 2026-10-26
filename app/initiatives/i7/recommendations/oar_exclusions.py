"""Removing a similar material from an OAR recommendation, and putting it back.

A reviewer who does not want a donor counted for one target removes it. This module is the one place
that decision is carried out, so the API, the generation run and the tests all go through the same code:

    remove(D)  ->  re-select the target's neighbours without D (``oar.refresh``: the pipeline the run
                   used, so the next-best donor takes D's slot)
               ->  refuse, saving nothing, if that does not leave an evaluable estimate
               ->  one transaction: the exclusion row, the target's neighbour and result rows, and the
                   recommendation's values

Nothing here scores or ranks anything. The estimate that comes back is ``estimate.calculate``'s, so the
minimum-neighbour rule, the weighted median, the rounding and the max-stock floor are the ones every
other OAR result went through. The Min-Max conversion decision is not touched either: it is read from
the feature row by ``builder.build_oar_recommendation``, exactly as when the recommendation was made.

**Who may do it.** Only while the recommendation has not been submitted: READY_FOR_REVIEW, or
SENT_BACK for rework. Once it is in the approval chain, approved or executed, its numbers are what an
approver signed, so the request is refused with 409 rather than changed underneath them.
:func:`authorize_exclusion` is the hook where a role check belongs; see its docstring for what is, and
is not, enforced today.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.initiatives.i7.oar import exclusions as exclusions_module
from app.initiatives.i7.oar import refresh as refresh_module
from app.initiatives.i7.oar.retrieval import RetrievalConfig
from app.initiatives.i7.oar.text_similarity import BgeEmbeddingProvider, EmbeddingProvider
from app.initiatives.i7.oar.types import EstimateStatus
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import builder, rationale, repository
from app.initiatives.i7.recommendations import service as recommendation_service
from app.initiatives.i7.recommendations.types import LifecycleStatus
from app.models.i7_oar import OarDonorExclusion, OarNeighbour, OarRun, OarTargetResult
from app.models.i7_recommendation import Recommendation, RecommendationVersion

logger = logging.getLogger(__name__)

EDITABLE_STATUSES = frozenset(
    {LifecycleStatus.READY_FOR_REVIEW.value, LifecycleStatus.SENT_BACK.value}
)
"""Statuses in which a donor may be removed or restored: before submission, or sent back for rework."""

_SYNCABLE_STATUSES = EDITABLE_STATUSES | {LifecycleStatus.NOT_EVALUABLE.value}
"""Statuses the generation run may bring back in line with their target. NOT_EVALUABLE is included
because a restore can make one evaluable; nobody has signed a number that does not exist."""

_KEEP = frozenset(
    {
        # identity and lineage -- the row stays the same recommendation of the same run
        "recommendation_id", "feature_run_id", "forecast_run_id", "inventory_run_id", "oar_run_id",
        "policy_id", "policy_version", "formula_version",
        # workflow state -- owned by the approval workflow, never by a recalculation
        "status", "chain_index", "adjustment_count", "current_version",
    }
)

MAX_REASON_LENGTH = 500
MAX_ACTOR_LENGTH = 64


class OarExclusionError(Exception):
    """A remove or restore was refused. Nothing was saved. ``status_code`` / ``code`` map to the API."""

    def __init__(self, status_code: int, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True)
class ExclusionOutcome:
    recommendation: Recommendation
    exclusion: OarDonorExclusion
    used_exhaustive_fallback: bool


def authorize_exclusion(actor_id: str, recommendation: Recommendation) -> None:
    """The one place a role check for removing or restoring a similar material belongs.

    **Not enforced yet.** This branch has no trusted authentication (``app/core/security.py`` is a
    placeholder; Entra ID is on the unmerged ``feat/KP/Entra_id``), so there is no verified role to
    check, and the browser's role selector is a view preference, not authority. Today every caller is
    allowed. When ``Principal`` lands, raise ``OarExclusionError(403, "NOT_PERMITTED", ...)`` here --
    the remove and restore paths already call it first, before touching anything.
    """


def edit_block_reason(recommendation: Recommendation) -> str | None:
    """Why this recommendation's similar materials cannot be changed now, or ``None`` if they can."""
    if not recommendation.is_oar or recommendation.oar_run_id is None:
        return "This recommendation was not sized from similar materials."
    if recommendation.status not in EDITABLE_STATUSES:
        return (
            "Similar materials can only be changed before a recommendation is submitted or while it is "
            f"sent back; this one is {recommendation.status}."
        )
    return None


def _clean_actor(actor_id: str) -> str:
    return (actor_id or "").strip()[:MAX_ACTOR_LENGTH] or "unknown-actor"


def _require_editable(recommendation: Recommendation) -> None:
    reason = edit_block_reason(recommendation)
    if reason is not None:
        not_oar = not recommendation.is_oar or recommendation.oar_run_id is None
        raise OarExclusionError(
            409,
            "NOT_AN_OAR_RECOMMENDATION" if not_oar else "RECOMMENDATION_NOT_EDITABLE",
            reason,
            status=recommendation.status,
            editable_statuses=sorted(EDITABLE_STATUSES),
        )


def _default_policy() -> PolicyDocument:
    from app.initiatives.i7.policy.dev_fixtures import default_policy

    return default_policy()


def default_provider() -> EmbeddingProvider:
    """The embedding provider an OAR run uses by default."""
    return BgeEmbeddingProvider(batch_size=get_settings().i7_oar_embedding_batch_size)


def _reselect(
    session: Session,
    recommendation: Recommendation,
    excluded: frozenset[exclusions_module.DonorKey],
    *,
    policy: PolicyDocument,
    provider: EmbeddingProvider,
) -> refresh_module.TargetRefresh:
    """The target's neighbours and estimate with exactly ``excluded`` left out. Writes nothing."""
    key = (recommendation.sap_material_number, recommendation.sap_plant_code)
    settings = get_settings()
    run = session.get(OarRun, recommendation.oar_run_id)
    if run is None:
        raise OarExclusionError(409, "OAR_RUN_MISSING", f"OAR run {recommendation.oar_run_id} no longer exists.")
    try:
        results = refresh_module.compute(
            session,
            run,
            {key: excluded},
            policy=policy,
            provider=provider,
            config=RetrievalConfig.from_settings(settings),
            settings=settings,
        )
    except refresh_module.RefreshError as exc:
        raise OarExclusionError(409, exc.code, str(exc)) from exc
    if key not in results:
        raise OarExclusionError(
            409,
            refresh_module.TargetNotInRunError.code,
            f"{key[0]}/{key[1]} is not an OAR target in the current data, so its similar materials "
            "cannot be re-selected.",
        )
    return results[key]


def _require_evaluable(refreshed: refresh_module.TargetRefresh, policy: PolicyDocument, action: str) -> None:
    """Refuse unless the re-selected target still has an estimate -- and say why not.

    Covers the minimum-neighbour rule (``policy.similarity.minimum_neighbours``, never a literal here)
    and a donor set none of whose members can lend values. A stale number is never reported as fresh.
    """
    outcome = refreshed.outcome
    if outcome.estimate.status is EstimateStatus.SUCCESS:
        return
    minimum = policy.similarity.minimum_neighbours
    raise OarExclusionError(
        409,
        "INSUFFICIENT_NEIGHBOURS_AFTER_EXCLUSION",
        f"Cannot {action}: {len(outcome.neighbours)} similar material(s) would be left and at least "
        f"{minimum} are needed to size this recommendation ({outcome.estimate.status.value}). "
        "Nothing was changed.",
        remaining_neighbours=len(outcome.neighbours),
        minimum_neighbours=minimum,
        estimate_status=outcome.estimate.status.value,
    )


def refresh_recommendation(
    session: Session,
    row: Recommendation,
    policy: PolicyDocument,
    *,
    changed_by: str,
    reason: str,
) -> None:
    """Rebuild this recommendation's calculated fields from its target's current result row.

    The builder, the explanation and the row mapping are the ones ``generate_recommendations`` uses.
    What is NOT touched: the row's identity and run lineage, and its workflow state (``status`` follows
    only between READY_FOR_REVIEW and NOT_EVALUABLE, as a regeneration would give; a SENT_BACK one
    stays SENT_BACK). The values being replaced are kept as an immutable ``RecommendationVersion``
    snapshot and ``current_version`` moves on, so what an approver or reviewer saw before is not lost.
    """
    key = (row.sap_material_number, row.sap_plant_code)
    inputs = repository.load_oar_inputs(session, row.oar_run_id, target=key)
    if not inputs:
        raise OarExclusionError(
            409, "OAR_TARGET_MISSING", f"OAR run {row.oar_run_id} holds no result for {key[0]}/{key[1]}."
        )
    built = builder.build_oar_recommendation(inputs[0], policy)
    values = recommendation_service._to_row(built, rationale.generate_rationale(built))

    newest_snapshot = session.execute(
        select(func.max(RecommendationVersion.version)).where(
            RecommendationVersion.recommendation_id == row.recommendation_id
        )
    ).scalar()
    # recommendation_id is a label shared by regenerated rows, so a snapshot of an earlier row may already
    # hold this version number; take the next free one rather than collide.
    snapshot_version = max(row.current_version, (newest_snapshot or 0) + 1)
    session.add(
        RecommendationVersion(
            recommendation_id=row.recommendation_id,
            version=snapshot_version,
            recommended_safety_stock=row.recommended_safety_stock,
            recommended_rop=row.recommended_rop,
            recommended_max_stock=row.recommended_max_stock,
            reason=reason[:500],
            changed_by=changed_by[:MAX_ACTOR_LENGTH],
        )
    )
    for column, value in values.items():
        if column not in _KEEP:
            setattr(row, column, value)
    if row.status in (LifecycleStatus.READY_FOR_REVIEW.value, LifecycleStatus.NOT_EVALUABLE.value):
        row.status = built.status.value
    row.current_version = snapshot_version + 1
    session.flush()


def exclude_donor(
    session: Session,
    recommendation: Recommendation,
    donor_material: str,
    donor_plant: str,
    *,
    actor_id: str,
    reason: str | None = None,
    provider: EmbeddingProvider | None = None,
    policy: PolicyDocument | None = None,
) -> ExclusionOutcome:
    """Remove one similar material from this recommendation's target. Commits, or raises and saves nothing."""
    actor = _clean_actor(actor_id)
    authorize_exclusion(actor, recommendation)
    _require_editable(recommendation)
    policy = policy or _default_policy()
    provider = provider or default_provider()
    key = (recommendation.sap_material_number, recommendation.sap_plant_code)
    donor = (donor_material, donor_plant)

    active = exclusions_module.active_for_target(session, *key)
    if donor in active:
        raise OarExclusionError(
            409, "DONOR_ALREADY_EXCLUDED", f"{donor_material}/{donor_plant} is already excluded for this material."
        )
    neighbour = session.execute(
        select(OarNeighbour).where(
            OarNeighbour.oar_run_id == recommendation.oar_run_id,
            OarNeighbour.sap_material_number == key[0],
            OarNeighbour.sap_plant_code == key[1],
            OarNeighbour.neighbour_material == donor_material,
            OarNeighbour.neighbour_plant == donor_plant,
        )
    ).scalar_one_or_none()
    if neighbour is None:
        raise OarExclusionError(
            404,
            "DONOR_NOT_A_NEIGHBOUR",
            f"{donor_material}/{donor_plant} is not one of this material's similar materials.",
        )

    refreshed = _reselect(session, recommendation, active | {donor}, policy=policy, provider=provider)
    _require_evaluable(refreshed, policy, "remove this similar material")

    try:
        if exclusions_module.active_for_target(session, *key) != active:
            raise OarExclusionError(
                409,
                "EXCLUSIONS_CHANGED",
                "This material's similar materials were changed by someone else while this was being "
                "calculated. Reload and try again.",
            )
        exclusion = OarDonorExclusion(
            sap_material_number=key[0],
            sap_plant_code=key[1],
            donor_material=donor_material,
            donor_plant=donor_plant,
            donor_similarity=neighbour.combined_similarity,
            donor_rank=neighbour.rank,
            reason=(reason or "").strip()[:MAX_REASON_LENGTH] or None,
            created_by=actor,
            active_slot=OarDonorExclusion.ACTIVE_SLOT,
        )
        session.add(exclusion)
        try:
            session.flush()
        except IntegrityError as exc:
            # Someone else saved the same removal between the check above and now: the unique
            # constraint on (target, donor, active_slot) is what decides, not the check.
            raise OarExclusionError(
                409,
                "DONOR_ALREADY_EXCLUDED",
                f"{donor_material}/{donor_plant} is already excluded for this material.",
            ) from exc
        refresh_module.persist(session, recommendation.oar_run_id, refreshed.outcome)
        refresh_recommendation(
            session,
            recommendation,
            policy,
            changed_by=actor,
            reason=f"Similar material {donor_material}/{donor_plant} removed"
            + (f": {exclusion.reason}" if exclusion.reason else ""),
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return ExclusionOutcome(recommendation, exclusion, refreshed.used_exhaustive_fallback)


def restore_donor(
    session: Session,
    recommendation: Recommendation,
    donor_material: str,
    donor_plant: str,
    *,
    actor_id: str,
    provider: EmbeddingProvider | None = None,
    policy: PolicyDocument | None = None,
) -> ExclusionOutcome:
    """Make an excluded similar material eligible again. The exclusion row is kept, marked restored.

    The donor is not forced back in: it re-enters the target's neighbours only if the unchanged ranking
    puts it in the top-K again, after any donor that was chosen in its place.
    """
    actor = _clean_actor(actor_id)
    authorize_exclusion(actor, recommendation)
    _require_editable(recommendation)
    policy = policy or _default_policy()
    provider = provider or default_provider()
    key = (recommendation.sap_material_number, recommendation.sap_plant_code)
    donor = (donor_material, donor_plant)

    exclusion = session.execute(
        select(OarDonorExclusion).where(
            OarDonorExclusion.sap_material_number == key[0],
            OarDonorExclusion.sap_plant_code == key[1],
            OarDonorExclusion.donor_material == donor_material,
            OarDonorExclusion.donor_plant == donor_plant,
            OarDonorExclusion.active_slot == OarDonorExclusion.ACTIVE_SLOT,
        )
    ).scalar_one_or_none()
    if exclusion is None:
        raise OarExclusionError(
            404, "EXCLUSION_NOT_FOUND", f"{donor_material}/{donor_plant} is not currently excluded for this material."
        )

    active = exclusions_module.active_for_target(session, *key)
    refreshed = _reselect(session, recommendation, active - {donor}, policy=policy, provider=provider)
    _require_evaluable(refreshed, policy, "restore this similar material")

    try:
        if exclusions_module.active_for_target(session, *key) != active:
            raise OarExclusionError(
                409,
                "EXCLUSIONS_CHANGED",
                "This material's similar materials were changed by someone else while this was being "
                "calculated. Reload and try again.",
            )
        exclusion.restored_by = actor
        exclusion.restored_at = func.now()
        exclusion.active_slot = exclusion.id
        session.flush()
        refresh_module.persist(session, recommendation.oar_run_id, refreshed.outcome)
        refresh_recommendation(
            session,
            recommendation,
            policy,
            changed_by=actor,
            reason=f"Similar material {donor_material}/{donor_plant} restored",
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return ExclusionOutcome(recommendation, exclusion, refreshed.used_exhaustive_fallback)


def _in_line_with_target(row: Recommendation, target: OarTargetResult) -> bool:
    """Whether the recommendation still reflects its target's stored result.

    Equal numbers are not enough: a replacement donor can give the very same median, and the row's
    explanation and trace were still written from the donors that are gone. So the best similarity and
    confidence must agree too, and a row written before its target's result was last written (both
    stamped by the database clock) is out of date whatever the numbers say.
    """
    if not (
        row.oar_neighbour_count == target.neighbour_count
        and row.recommended_rop == target.rop
        and row.recommended_safety_stock == target.safety_stock
        and row.recommended_max_stock == target.max_stock
        and row.oar_estimate_status == target.estimate_status
        and row.oar_best_similarity == target.best_similarity
        and row.confidence == target.confidence
    ):
        return False
    try:
        return not (row.updated_at < target.generated_at)
    except TypeError:  # one side timezone-naive: the values agree, so do not rebuild on a guess
        return True


def sync_recommendations(session: Session, oar_run_id: int, policy: PolicyDocument) -> int:
    """Bring unsubmitted recommendations back in line with their target's result, where an exclusion exists.

    ``generate_recommendations`` leaves an existing row alone (its identity has not changed), so a target
    re-selected afterwards -- by a reused run's refresh -- would leave the row holding the old donors'
    numbers. Only targets that have an exclusion at all, and only rows nobody has submitted, are looked
    at; a submitted one is reported in the log and left, because an approver is already holding it.
    Returns how many rows were rebuilt. Commits.
    """
    has_exclusion = (
        select(OarDonorExclusion.id)
        .where(
            OarDonorExclusion.sap_material_number == Recommendation.sap_material_number,
            OarDonorExclusion.sap_plant_code == Recommendation.sap_plant_code,
        )
        .exists()
    )
    pairs = session.execute(
        select(Recommendation, OarTargetResult)
        .join(
            OarTargetResult,
            (OarTargetResult.oar_run_id == Recommendation.oar_run_id)
            & (OarTargetResult.sap_material_number == Recommendation.sap_material_number)
            & (OarTargetResult.sap_plant_code == Recommendation.sap_plant_code),
        )
        .where(Recommendation.oar_run_id == oar_run_id, Recommendation.is_oar, has_exclusion)
    ).all()

    rebuilt = 0
    for row, target in pairs:
        if _in_line_with_target(row, target):
            continue
        if row.status not in _SYNCABLE_STATUSES:
            logger.warning(
                "recommendation %s is %s and no longer matches its OAR result after a donor exclusion; "
                "left unchanged because it is already in the approval workflow",
                row.recommendation_id,
                row.status,
            )
            continue
        refresh_recommendation(
            session,
            row,
            policy,
            changed_by="oar-generation",
            reason="Recalculated after a similar-material exclusion",
        )
        rebuilt += 1
    session.commit()
    return rebuilt


def apply_exclusions_before_generation(
    session: Session,
    oar_run_id: int,
    policy: PolicyDocument,
    provider: EmbeddingProvider | None = None,
) -> None:
    """What ``generate_recommendations`` runs before it reads the OAR run: no saved decision is skipped.

    Re-selects any target whose stored neighbours still contain an excluded donor (or predate an
    exclusion), then rebuilds the unsubmitted recommendations that no longer match their target.
    Free when nothing has been excluded: two small queries.

    ``provider`` defaults to the one an OAR run uses. If the run was made with the embedding model and
    this host does not have it, the refresh raises (see ``oar.refresh.check_run_is_current``) -- the
    generation run fails loudly rather than build from a selection that ignores a user's decision.
    """
    if exclusions_module.stale_targets(session, oar_run_id):
        settings = get_settings()
        refresh_module.refresh_stale_targets(
            session,
            oar_run_id,
            policy=policy,
            provider=provider or default_provider(),
            config=RetrievalConfig.from_settings(settings),
            settings=settings,
        )
    sync_recommendations(session, oar_run_id, policy)
