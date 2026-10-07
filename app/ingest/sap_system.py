"""Which SAP system the database's data came from, and the guard that keeps it one.

    python -m app.ingest --sap-system                 # configured vs recorded
    python -m app.ingest --adopt-sap-system           # declare the DB as this system's

Switching ``CPI_PATH`` from DEV to QA switches every call, which is the point.
It does not switch the data already loaded: ``raw_ekpo`` would still hold DEV's
purchase orders, the watermarks would still be DEV's days, and the next QA
delta would merge QA rows into DEV tables -- keyed on purchase-order numbers
the two systems both use for different documents. Nothing would fail; every
number on every page would be a blend of two systems.

So the database records the ``CPI_PATH`` its SAP data was loaded from, the
first time anything loads, and every load from SAP -- a CSV extract, an OData
fetch, a delta merge -- is refused while the configured path differs. The
record is a reserved row in ``ingest_watermark`` (``__sap_system__``), so it
needs no migration and goes with the data: a wipe that deletes every row
deletes it too, and the first load after the wipe records the new system.

To switch: wipe the database and reload from the new system (the clean way),
or, if the database really should be treated as the new system's -- it was
just reloaded, say, by a route that does not record -- adopt it explicitly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import get_settings, normalise_cpi_path
from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.models.ingest_watermark import IngestWatermark

logger = get_logger(__name__)

# The reserved key. Not an entity set name, so no watermark lookup can hit it.
RECORD = "__sap_system__"
RECORD_FIELD = "cpi_path"


class SapSystemMismatch(RuntimeError):
    """The database holds another SAP system's data."""


def configured() -> str:
    return get_settings().cpi_path


def recorded() -> str | None:
    """The CPI_PATH the database's SAP data was loaded from, or None."""
    with get_sessionmaker()() as session:
        row = session.get(IngestWatermark, RECORD)
        return normalise_cpi_path(row.value) if row is not None else None


def verdict(recorded_path: str | None, configured_path: str) -> str | None:
    """Why a load must be refused, or None. Pure, for the tests."""
    if recorded_path is None or recorded_path == configured_path:
        return None
    return (
        f"the database holds SAP data loaded from CPI_PATH={recorded_path}, and this "
        f"process is configured for CPI_PATH={configured_path}. Loading on top would "
        "mix two SAP systems in the same tables, under the same keys. To switch, wipe "
        "the database and reload from the new system; or, if the database already "
        "holds only the new system's data, record that: python -m app.ingest "
        "--adopt-sap-system"
    )


def _write(path: str) -> None:
    with get_sessionmaker()() as session:
        row = session.get(IngestWatermark, RECORD)
        if row is None:
            row = IngestWatermark(entity_set=RECORD, field=RECORD_FIELD, value=path)
            session.add(row)
        row.field = RECORD_FIELD
        row.value = path
        row.rows_last_run = 0
        row.updated_at = datetime.now(timezone.utc)
        session.commit()


def ensure() -> None:
    """Refuse a load from SAP into another system's database; claim an
    unclaimed one. Called before every load that writes SAP data."""
    wanted = configured()
    found = recorded()
    problem = verdict(found, wanted)
    if problem:
        raise SapSystemMismatch(problem)
    if found is None:
        _write(wanted)
        logger.info("database recorded as holding SAP data from CPI_PATH=%s", wanted)


def adopt() -> tuple[str | None, str]:
    """Record the configured system as the database's. Returns (was, now)."""
    was, now = recorded(), configured()
    _write(now)
    logger.warning("database re-recorded as CPI_PATH=%s (was %s)", now, was or "unrecorded")
    return was, now
