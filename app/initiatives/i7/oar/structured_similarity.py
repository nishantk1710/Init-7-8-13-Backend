"""Dimension 1 -- structured attributes, Gower distance.

Solution Design, Stage 5::

    categorical:  d = 0 if same, 1 if different
    numeric:      d = |a - b| / range

    S_struct = 1 - mean(per-feature distances)

Features: material group, equipment type, unit of measure.

**A missing attribute contributes nothing rather than zero distance.** Two
materials that both lack an equipment type have not been shown to be alike; they
have been shown to be unknown. Counting that as a match (distance 0) would
reward absent data, and on this extract absent data is the norm. So a feature is
averaged only when both sides know it, and the count of available and missing
features travels with the score.

If no feature is known on both sides, the dimension is unavailable -- not 0.0,
which would read as "completely dissimilar".

**Numeric ranges come from the candidate population, not the pair.** Dividing by
``|a - b|`` computed from the two values being compared would make every pair
maximally distant. The range is a property of the population and is recorded for
reproducibility.
"""

from decimal import Decimal

from app.initiatives.i7.oar.types import (
    CandidateAttributes,
    DimensionScore,
    SimilarityStatus,
)

CATEGORICAL_FEATURES = ("material_group", "equipment_type", "base_unit_of_measure")
"""The Solution Design's structured features. Equipment type has no source in
the current extract, so it is normally unavailable -- recorded as missing rather
than dropped from the definition."""


def categorical_distance(left: str | None, right: str | None) -> Decimal | None:
    """0 if identical, 1 if different, ``None`` if either side is unknown."""
    if left is None or right is None:
        return None
    return Decimal(0) if left == right else Decimal(1)


def numeric_distance(
    left: Decimal | None, right: Decimal | None, value_range: Decimal | None
) -> Decimal | None:
    """``|a - b| / range``, clamped to [0, 1].

    ``None`` when either value or the range is unknown, and when the range is
    zero -- a population where every value is identical offers no
    discrimination, and dividing by it would raise rather than inform.
    """
    if left is None or right is None or value_range is None or value_range <= 0:
        return None
    distance = abs(left - right) / value_range
    return min(distance, Decimal(1))


def score(
    target: CandidateAttributes, candidate: CandidateAttributes
) -> DimensionScore:
    """Gower similarity over the structured features."""
    distances: list[Decimal] = []
    missing = 0

    for feature in CATEGORICAL_FEATURES:
        distance = categorical_distance(
            getattr(target, feature), getattr(candidate, feature)
        )
        if distance is None:
            missing += 1
        else:
            distances.append(distance)

    if not distances:
        return DimensionScore(
            value=None,
            status=SimilarityStatus.NOT_AVAILABLE_NO_FEATURES,
            available_features=0,
            missing_features=missing,
        )

    mean_distance = sum(distances, Decimal(0)) / Decimal(len(distances))
    return DimensionScore(
        value=Decimal(1) - mean_distance,
        status=SimilarityStatus.AVAILABLE,
        available_features=len(distances),
        missing_features=missing,
    )
