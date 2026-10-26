"""User-removed similar materials: loading them, applying them, finding the targets they touch.

A removal takes ONE donor out of ONE target's similar materials (``i7_oar_donor_exclusion``) and **nothing
takes its place**. The run selects its top-K exactly as it always did; :func:`split_excluded` then sets the
removed ones aside, and the estimate, the confidence and the best similarity are built from the rest. A
target that had five and lost one has four, which is what the reviewer saw when they removed it.

Why after the top-K and not before: leaving the donor out of the candidates first would let the sixth-best
candidate fill the slot -- a material the reviewer never looked at, chosen quietly. Setting it aside after
the cut keeps the list the reviewer judged and removes only what they removed.

Everything here is a pure function or a read. The writes (add, restore) belong to
``recommendations.oar_exclusions`` because they also have to refresh the recommendation.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.i7_oar import OarDonorExclusion

TargetKey = tuple[str, str]
"""``(sap_material_number, sap_plant_code)`` of a target."""

DonorKey = tuple[str, str]
"""``(material, plant)`` of a donor."""

NO_EXCLUSIONS: frozenset[DonorKey] = frozenset()


def split_excluded(neighbours: Sequence, excluded: Iterable[DonorKey]) -> tuple[list, list]:
    """``(kept, left_out)``: the ranked neighbours split by whether a user removed them.

    Order, rank and scores of every neighbour are untouched -- a neighbour is either in one list or the
    other, never rebuilt -- and an empty ``excluded`` returns everything as ``kept``.
    """
    blocked = frozenset(excluded)
    if not blocked:
        return list(neighbours), []
    kept, left_out = [], []
    for neighbour in neighbours:
        (left_out if (neighbour.material, neighbour.plant) in blocked else kept).append(neighbour)
    return kept, left_out


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


def targets_with_exclusions(session: Session) -> list[TargetKey]:
    """Every target that has ever had a similar material removed, active or since restored.

    Restored ones are included on purpose: a restore leaves the target needing a recalculation just as a
    removal does. The list is as long as the number of materials a person has edited -- tens, not tens of
    thousands -- so recalculating all of them from stored rows is cheap.
    """
    return sorted(
        set(
            session.execute(
                select(OarDonorExclusion.sap_material_number, OarDonorExclusion.sap_plant_code)
            ).all()
        )
    )


def excluded_for(
    exclusions: Mapping[TargetKey, frozenset[DonorKey]] | None, material: str, plant: str
) -> frozenset[DonorKey]:
    """One target's set out of a ``load_active`` mapping; empty when there is none."""
    if not exclusions:
        return NO_EXCLUSIONS
    return exclusions.get((material, plant), NO_EXCLUSIONS)
