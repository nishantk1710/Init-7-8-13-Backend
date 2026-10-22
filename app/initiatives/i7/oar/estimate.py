"""Similarity-weighted inventory estimate.

Formula Reference, Stage 5 Path C::

    SS_oar  = sum(s_i x SS_i)  / sum(s_i)
    ROP_oar = sum(s_i x ROP_i) / sum(s_i)
    Max_oar = sum(s_i x Max_i) / sum(s_i)

A closer neighbour contributes more. Rounding happens once, at the end -- each
neighbour's raw value enters the average, because rounding first would compound
several ceilings into a systematic overstatement.

**Max stock consistency floor** (approved deviation, ``oar-similarity-6``,
2026-10-19). The three averages are independent, so the Max can come out at or
below the reorder point -- a Max with no room above the point that triggers a
reorder. After all three are calculated and rounded::

    Max_final = max( Max_weighted , ROP_final + SS_final )

applied only when ROP, safety stock and the weighted Max are all present; a
missing value is never filled in. ROP and safety stock are never changed. The
audit trace keeps the original weighted Max next to the floor, the final value
and whether the floor was applied -- the original is never overwritten. Stock
items are not affected: their Max comes from the Max Stock strategy
(``inventory/max_stock.py``), not from this module.

**Nothing is borrowed from a neighbour that has nothing to lend.** A neighbour
contributes only when it carries values (``inventory_eligible``). The OAR run
admits only donors whose current SAP ROP, safety stock and max stock are all
above zero, and those are what they lend (see ``oar.repository``), so in a
real run every neighbour contributes. The function still guards the case for
callers that pass their own values.

**A minimum-neighbour count is a hard gate, not a confidence input.**
``neighbours`` arriving here has already been filtered to
``SimilarityPolicy.minimum_similarity`` (>= 0.60) and capped at Top-K by
``ranking.select_top_k`` -- this module only checks the resulting *count*
against ``minimum_neighbours`` (2). Below it, no partial SS/ROP/Max is
computed from the neighbours that do qualify:
``NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS`` reports exactly how many qualified
and how many were required, distinct from ``NOT_EVALUABLE_NO_NEIGHBORS``
(zero candidates at all).
"""

from decimal import ROUND_CEILING, Decimal

from app.initiatives.i7.oar.types import (
    ESTIMATE_LABEL,
    EstimateStatus,
    Neighbour,
    OarEstimate,
)


MAX_FLOOR_REASON = "MAX_RAISED_TO_ROP_PLUS_SAFETY_STOCK"
"""The trace reason recorded when the floor raised the Max."""


def apply_max_floor(
    rop: int | None, safety_stock: int | None, weighted_max: int | None
) -> tuple[int | None, tuple[tuple[str, str], ...]]:
    """``(final_max, trace_entries)`` -- Max raised to ROP + safety stock if it is below.

    Only when all three are present (zero is a present value). Otherwise the
    weighted Max is returned as it was -- ``None`` stays ``None`` -- and the
    trace says the floor was not evaluated, rather than inventing a value.
    """
    if rop is None or safety_stock is None or weighted_max is None:
        return weighted_max, (
            ("max_stock_weighted", "none" if weighted_max is None else str(weighted_max)),
            ("max_stock_floor_applied", "false"),
            ("max_stock_floor_reason", "NOT_EVALUATED_MISSING_INPUT"),
        )
    floor = rop + safety_stock
    raised = weighted_max < floor
    final = floor if raised else weighted_max
    return final, (
        ("max_stock_weighted", str(weighted_max)),
        ("max_stock_floor", str(floor)),
        ("max_stock_final", str(final)),
        ("max_stock_floor_applied", "true" if raised else "false"),
        ("max_stock_floor_reason", MAX_FLOOR_REASON if raised else "NOT_NEEDED"),
    )


def _weighted(
    neighbours: list[Neighbour], attribute: str
) -> tuple[Decimal, Decimal] | None:
    """``(numerator, denominator)`` for one parameter, or ``None``."""
    numerator = Decimal(0)
    denominator = Decimal(0)

    for neighbour in neighbours:
        value = getattr(neighbour, attribute)
        similarity = neighbour.score.combined
        if value is None or similarity is None or similarity <= 0:
            continue
        numerator += similarity * Decimal(value)
        denominator += similarity

    if denominator <= 0:
        return None
    return numerator, denominator


