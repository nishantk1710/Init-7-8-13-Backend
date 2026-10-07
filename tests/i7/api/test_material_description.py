"""The MAKT material description reaches the API response.

The description is staged correctly (``i7_staged_material.description``, joined
from MAKT in ``_MATERIAL_SQL``) but ``i7_recommendation`` carries no description
column, so before this it could not be served at all: every I07 screen rendered
a blank name, and the search haystacks on the Recommendations, Overview and
Pipeline tables -- which concatenate material number and description -- matched
nothing when searched by name.

It is joined at read time rather than stored on the recommendation row because
it is display-only and feeds no calculation. A snapshot would be right for
``unit_price``, where the value in force when the recommendation was generated
is itself the fact being recorded; a description has no such property.

No database: these assert the mapping layer, which is where the value was being
dropped. The staged join itself is covered by
``test_staging.py::test_descriptions_are_joined_from_makt``.
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.models.i7_recommendation import Recommendation
from app.schemas.i7.recommendations import RecommendationSummary


def _row() -> Recommendation:
    """A recommendation row shaped like the one behind the reported bug."""
    return Recommendation(
        recommendation_id="REC-1000000050-3000",
        sap_material_number="1000000050",
        sap_plant_code="3000",
        status="NEEDS_REVIEW",
        is_oar=False,
        demand_class="LUMPY",
        criticality="CRITICAL",
        confidence="LOW",
        chain_index=0,
        unit_price=Decimal("0"),
        impact_status="INCREASE",
        generated_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )


def test_description_is_carried_onto_the_summary():
    """The value staged from MAKT reaches the response verbatim."""
    summary = RecommendationSummary.from_model(
        _row(), route=("PLANNER",), description="SULPHUR PRILLS"
    )

    assert summary.description == "SULPHUR PRILLS"
    assert summary.material == "1000000050"


def test_description_is_none_when_makt_has_no_row():
    """Absent, never a placeholder derived from the material number.

    A material number echoed into the description field is what the frontend
    did before this fix, and it is worse than an empty value: it renders
    "1000000050 -- 1000000050" and makes every search by name match a number.
    """
    summary = RecommendationSummary.from_model(_row(), route=("PLANNER",))

    assert summary.description is None
    assert summary.material == "1000000050"
