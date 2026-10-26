"""User-excluded donors: loading them, applying them, and finding the targets that are out of date.

An exclusion removes ONE donor from ONE target's neighbour selection (``i7_oar_donor_exclusion``). It
changes nothing about how a donor is scored or ranked: ``oar.service.finalise_target`` calls
:func:`filter_scored` on the scored candidates it was given, before ``ranking.select_top_k``, so the
next-best donor that was already scored takes the freed slot. Order and scores of the survivors are
untouched.

Everything here is either a pure function or a read; the writes (add, restore) belong to
``recommendations.oar_exclusions`` because they also have to refresh the recommendation.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.i7_oar import OarDonorExclusion, OarNeighbour, OarTargetResult

TargetKey = tuple[str, str]
"""``(sap_material_number, sap_plant_code)`` of a target."""

DonorKey = tuple[str, str]
"""``(material, plant)`` of a donor."""

NO_EXCLUSIONS: frozenset[DonorKey] = frozenset()


def filter_scored(scored: Sequence, excluded: Iterable[DonorKey]) -> list:
    """The scored entries whose donor is not excluded, in their original order.

    An entry is ``(material, plant, score, same_circuit, extra)`` (``ranking.ScoredEntry``); only the
    first two fields are read. Nothing is rescored or reordered, and an empty ``excluded`` returns an
    equal list.
    """
    blocked = frozenset(excluded)
    if not blocked:
        return list(scored)
    return [entry for entry in scored if (entry[0], entry[1]) not in blocked]


def load_active(session: Session) -> dict[TargetKey, frozenset[DonorKey]]:
    """Every active exclusion, grouped by target. One query for a whole run."""
    grouped: dict[TargetKey, set[DonorKey]] = defaultdict(set)
    for row in session.execute(
        select(OarDonorExclusion).where(OarDonorExclusion.active_slot == OarDonorExclusion.ACTIVE_SLOT)
    ).scalars():
        grouped[(row.sap_material_number, row.sap_plant_code)].add((row.donor_material, row.donor_plant))
    return {key: frozenset(donors) for key, donors in grouped.items()}


def load_active_for(session: Session, keys: Sequence[TargetKey]) -> dict[TargetKey, frozenset[DonorKey]]:
    """The active exclusions of just these targets."""
    if not keys:
        return {}
    grouped: dict[TargetKey, set[DonorKey]] = defaultdict(set)
    for chunk_start in range(0, len(keys), 400):
        chunk = keys[chunk_start : chunk_start + 400]
        condition = or_(
            *(
                and_(
                    OarDonorExclusion.sap_material_number == material,
                    OarDonorExclusion.sap_plant_code == plant,
                )
                for material, plant in chunk
            )
        )
        for row in session.execute(
            select(OarDonorExclusion).where(
                OarDonorExclusion.active_slot == OarDonorExclusion.ACTIVE_SLOT, condition
            )
        ).scalars():
            grouped[(row.sap_material_number, row.sap_plant_code)].add((row.donor_material, row.donor_plant))
    return {key: frozenset(donors) for key, donors in grouped.items()}


def active_for_target(session: Session, material: str, plant: str) -> frozenset[DonorKey]:
    return load_active_for(session, [(material, plant)]).get((material, plant), NO_EXCLUSIONS)


def stale_targets(session: Session, oar_run_id: int) -> list[TargetKey]:
    """Targets of this run whose stored selection no longer reflects their exclusions.

    Two ways a target is stale, both cheap to see:

    * an ACTIVE exclusion still names one of the donors stored as that target's neighbour -- the run
      predates the decision, or was written without it;
    * an exclusion was created or restored after the target's result was written
      (``i7_oar_target.generated_at``) -- covers a restore, which leaves nothing to spot in the
      neighbour rows. Compared in SQL, both sides stamped by the database clock.

    Only targets that have an exclusion at all are ever candidates, so an unchanged population costs
    two small queries.
    """
    stale: set[TargetKey] = set()

    still_listed = session.execute(
        select(OarDonorExclusion.sap_material_number, OarDonorExclusion.sap_plant_code)
        .join(
            OarNeighbour,
            and_(
                OarNeighbour.oar_run_id == oar_run_id,
                OarNeighbour.sap_material_number == OarDonorExclusion.sap_material_number,
                OarNeighbour.sap_plant_code == OarDonorExclusion.sap_plant_code,
                OarNeighbour.neighbour_material == OarDonorExclusion.donor_material,
                OarNeighbour.neighbour_plant == OarDonorExclusion.donor_plant,
            ),
        )
        .where(OarDonorExclusion.active_slot == OarDonorExclusion.ACTIVE_SLOT)
        .distinct()
    )
    stale.update((material, plant) for material, plant in still_listed)

    changed_since = session.execute(
        select(OarDonorExclusion.sap_material_number, OarDonorExclusion.sap_plant_code)
        .join(
            OarTargetResult,
            and_(
                OarTargetResult.oar_run_id == oar_run_id,
                OarTargetResult.sap_material_number == OarDonorExclusion.sap_material_number,
                OarTargetResult.sap_plant_code == OarDonorExclusion.sap_plant_code,
            ),
        )
        .where(
            or_(
                OarDonorExclusion.created_at > OarTargetResult.generated_at,
                OarDonorExclusion.restored_at > OarTargetResult.generated_at,
            )
        )
        .distinct()
    )
    stale.update((material, plant) for material, plant in changed_since)

    return sorted(stale)


def excluded_for(
    exclusions: Mapping[TargetKey, frozenset[DonorKey]] | None, material: str, plant: str
) -> frozenset[DonorKey]:
    """One target's set out of a ``load_active`` mapping; empty when there is none."""
    if not exclusions:
        return NO_EXCLUSIONS
    return exclusions.get((material, plant), NO_EXCLUSIONS)
