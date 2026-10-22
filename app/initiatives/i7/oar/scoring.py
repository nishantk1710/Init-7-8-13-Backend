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

So, for the dimensions D available for a pair (each already in [0, 1])::

    S = sum(W_d x S_d for d in D) / sum(W_d for d in D)

Renormalising answers a well-defined question -- "how similar are these on the
evidence available?" -- and ``score_completeness`` records how much evidence
that was, so a 0.9 computed from one dimension is distinguishable from a 0.9
computed from three.

When no dimension is available the score is ``None``, never 0.0: nothing was
measured, which is not the same as measuring no resemblance.

**Renormalising has a failure mode, and the evidence rule closes it.** If only
one weak feature is known for a pair -- typically the unit of measure, shared by
thousands of unrelated parts -- the renormalised score is 1.0 on that feature
alone, and it would outrank a genuine match measured on everything.
:func:`assess_evidence` therefore refuses admission to a pair unless:

* at least ``minimum_features`` features were actually compared, and
* at least one of them is an *identity* feature -- one that says what the part
  is (material group, description), not merely how it is counted or priced.

The score is still computed and kept for explanation; it is only refused as a
neighbour. Both requirements are policy (``SimilarityPolicy``), and setting
``minimum_features`` to 0 with no identity features restores the old behaviour.
"""

from decimal import Decimal

from app.initiatives.i7.oar.types import CombinedScore, DimensionScore

TOLERANCE = Decimal("0.000001")
"""Floating-point slack when clamping the result into [0, 1]. Renormalised
weights can sum to 0.9999999998, which must not push a perfect score out of
range -- but a genuinely out-of-bounds score is still an error."""


def assess_evidence(
    features_used: tuple[str, ...],
    minimum_features: int,
    identity_features: tuple[str, ...],
) -> tuple[bool, str | None]:
    """Whether a pair's compared features are enough to admit it.

    Returns ``(sufficient, reason)``. With ``minimum_features`` 0 and no identity
    features, every pair with a score is sufficient -- the pre-``-2`` behaviour.
    """
    if len(features_used) < minimum_features:
        return False, (
            f"{len(features_used)} feature(s) compared, fewer than the "
            f"{minimum_features} required"
        )
    if identity_features and not any(f in features_used for f in identity_features):
        return False, (
            "no identity feature compared (needs one of "
            f"{', '.join(identity_features)})"
        )
    return True, None


def combine(
    structured: DimensionScore,
    text: DimensionScore,
    business: DimensionScore,
    structured_weight: Decimal,
    text_weight: Decimal,
    business_weight: Decimal,
    minimum_evidence_features: int = 0,
    identity_evidence_features: tuple[str, ...] = (),
    same_material_group: bool = False,
    identity_text_floor: Decimal | None = None,
) -> CombinedScore:
    """Weighted combination over the available dimensions.

    The two evidence arguments default to "no requirement", so a caller that
    does not pass a policy gets exactly the arithmetic it always got.
    """
    contributions: list[tuple[str, Decimal, Decimal]] = []

    for name, dimension, weight in (
        ("structured", structured, structured_weight),
        ("text", text, text_weight),
        ("business", business, business_weight),
    ):
        if dimension.is_available:
            contributions.append((name, dimension.value, weight))

    features_used = structured.features + text.features + business.features
    features_missing = structured.missing + text.missing + business.missing
    intended_total = structured_weight + text_weight + business_weight

    available_weight = sum((weight for _, _, weight in contributions), Decimal(0))
    if not contributions or available_weight <= 0:
        return CombinedScore(
            combined=None,
            structured=structured,
            text=text,
            business=business,
            score_completeness=Decimal(0),
            features_used=features_used,
            features_missing=features_missing,
            evidence_sufficient=False,
            evidence_detail="no similarity dimension could be computed",
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
    sufficient, reason = assess_evidence(
        features_used, minimum_evidence_features, identity_evidence_features
    )
    if sufficient and identity_text_floor is not None:
        text_value = text.value if text.is_available else None
        if not same_material_group and not (text_value is not None and text_value >= identity_text_floor):
            sufficient = False
            reason = "no identity match: different material group and " + (
                "no text comparison"
                if text_value is None
                else f"text similarity {text_value:.2f} below {identity_text_floor}"
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
        features_used=features_used,
        features_missing=features_missing,
        evidence_sufficient=sufficient,
        evidence_detail=reason,
    )


def explain(score: CombinedScore, limit: int = 500) -> str:
    """A one-line, stored explanation of a score: what entered it and how.

    ``used=...;missing=...;weights=...;text=...;evidence=...`` -- enough to
    recompute the combined value from the stored dimension scores and to see why
    a feature did or did not count. Truncated to ``limit`` characters for the
    column it is stored in.
    """
    weights = ",".join(f"{name}:{Decimal(w):.4f}" for name, w in score.applied_weights)
    parts = [
        f"used={','.join(score.features_used) or '-'}",
        f"missing={','.join(score.features_missing) or '-'}",
        f"weights={weights or '-'}",
        f"text={score.text.status.value}",
        "evidence=" + ("ok" if score.evidence_sufficient else f"low({score.evidence_detail})"),
    ]
    return ";".join(parts)[:limit]
