"""Dimension 3 -- business attributes.

Solution Design, Stage 5::

    criticality match:     HARD CONSTRAINT (handled in eligibility)
    circuit match:         0 if same, 1 if different  (soft preference)
    price band proximity:  |price_a - price_b| / range
    OEM match:             0 if same, 1 if different

    S_business = 1 - mean(business feature distances)

Criticality is deliberately absent from the arithmetic here. It is a *filter*,
applied before scoring, so every candidate reaching this function already
matches. Scoring it as well would add a constant 0 distance to every pair --
inflating each score identically and discriminating between nothing.

Price uses a normalised numeric distance over a population range rather than
bands. No approved banding policy exists, and inventing thresholds
("under R100", "R100-1,000") would be inventing business rules.

OEM comes from the canonical manufacturer field. Nothing infers OEM-versus-
generic from description text; absent a manufacturer, the feature is missing.
"""

from decimal import Decimal

from app.initiatives.i7.oar.structured_similarity import (
    categorical_distance,
    numeric_distance,
)
from app.initiatives.i7.oar.types import (
    CandidateAttributes,
    DimensionScore,
    SimilarityStatus,
)


def score(
    target: CandidateAttributes,
    candidate: CandidateAttributes,
    price_range: Decimal | None,
) -> DimensionScore:
    """Business similarity over circuit, price proximity and manufacturer."""
    distances: list[Decimal] = []
    missing = 0

    for feature in ("circuit", "manufacturer"):
        distance = categorical_distance(
            getattr(target, feature), getattr(candidate, feature)
        )
        if distance is None:
            missing += 1
        else:
            distances.append(distance)

    price_distance = numeric_distance(
        target.unit_price, candidate.unit_price, price_range
    )
    if price_distance is None:
        missing += 1
    else:
        distances.append(price_distance)

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
