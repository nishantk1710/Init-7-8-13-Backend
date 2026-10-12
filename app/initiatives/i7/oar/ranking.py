"""Deterministic ranking and Top-K selection.

Ties are broken explicitly, in a documented order, so the same inputs always
produce the same neighbour list. Two candidates with equal scores are common --
identical descriptions, same material group, same missing attributes -- and
without an explicit tie-break the order would depend on dictionary or query
ordering, which is stable until the day it is not.

Order:

1. admission -- a candidate is dropped before ranking even considers it when
   its combined score is below ``minimum_similarity`` or rests on too little
   evidence (``CombinedScore.evidence_sufficient``); never used to pad the
   neighbour count
2. combined similarity, descending
3. material number, ascending
4. plant, ascending

Circuit is not part of the order (since ``oar-similarity-2``): no source
carries it, so a "same circuit first" rule would only ever have compared two
unknowns. The ``same_circuit`` slot in each entry is accepted for compatibility
and ignored.

Fewer than K qualifying candidates yields fewer than K neighbours. Nothing is
duplicated or padded to reach the target.
"""

from decimal import Decimal

from app.initiatives.i7.oar.types import CombinedScore, Neighbour


def sort_key(
    material: str, plant: str, score: CombinedScore, same_circuit: bool | None = None
) -> tuple:
    """Deterministic ordering key.

    Similarity is negated so that a plain ascending sort puts the best first,
    while the identifiers stay ascending as a final, always-decisive tie-break.
    ``same_circuit`` is accepted and ignored -- see the module docstring.
    """
    similarity = score.combined if score.combined is not None else Decimal(-1)
    return (-similarity, material, plant)


def admitted(score: CombinedScore, minimum_similarity: Decimal | None) -> bool:
    """Whether a scored candidate may become a neighbour at all."""
    if score.combined is None or not score.evidence_sufficient:
        return False
    return minimum_similarity is None or score.combined >= minimum_similarity


def select_top_k(
    scored: list[tuple[str, str, CombinedScore, bool | None, dict]],
    top_k: int,
    minimum_similarity: Decimal | None = None,
) -> list[Neighbour]:
    """Filter to qualifying candidates, rank, and take the best ``top_k``.

    Candidates whose combined score could not be computed are dropped: a
    neighbour with no measurable similarity is not evidence of anything, and
    ranking it would place an unmeasured material above a measured one. So are
    candidates whose score rests on too little evidence.

    ``minimum_similarity``, when given, is a hard admission floor applied with
    ``>=`` *before* the Top-K cut -- a candidate below it never becomes a
    neighbour, however few qualifying candidates exist. This is distinct from
    (and applied before) the Top-K cap: filtering first, then capping, means
    the minimum-neighbour check downstream sees only genuinely qualifying
    candidates, never candidates kept solely to pad the count.
    """
    qualifying = [entry for entry in scored if admitted(entry[2], minimum_similarity)]

    ordered = sorted(
        qualifying,
        key=lambda entry: sort_key(entry[0], entry[1], entry[2]),
    )

    neighbours: list[Neighbour] = []
    for rank, (material, plant, score, _same_circuit, extra) in enumerate(
        ordered[:top_k], start=1
    ):
        neighbours.append(
            Neighbour(
                material=material,
                plant=plant,
                rank=rank,
                score=score,
                same_circuit=None,
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
