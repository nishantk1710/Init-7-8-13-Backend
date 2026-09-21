"""Build ``material_plant`` from the raw OData tables.

MARC is the spine -- one row per material and plant, which is the grain. MARA
and MAKT are looked up per material and widen it.

A LEFT JOIN, not an INNER one, and that is a decision rather than a default.
The 21-Sep sweep measured MARA covering 93.4% of the MARC population. An inner
join would silently drop the other 6.6%, and those rows are real material-plant
combinations that stock and movements will reference. Better a dimension row
with a null description than a movement pointing at a material the dimension
has never heard of.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

from sqlalchemy import text as sql

from app.core.db import get_engine, get_sessionmaker
from app.core.logging import get_logger
from app.integrations.sap.known_conditions import OAR_MRP_TYPES
from app.models.serving import MaterialPlant
from app.normalise import coerce, matnr

logger = get_logger(__name__)

TARGET = "material_plant"

# The raw tables this reads. Named here so a missing one is reported as a
# prerequisite rather than as a SQL error somewhere in the middle.
SOURCE_MARC = "odata_material_plant"
SOURCE_MARA = "odata_material"
SOURCE_MAKT = "odata_material_description"

# Rows per INSERT round trip, matching the seed loader's batch size.
BATCH_ROWS = 1000


@dataclass
class BuildResult:
    rows: int = 0
    seconds: float = 0.0
    oar_rows: int = 0
    missing_mara: int = 0
    missing_maktx: int = 0
    skipped_no_key: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


def _table_exists(connection, table: str) -> bool:
    return connection.execute(
        sql("SELECT OBJECT_ID(:t, 'U')"), {"t": f"dbo.{table}"}
    ).scalar() is not None


def _rows(connection, table: str, columns: str) -> Iterable[dict[str, Any]]:
    result = connection.execute(sql(f"SELECT {columns} FROM [{table}]"))
    for row in result:
        yield dict(row._mapping)


def _lookup(connection, table: str, columns: str, key: str) -> dict[str, dict]:
    """A material-keyed lookup, with the key padded on the way in.

    Padding here rather than at the join is the whole point: MARA and MAKT may
    carry the short form where MARC carries the padded one, and matching raw
    strings would return nothing for exactly the rows that differ.
    """
    out: dict[str, dict] = {}
    for row in _rows(connection, table, columns):
        padded = matnr.pad(row.get(key))
        if padded is not None:
            out[padded] = row
    return out


def build(*, run_date: date | None = None) -> BuildResult:
    """Rebuild the dimension. Never raises -- failures are returned."""
    started = time.monotonic()
    result = BuildResult()
    run_date = run_date or datetime.now(timezone.utc).date()

    try:
        engine = get_engine()
        with engine.begin() as connection:
            missing = [
                t
                for t in (SOURCE_MARC, SOURCE_MARA, SOURCE_MAKT)
                if not _table_exists(connection, t)
            ]
            if SOURCE_MARC in missing:
                result.error = (
                    f"{SOURCE_MARC} does not exist, and it is the spine of this "
                    "table. Ingest it first: "
                    "python -m app.ingest --fetch --load --set MaterialPlantSet"
                )
                return result
            for table in missing:
                # MARA and MAKT only widen the row. Building without them gives
                # a usable dimension with null descriptions, which beats
                # refusing to build at all.
                result.warnings.append(
                    f"{table} is missing; the columns it supplies will be null"
                )
                logger.warning("%s: %s is missing, continuing without it", TARGET, table)

            mara = (
                {}
                if SOURCE_MARA in missing
                else _lookup(connection, SOURCE_MARA, "Matnr, Mtart, Matkl, Meins", "Matnr")
            )
            makt = (
                {}
                if SOURCE_MAKT in missing
                else _lookup(connection, SOURCE_MAKT, "Matnr, Maktx", "Matnr")
            )

            connection.execute(sql(f"DELETE FROM [{TARGET}]"))

            batch: list[dict] = []
            for marc in _rows(
                connection,
                SOURCE_MARC,
                "Matnr, Werks, Dismm, Eisbe, Minbe, Mabst, Losgr, Plifz",
            ):
                row = _dimension_row(marc, mara, makt, run_date, result)
                if row is None:
                    continue
                batch.append(row)
                if len(batch) >= BATCH_ROWS:
                    _insert(connection, batch)
                    result.rows += len(batch)
                    batch = []
            if batch:
                _insert(connection, batch)
                result.rows += len(batch)

    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        logger.error("%s: build failed -- %s", TARGET, result.error)
        return result
    finally:
        result.seconds = time.monotonic() - started

    logger.info(
        "%s: %d rows (%d in OAR scope) in %.1fs; %d without MARA, %d without a "
        "description, %d skipped for no key",
        TARGET,
        result.rows,
        result.oar_rows,
        result.seconds,
        result.missing_mara,
        result.missing_maktx,
        result.skipped_no_key,
    )
    return result


def _dimension_row(
    marc: dict,
    mara: dict[str, dict],
    makt: dict[str, dict],
    run_date: date,
    result: BuildResult,
) -> dict | None:
    """One raw MARC row, normalised and widened. None if it has no key."""
    padded = matnr.pad(marc.get("Matnr"))
    werks = coerce.text(marc.get("Werks"))
    if padded is None or werks is None:
        # Without both halves of the key the row cannot be addressed, joined
        # to, or corrected later. Counted rather than silently dropped.
        result.skipped_no_key += 1
        return None

    master = mara.get(padded)
    description = makt.get(padded)
    if master is None:
        result.missing_mara += 1
    if description is None:
        result.missing_maktx += 1

    dismm = coerce.text(marc.get("Dismm"))
    is_oar = dismm in OAR_MRP_TYPES
    if is_oar:
        result.oar_rows += 1

    return {
        "matnr": padded,
        "werks": werks,
        "mtart": coerce.text((master or {}).get("Mtart")),
        "matkl": coerce.text((master or {}).get("Matkl")),
        "meins": coerce.text((master or {}).get("Meins")),
        "maktx": coerce.text((description or {}).get("Maktx")),
        "dismm": dismm,
        "is_oar": is_oar,
        "eisbe": coerce.number(marc.get("Eisbe")),
        "minbe": coerce.number(marc.get("Minbe")),
        "mabst": coerce.number(marc.get("Mabst")),
        "losgr": coerce.number(marc.get("Losgr")),
        "plifz": coerce.integer(marc.get("Plifz")),
        "source_run_date": run_date,
    }


def _insert(connection, batch: list[dict]) -> None:
    connection.execute(MaterialPlant.__table__.insert(), batch)
