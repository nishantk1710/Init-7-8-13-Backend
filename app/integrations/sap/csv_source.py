"""CSV-backed reader for SAP OData entity sets.

Real SAP/CPI credentials are not available in this environment, so both the
live-set gateway and the reduced mock gateway read from the same synthetic
CSV dataset the wider Spares AI project already generates from live
``$metadata`` contracts (``data-generator/generated/sap/*.csv`` --
see ``data-generator/discovery/``). This is the single seam a real CPI OData
transport would replace later: everything above ``load_entity_set`` deals
only in normalised Python rows, never CSV mechanics.
"""

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

Row = dict[str, Any]

_TRUE_VALUES = {"true", "1", "x"}
_FALSE_VALUES = {"false", "0", ""}


@dataclass(frozen=True)
class EntitySetSchema:
    """Column-type hints for one entity set's CSV, so values are coerced to
    real Python types rather than guessed at each call site."""

    date_fields: frozenset[str] = field(default_factory=frozenset)
    decimal_fields: frozenset[str] = field(default_factory=frozenset)
    int_fields: frozenset[str] = field(default_factory=frozenset)
    bool_fields: frozenset[str] = field(default_factory=frozenset)


def _coerce_date(raw: str) -> date | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw).date()


def _coerce_decimal(raw: str) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _coerce_int(raw: str) -> int | None:
    if raw is None or raw == "":
        return None
    return int(Decimal(raw))


def _coerce_bool(raw: str) -> bool:
    return raw.strip().lower() in _TRUE_VALUES


def load_entity_set(path: Path, schema: EntitySetSchema) -> list[Row]:
    """Load one entity-set CSV into normalised rows.

    Returns an empty list (not an error) when the file does not exist or has
    no data rows -- callers distinguish "missing file" from "empty entity
    set" via ``DataSourceStatus``, not via exceptions here.
    """
    if not path.exists():
        return []

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows: list[Row] = []
        for raw_row in reader:
            row: Row = dict(raw_row)
            for column in schema.date_fields:
                if column in row:
                    row[column] = _coerce_date(row[column])
            for column in schema.decimal_fields:
                if column in row:
                    row[column] = _coerce_decimal(row[column])
            for column in schema.int_fields:
                if column in row:
                    row[column] = _coerce_int(row[column])
            for column in schema.bool_fields:
                if column in row:
                    row[column] = _coerce_bool(row[column])
            rows.append(row)
        return rows
