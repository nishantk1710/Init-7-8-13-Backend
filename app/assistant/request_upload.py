"""A multi-material request's materials from an Excel file (UAT, 22 Oct).

Instead of adding materials one by one, the requester downloads the template
(:func:`build_template`), lists the materials in it and uploads it
(:func:`extract`). This module only reads and checks the file: it writes
nothing, and every material it returns is then previewed exactly as one added
by hand (``POST /assistant/requests/preview``). Quantity, window and
justification are still answered on screen.

The format is fixed by the template -- a ``Materials`` sheet whose first row
names the columns ``Material`` and ``Plant``. One request is one plant, so a
file naming more than one is refused as a whole, never trimmed; more materials
than the request's cap is refused too, never cut off. Every problem is reported
at once, so the file can be fixed in one go.

The department is not in the file (TL, 20 Nov): it is picked once on the
request itself, so the column was asking for the same answer twice. A
``Department`` column left over in an older copy of the template is simply
ignored, like any other column the requester keeps for themselves.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from app.shared.plant_scope import IN_SCOPE_PLANTS

#: Far larger than any honest list of 50 materials; a guard against the wrong file.
MAX_UPLOAD_BYTES = 1024 * 1024

SHEET = "Materials"
HELP_SHEET = "How to fill"
HEADERS = ("Material", "Plant")
"""The columns read. Any other column is the requester's own and is ignored."""

#: The template's columns as headed, each saying whether it must be filled
#: (UAT, 2 Nov): the two read, plus a name for the requester's own reference
#: -- never read, the screen shows SAP's description. The reader ignores the
#: bracketed part, so a plain "Material" header is just as good.
TEMPLATE_COLUMNS = (
    "Material (required)",
    "Material name (optional)",
    "Plant (required)",
)
_OPTIONAL_COLUMNS = {"Material name (optional)"}
TEMPLATE_FILE_NAME = "material-request-template.xlsx"

#: Header text as written, lower-cased with spaces removed -> column.
_HEADER_KEYS = {"material": "material", "plant": "plant"}

#: What a material number may look like; the preview says whether it exists.
_MATERIAL = re.compile(r"[A-Za-z0-9-]{1,40}")

#: Rows listed by number in one message before "and N more".
_ROWS_SHOWN = 8


class UploadError(ValueError):
    """The file cannot be used. ``errors`` lists every reason."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__(" ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class ExtractedFile:
    plant: str
    materials: list[str]
    """In file order, duplicates removed."""
    row_count: int
    """Rows read, blank rows aside."""
    duplicates: list[str] = field(default_factory=list)


# --- template --------------------------------------------------------------------


def build_template(max_lines: int) -> bytes:
    """The blank template: text columns, so Excel keeps leading zeros. Required
    headers are bold on blue, the optional one plain on grey."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SHEET
    sheet.append(list(TEMPLATE_COLUMNS))
    for cell in sheet[1]:
        optional = cell.value in _OPTIONAL_COLUMNS
        cell.font = Font(bold=not optional, italic=optional, color="595959" if optional else "000000")
        cell.fill = PatternFill("solid", fgColor="F2F2F2" if optional else "DDEBF7")
    sheet.freeze_panes = "A2"
    for letter, width in zip("ABC", (22, 45, 18), strict=True):
        sheet.column_dimensions[letter].width = width
    # Text format on the rows a requester will fill, so 000000000010001234
    # stays as typed instead of becoming a number.
    for row in sheet.iter_rows(min_row=2, max_row=max_lines + 51, max_col=len(TEMPLATE_COLUMNS)):
        for cell in row:
            cell.number_format = "@"

    notes = workbook.create_sheet(HELP_SHEET)
    for line in (
        f"List the materials on the '{SHEET}' sheet, one per row. Do not rename the sheet or the headers.",
        "Material (required): the SAP material number. Leading zeros are optional.",
        "Material name (optional): for your own reference; may be left blank. It is not read: the screen shows SAP's description.",
        f"Plant (required): the same plant on every row ({' or '.join(IN_SCOPE_PLANTS)}).",
        "The department is not in the file: pick it once on the request, above the materials.",
        f"Up to {max_lines} materials per file.",
        "After upload each material is checked; quantities and reasons are answered on screen.",
    ):
        notes.append([line])
    notes.column_dimensions["A"].width = 100

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --- extraction ------------------------------------------------------------------


