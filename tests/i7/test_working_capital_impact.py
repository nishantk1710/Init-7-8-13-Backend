"""A row's Value change: ``unit_price * (current_rop - recommended_rop)``, or ``None`` when it cannot be worked out.

The Overview's Working Capital Impact card sums the same expression in SQL
(``net_rop_value_impact``), so the row and the card must agree on sign and on which rows count. ``None`` is
never 0: a blocked recommendation or a missing price has no impact, which is not the same claim as "no change".
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models.i7_recommendation import Recommendation
from app.schemas.i7.recommendations import RecommendationDetail, RecommendationSummary, working_capital_impact


def rec(**overrides) -> Recommendation:
    values = dict(
        recommendation_id="REC-1",
        sap_material_number="8000005277",
        sap_plant_code="1300",
        feature_run_id=1,
        forecast_run_id=1,
        inventory_run_id=1,
        oar_run_id=1,
        policy_id="p",
        policy_version=1,
        formula_version="f",
        status="READY_FOR_REVIEW",
        impact_status="AVAILABLE",
        is_oar=False,
        unit_price=Decimal("100"),
        current_rop=Decimal("1"),
        recommended_rop=Decimal("2"),
        generated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        chain_index=0,
        adjustment_count=0,
        current_version=1,
    )
    values.update(overrides)
    return Recommendation(**values)


def test_a_higher_reorder_point_ties_capital_up_and_is_negative():
    # 1 -> 2 at 100 a unit: one more unit on the shelf, 100 more tied up.
    assert working_capital_impact(rec()) == Decimal("-100")


def test_a_lower_reorder_point_releases_capital_and_is_positive():
    assert working_capital_impact(rec(current_rop=Decimal("5"), recommended_rop=Decimal("2"))) == Decimal("300")


def test_an_unchanged_reorder_point_is_a_real_zero():
    result = working_capital_impact(rec(current_rop=Decimal("3"), recommended_rop=Decimal("3")))
    assert result == 0
    assert result is not None


def test_only_the_reorder_point_is_priced_not_the_safety_stock():
    # The case the old column got wrong: safety stock 1 -> 1, reorder point 0 -> 2.
    row = rec(
        current_safety_stock=Decimal("1"),
        recommended_safety_stock=Decimal("1"),
        current_rop=Decimal("0"),
        recommended_rop=Decimal("2"),
        unit_price=Decimal("505837"),
    )
    assert working_capital_impact(row) == Decimal("-1011674")


@pytest.mark.parametrize("missing", ["unit_price", "current_rop", "recommended_rop"])
def test_a_missing_input_is_none_not_zero(missing):
    assert working_capital_impact(rec(**{missing: None})) is None


def test_a_blocked_recommendation_is_none_even_with_numbers_stored():
    # NOT_EVALUABLE rows can carry 0/0 for stock parameters that were never calculated.
    row = rec(status="NOT_EVALUABLE", current_rop=Decimal("0"), recommended_rop=Decimal("0"))
    assert working_capital_impact(row) is None


@pytest.mark.parametrize(
    "impact_status",
    ["NOT_EVALUABLE_MISSING_CURRENT", "NOT_EVALUABLE_MISSING_RECOMMENDED", "NOT_EVALUABLE_COST_DATA_UNAVAILABLE"],
)
def test_an_impact_that_is_not_available_is_none(impact_status):
    assert working_capital_impact(rec(impact_status=impact_status)) is None


def test_the_list_row_and_the_detail_carry_the_same_figure():
    row = rec(current_rop=Decimal("4"), recommended_rop=Decimal("1"), unit_price=Decimal("10"))
    assert RecommendationSummary.from_model(row, ("Plant Head",)).working_capital_impact == Decimal("30")
    assert RecommendationDetail.from_model(row).working_capital_impact == Decimal("30")


def test_the_list_row_says_none_when_it_cannot_be_calculated():
    row = rec(unit_price=None)
    assert RecommendationSummary.from_model(row, ()).working_capital_impact is None
    assert RecommendationDetail.from_model(row).working_capital_impact is None


# --- the row and the Working Capital Impact card -----------------------------------------------


def test_the_rows_add_up_to_the_card_over_the_same_recommendations(monkeypatch):
    """The card's SQL sum and the per-row figure are two implementations of one rule: over the same rows
    they must give the same total, and the same rows must be left out."""
    from datetime import date

    from sqlalchemy import MetaData, create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.initiatives.i7.reporting import kpi_definitions
    from app.initiatives.i7.stockout_risk.counts import StockoutRiskCounts
    from app.models.i7_features import MaterialFeature
    from app.models.i7_staging import StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun
    from app.models.i7_stockout_risk import StockoutRiskInput

    monkeypatch.setattr(
        kpi_definitions, "stockout_risk_counts",
        lambda session, base, **kw: StockoutRiskCounts(at_risk=0, evaluated=0, insufficient_data=0, as_of=None),
    )
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (Recommendation, StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun, StockoutRiskInput, MaterialFeature):
        model.__table__.to_metadata(metadata).indexes.clear()
    metadata.create_all(engine)

    rows = [
        rec(recommendation_id="R1", sap_material_number="A", current_rop=Decimal("1"), recommended_rop=Decimal("2")),
        rec(recommendation_id="R2", sap_material_number="B", current_rop=Decimal("6"), recommended_rop=Decimal("2"),
            unit_price=Decimal("7")),
        rec(recommendation_id="R3", sap_material_number="C", unit_price=None),          # no price: left out
        rec(recommendation_id="R4", sap_material_number="D", recommended_rop=None),     # no reorder point: left out
    ]
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
        figures = kpi_definitions.position_figures(session, None, date(2027, 1, 1), date(2026, 10, 21))
        per_row = [working_capital_impact(row) for row in rows]

    assert per_row == [Decimal("-100"), Decimal("28"), None, None]
    assert figures.net_rop_value_impact == sum(v for v in per_row if v is not None)
    assert figures.priced_count == sum(1 for v in per_row if v is not None)
