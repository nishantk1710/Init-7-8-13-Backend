"""Stock item to OAR, as the Overview's Working Capital card sees it.

A Min-Max (stock item) material that is eligible to convert to OAR would have its reorder point go to zero, so the
capital held at its current reorder point is released. This module is the Overview's view of that, in SQL over
``Recommendation``: which stock-item rows are eligible, and what their release is worth.

WHICH MATERIALS ARE ELIGIBLE. A material-plant is eligible when the newest stored Min-Max -> OAR run
(``i7_reverse_recommendation``) puts it in Convert to OAR -- any confidence, High, Medium or Low -- and its newest
conversion request, if it has one, was not rejected. A rejected one has been decided against and is left out, as the
OAR rule leaves out a rejected conversion.

WHAT THE RELEASE IS. The Overview's own saved Working Capital formula, with the recommended reorder point taken as 0:
by default ``unit_price * (current_rop - 0)``. Reorder point only, exactly as the existing figure; maximum stock is
not added. Stated in ZAR by the same currency rule as every other Working Capital figure.

This reads the Min-Max -> OAR tables and changes nothing in them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import and_, exists, false, func, literal, not_, or_, select
from sqlalchemy.orm import aliased

from app.initiatives.i7.kpi_formulas.data_catalog import recommendation_columns
from app.initiatives.i7.kpi_formulas.expression import compile_sql
from app.initiatives.i7.kpi_formulas.resolved import Formulas
from app.models.i7_recommendation import Recommendation
from app.models.i7_reverse import RUN_SUCCEEDED, ReverseConversionRequest, ReverseRecommendation, ReverseRun

ELIGIBLE_CATEGORY = "Recommended for OAR"
REJECTED = "REJECTED"


def stock_item_row() -> Any:
    """A recommendation of a stock item: everything that is not OAR."""
    return or_(Recommendation.is_oar.is_(None), Recommendation.is_oar == false())


def eligible_for_oar() -> Any:
    """True for a recommendation whose material-plant is eligible to convert to OAR (see the module note).

    A correlated EXISTS on the recommendation's material and plant, over the newest succeeded run only."""
    newest_run = select(func.max(ReverseRun.id)).where(ReverseRun.status == RUN_SUCCEEDED).scalar_subquery()
    later = aliased(ReverseConversionRequest)
    newest_request = (
        select(func.max(later.id))
        .where(
            later.sap_material_number == Recommendation.sap_material_number,
            later.sap_plant_code == Recommendation.sap_plant_code,
        )
        .scalar_subquery()
    )
    rejected = exists().where(
        ReverseConversionRequest.sap_material_number == Recommendation.sap_material_number,
        ReverseConversionRequest.sap_plant_code == Recommendation.sap_plant_code,
        ReverseConversionRequest.status == REJECTED,
        ReverseConversionRequest.id == newest_request,
    )
    return exists().where(
        ReverseRecommendation.run_id == newest_run,
        ReverseRecommendation.category == ELIGIBLE_CATEGORY,
        ReverseRecommendation.sap_material_number == Recommendation.sap_material_number,
        ReverseRecommendation.sap_plant_code == Recommendation.sap_plant_code,
        not_(rejected),
    )


def oar_release_positive() -> Any:
    """True when converting to OAR would release capital: a price and a current reorder point above zero. Used to pick
    the rows of a view; the amount itself comes from :func:`oar_release_value`."""
    return and_(
        Recommendation.unit_price.is_not(None),
        Recommendation.current_rop.is_not(None),
        Recommendation.unit_price * Recommendation.current_rop > 0,
    )


def oar_release_value(formulas: Formulas) -> Any:
    """The saved Working Capital formula with the recommended reorder point replaced by 0. NULL where the formula
    cannot be worked out (a missing price or current reorder point), which a SUM leaves out rather than reading as 0."""
    columns = recommendation_columns()
    columns["recommended_rop"] = literal(Decimal(0))
    return compile_sql(formulas.parsed["working_capital_value"], columns)
