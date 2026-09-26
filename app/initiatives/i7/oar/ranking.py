"""Deterministic ranking and Top-K selection.

Ties are broken explicitly, in a documented order, so the same inputs always
produce the same neighbour list. Two candidates with equal scores are common --
identical descriptions, same material group, same missing attributes -- and
without an explicit tie-break the order would depend on dictionary or query
ordering, which is stable until the day it is not.

Order:

1. minimum_similarity admission -- a candidate below the floor is dropped
   before ranking even considers it (never used to pad the neighbour count)
2. combined similarity, descending
3. same circuit first (the Solution Design's soft preference)
4. material number, ascending
5. plant, ascending

Fewer than K qualifying candidates yields fewer than K neighbours. Nothing is
duplicated or padded to reach the target.
"""

from decimal import Decimal

from app.initiatives.i7.oar.types import CombinedScore, Neighbour


def sort_key(
    material: str, plant: str, score: CombinedScore, same_circuit: bool | None
) -> tuple:
    """Deterministic ordering key.

    Similarity and the circuit flag are negated so that a plain ascending sort
    puts the best first, while the identifiers stay ascending as a final,
    always-decisive tie-break.
    """
    similarity = score.combined if score.combined is not None else Decimal(-1)
    return (-similarity, 0 if same_circuit else 1, material, plant)


def select_top_k(
    scored: list[tuple[str, str, CombinedScore, bool | None, dict]],
    top_k: int,
    minimum_similarity: Decimal | None = None,
) -> list[Neighbour]:
    """Filter to qualifying candidates, rank, and take the best ``top_k``.

    Candidates whose combined score could not be computed are dropped: a
    neighbour with no measurable similarity is not evidence of anything, and
    ranking it would place an unmeasured material above a measured one.

    ``minimum_similarity``, when given, is a hard admission floor applied with
    ``>=`` *before* the Top-K cut -- a candidate below it never becomes a
    neighbour, however few qualifying candidates exist. This is distinct from
    (and applied before) the Top-K cap: filtering first, then capping, means
    the minimum-neighbour check downstream sees only genuinely qualifying
    candidates, never candidates kept solely to pad the count.
    """
    measurable = [entry for entry in scored if entry[2].combined is not None]
    if minimum_similarity is not None:
        measurable = [entry for entry in measurable if entry[2].combined >= minimum_similarity]

    ordered = sorted(
        measurable,
        key=lambda entry: sort_key(entry[0], entry[1], entry[2], entry[3]),
    )

    neighbours: list[Neighbour] = []
    for rank, (material, plant, score, same_circuit, extra) in enumerate(
        ordered[:top_k], start=1
    ):
        neighbours.append(
            Neighbour(
                material=material,
                plant=plant,
                rank=rank,
                score=score,
                same_circuit=same_circuit,
                same_material_group=extra.get("same_material_group"),
                criticality=extra.get("criticality"),
                history_months=extra.get("history_months"),
                is_active=extra.get("is_active"),
                safety_stock=extra.get("safety_stock"),
                rop=extra.get("rop"),
                max_stock=extra.get("max_stock"),
                inventory_eligible=extra.get("inventory_eligible", False),
            )
        )
    return neighbours