def _text(value: Any) -> str:
    """A cell as the text a person sees -- a number without ``.0`` or exponent."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _rows(numbers: list[int]) -> str:
    shown = ", ".join(str(n) for n in numbers[:_ROWS_SHOWN])
    if len(numbers) > _ROWS_SHOWN:
        shown += f" and {len(numbers) - _ROWS_SHOWN} more"
    return f"row{'' if len(numbers) == 1 else 's'} {shown}"


def _bare(material: str) -> str:
    """Leading zeros are not part of a material's identity: Excel drops them."""
    return material.upper().lstrip("0") or "0"


def _read_sheet(content: bytes) -> list[list[str]]:
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as error:
        raise UploadError(
            [f"The file could not be read as an Excel workbook. Download the template and fill that in. ({type(error).__name__})"]
        ) from None
    try:
        if SHEET in workbook.sheetnames:
            sheet = workbook[SHEET]
        elif len(workbook.sheetnames) == 1:
            # A requester who renamed the only sheet still means it.
            sheet = workbook[workbook.sheetnames[0]]
        else:
            raise UploadError([f"The workbook has no '{SHEET}' sheet. Use the template and keep its sheet name."])
        return [[_text(v) for v in row] for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def extract(content: bytes, *, max_lines: int, file_name: str = "") -> ExtractedFile:
    """The materials in an uploaded template, or :class:`UploadError` listing every problem."""
    if file_name and not file_name.lower().endswith(".xlsx"):
        hint = (
            "Old .xls files cannot be read: in Excel, Save As → Excel Workbook (.xlsx)."
            if file_name.lower().endswith(".xls")
            else "Upload the template as an Excel workbook (.xlsx)."
        )
        raise UploadError([f"{file_name} is not an .xlsx file. {hint}"])
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadError([f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. List the materials only."])

    rows = _read_sheet(content)
    header_at = next((i for i, row in enumerate(rows) if any(row)), None)
    if header_at is None:
        raise UploadError(["The file is empty."])

    # "Material (required)" is "material": the bracketed hint is the template's, not a column name.
    header = [re.sub(r"\(.*?\)|[\s*]", "", cell).lower() for cell in rows[header_at]]
    index = {column: header.index(key) for key, column in _HEADER_KEYS.items() if key in header}
    missing = [title for title in HEADERS if title.lower() not in index]
    if missing:
        raise UploadError(
            [f"The first row must name the columns {', '.join(HEADERS)}. Missing: {', '.join(missing)}. Use the template."]
        )

    def cell(row: list[str], column: str) -> str:
        at = index[column]
        return row[at] if at < len(row) else ""

    empty: dict[str, list[int]] = {title: [] for title in HEADERS}
    not_material: list[int] = []
    plants: dict[str, None] = {}
    found: list[str] = []
    row_count = 0
    for offset, row in enumerate(rows[header_at + 1 :]):
        number = header_at + offset + 2
        material, plant = cell(row, "material"), cell(row, "plant")
        if not (material or plant):
            continue
        row_count += 1
        for title, value in zip(HEADERS, (material, plant), strict=True):
            if not value:
                empty[title].append(number)
        if material and not _MATERIAL.fullmatch(material):
            not_material.append(number)
        elif material:
            found.append(material)
        if plant:
            plants.setdefault(plant)

    if row_count == 0:
        raise UploadError(["The file has the headers but no materials."])

    errors = [f"{title} is empty on {_rows(numbers)}." for title, numbers in empty.items() if numbers]
    if not_material:
        errors.append(f"Material is not a material number on {_rows(not_material)}.")

    plant = next(iter(plants), "")
    if len(plants) > 1:
        errors.append(
            f"The file has more than one plant ({', '.join(plants)}). "
            "One request covers one plant: upload a file per plant."
        )
    elif plant and plant not in IN_SCOPE_PLANTS:
        errors.append(f"Plant {plant} is not {' or '.join(IN_SCOPE_PLANTS)}.")

    seen: set[str] = set()
    materials: list[str] = []
    duplicates: list[str] = []
    for material in found:
        key = _bare(material)
        if key in seen:
            duplicates.append(material)
        else:
            seen.add(key)
            materials.append(material)
    if len(materials) > max_lines:
        errors.append(
            f"The file has {len(materials)} materials: a request holds up to {max_lines}. "
            "Split the file and upload the rest as another request."
        )

    if errors:
        raise UploadError(errors)
    return ExtractedFile(
        plant=plant,
        materials=materials,
        row_count=row_count,
        duplicates=duplicates,
    )


__all__ = [
    "HEADERS",
    "MAX_UPLOAD_BYTES",
    "SHEET",
    "TEMPLATE_COLUMNS",
    "TEMPLATE_FILE_NAME",
    "ExtractedFile",
    "UploadError",
    "build_template",
    "extract",
]
