"""OAR confidence grading.

Formula Reference, Stage 9::

    HIGH:   >= 5 neighbours, best similarity > 0.80
    MEDIUM: 3-4 neighbours, similarity 0.60-0.80
    LOW:    < 3 neighbours, OR similarity < 0.60

The Solution Design adds a same-circuit condition to HIGH.

Confidence is not the similarity score. A single 0.97 neighbour is an excellent
match and still LOW confidence, because one donor is not a basis for setting a
stocking parameter. Grading counts evidence, not closeness.

The boundaries are read exactly as written: HIGH needs similarity **above** 0.80,
so 0.80 itself is not HIGH; LOW is **below** 0.60, so 0.60 itself is not LOW.
That leaves 0.80 and 0.60 in MEDIUM, which is what the bands say. The gap
between the HIGH and MEDIUM neighbour counts is deliberate too: >= 5 neighbours
whose best similarity is only 0.65 satisfies neither band as written, and falls
to LOW rather than being promoted on a rule nobody wrote.
"""

from decimal import Decimal

from app.initiatives.i7.oar.types import Neighbour, OarConfidence


def grade(
    neighbours: list[Neighbour],
    high_minimum_neighbours: int,
    high_minimum_similarity: Decimal,
    medium_minimum_neighbours: int,
    medium_minimum_similarity: Decimal,
    require_same_circuit_for_high: bool = True,
) -> OarConfidence:
    """Grade a neighbour set. Thresholds come from policy."""
    count = len(neighbours)
    if count == 0:
        return OarConfidence.LOW

    best = max(
        (n.score.combined for n in neighbours if n.score.combined is not None),
        default=None,
    )
    if best is None:
        return OarConfidence.LOW

    if best < medium_minimum_similarity:
        return OarConfidence.LOW
    if count < medium_minimum_neighbours:
        return OarConfidence.LOW

    if count >= high_minimum_neighbours and best > high_minimum_similarity:
        if not require_same_circuit_for_high:
            return OarConfidence.HIGH
        # The Solution Design's HIGH band expects neighbours from the same
        # circuit. Unknown circuit does not satisfy it -- there is no evidence
        # either way, and HIGH is the grade that most needs evidence.
        if any(neighbour.same_circuit for neighbour in neighbours):
            return OarConfidence.HIGH
        return OarConfidence.MEDIUM

    if (
        medium_minimum_neighbours <= count
        and medium_minimum_similarity <= best <= high_minimum_similarity
    ):
        return OarConfidence.MEDIUM

    # Enough neighbours and a high score, but the same-circuit or count
    # condition for HIGH was not met.
    return OarConfidence.MEDIUM
