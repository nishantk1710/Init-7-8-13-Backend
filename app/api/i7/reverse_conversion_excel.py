"""Min-Max -> OAR recommendations as an Excel workbook: the Stock item to OAR tab's "Export Excel".

The same rows and filters as the list and the CSV export, as an ``.xlsx`` with one more column, "Counted in Working
Capital": whether the Overview's Stock item to OAR working capital figures count the material-plant (``Yes``, or
``No - <reason>``). The flag is the Working Capital card's own rule, evaluated in SQL on the material-plant's newest
Stock item recommendation (``counted_as_stock_to_oar``), so the file and the card cannot disagree.

A file of its own, apart from ``reverse_conversion.py``: that module must not load the OAR -> Min-Max recommendation
machinery (a test pins it), and the flag needs the recommendation table and the working-capital rule.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.i7.reverse_conversion import (
    CategoryCode,
    ConfidenceCode,
    EligibilityCode,
    LeadTimeBucket,
    _filters,
    get_reverse_report,
)
from app.core.db import get_db
from app.initiatives.i7.kpi_formulas.resolved import load_active
from app.initiatives.i7.kpi_formulas.stock_to_oar import eligible_for_oar
from app.initiatives.i7.kpi_formulas.working_capital import counted_as_stock_to_oar, why_not_counted_as_stock_to_oar
from app.initiatives.i7.reporting.recommendations_export import counted_label
from app.initiatives.i7.reverse_conversion.export import rows_to_xlsx
from app.initiatives.i7.reverse_conversion.service import RecommendationRow, ReverseReport, apply_filters
from app.initiatives.i7.reverse_conversion.types import ReverseCategory
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterialPlant

router = APIRouter(prefix="/reverse-conversion", tags=["i7-reverse-conversion"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_CHUNK = 500

NOT_ELIGIBLE_NOTE = "not eligible to convert to OAR"
NO_RECOMMENDATION_NOTE = "no Stock item recommendation to value it from"


def counted_labels(session: Session, rows: list[RecommendationRow]) -> dict[tuple[str, str], str]:
    """``(material, plant) -> "Yes" | "No - <reason>"`` for every listed row, a chunk of material-plants at a time."""
    rule = counted_as_stock_to_oar(load_active(session))
    eligible = eligible_for_oar()
    labels: dict[tuple[str, str], str] = {}
    for start in range(0, len(rows), _CHUNK):
        chunk = rows[start : start + _CHUNK]
        materials = sorted({row.material for row in chunk})
        newest = (
            select(func.max(Recommendation.id))
            .where(Recommendation.sap_material_number.in_(materials))
            .group_by(Recommendation.sap_material_number, Recommendation.sap_plant_code)
        )
        recommendations = {
            (material, plant): (bool(counted), bool(still_eligible), price, current_rop)
            for material, plant, counted, still_eligible, price, current_rop in session.execute(
                select(
                    Recommendation.sap_material_number,
                    Recommendation.sap_plant_code,
                    case((rule, 1), else_=0),
                    case((eligible, 1), else_=0),
                    Recommendation.unit_price,
                    Recommendation.current_rop,
                ).where(Recommendation.id.in_(newest))
            )
        }
        currencies = {
            (material, plant): currency
            for material, plant, currency in session.execute(
                select(
                    StagedMaterialPlant.sap_material_number, StagedMaterialPlant.sap_plant_code, StagedMaterialPlant.currency
                ).where(StagedMaterialPlant.sap_material_number.in_(materials))
            )
        }
        for row in chunk:
            key = (row.material, row.plant)
            found = recommendations.get(key)
            if row.category is not ReverseCategory.RECOMMENDED_FOR_OAR:
                labels[key] = counted_label(False, NOT_ELIGIBLE_NOTE)
            elif found is None:
                labels[key] = counted_label(False, NO_RECOMMENDATION_NOTE)
            else:
                counted, still_eligible, price, current_rop = found
                labels[key] = counted_label(
                    counted,
                    None
                    if counted
                    else why_not_counted_as_stock_to_oar(
                        unit_price=price, currency=currencies.get(key), current_rop=current_rop, eligible=still_eligible
                    ),
                )
    return labels


@router.get(
    "/recommendations/export-excel",
    summary="Export Min-Max to OAR recommendations as an Excel workbook",
    description="The recommendations the same filters select, every page, as an .xlsx with the calculation anchor and "
    "policy version on every row and a 'Counted in Working Capital' column (Yes, or No with the reason) saying whether the "
    "Overview's Stock item to OAR working capital counts the material-plant. Read-only; calls no SAP API.",
    response_class=Response,
)
def export_reverse_recommendations_excel(
    report: Annotated[ReverseReport, Depends(get_reverse_report)],
    session: Annotated[Session, Depends(get_db)],
    category: CategoryCode | None = None,
    plant: str | None = None,
    reason: str | None = None,
    confidence: ConfidenceCode | None = None,
    eligibility: EligibilityCode | None = None,
    criticality: str | None = None,
    lead_time_bucket: LeadTimeBucket | None = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
) -> Response:
    filters = _filters(category, plant, reason, criticality, lead_time_bucket, search, confidence, eligibility)
    rows = apply_filters(report.rows, filters)
    workbook = rows_to_xlsx(rows, report.anchor, report.policy, counted_labels(session, rows))
    filename = f"i7-stock-item-to-oar-{report.anchor.month:%Y-%m}.xlsx"
    return Response(
        content=workbook.getvalue(),
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Anchor-Month": f"{report.anchor.month:%Y-%m}",
            "X-Policy-Version": report.policy.policy_version,
        },
    )
