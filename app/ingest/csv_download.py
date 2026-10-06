"""Copy landed CSV extracts out of storage onto local disk, for a person to open.

The landing area (ADLS, behind a private endpoint) is not reachable from a
laptop, so a file has to be copied somewhere a person can fetch it. On the App
Service that is /home: the only path that survives a restart, and one Kudu
serves as a zip --

    https://<app>.scm.<region>.azurewebsites.net/api/zip/csv_downloads/

WHICH FILE PER TABLE

The newest extract request for the table that has a file, taken from
``csv_extract_request`` -- so the file is named for the request that asked for
it and the manifest can say whether that delivery reconciled. A table with no
tracked request (a chunk that arrived with nothing open, or a database that is
not reachable) falls back to the newest file under ``csv/<TABLE>/`` in storage,
which is where csv_upload lands every recognised table.

Read-only against storage and the database. Nothing here moves, renames or
deletes a landed file.
"""

from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.core.storage import Storage, get_storage
from app.models.csv_extract import STATUS_COMPLETE, CsvExtractRequest

logger = get_logger(__name__)

CSV_PREFIX = "csv"
COPY_CHUNK = 1024 * 1024

# /home persists on App Service and is what Kudu zips; anywhere else (a
# developer machine) the copy goes beside the working directory.
APP_SERVICE_HOME = Path("/home")
DEFAULT_FOLDER = "csv_downloads"

MANIFEST_FILE = "_download_manifest.csv"


@dataclass
class Downloaded:
    sap_table: str
    source_key: str | None
    local_path: str | None = None
    request_id: str | None = None
    status: str | None = None
    received_rows: int | None = None
    expected_rows: int | None = None
    bytes: int = 0
    sha256: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.local_path is not None


def default_target(now: datetime | None = None) -> Path:
    """``/home/csv_downloads/<UTC stamp>`` on App Service, else ``./csv_downloads/<stamp>``."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M%S")
    base = APP_SERVICE_HOME if APP_SERVICE_HOME.is_dir() and os.access(APP_SERVICE_HOME, os.W_OK) else Path.cwd()
    return base / DEFAULT_FOLDER / stamp


def _newest_request(sap_table: str, request_id: str | None) -> CsvExtractRequest | None:
    """The request whose file to copy: the one named, else the newest that has
    a file -- a reconciled one in preference to one that is not."""
    sessionmaker = get_sessionmaker()
    with sessionmaker() as session:
        if request_id:
            return session.get(CsvExtractRequest, request_id)
        query = (
            select(CsvExtractRequest)
            .where(
                CsvExtractRequest.sap_table == sap_table,
                CsvExtractRequest.data_key.is_not(None),
            )
            .order_by(CsvExtractRequest.fired_at.desc())
        )
        rows = list(session.scalars(query.limit(20)))
        complete = [r for r in rows if r.status == STATUS_COMPLETE]
        return (complete or rows or [None])[0]


def _newest_landed_key(storage: Storage, sap_table: str) -> str | None:
    """The most recently modified ``<TABLE>.csv`` under ``csv/<TABLE>/``."""
    wanted = f"/{sap_table}.csv"
    # No trailing slash: the ADLS adapter refuses an empty path segment, and a
    # directory path already lists only what is inside it.
    candidates = [k for k in storage.list(f"{CSV_PREFIX}/{sap_table}") if k.endswith(wanted)]
    if not candidates:
        return None

    def modified(key: str) -> datetime:
        stamp = storage.stat(key).modified
        return stamp or datetime.min.replace(tzinfo=timezone.utc)

    return max(candidates, key=modified)


def _copy(storage: Storage, key: str, destination: Path) -> tuple[int, str]:
    """Stream ``key`` to ``destination``. Returns (bytes, sha256)."""
    digest = hashlib.sha256()
    written = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with storage.open_read(key) as source, open(destination, "wb") as sink:
        while chunk := source.read(COPY_CHUNK):
            sink.write(chunk)
            digest.update(chunk)
            written += len(chunk)
    return written, digest.hexdigest()


def download_table(
    sap_table: str,
    target: Path,
    *,
    request_id: str | None = None,
    storage: Storage | None = None,
) -> Downloaded:
    """Copy one table's newest landed extract into ``target``. Never raises."""
    sap_table = sap_table.strip().upper()
    storage = storage or get_storage()
    result = Downloaded(sap_table=sap_table, source_key=None)

    try:
        record = _newest_request(sap_table, request_id)
    except Exception as exc:
        # A database that cannot be reached is not a reason to refuse the copy.
        logger.warning("%s: request lookup failed (%s); falling back to storage", sap_table, exc)
        record = None

    if request_id and record is None:
        result.error = f"request {request_id} is not in csv_extract_request"
        return result

    if record is not None and record.data_key:
        result.source_key = record.data_key
        result.request_id = record.request_id
        result.status = record.status
        result.received_rows = record.received_rows
        result.expected_rows = record.expected_rows
    else:
        try:
            result.source_key = _newest_landed_key(storage, sap_table)
        except Exception as exc:
            result.error = f"could not list {CSV_PREFIX}/{sap_table}/ in storage: {exc}"
            return result
        if result.source_key is None:
            result.error = f"nothing landed under {CSV_PREFIX}/{sap_table}/"
            return result
        # csv/<TABLE>/<request id | unattributed-date>/<TABLE>.csv
        result.request_id = result.source_key.split("/")[-2]

    destination = target / f"{sap_table}_{result.request_id}.csv"
    try:
        result.bytes, result.sha256 = _copy(storage, result.source_key, destination)
    except Exception as exc:
        result.error = f"could not copy {result.source_key}: {exc}"
        return result
    result.local_path = str(destination)
    logger.info("%s: %s -> %s (%d bytes)", sap_table, result.source_key, destination, result.bytes)
    return result


def download(
    tables: list[str],
    target: Path | None = None,
    *,
    request_id: str | None = None,
    storage: Storage | None = None,
) -> tuple[Path, list[Downloaded]]:
    """Copy each table's newest extract into one folder, plus a manifest."""
    target = target or default_target()
    target.mkdir(parents=True, exist_ok=True)
    storage = storage or get_storage()
    results = [
        download_table(name, target, request_id=request_id, storage=storage) for name in tables
    ]
    _write_manifest(target / MANIFEST_FILE, results)
    return target, results


def _write_manifest(path: Path, results: list[Downloaded]) -> None:
    """What was copied, from where, and whether its delivery reconciled.

    The sha256 is of the bytes as stored, so a copy can be matched to the
    landed object later without trusting a file name.
    """
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "sap_table", "request_id", "status", "received_rows", "expected_rows",
            "bytes", "sha256", "source_key", "local_file", "error",
        ])
        for r in results:
            writer.writerow([
                r.sap_table, r.request_id or "", r.status or "",
                "" if r.received_rows is None else r.received_rows,
                "" if r.expected_rows is None else r.expected_rows,
                r.bytes, r.sha256 or "", r.source_key or "",
                Path(r.local_path).name if r.local_path else "", r.error or "",
            ])
