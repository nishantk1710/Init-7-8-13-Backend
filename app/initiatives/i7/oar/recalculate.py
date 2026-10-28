"""Recalculate one target's figures from the similar materials its run already stored.

A reviewer removes a similar material; nothing takes its place. So there is nothing to find: the target
keeps the neighbours the run stored (``i7_oar_neighbour`` -- all of the top-K, including a removed one),
the removed ones are set aside, and the confidence, the weighted estimate (reorder point, safety stock, max
stock) and the best similarity are computed from the rest -- by ``service.summarise_neighbours``, the same
calls a run makes at the end of ``finalise_target``.

No candidate is scored, no donor pool is loaded and no embedding model is touched, which is why this is
immediate and why it works on any run, whatever algorithm version made it.

The neighbour rows are never changed here. Only the target's own result row (``i7_oar_target``) is.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.initiatives.i7.oar import exclusions as exclusions_module
from app.initiatives.i7.oar import service
from app.initiatives.i7.oar.exclusions import DonorKey, TargetKey
from app.initiatives.i7.oar.types import (
    CombinedScore,
    DimensionScore,
    Neighbour,
    OarConfidence,
    OarEstimate,
    SimilarityStatus,
)
from app.initiatives.i7.policy import PolicyDocument
from app.models.i7_oar import OarNeighbour, OarRun, OarTargetResult

logger = logging.getLogger(__name__)

_ZERO = Decimal(0)


class RecalculationError(Exception):
    """A target could not be recalculated. ``code`` is stable; the message says why."""

    code = "OAR_RECALCULATION_FAILED"


class PolicyChangedError(RecalculationError):
    code = "OAR_RUN_NOT_CURRENT"


class TargetNotInRunError(RecalculationError):
    code = "OAR_TARGET_NOT_IN_RUN"


@dataclass(frozen=True)
class Recalculated:
    kept: tuple[Neighbour, ...]
    left_out: tuple[Neighbour, ...]
    confidence: OarConfidence
    estimate: OarEstimate
    best_similarity: Decimal | None


def _dimension(value: Decimal | None) -> DimensionScore:
    return DimensionScore(
        value=value,
        status=SimilarityStatus.AVAILABLE if value is not None else SimilarityStatus.NOT_AVAILABLE_NO_FEATURES,
    )


def neighbour_from_row(row: OarNeighbour) -> Neighbour:
    """A stored neighbour back as the :class:`Neighbour` the estimator reads.

    Only what the estimate, the confidence and the audit trace read is rebuilt: the combined score (the
    weight), the donor's stock values and whether they may be lent. The dimension scores are the stored
    ones, kept for completeness; nothing here is re-scored.
    """
    return Neighbour(
        material=row.neighbour_material,
        plant=row.neighbour_plant,
        rank=row.rank,
        score=CombinedScore(
            combined=row.combined_similarity,
            structured=_dimension(row.structured_similarity),
            text=_dimension(row.text_similarity),
            business=_dimension(row.business_similarity),
            score_completeness=row.score_completeness if row.score_completeness is not None else _ZERO,
        ),
        same_circuit=row.same_circuit,
        same_material_group=row.same_material_group,
        criticality=row.criticality,
        history_months=row.history_months,
        is_active=row.is_active,
        safety_stock=row.safety_stock,
        rop=row.rop,
        max_stock=row.max_stock,
        inventory_eligible=bool(row.inventory_eligible),
    )


def stored_neighbours(session: Session, run_id: int, key: TargetKey) -> list[Neighbour]:
    """Every top-K neighbour the run stored for this target, best first (removed ones included)."""
    rows = session.execute(
        select(OarNeighbour)
        .where(
            OarNeighbour.oar_run_id == run_id,
            OarNeighbour.sap_material_number == key[0],
            OarNeighbour.sap_plant_code == key[1],
        )
        .order_by(OarNeighbour.rank)
    ).scalars()
    return [neighbour_from_row(row) for row in rows]


def recalculate(
    session: Session, run_id: int, key: TargetKey, excluded: frozenset[DonorKey], policy: PolicyDocument
) -> Recalculated:
    """The target's figures with exactly ``excluded`` set aside. Writes nothing."""
    kept, left_out = exclusions_module.split_excluded(stored_neighbours(session, run_id, key), excluded)
    confidence, estimate, best = service.summarise_neighbours(kept, policy)
    return Recalculated(tuple(kept), tuple(left_out), confidence, estimate, best)


