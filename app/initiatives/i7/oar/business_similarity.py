"""Dimension 3 -- business attributes.

Solution Design, Stage 5::

    criticality match:     HARD CONSTRAINT (handled in eligibility)
    circuit match:         0 if same, 1 if different  (soft preference)
    price band proximity:  |price_a - price_b| / range
    OEM match:             0 if same, 1 if different

    S_business = 1 - mean(business feature distances)

**Circuit is not scored** (since ``oar-similarity-2``). The Solution Design lists
it as a soft preference, but no SAP field delivered so far carries a circuit, so
every pair would either compare two missing values or be scored from an invented
default. Neither is evidence. Circuit therefore contributes no match bonus and
no difference penalty, and it is reported nowhere as compared. Adding it back is
a matter of supplying a trustworthy source and listing it in
``BUSINESS_FEATURES`` again -- not of defaulting it.

Criticality is deliberately absent from the arithmetic here. It is a *filter*,
applied before scoring, so every candidate reaching this function already
matches. Scoring it as well would add a constant 0 distance to every pair --
inflating each score identically and discriminating between nothing.

Price uses a normalised numeric distance over a population range rather than
bands. No approved banding policy exists, and inventing thresholds
("under R100", "R100-1,000") would be inventing business rules.

OEM comes from the canonical manufacturer field. Nothing infers OEM-versus-
generic from description text; absent a manufacturer, the feature is missing.
(On the CSV extract MARA carries no MFRNR at all, so today this is normally
missing -- reported as such, never treated as a match.)
"""

import math
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

BUSINESS_CATEGORICAL_FEATURES = ("manufacturer",)
"""Categorical business features that are scored. Circuit is excluded -- see the
module docstring."""

PRICE_FEATURE = "unit_price"


def log_price(price: Decimal | None) -> Decimal | None:
    """ln(price), or ``None`` when there is no positive price to take it of.

    Price is compared on a log scale (``oar-similarity-7``): what makes two parts alike in price is
    the ratio, not the rupee difference. On the linear scale one repairable at several million
    stretched the population range until a 100-rupee item and a 10,000-rupee item scored 0.999
    alike. ``price_range`` is the matching spread of ln(price).
    """
    if price is None or price <= 0:
        return None
    return Decimal(str(math.log(float(price))))


def score(
    target: CandidateAttributes,
    candidate: CandidateAttributes,
    price_range: Decimal | None,
) -> DimensionScore:
    """Business similarity over manufacturer and price proximity."""
    distances: list[Decimal] = []
    used: list[str] = []
    missing: list[str] = []

    for feature in BUSINESS_CATEGORICAL_FEATURES:
        distance = categorical_distance(
            getattr(target, feature), getattr(candidate, feature)
        )
        if distance is None:
            missing.append(feature)
        else:
            distances.append(distance)
            used.append(feature)

    price_distance = numeric_distance(
        log_price(target.unit_price), log_price(candidate.unit_price), price_range
    )
    if price_distance is None:
        missing.append(PRICE_FEATURE)
    else:
        distances.append(price_distance)
        used.append(PRICE_FEATURE)

    if not distances:
        return DimensionScore(
            value=None,
            status=SimilarityStatus.NOT_AVAILABLE_NO_FEATURES,
            available_features=0,
            missing_features=len(missing),
            missing=tuple(missing),
        )

    mean_distance = sum(distances, Decimal(0)) / Decimal(len(distances))
    return DimensionScore(
        value=Decimal(1) - mean_distance,
        status=SimilarityStatus.AVAILABLE,
        available_features=len(distances),
        missing_features=len(missing),
        features=tuple(used),
        missing=tuple(missing),
    )
