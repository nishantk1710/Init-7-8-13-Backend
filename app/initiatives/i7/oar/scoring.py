"""Combined similarity score.

Solution Design, Stage 5::

    S = W1 x S_struct + W2 x S_text + W3 x S_business

    starting weights: W1 = 0.35, W2 = 0.30, W3 = 0.35

**Unavailable dimensions are excluded and the weights renormalised over what
remains.** Treating a missing dimension as 0.0 would be arithmetically
convenient and badly wrong: a candidate with no description would lose 30% of
its possible score for a reason that says nothing about whether it resembles the
target, and would rank below a worse candidate that happened to have text. It
would also make scores incomparable between pairs, since each would be penalised
differently by data availability.

Renormalising answers a well-defined question -- "how similar are these on the
evidence available?" -- and ``score_completeness`` records how much evidence
that was, so a 0.9 computed from one dimension is distinguishable from a 0.9
computed from three.

When no dimension is available the score is ``None``, never 0.0: nothing was
measured, which is not the same as measuring no resemblance.
"""

from decimal import Decimal

from app.initiatives.i7.oar.types import CombinedScore, DimensionScore

TOLERANCE = Decimal("0.000001")
"""Floating-point slack when clamping the result into [0, 1]. Renormalised
weights can sum to 0.9999999998, which must not push a perfect score out of
range -- but a genuinely out-of-bounds score is still an error."""


def combine(
    structured: DimensionScore,
    text: DimensionScore,
    business: DimensionScore,
    structured_weight: Decimal,
    text_weight: Decimal,
    business_weight: Decimal,
) -> CombinedScore:
    """Weighted combination over the available dimensions."""
    contributions: list[tuple[str, Decimal, Decimal]] = []

    for name, dimension, weight in (
        ("structured", structured, structured_weight),
        ("text", text, text_weight),
        ("business", business, business_weight),
    ):
        if dimension.is_available:
            contributions.append((name, dimension.value, weight))

    intended_total = structured_weight + text_weight + business_weight

    if not contributions:
        return CombinedScore(
            combined=None,
            structured=structured,
            text=text,
            business=business,
            score_completeness=Decimal(0),
        )

    available_weight = sum(weight for _, _, weight in contributions)
    if available_weight <= 0:
        return CombinedScore(
            combined=None,
            structured=structured,
            text=text,
            business=business,
            score_completeness=Decimal(0),
        )

    total = sum(
        value * (weight / available_weight) for _, value, weight in contributions
    )

    # Each dimension is already in [0, 1] and the renormalised weights sum to 1,
    # so the result is too apart from rounding. Clamp within tolerance; anything
    # further out is a genuine defect and is left visible.
    if -TOLERANCE <= total < 0:
        total = Decimal(0)
    elif Decimal(1) < total <= Decimal(1) + TOLERANCE:
        total = Decimal(1)

    completeness = (
        available_weight / intended_total if intended_total > 0 else Decimal(0)
    )

    return CombinedScore(
        combined=total,
        structured=structured,
        text=text,
        business=business,
        score_completeness=completeness,
        applied_weights=tuple(
            (name, str(weight / available_weight)) for name, _, weight in contributions
        ),
    )