def check_policy_matches_run(run: OarRun, policy: PolicyDocument) -> None:
    """The estimate depends on the policy (minimum neighbours, estimator, service level), so a recalculation
    under a different one than the run's would not be the run's own answer with a donor removed."""
    if run.policy_id != policy.policy_id or run.policy_version != policy.policy_version:
        raise PolicyChangedError(
            f"OAR run {run.id} was made under policy {run.policy_id} v{run.policy_version}, not "
            f"{policy.policy_id} v{policy.policy_version}. A new OAR run will apply the removed similar "
            "materials under the current policy."
        )


def _merged_detail(old: str | None, estimate_detail: str | None, left_out_count: int) -> str | None:
    """The target's ``detail``: keep what the run wrote about evidence, replace the removed-materials note and
    the estimate's own sentence, which are the parts a recalculation changes.

    Same order a run writes (``service._target_row``: the result's detail -- evidence note, then removed
    note -- followed by the estimate's), so recalculating a target nobody touched changes nothing."""
    parts = [
        part
        for part in (old or "").split("; ")
        if part and "refused for too little evidence" in part
    ]
    if left_out_count:
        parts.append(service.left_out_note(left_out_count))
    if estimate_detail:
        parts.append(estimate_detail)
    return "; ".join(parts)[:500] or None


def target_values(result: Recalculated, old_detail: str | None) -> dict[str, Any]:
    """The ``i7_oar_target`` columns a recalculation owns -- the same ones ``service._target_row`` fills from
    a run's own result. Everything about how the candidates were found stays as the run wrote it."""
    estimate = result.estimate
    return {
        "confidence": result.confidence.value,
        "neighbour_count": len(result.kept),
        "best_similarity": result.best_similarity,
        "estimate_status": estimate.status.value,
        "estimate_label": estimate.label if estimate.status.value == "SUCCESS" else None,
        "safety_stock": estimate.safety_stock,
        "rop": estimate.rop,
        "max_stock": estimate.max_stock,
        "inventory_eligible_neighbours": estimate.inventory_eligible_neighbours,
        "inventory_ineligible_neighbours": estimate.inventory_ineligible_neighbours,
        "estimate_trace": service._trace_text(estimate.trace),
        "detail": _merged_detail(old_detail, estimate.detail, len(result.left_out)),
    }


def write(session: Session, run_id: int, key: TargetKey, result: Recalculated) -> bool:
    """Update the target's result row to ``result``, inside the caller's transaction.

    Returns whether anything changed. ``generated_at`` moves only when it did, so an unchanged target is
    not mistaken for a recalculated one. The neighbour rows are not touched.
    """
    row = session.execute(
        select(OarTargetResult).where(
            OarTargetResult.oar_run_id == run_id,
            OarTargetResult.sap_material_number == key[0],
            OarTargetResult.sap_plant_code == key[1],
        )
    ).scalar_one_or_none()
    if row is None:
        raise TargetNotInRunError(f"OAR run {run_id} holds no result for {key[0]}/{key[1]}.")
    values = target_values(result, row.detail)
    changed = {column: value for column, value in values.items() if getattr(row, column) != value}
    if not changed:
        return False
    for column, value in changed.items():
        setattr(row, column, value)
    row.generated_at = func.now()
    session.flush()
    return True


def apply_to_run(session: Session, run_id: int, policy: PolicyDocument) -> int:
    """Bring every target that has had a similar material removed (or restored) up to date. Commits.

    For a reused OAR run and for the step before recommendations are generated: a removal made after the
    run, or one a script saved, is reflected without waiting for the next run. Idempotent -- a target that
    already matches is left alone. Returns how many changed.
    """
    run = session.get(OarRun, run_id)
    if run is None:
        raise TargetNotInRunError(f"OAR run {run_id} does not exist.")
    keys = exclusions_module.targets_with_exclusions(session)
    if not keys:
        return 0
    check_policy_matches_run(run, policy)
    active = exclusions_module.load_active_for(session, keys)
    changed = 0
    for key in keys:
        try:
            result = recalculate(session, run_id, key, active.get(key, exclusions_module.NO_EXCLUSIONS), policy)
            if write(session, run_id, key, result):
                changed += 1
        except TargetNotInRunError:
            # No longer an OAR target in this run (its scope changed): nothing to recalculate.
            logger.info("OAR run %d: no result for %s/%s; skipped", run_id, *key)
    session.commit()
    if changed:
        logger.info("OAR run %d: %d target(s) recalculated for removed similar materials", run_id, changed)
    return changed
