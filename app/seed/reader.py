"""Streaming reader for the extract workbooks.

One rule governs this module: **never hold a workbook, a sheet or a row list in
memory.** The largest extract is 143 MB and MSEG runs to millions of rows across
two files. Every function here is a generator, and ``openpyxl`` is always opened
with ``read_only=True``, which gives a forward-only cursor over the sheet rather
than an object graph.

The second rule: **every value becomes text.** Coercion belongs in the normalise
step, where it is reversible and reviewable. Coercing here is how
``000000008000000000`` silently becomes ``8e+15`` and never comes back.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import openpyxl

from app.core.storage import Storage

# The extract's headers are business labels -- "Ext. Material Group", "DF at
# client level" -- so they need real sanitising, not just a .lower().
#
# 63 characters is retained deliberately. Azure SQL allows 128, but widening the
# cap would silently rename every column whose header sanitises longer than 63,
# and those names are already referenced by the manifest and the mapping work.
# A lower cap is portable; a changed column name is a migration.
_NON_IDENT = re.compile(r"[^0-9a-z]+")
_MAX_IDENT = 63


class ExtractFormatError(RuntimeError):
    """The workbook is not shaped the way the loader requires."""


def column_name(header: Any, position: int) -> str:
    """Turn one extract header into a database column name.

    ``"Ext. Material Group"`` -> ``ext_material_group``. A blank header becomes
    ``column_<n>`` rather than being dropped: an unnamed column still holds data,
    and silently discarding it would be the worst possible outcome.
    """
    text = "" if header is None else str(header).strip().lower()
    name = _NON_IDENT.sub("_", text).strip("_")
    if not name:
        return f"column_{position}"
    if name[0].isdigit():
        name = f"c_{name}"
    return name[:_MAX_IDENT]


def unique_column_names(headers: list[Any]) -> list[str]:
    """Column names for a header row, with collisions resolved positionally.

    A 244-column sheet very plausibly contains two headers that sanitise to the
    same identifier. Suffixing keeps both columns instead of losing one.
    """
    names: list[str] = []
    seen: dict[str, int] = {}
    for position, header in enumerate(headers, start=1):
        base = column_name(header, position)
        if base in seen:
            seen[base] += 1
            candidate = f"{base}_{seen[base]}"[:_MAX_IDENT]
            while candidate in seen:
                seen[base] += 1
                candidate = f"{base}_{seen[base]}"[:_MAX_IDENT]
            names.append(candidate)
            seen[candidate] = 0
        else:
            seen[base] = 0
            names.append(base)
    return names


def to_text(value: Any) -> str | None:
    """Render one cell as text, preserving what SAP meant.

    The cases that matter, and why:

    * ``None`` and whitespace-only stay ``None``. SAP's blank and Excel's empty
      cell are the same absence, and ``''`` in a key column would be a lie.
    * ``datetime``/``date`` become ISO. Excel has already parsed SAP's date
      format, and ISO is the one form that sorts and re-parses unambiguously.
    * ``float`` that is integral loses the ``.0``. Excel stores every number as a
      float, so a material number arrives as ``2000000131.0``; ``2000000131`` is
      what SAP meant.
    * ``bool`` becomes ``X``/``''`` -- SAP's flag convention, not ``True``/``False``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, bool):
        return "X" if value else None
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0, 0) else value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, (int, Decimal)):
        return str(value)
    return str(value)


def _sheet(workbook, key: str, sheet: str | None):
    """The sheet to read, by name when given, else the first one.

    The SAP table extracts are single-sheet, but the ZMM065 and GR reports are
    real workbooks -- pivots, filters and working sheets alongside the data. For
    those, "first sheet" is wrong: BMM's data is the third of five.
    """
    if sheet is None:
        return workbook[workbook.sheetnames[0]]
    if sheet not in workbook.sheetnames:
        raise ExtractFormatError(
            f"{key}: no sheet named {sheet!r}. Sheets present: {workbook.sheetnames}"
        )
    return workbook[sheet]


def read_headers(
    storage: Storage, key: str, sheet: str | None = None, header_row: int = 1
) -> list[str]:
    """Column names from a workbook's header row.

    ``header_row`` is 1-based. Both ZMM065 reports carry a blank title row above
    their headers, so they need 2 -- reading row 1 there would name every column
    ``column_n`` and silently produce a table of unusable labels.
    """
    with storage.open_read(key) as handle:
        workbook = openpyxl.load_workbook(handle, read_only=True, data_only=True)
        try:
            worksheet = _sheet(workbook, key, sheet)
            raw = next(
                worksheet.iter_rows(min_row=header_row, max_row=header_row, values_only=True),
                None,
            )
            if raw is None:
                raise ExtractFormatError(f"{key}: no row {header_row} to read headers from.")
            return unique_column_names(list(raw))
        finally:
            workbook.close()


def read_rows(
    storage: Storage,
    key: str,
    expected: list[str] | None = None,
    sheet: str | None = None,
    header_row: int = 1,
) -> Iterator[tuple[str | None, ...]]:
    """Yield every data row of a workbook as a tuple of text values.

    ``expected`` is the column list the table was created with. When given, the
    workbook's own header is checked against it and a mismatch raises. That check
    is the whole reason the split files (MSEG_1/MSEG_2, CDHDR1/CDHDR2) are safe
    to concatenate: if the second file's shape differs, the load fails loudly
    rather than writing misaligned rows.

    Rows are padded or trimmed to the header width. Excel routinely reports a
    ragged final column, and a short row must not shift every value left.
    """
    with storage.open_read(key) as handle:
        workbook = openpyxl.load_workbook(handle, read_only=True, data_only=True)
        try:
            worksheet = _sheet(workbook, key, sheet)
            rows = worksheet.iter_rows(min_row=header_row, values_only=True)

            header = next(rows, None)
            if header is None:
                raise ExtractFormatError(f"{key}: no row {header_row} to read headers from.")

            names = unique_column_names(list(header))
            if expected is not None and names != expected:
                raise ExtractFormatError(
                    f"{key}: header does not match the table.\n"
                    f"  table has {len(expected)} columns, file has {len(names)}\n"
                    f"  only in table: {sorted(set(expected) - set(names))[:8]}\n"
                    f"  only in file : {sorted(set(names) - set(expected))[:8]}"
                )

            width = len(names)
            for row in rows:
                values = [to_text(v) for v in row[:width]]
                if len(values) < width:
                    values.extend([None] * (width - len(values)))
                if any(v is not None for v in values):
                    # Skip rows that are entirely blank. Excel exports often
                    # carry trailing empty rows inside the declared dimension.
                    yield tuple(values)
        finally:
            workbook.close()
