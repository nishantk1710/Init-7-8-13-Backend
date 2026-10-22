"""Similarity-weighted inventory estimate.

Formula Reference, Stage 5 Path C, as written, is a similarity-weighted MEAN::

    SS_oar  = sum(s_i x SS_i)  / sum(s_i)
    ROP_oar = sum(s_i x ROP_i) / sum(s_i)
    Max_oar = sum(s_i x Max_i) / sum(s_i)

A closer neighbour contributes more. Rounding happens once, at the end -- each
neighbour's raw value enters the estimate, because rounding first would compound
several ceilings into a systematic overstatement.

**The default is now a similarity-weighted MEDIAN** (``oar-similarity-10``,
``SimilarityPolicy.estimator``). A mean is dragged by a single extreme donor: four filter donors with ROP
3, 9, 6 and 3 and a fifth with ROP 61 gave a mean of about 15 -- more than twice what any of the four
suggests. The weighted median of each parameter is the value at which half of the total similarity weight
lies on either side, so one outlier moves it by at most one step through the sorted values. The mean is
still computed, kept in the audit trace beside the median, and selectable with ``weighted_mean``.

Weighted median, exactly: sort the donors by value; walk up accumulating weight; the median is the first
value at which the accumulated weight reaches half the total. When the accumulated weight lands on exactly
half, the median is the midpoint of that value and the next -- the standard definition for an even split --
and is then rounded up like every other estimate. It is computed in ``Decimal``, so a tie at exactly half
is a tie and not a rounding accident. No donor is removed for having a high value: that is the estimator's
job, not a filter's, and a high ROP is not evidence of a wrong match (identity is ``oar.identity``'s).

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
from typing import Sequence

from app.initiatives.i7.oar.types import (
    ESTIMATE_LABEL,
    EstimateDonor,
    EstimateStatus,
    FieldAudit,
    Neighbour,
    OarEstimate,
)

STRATEGY_MEAN = "weighted_mean"
STRATEGY_MEDIAN = "weighted_median"
STRATEGIES = (STRATEGY_MEDIAN, STRATEGY_MEAN)

DEFAULT_MATERIAL_DIFFERENCE_RATIO = Decimal("0.25")
"""Diagnostic only -- see ``SimilarityPolicy.estimator_material_difference_ratio``."""

_AUDIT_PLACES = Decimal("0.0001")


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


def _pairs(neighbours: list[Neighbour], attribute: str) -> list[tuple[Decimal, Decimal]]:
    """``(value, weight)`` for every neighbour that has the value and a positive similarity."""
    pairs: list[tuple[Decimal, Decimal]] = []
    for neighbour in neighbours:
        value = getattr(neighbour, attribute)
        similarity = neighbour.score.combined
        if value is None or similarity is None or similarity <= 0:
            continue
        pairs.append((Decimal(value), similarity))
    return pairs


def weighted_mean(
    pairs: Sequence[tuple[Decimal, Decimal]],
) -> tuple[Decimal, Decimal, Decimal] | None:
    """``(mean, numerator, denominator)`` of ``(value, weight)`` pairs, or ``None`` with no weight."""
    numerator = sum((value * weight for value, weight in pairs), Decimal(0))
    denominator = sum((weight for _, weight in pairs), Decimal(0))
    if denominator <= 0:
        return None
    return numerator / denominator, numerator, denominator


def weighted_median(pairs: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None:
    """The weighted median of ``(value, weight)`` pairs, or ``None`` if there is no positive weight.

    Deterministic: pairs are sorted by value (equal values carry the same number whatever order they sit
    in, so their relative order cannot change the answer). The median is the first value whose accumulated
    weight reaches half the total; when it lands on exactly half it is the midpoint of that value and the
    next one. Compared as ``2 x accumulated`` against ``total`` so the half-way tie is exact, not rounded.
    """
    positive = sorted(((value, weight) for value, weight in pairs if weight > 0), key=lambda p: p[0])
    if not positive:
        return None
    total = sum((weight for _, weight in positive), Decimal(0))
    accumulated = Decimal(0)
    for index, (value, weight) in enumerate(positive):
        accumulated += weight
        doubled = accumulated * 2
        if doubled > total:
            return value
        if doubled == total:
            following = positive[index + 1][0] if index + 1 < len(positive) else value
            return (value + following) / 2
    return positive[-1][0]  # unreachable: the last value always reaches the total


def _plain(value: Decimal) -> str:
    """A decimal as plain digits without trailing zeros (``6``, ``7.5``) -- never ``1E+1``."""
    text = format(value.quantize(_AUDIT_PLACES), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def calculate(
    neighbours: list[Neighbour],
    service_level_configured: bool,
    minimum_neighbours: int = 1,
    minimum_similarity: Decimal | None = None,
    strategy: str = STRATEGY_MEDIAN,
    material_difference_ratio: Decimal = DEFAULT_MATERIAL_DIFFERENCE_RATIO,
) -> OarEstimate:
    """Weighted SS, ROP and Max from the neighbours that have them.

    ``strategy`` is ``weighted_median`` (default) or ``weighted_mean``; both are always computed and
    recorded, the strategy only names which one becomes the estimate.

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

    if strategy not in STRATEGIES:
        raise ValueError(f"unknown estimator {strategy!r}; expected one of {STRATEGIES}")

    trace: list[tuple[str, str]] = [
        ("eligible_neighbours", str(len(eligible))),
        ("ineligible_neighbours", str(ineligible)),
        ("qualifying_neighbours", str(qualifying)),
        ("minimum_neighbours", str(minimum_neighbours)),
    ]
    results: dict[str, int | None] = {}
    detail: list[tuple[str, str]] = []
    comparison: list[tuple[str, str]] = [("estimator", strategy)]
    audit: list[FieldAudit] = []

    for attribute in ("safety_stock", "rop", "max_stock"):
        pairs = _pairs(eligible, attribute)
        mean = weighted_mean(pairs)
        median = weighted_median(pairs)
        if mean is None or median is None:
            results[attribute] = None
            detail.append((f"{attribute}_status", "no_contributing_neighbour"))
            continue
        mean_raw, numerator, denominator = mean
        chosen_raw = median if strategy == STRATEGY_MEDIAN else mean_raw
        mean_final, median_final = _ceil(mean_raw), _ceil(median)
        results[attribute] = _ceil(chosen_raw)

        difference = abs(median_final - mean_final)
        material = difference > 0 and Decimal(difference) >= material_difference_ratio * mean_final
        audit.append(
            FieldAudit(
                field=attribute,
                mean_raw=mean_raw,
                median_raw=median,
                mean_final=mean_final,
                median_final=median_final,
                chosen=strategy,
                material_difference=material,
            )
        )
        comparison.extend(
            (
                (f"{attribute}_mean", str(mean_raw.quantize(_AUDIT_PLACES))),
                (f"{attribute}_median", _plain(median)),
                (f"{attribute}_material_difference", "true" if material else "false"),
            )
        )
        detail.extend(
            (
                (f"{attribute}_numerator", str(numerator)),
                (f"{attribute}_denominator", str(denominator)),
                (f"{attribute}_raw", str(chosen_raw)),
            )
        )

    results["max_stock"], floor_trace = apply_max_floor(
        results["rop"], results["safety_stock"], results["max_stock"]
    )
    # The floor's record goes ahead of everything else, then the estimator audit, then the per-field
    # arithmetic: the stored trace is cut at a fixed length, and the entries an approver reads must
    # survive that before the raw digits do. The donors themselves, with their weights and values, are
    # the neighbour rows already stored for this target.
    trace.extend(floor_trace)
    trace.extend(comparison)
    trace.extend(detail)
    trace.append(("rounding_method", "CEILING"))
    donors = tuple(
        EstimateDonor(
            material=n.material,
            plant=n.plant,
            weight=n.score.combined if n.score.combined is not None else Decimal(0),
            safety_stock=n.safety_stock,
            rop=n.rop,
            max_stock=n.max_stock,
        )
        for n in eligible
    )

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
        strategy=strategy,
        donors=donors,
        audit=tuple(audit),
    )