def calculate(
    neighbours: list[Neighbour],
    service_level_configured: bool,
    minimum_neighbours: int = 1,
    minimum_similarity: Decimal | None = None,
) -> OarEstimate:
    """Weighted SS, ROP and Max from the neighbours that have them.

    ``neighbours`` is expected to already be filtered to the
    ``minimum_similarity`` admission floor and capped at Top-K (see
    ``ranking.select_top_k``) -- this function does not itself re-check
    similarity, only the neighbour *count* against ``minimum_neighbours``.
    Fewer than ``minimum_neighbours`` qualifying neighbours blocks the
    estimate entirely: 1-4 neighbours are never used to compute a partial
    SS/ROP/Max, however good their individual similarity.
    """
    qualifying = len(neighbours)

    if not neighbours:
        return OarEstimate(
            status=EstimateStatus.NOT_EVALUABLE_NO_NEIGHBORS,
            minimum_neighbours=minimum_neighbours,
            minimum_similarity=minimum_similarity,
            detail="no eligible neighbours to borrow parameters from",
        )

    if qualifying < minimum_neighbours:
        return OarEstimate(
            status=EstimateStatus.NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS,
            qualifying_neighbours=qualifying,
            minimum_neighbours=minimum_neighbours,
            minimum_similarity=minimum_similarity,
            detail=(
                f"{qualifying} qualifying neighbour(s), fewer than the "
                f"required minimum of {minimum_neighbours} -- no partial "
                "estimate is computed"
            ),
        )

    eligible = [neighbour for neighbour in neighbours if neighbour.inventory_eligible]
    ineligible = len(neighbours) - len(eligible)

    if not eligible:
        # The two blocked states have different remedies, so they are reported
        # differently: an unsigned service level is a Vedanta decision, while
        # neighbours that individually failed Phase 5 are a data problem.
        status = (
            EstimateStatus.NOT_EVALUABLE_NEIGHBOR_INVENTORY
            if service_level_configured
            else EstimateStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET
        )
        detail = (
            "no neighbour has a successful Phase 5 inventory calculation"
            if service_level_configured
            else (
                "neighbours found, but no Phase 5 inventory values exist because "
                "the service-level matrix is unsigned; no value is assumed here"
            )
        )
        return OarEstimate(
            status=status,
            inventory_eligible_neighbours=0,
            inventory_ineligible_neighbours=ineligible,
            qualifying_neighbours=qualifying,
            minimum_neighbours=minimum_neighbours,
            minimum_similarity=minimum_similarity,
            detail=detail,
        )

    trace: list[tuple[str, str]] = [
        ("eligible_neighbours", str(len(eligible))),
        ("ineligible_neighbours", str(ineligible)),
        ("qualifying_neighbours", str(qualifying)),
        ("minimum_neighbours", str(minimum_neighbours)),
    ]
    results: dict[str, int | None] = {}
    detail: list[tuple[str, str]] = []

    for attribute in ("safety_stock", "rop", "max_stock"):
        weighted = _weighted(eligible, attribute)
        if weighted is None:
            results[attribute] = None
            detail.append((f"{attribute}_status", "no_contributing_neighbour"))
            continue
        numerator, denominator = weighted
        raw = numerator / denominator
        results[attribute] = int(raw.to_integral_value(rounding=ROUND_CEILING))
        detail.extend(
            (
                (f"{attribute}_numerator", str(numerator)),
                (f"{attribute}_denominator", str(denominator)),
                (f"{attribute}_raw", str(raw)),
            )
        )

    results["max_stock"], floor_trace = apply_max_floor(
        results["rop"], results["safety_stock"], results["max_stock"]
    )
    # The floor's record goes ahead of the per-field arithmetic: the stored trace is cut at a fixed
    # length, and the audit entries must survive that before the raw digits do.
    trace.extend(floor_trace)
    trace.extend(detail)
    trace.append(("rounding_method", "CEILING"))

    return OarEstimate(
        status=EstimateStatus.SUCCESS,
        label=ESTIMATE_LABEL,
        safety_stock=results["safety_stock"],
        rop=results["rop"],
        max_stock=results["max_stock"],
        inventory_eligible_neighbours=len(eligible),
        inventory_ineligible_neighbours=ineligible,
        qualifying_neighbours=qualifying,
        minimum_neighbours=minimum_neighbours,
        minimum_similarity=minimum_similarity,
        trace=tuple(trace),
    )
