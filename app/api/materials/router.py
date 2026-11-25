"""More -> Materials: every active material and plant with its stock on hand.

Mounted at ``/api/materials``, outside the initiative prefixes, because the
list covers every material type rather than one initiative's. Read-only. The
rule is in :mod:`app.shared.materials_list`.

- ``GET /materials``: one page, ordered by material then plant; the filtered
  total travels in ``X-Total-Count``;
- ``GET /materials/counts``: the totals behind the All / OAR / Non-OAR choice,
  and when the list was read;
- ``GET /materials/export.xlsx``: the whole filtered list, not one page.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from io import BytesIO
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from openpyxl import Workbook
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.i13.deps import page
from app.core.db import get_db
from app.shared import materials_list
from app.shared.materials_list import Classification, Filters

router = APIRouter(prefix="/materials", tags=["materials"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_SEARCH = Query(None, max_length=100, description="Material number prefix, or words in the description")
_PLANT = Query(None, max_length=10)
_CLASSIFICATION = Query(None, description="OAR or NON_OAR; omitted for both")


class MaterialStockResponse(BaseModel):
    material: str
    material_description: str | None
    plant: str
    plant_name: str | None
    classification: Literal["OAR", "NON_OAR"]
    #: MARD-LABST summed over storage locations, as a string so no float
    #: rounding reaches the screen.
    stock_on_hand: Decimal
    unit: str | None


class MaterialCountsResponse(BaseModel):
    total: int
    oar: int
    non_oar: int
    #: When MARC/MARD/MARA were read for the list being served.
    loaded_at: datetime


def _filters(search: str | None, plant: str | None, classification: Classification | None) -> Filters:
    return Filters(search=search, plant=plant, classification=classification)


@router.get("", response_model=list[MaterialStockResponse])
def list_materials(
    response: Response,
    search: str | None = _SEARCH,
    plant: str | None = _PLANT,
    classification: Classification | None = _CLASSIFICATION,
    limit: int = Query(25, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[MaterialStockResponse]:
    rows = materials_list.matching(materials_list.current(db).rows, _filters(search, plant, classification))
    shown = page(rows, response, limit=limit, offset=offset)
    names = materials_list.descriptions(shown)
    return [_response(row, names) for row in shown]


@router.get("/counts", response_model=MaterialCountsResponse)
def count_materials(
    search: str | None = _SEARCH,
    plant: str | None = _PLANT,
    db: Session = Depends(get_db),
) -> MaterialCountsResponse:
    """Classification is not a parameter: these are the totals the
    All / OAR / Non-OAR choice offers, under the other filters."""
    loaded = materials_list.current(db)
    totals = materials_list.counts(loaded.rows, _filters(search, plant, None))
    return MaterialCountsResponse(
        total=totals.total, oar=totals.oar, non_oar=totals.non_oar, loaded_at=loaded.loaded_at
    )


@router.get("/export.xlsx")
def export_materials(
    search: str | None = _SEARCH,
    plant: str | None = _PLANT,
    classification: Classification | None = _CLASSIFICATION,
    db: Session = Depends(get_db),
) -> Response:
    loaded = materials_list.current(db)
    rows = materials_list.matching(loaded.rows, _filters(search, plant, classification))
    names = materials_list.descriptions(rows)

    # Write-only: rows stream to the file instead of building every cell
    # object first, which matters at tens of thousands of rows.
    book = Workbook(write_only=True)
    sheet = book.create_sheet("Materials")
    sheet.append(["Material", "Description", "Plant", "Plant name", "Classification", "Stock on hand", "Unit"])
    for row in rows:
        sheet.append(
            [
                row.material,
                materials_list.description_of(row, names),
                row.plant,
                row.plant_name,
                _label(row.classification),
                row.stock_on_hand,
                row.unit,
            ]
        )
    out = BytesIO()
    book.save(out)
    filename = f"materials-stock-{loaded.loaded_at:%Y-%m-%d}.xlsx"
    return Response(
        content=out.getvalue(),
        media_type=XLSX,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _label(classification: Classification) -> str:
    return "OAR" if classification is Classification.OAR else "Non-OAR"


def _response(row: materials_list.MaterialStock, names: dict[str, str]) -> MaterialStockResponse:
    return MaterialStockResponse(
        material=row.material,
        material_description=materials_list.description_of(row, names),
        plant=row.plant,
        plant_name=row.plant_name,
        classification=row.classification.value,
        stock_on_hand=row.stock_on_hand,
        unit=row.unit,
    )
