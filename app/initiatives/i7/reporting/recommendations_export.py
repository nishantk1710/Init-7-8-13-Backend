"""The Recommendations page's Excel export: one formatted sheet, one row per recommendation.

Quantities are written as real numbers (so Excel can sort, filter and sum them), a missing one as an empty
cell -- never 0 -- and every text cell as text, so a description or reason can never run as a formula.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

SHEET_NAME = "Recommendations"

COLUMNS: tuple[str, ...] = (
    "Material Number",
    "Material Description",
    "Plant",
    "Recommendation Type",
    "Criticality",
    "Status",
    "Current ROP (SAP)",
    "Current Safety Stock (SAP)",
    "Current Max Stock (SAP)",
    "Recommended ROP",
    "Recommended Safety Stock",
    "Recommended Max Stock",
    "Reason",
)

# Column widths (characters), in COLUMNS order. The reason wraps inside its own wide column.
_WIDTHS = (18, 38, 9, 16, 12, 18, 14, 16, 14, 14, 18, 16, 80)
_FIRST_QUANTITY, _LAST_QUANTITY = 7, 12  # 1-based columns Current ROP .. Recommended Max Stock
_REASON = len(COLUMNS)

_QUANTITY_FORMAT = "#,##0.###"
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="1F3864")
_HEADER_BORDER = Border(bottom=Side(style="thin", color="808080"))
_EXCEL_CELL_LIMIT = 32767
# Characters XML (and so an .xlsx) cannot hold; openpyxl refuses a string that has one.
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _text(value: str | None) -> str:
    """Free text for a cell: control characters dropped, kept inside Excel's per-cell limit."""
    return _ILLEGAL.sub("", value or "")[:_EXCEL_CELL_LIMIT]


def build_recommendations_workbook(rows: Iterable[Sequence[str | Decimal | None]]) -> BytesIO:
    """The workbook for ``rows`` (each one value per entry of :data:`COLUMNS`), as an in-memory ``BytesIO``
    positioned at 0, ready to send as a response body."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SHEET_NAME
    sheet.append(list(COLUMNS))

    count = 0
    for row in rows:
        values = [
            value if isinstance(value, Decimal) or value is None else _text(value)
            for value in row
        ]
        sheet.append(values)
        count += 1
        excel_row = sheet.max_row
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=excel_row, column=column)
            if isinstance(value, str):
                # A text cell is always text: openpyxl would turn a leading "=" into a formula.
                cell.data_type = "s"
            elif _FIRST_QUANTITY <= column <= _LAST_QUANTITY and value is not None:
                cell.number_format = _QUANTITY_FORMAT
        sheet.cell(row=excel_row, column=_REASON).alignment = Alignment(wrap_text=True, vertical="top")

    for cell in sheet[1]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.border = _HEADER_BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.row_dimensions[1].height = 32
    for index, width in enumerate(_WIDTHS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    # The filter arrows on the header row, covering the data (just the header when there are no rows).
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(count + 1, 2)}"

    out = BytesIO()
    workbook.save(out)
    out.seek(0)
    return out
