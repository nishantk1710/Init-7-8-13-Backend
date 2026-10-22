"""Deterministic ranking and Top-K selection.

Ties are broken explicitly, in a documented order, so the same inputs always
produce the same neighbour list. Two candidates with equal scores are common --
identical descriptions, same material group, same missing attributes -- and
without an explicit tie-break the order would depend on dictionary or query
ordering, which is stable until the day it is not.

Order:

1. admission -- a candidate is dropped before ranking even considers it when
   it failed the identity stage (``CombinedScore.identity``), its combined score
   is below ``minimum_similarity`` or it rests on too little evidence
   (``CombinedScore.evidence_sufficient``); never used to pad the neighbour
   count. Only candidates that pass BOTH identity and the floor are ranked.
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

from app.initiatives.i7.oar.types import CombinedScore, Neighbour, RejectedCandidate

ScoredEntry = tuple[str, str, CombinedScore, bool | None, dict]


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


def identity_passed(score: CombinedScore) -> bool:
    """Whether the identity stage confirmed the candidate. A score with no verdict (the stage did not run)
    is not refused for lack of one."""
    return score.identity is None or score.identity.passed


def passes_floor(score: CombinedScore, minimum_similarity: Decimal | None) -> bool:
    return score.combined is not None and (
        minimum_similarity is None or score.combined >= minimum_similarity
    )


def admitted(score: CombinedScore, minimum_similarity: Decimal | None) -> bool:
    """Whether a scored candidate may become a neighbour at all: identity confirmed, enough evidence, and
    at or above the score floor."""
    if score.combined is None or not score.evidence_sufficient or not identity_passed(score):
        return False
    return passes_floor(score, minimum_similarity)


def unique_entries(scored: list[ScoredEntry]) -> list[ScoredEntry]:
    """One entry per material-plant, the best-scoring if a donor appears twice.

    A donor listed twice would count twice in the weighted estimate. Retrieval already drops duplicate
    donors; this is the guard that holds whatever fed the list.
    """
    best: dict[tuple[str, str], ScoredEntry] = {}
    for entry in scored:
        key = (entry[0], entry[1])
        held = best.get(key)
        if held is None or _better(entry[2], held[2]):
            best[key] = entry
    return list(best.values()) if len(best) != len(scored) else scored


def _better(candidate: CombinedScore, held: CombinedScore) -> bool:
    return (candidate.combined if candidate.combined is not None else Decimal(-1)) > (
        held.combined if held.combined is not None else Decimal(-1)
    )


def stage_tally(
    scored: list[ScoredEntry], minimum_similarity: Decimal | None, top_k: int
) -> tuple[int, int, int]:
    """``(identity_passes, score_floor_passes, final)`` over the scored candidates, in that order.

    Each stage counts the survivors of the one before it, so the three numbers can only fall. A candidate
    the evidence rule refused (too few features compared) is not an identity pass either.
    """
    identity_ok = [e for e in scored if identity_passed(e[2]) and e[2].evidence_sufficient]
    floor_ok = [e for e in identity_ok if e[2].combined is not None and passes_floor(e[2], minimum_similarity)]
    return len(identity_ok), len(floor_ok), min(len(floor_ok), top_k)


def refusal_reason(score: CombinedScore, minimum_similarity: Decimal | None) -> str | None:
    """Why a scored candidate was refused, or ``None`` if it was not."""
    if score.combined is None:
        return score.evidence_detail or "no similarity dimension could be computed"
    if score.identity is not None and not score.identity.passed:
        return score.identity.reason
    if not score.evidence_sufficient:
        return score.evidence_detail or "too little evidence"
    if not passes_floor(score, minimum_similarity):
        return f"combined similarity {score.combined:.4f} below the {minimum_similarity} floor"
    return None


def rejected_sample(
    scored: list[ScoredEntry], minimum_similarity: Decimal | None, limit: int = 10
) -> tuple[RejectedCandidate, ...]:
    """The best-scoring refused candidates, highest combined score first, with the reason for each.

    Kept so a refusal can be examined later: the neighbour table holds only the accepted. Bounded -- a
    target with a thousand refusals does not carry a thousand rows.
    """
    refused = []
    for material, plant, score, _circuit, _extra in scored:
        reason = refusal_reason(score, minimum_similarity)
        if reason is not None:
            refused.append(
                RejectedCandidate(
                    material=material,
                    plant=plant,
                    combined=score.combined,
                    reason=reason,
                    status=score.identity.status.value if score.identity is not None else None,
                )
            )
    refused.sort(
        key=lambda r: (-(r.combined if r.combined is not None else Decimal(-1)), r.material, r.plant)
    )
    return tuple(refused[:limit])


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
    qualifying = [
        entry for entry in unique_entries(scored) if admitted(entry[2], minimum_similarity)
    ]

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
