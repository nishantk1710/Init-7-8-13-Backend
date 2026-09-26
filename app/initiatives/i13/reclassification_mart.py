"""W6.5: persistence for the reclassification evidence mart
(``app.models.i13_reclassification.ReclassificationCandidateMart``).

Mirrors ``watch_mart.py``/``consumption_attribution_mart.py`` exactly:
refresh is a full, idempotent recompute over the requested scope -- delete
every row in that scope, then insert one row per computed
``ReclassificationCandidate``, in a single unit of work. Running it twice
against unchanged source data leaves the same rows in place, never
duplicates. Portable delete-then-insert, not an ``ON CONFLICT``/``MERGE``
upsert, for the same cross-dialect (Postgres/Azure SQL) reason documented on
``ReclassificationCandidateMart``.

Reuses ``build_reclassification_candidates`` unchanged -- this module never
recomputes OAR scope, consumption counts, criticality or HOD evidence
itself, only persists what that function returns.
"""

from datetime import date

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.initiatives.i13.config import I13Config
from app.initiatives.i13.hod_justification_provider import HodJustificationProvider
from app.initiatives.i13.models import ReclassificationCandidate
from app.initiatives.i13.reclassification import build_reclassification_candidates
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.models.i13_reclassification import ReclassificationCandidateMart
from app.shared import CriticalitySource


def _to_row(candidate: ReclassificationCandidate) -> ReclassificationCandidateMart:
    return ReclassificationCandidateMart(
        material=candidate.material,
        plant=candidate.plant,
        as_of_date=candidate.as_of_date,
        consumption_count_12m=candidate.consumption_count_12m,
        consumption_threshold=candidate.consumption_threshold,
        consumed_more_than_threshold=candidate.consumed_more_than_threshold,
        critical_impact_indicator=candidate.critical_impact_indicator,
        hod_justified_request_indicator=candidate.hod_justified_request_indicator,
        data_available=candidate.data_available,
        candidate_flag=candidate.candidate_flag,
        candidate_reasons=",".join(candidate.candidate_reasons),
        refreshed_at=candidate.generated_at,
    )


def refresh_reclassification_mart(
    session: Session,
    movement_repository: PostgresMovementRepository,
    material_scope_index: dict[tuple[str, str], str | None],
    config: I13Config,
    *,
    material: str | None = None,
    plant: str | None = None,
    as_of: date | None = None,
    criticality_source: CriticalitySource | None = None,
    hod_provider: HodJustificationProvider | None = None,
) -> list[ReclassificationCandidate]:
    """Recompute reclassification evidence for the requested scope and
    replace its rows in the mart.

    The caller owns the transaction boundary (this codebase's convention --
    see ``watch_mart.py``'s docstring): this function does not call
    ``session.commit()``.
    """
    candidates = build_reclassification_candidates(
        movement_repository,
        material_scope_index,
        config,
        material=material,
        plant=plant,
        as_of=as_of,
        criticality_source=criticality_source,
        hod_provider=hod_provider,
    )

    delete_stmt = delete(ReclassificationCandidateMart)
    if material:
        delete_stmt = delete_stmt.where(ReclassificationCandidateMart.material == material)
    if plant:
        delete_stmt = delete_stmt.where(ReclassificationCandidateMart.plant == plant)
    session.execute(delete_stmt)

    for candidate in candidates:
        session.add(_to_row(candidate))
    session.flush()

    return candidates


def get_reclassification_candidate(session: Session, material: str, plant: str) -> ReclassificationCandidateMart | None:
    """Read-path counterpart to ``refresh_reclassification_mart`` -- the
    persisted evidence for one material-plant, or ``None`` if it hasn't been
    (re)computed yet."""
    return session.get(ReclassificationCandidateMart, (material, plant))
