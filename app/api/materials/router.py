"""More -> Materials: every active material and plant with its stock on hand.

Mounted at ``/api/materials``, outside the initiative prefixes, because the
list covers every material type rather than one initiative's. Read-only. The
rule is in :mod:`app.shared.materials_list`.

- ``GET /materials``: one page, ordered by material then plant; the filtered
  total travels in ``X-Total-Count``;
- ``GET /materials/counts``: the totals behind the All / OAR / Non-OAR choice,
  and when the list was read;
- ``GET /materials/export.xlsx``: the whole filtered list, not one page.

All three answer 503 with ``Retry-After`` until the list's first read from SAP
lands; that read runs in the background, never in a request.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from io import BytesIO
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from openpyxl import Workbook
from pydantic import BaseModel

from app.api.i13.deps import page
from app.shared import materials_list
from app.shared.materials_list import Classification, Filters, MaterialList, NotLoaded

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
) -> list[MaterialStockResponse]:
    rows = materials_list.matching(_loaded().rows, _filters(search, plant, classification))
    shown = page(rows, response, limit=limit, offset=offset)
    names = materials_list.descriptions(shown)
    return [_response(row, names) for row in shown]


@router.get("/counts", response_model=MaterialCountsResponse)
def count_materials(
    search: str | None = _SEARCH,
    plant: str | None = _PLANT,
) -> MaterialCountsResponse:
    """Classification is not a parameter: these are the totals the
    All / OAR / Non-OAR choice offers, under the other filters."""
    loaded = _loaded()
    totals = materials_list.counts(loaded.rows, _filters(search, plant, None))
    return MaterialCountsResponse(
        total=totals.total, oar=totals.oar, non_oar=totals.non_oar, loaded_at=loaded.loaded_at
    )


@router.get("/export.xlsx")
def export_materials(
    search: str | None = _SEARCH,
    plant: str | None = _PLANT,
    classification: Classification | None = _CLASSIFICATION,
) -> Response:
    loaded = _loaded()
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


#: Seconds a client is told to wait before asking again while the list loads.
RETRY_AFTER_SECONDS = 30


def _loaded() -> MaterialList:
    try:
        return materials_list.current()
    except NotLoaded as not_loaded:
        detail = (
            "The materials list could not be read from SAP. It is tried again in a minute."
            if not_loaded.failed
            else "The materials list is still loading from SAP. Try again in a minute."
        )
        raise HTTPException(status_code=503, detail=detail, headers={"Retry-After": str(RETRY_AFTER_SECONDS)}) from None


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
