"""Workbook fallback for the tables the live SAP routes cannot fill. TEMPORARY.

    python -m app.ingest.fallback --list
    python -m app.ingest.fallback --upload "…/EKKO.XLSX" "…/EKET.XLSX" …
    python -m app.ingest.fallback --upload                # from FALLBACK_SOURCE_DIR
    python -m app.ingest.fallback --load                  # all five tables
    python -m app.ingest.fallback --load ekko eket --try-sap
    python -m app.ingest.fallback --load ekko --force     # overwrite live data
    python -m app.ingest.fallback --sync                  # folder -> storage -> SQL
    python -m app.ingest.fallback --sync zmm065_gb zmm065_bmm

WHAT IT IS FOR

Five raw tables have no working live source today:

    ekko, eket     SAP acknowledges the CSV extract and never delivers it
                   (60 requests, every shape, 25-26 Sep 2026 -- see
                   known_conditions.CSV_UNDELIVERED_TABLES); the OData sets
                   behind EKET answer HTTP 500 to every row read.
    zmm065_gb,     Reports, not SAP tables. No CSV route and no OData set;
    zmm065_bmm,    the July workbooks Rohit shared are the only source.
    gr_30day

The initiatives read all five (through the ``n_<table>`` normalise views for
the SAP ones, directly for ZMM065), so until SAP fixes its side the July
workbooks are what stands in. This command loads exactly those five, from the
workbooks, and nothing else.

WHAT IT MUST NEVER DO

Overwrite live data. ``raw_ekko`` is the table the CSV route will fill the
day SAP's job works, and a workbook load into it is a step backwards. So
before touching a table this checks ``csv_extract_request``: if a completed
CSV extract for that table has been loaded, the workbook is refused and the
reason printed. ``--force`` overrides it, on purpose, once.

Nothing in the live routes is changed by this module. ``--csv-pull`` still
leaves EKKO and EKET out with the recorded reason; ``--try-sap`` here is the
one place that asks SAP for them again, and it falls back to the workbook
only when SAP delivers nothing or delivers short.

HOW THE LOAD WORKS

The seed loader (``app.seed``) already reads these workbooks -- sheet, header
row and column naming are in its manifest -- and its ``load_table`` rebuilds
the ``n_<table>`` view afterwards, so the initiatives see the workbook labels
whether or not the table was in SAP field names a minute ago. This module
adds only the guard, the upload, and the SAP-first attempt.

The workbooks live in the landing container under the keys the seed manifest
names (``KPI 02 Data Extract/Tables/EKKO.XLSX``, ``Resources Shared -
Rohit/…``). ``--upload`` puts local copies there through the storage port,
matched by file name; it needs STORAGE_URL and an Azure credential (``az
login``) on the machine running it, which the App Service has and a laptop
may not -- the Portal's upload to the same path is the same thing.

THE SOURCE FOLDER, AND --sync

``--upload`` with no files scans ``FALLBACK_SOURCE_DIR`` (default
``data/fallback`` under the backend root, gitignored -- it holds real SAP
extracts) for the five workbooks, by the same case-insensitive name match, and
reports each as uploaded, missing, or refused. A file there that is not one of
the five is named and skipped, never uploaded. ``data/fallback/README.md``
lists the names.

``--sync [TABLE…]`` is that upload followed by ``--load`` for the tables whose
workbook was found and uploaded -- every upload first, then every load. The
guard is ``load()``'s own, unchanged: a table holding live CSV data is refused
without ``--force``, and ``--try-sap`` still asks SAP first for ekko/eket. A
table whose workbook is absent is skipped and reported, not failed; one whose
upload failed is not loaded, since storage may hold an older copy that would
then be loaded as if current.

WHERE IT RUNS

Uploading writes to storage through ``app.core.storage.get_storage()``, the
same port as everything else -- no credential handling here. On a laptop that
means STORAGE_URL set and an ``az login`` session; on the App Service, its
managed identity. Either needs 'Storage Blob Data Contributor'. The load half
needs DATABASE_URL, which on Azure SQL means running inside the VNet (the App
Service SSH console), so ``--sync`` end to end is an App Service command.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from datetime import datetime, timedelta

from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.db import get_engine, get_sessionmaker
from app.ingest.watermarks import set_watermark
from app.seed.sqlserver import quote
from app.core.logging import configure_logging, get_logger
from app.core.storage import Storage, StorageNotConfiguredError, get_storage
from app.models.csv_extract import STATUS_COMPLETE, CsvExtractRequest
from app.seed import loader as seed_loader
from app.seed.manifest import ExtractSpec, spec_for

logger = get_logger(__name__)

# The whole scope of this command. Anything else is a live table with a
# working route, and loading a July workbook over it would be a regression.
FALLBACK_TABLES: tuple[str, ...] = ("ekko", "eket", "zmm065_gb", "zmm065_bmm", "gr_30day")

# The two that HAVE a CSV route, in case SAP fixes it: --try-sap fires it
# first and only falls back when nothing usable arrives.
SAP_BACKED: dict[str, str] = {"ekko": "EKKO", "eket": "EKET"}

# The window to ask SAP for when trying first: everything, as the sweep does.
TRY_SAP_FROM = "19000101"
TRY_SAP_FORWARD_DAYS = 365

SOURCE_SAP = "sap"
SOURCE_WORKBOOK = "workbook"
STATUS_REFUSED = "refused"

# A workbook load is a baseline like a CSV load: the OData delta merges into
# the table it filled, so its newest date is where the delta starts. Read
# through the normalise view, which has already turned the workbook's dates
# into ISO whatever the sheet held.
#   seed table -> (entity set, delta field, view, view column)
WATERMARK_FROM_VIEW: dict[str, tuple[str, str, str, str]] = {
    "ekko": ("PurchaseOrderSet", "Aedat", "n_ekko", "created_on"),
}


@dataclass
class Outcome:
    table: str
    source: str | None
    status: str
    rows: int = 0
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (seed_loader.STATUS_SUCCEEDED, seed_loader.STATUS_SKIPPED)


def specs(tables: list[str] | None = None) -> list[ExtractSpec]:
    """The manifest entries for the requested tables, refusing any outside scope."""
    names = [t.lower() for t in (tables or FALLBACK_TABLES)]
    outside = sorted(set(names) - set(FALLBACK_TABLES))
    if outside:
        raise ValueError(
            f"{', '.join(outside)}: not a fallback table. This command loads only "
            f"{', '.join(FALLBACK_TABLES)} -- the tables with no working live "
            "source. Everything else comes from the CSV route."
        )
    return [spec_for(name) for name in names]


# --- The guard --------------------------------------------------------------


def live_data_loaded(sap_table: str) -> CsvExtractRequest | None:
    """The completed, loaded CSV extract for this table, if there is one.

    That is the one state in which a workbook load would overwrite live data.
    An open, timed-out or failed request is not: nothing from it reached the
    table.
    """
    try:
        sessionmaker = get_sessionmaker()
    except Exception:
        return None
    with sessionmaker() as session:
        return session.scalars(
            select(CsvExtractRequest)
            .where(
                CsvExtractRequest.sap_table == sap_table.upper(),
                CsvExtractRequest.status == STATUS_COMPLETE,
                CsvExtractRequest.loaded_at.is_not(None),
            )
            .order_by(CsvExtractRequest.loaded_at.desc())
        ).first()


def refusal(spec: ExtractSpec, *, force: bool) -> str | None:
    """Why this table must not be loaded from the workbook right now, or None."""
    if force:
        return None
    live = live_data_loaded(spec.sap_table)
    if live is None:
        return None
    return (
        f"{spec.raw_table} holds live SAP data: request {live.request_id} "
        f"delivered {live.received_rows:,} row(s) and was loaded "
        f"{live.loaded_at:%Y-%m-%d %H:%M}Z. A July workbook over that is a step "
        "backwards. Pass --force if that is really what you want."
    )


# --- Upload -----------------------------------------------------------------


def _copy(local: Path, key: str, storage: Storage) -> int:
    """Stream one local file to one storage key. Returns bytes written."""
    size = 0
    with local.open("rb") as source, storage.open_write(key) as sink:
        while chunk := source.read(4 * 1024 * 1024):
            sink.write(chunk)
            size += len(chunk)
    logger.info("uploaded %s -> %s (%d bytes)", local.name, key, size)
    return size


def upload(paths: list[str], storage: Storage | None = None) -> list[tuple[str, str, int]]:
    """Copy local workbooks to the storage keys the manifest expects.

    Matched by file name, case-insensitively, against the five tables' manifest
    entries -- so a file that is not one of theirs is refused rather than
    landed somewhere nothing reads. Returns ``(local, key, bytes)`` per file.
    """
    by_name = {
        Path(key).name.lower(): key
        for spec in specs()
        for key in spec.files
    }
    landed: list[tuple[str, str, int]] = []
    for raw in paths:
        local = Path(raw)
        if not local.is_file():
            raise FileNotFoundError(f"{local}: no such file")
        key = by_name.get(local.name.lower())
        if key is None:
            raise ValueError(
                f"{local.name}: not one of the fallback workbooks. Expected one of: "
                + ", ".join(sorted(Path(k).name for k in by_name.values()))
            )
        storage = storage or get_storage()
        size = _copy(local, key, storage)
        landed.append((str(local), key, size))
    return landed


# --- The source folder ------------------------------------------------------

FOUND = "found"
UPLOADED = "uploaded"
MISSING = "missing"
REFUSED = "refused"
ERROR = "error"

# Files that live in the source folder on purpose and are not workbooks: the
# committed list of expected names. Skipped silently, not reported as refused,
# or every scan would name it.
FOLDER_FURNITURE = frozenset({"readme.md"})


@dataclass
class FolderFile:
    """One expected workbook, or one unexpected file, and what became of it."""

    name: str
    """The expected workbook name (from the manifest), or the unexpected file's."""
    status: str
    table: str | None = None
    key: str | None = None
    local: Path | None = None
    bytes: int = 0
    detail: str | None = None


def _expected(tables: list[str] | None) -> dict[str, tuple[str, str]]:
    """``file name (lower) -> (table, storage key)`` for the requested tables."""
    return {
        Path(key).name.lower(): (spec.table, key)
        for spec in specs(tables)
        for key in spec.files
    }


def scan(folder: Path, tables: list[str] | None = None) -> list[FolderFile]:
    """Classify the folder against the workbooks the requested tables expect.

    One entry per expected workbook -- ``found`` (with ``local`` set) or
    ``missing`` -- and one ``refused`` entry per file that is not one of them. Matching is by file
    name, case-insensitive, exactly as :func:`upload`. Reads no file contents.

    A fallback workbook that belongs to a table *not* requested is left alone,
    neither uploaded nor refused: it is the right file, just not asked for.
    """
    expected = _expected(tables)
    every = _expected(None)
    by_key: dict[str, list[Path]] = {}
    refused: list[FolderFile] = []

    candidates = sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []
    for path in candidates:
        lowered = path.name.lower()
        if lowered in FOLDER_FURNITURE or path.name.startswith("."):
            continue
        if lowered in expected:
            by_key.setdefault(expected[lowered][1], []).append(path)
        elif lowered not in every:
            refused.append(
                FolderFile(
                    path.name, REFUSED, local=path,
                    detail="not one of the fallback workbooks; skipped, never uploaded",
                )
            )

    found: list[FolderFile] = []
    for table, key in expected.values():
        name = Path(key).name
        matches = by_key.get(key, [])
        if len(matches) > 1:
            # Only possible on a case-sensitive filesystem. Which copy is
            # current is not something to guess.
            found.append(
                FolderFile(
                    name, REFUSED, table=table, key=key,
                    detail="more than one file matches: "
                    + ", ".join(p.name for p in matches),
                )
            )
        elif matches:
            found.append(FolderFile(name, FOUND, table=table, key=key, local=matches[0]))
        else:
            found.append(
                FolderFile(name, MISSING, table=table, key=key, detail="not in the source folder")
            )
    return found + refused


def upload_folder(
    folder: Path | None = None,
    storage: Storage | None = None,
    tables: list[str] | None = None,
) -> list[FolderFile]:
    """Upload every expected workbook found in ``folder`` to its manifest key.

    ``folder`` defaults to ``FALLBACK_SOURCE_DIR``. Never raises per file: a
    workbook that is absent is reported ``missing``, a file that is not one of
    the five is reported ``refused``, and a failed upload ``error`` -- the rest
    still upload. Storage is only opened if there is something to upload.
    """
    folder = folder or get_settings().fallback_source_path
    entries = scan(folder, tables)
    for entry in entries:
        if entry.status != FOUND or entry.local is None or entry.key is None:
            continue
        try:
            storage = storage or get_storage()
            entry.bytes = _copy(entry.local, entry.key, storage)
            entry.status = UPLOADED
        except Exception as exc:  # one file's failure must not stop the others
            entry.status = ERROR
            entry.detail = f"{type(exc).__name__}: {exc}"
            logger.error("upload of %s failed: %s", entry.name, entry.detail)
    return entries


# --- Sync: folder -> storage -> Azure SQL -----------------------------------


@dataclass
class SyncRow:
    """One table's line in the --sync summary."""

    table: str
    workbook_found: bool
    uploaded: str
    """``yes``, ``skipped`` (no workbook in the folder) or ``error``."""
    load_status: str | None = None
    rows: int = 0
    reason: str | None = None

    @property
    def ok(self) -> bool:
        if self.uploaded == ERROR:
            return False
        if self.load_status is None:
            # Nothing was loaded because nothing was found: reported, not failed.
            return not self.workbook_found
        return self.load_status in (seed_loader.STATUS_SUCCEEDED, seed_loader.STATUS_SKIPPED)


def sync(
    tables: list[str] | None = None,
    *,
    force: bool = False,
    try_sap: bool = False,
    folder: Path | None = None,
    storage: Storage | None = None,
) -> tuple[list[SyncRow], list[FolderFile]]:
    """Upload what the source folder holds, then load those tables.

    Every upload finishes before the first load starts, and only tables whose
    workbook was found *and* uploaded are loaded: loading after a failed upload
    would read whatever older copy storage holds and report it as current.
    :func:`load` does the loading, so the live-data guard, ``--force`` and
    ``--try-sap`` behave exactly as they do for ``--load``.

    Returns the per-table summary and the refused files, if any.
    """
    requested = [spec.table for spec in specs(tables)]
    entries = upload_folder(folder, storage, requested)
    per_table = {e.table: e for e in entries if e.table is not None}
    refused_files = [e for e in entries if e.table is None]

    to_load = [t for t in requested if per_table[t].status == UPLOADED]
    outcomes = {o.table: o for o in load(to_load, force=force, try_sap=try_sap)} if to_load else {}

    summary: list[SyncRow] = []
    for table in requested:
        entry = per_table[table]
        found = entry.local is not None or entry.status == REFUSED
        if entry.status == UPLOADED:
            outcome = outcomes[table]
            summary.append(
                SyncRow(table, True, "yes", outcome.status, outcome.rows, outcome.detail)
            )
        elif entry.status in (ERROR, REFUSED):
            summary.append(SyncRow(table, found, ERROR, None, 0, entry.detail))
        else:
            summary.append(SyncRow(table, False, "skipped", None, 0, entry.detail))
    return summary, refused_files


# --- Watermark --------------------------------------------------------------


def _newest_in_view(view: str, column: str) -> str | None:
    with get_engine().connect() as connection:
        value = connection.execute(text(f"SELECT MAX({quote(column)}) FROM {quote(view)}")).scalar()
    return str(value).strip() if value else None


def watermark_from(newest_iso: str | None) -> str | None:
    """The delta's starting mark for a table whose newest date is ``newest_iso``.

    One day back, in the shape the OData delta writes, for the same reason the
    CSV loader does it: SAP's dates are local midnights, and ``ge`` from the
    previous day re-reads at most one day the merge absorbs.
    """
    if not newest_iso:
        return None
    try:
        newest = datetime.strptime(newest_iso[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return f"{newest - timedelta(days=1):%Y-%m-%d %H:%M:%S}"


def seed_watermark(table: str) -> str | None:
    """After a workbook load of ``table``: set its set's delta mark. Never raises."""
    entry = WATERMARK_FROM_VIEW.get(table)
    if entry is None:
        return None
    entity_set, field, view, column = entry
    try:
        mark = watermark_from(_newest_in_view(view, column))
        if mark is None:
            logger.warning("%s: no usable date in %s.%s; delta mark not seeded", table, view, column)
            return None
        set_watermark(entity_set, field, mark, 0)
        logger.info("%s: delta mark seeded at %s.%s = %s", table, entity_set, field, mark)
        return mark
    except Exception:
        logger.exception("%s: could not seed the delta mark", table)
        return None


# --- Load -------------------------------------------------------------------


def _try_sap(sap_table: str) -> Outcome | None:
    """Ask SAP for the table over the CSV route. An Outcome if it delivered
    and loaded, None if the workbook should stand in after all."""
    from app.ingest import csv_load, csv_pull

    to_date = (date.today() + timedelta(days=TRY_SAP_FORWARD_DAYS)).strftime("%Y%m%d")
    logger.info("%s: trying SAP first over the CSV route", sap_table)
    pulled = csv_pull.pull_one(sap_table, from_date=TRY_SAP_FROM, to_date=to_date)
    if not pulled.ok:
        logger.warning(
            "%s: SAP did not deliver (%s%s); falling back to the workbook",
            sap_table, pulled.status, f": {pulled.error}" if pulled.error else "",
        )
        return None
    loaded = csv_load.load_table(sap_table)
    if not loaded.ok:
        logger.warning(
            "%s: SAP delivered but the load failed (%s); falling back to the workbook",
            sap_table, loaded.error,
        )
        return None
    return Outcome(sap_table.lower(), SOURCE_SAP, loaded.status, rows=loaded.rows)


def load(
    tables: list[str] | None = None,
    *,
    force: bool = False,
    try_sap: bool = False,
) -> list[Outcome]:
    """Load the fallback tables from their workbooks, guarded. Never raises
    per table -- one failure does not stop the rest."""
    outcomes: list[Outcome] = []
    for spec in specs(tables):
        if try_sap and spec.table in SAP_BACKED:
            from_sap = _try_sap(SAP_BACKED[spec.table])
            if from_sap is not None:
                outcomes.append(from_sap)
                continue

        why = refusal(spec, force=force)
        if why:
            logger.error("%s: refused -- %s", spec.table, why)
            outcomes.append(Outcome(spec.table, None, STATUS_REFUSED, detail=why))
            continue

        # force=True to the seed loader: its "unchanged since last load" skip
        # compares workbook fingerprints, and a table the CSV route or a wipe
        # has since replaced would be skipped as up to date while holding
        # nothing of the sort. This command exists to be run on purpose.
        result = seed_loader.load_table(spec, force=True)
        if result.status == seed_loader.STATUS_SUCCEEDED:
            seed_watermark(spec.table)
        outcomes.append(
            Outcome(spec.table, SOURCE_WORKBOOK, result.status, rows=result.rows, detail=result.error)
        )
    return outcomes


# --- CLI --------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest.fallback",
        description=(
            "TEMPORARY: load the five tables the live SAP routes cannot fill "
            "(ekko, eket, zmm065_gb, zmm065_bmm, gr_30day) from the July workbooks. "
            "Refuses to overwrite a table the CSV route has delivered."
        ),
        epilog=(
            "--upload and --sync write to storage through STORAGE_URL. On a laptop that\n"
            "means STORAGE_URL set and an `az login` session; on the App Service, its\n"
            "managed identity. Either needs 'Storage Blob Data Contributor'."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--list", action="store_true",
                      help="show the five tables, their workbook keys, whether each is in storage, and whether live data would block it")
    what.add_argument("--upload", nargs="*", metavar="FILE",
                      help="copy local workbook(s) to their manifest keys in storage, matched by file name; "
                           "with no FILE, upload whichever of the five are in FALLBACK_SOURCE_DIR")
    what.add_argument("--load", nargs="*", metavar="TABLE",
                      help="load from the workbooks: all five, or the ones named")
    what.add_argument("--sync", nargs="*", metavar="TABLE",
                      help="upload from FALLBACK_SOURCE_DIR, then --load the tables whose workbook was "
                           "found and uploaded: all five, or the ones named")
    parser.add_argument("--try-sap", action="store_true",
                        help="for ekko/eket, fire the CSV extract first and use the workbook only if SAP delivers nothing")
    parser.add_argument("--force", action="store_true",
                        help="load the workbook even over live SAP data (refused otherwise)")
    return parser


def _list() -> int:
    try:
        storage = get_storage()
        available = set(storage.list())
    except StorageNotConfiguredError as exc:
        storage, available = None, set()
        print(f"STORAGE_URL is not set, so files cannot be checked.\n  {exc}\n")
    except Exception as exc:
        storage, available = None, set()
        print(f"Could not list storage: {type(exc).__name__}: {exc}\n")

    print(f"{'TABLE':<12}{'SOURCE':<10}{'WORKBOOK KEY':<52}{'IN STORAGE':<12}LIVE DATA")
    for spec in specs():
        for key in spec.files:
            present = "yes" if key in available else ("?" if storage is None else "MISSING")
            live = live_data_loaded(spec.sap_table) if get_settings().database_url else None
            blocked = f"yes -- {live.request_id}" if live else "no"
            source = "SAP (CSV)" if spec.table in SAP_BACKED else "report"
            print(f"{spec.table:<12}{source:<10}{key:<52}{present:<12}{blocked}")
    print("\nLIVE DATA = a completed CSV extract has been loaded into the table; "
          "--load refuses it without --force.")
    return 0


def _print_folder(entries: list[FolderFile], folder: Path) -> None:
    print(f"Source folder: {folder}")
    print(f"{'FILE':<36}{'STATUS':<10}{'BYTES':>12}  STORAGE KEY / DETAIL")
    for e in entries:
        size = f"{e.bytes:,}" if e.status == UPLOADED else "-"
        print(f"{e.name:<36}{e.status:<10}{size:>12}  {e.key or ''}")
        if e.detail:
            print(f"{'':<36}    {e.detail}")


def _upload_folder() -> int:
    folder = get_settings().fallback_source_path
    entries = upload_folder(folder)
    _print_folder(entries, folder)
    # An unexpected file is reported and skipped, not a failure. A workbook
    # that could not be uploaded (or matched twice) is.
    return 1 if any(e.status == ERROR or (e.status == REFUSED and e.table) for e in entries) else 0


def _sync(tables: list[str] | None, *, force: bool, try_sap: bool) -> int:
    folder = get_settings().fallback_source_path
    try:
        summary, refused_files = sync(tables, force=force, try_sap=try_sap, folder=folder)
    except ValueError as exc:
        print(exc)
        return 2

    print(f"Source folder: {folder}")
    for f in refused_files:
        print(f"  refused: {f.name} -- {f.detail}")
    print(f"\n{'TABLE':<12}{'WORKBOOK':<10}{'UPLOADED':<10}{'LOAD':<11}{'ROWS':>10}")
    for row in summary:
        print(
            f"{row.table:<12}{'yes' if row.workbook_found else 'no':<10}{row.uploaded:<10}"
            f"{row.load_status or '-':<11}{row.rows:>10,}"
        )
        if row.reason:
            print(f"    {row.reason}")
    return 0 if all(row.ok for row in summary) else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(get_settings())

    if args.list:
        return _list()

    if args.upload:
        try:
            for local, key, size in upload(args.upload):
                print(f"  {size:>12,}  {Path(local).name}  ->  {key}")
        except (FileNotFoundError, ValueError, StorageNotConfiguredError) as exc:
            print(exc)
            return 2
        return 0

    if args.upload is not None:  # --upload with no files: scan the source folder
        return _upload_folder()

    if args.sync is not None:
        return _sync(args.sync or None, force=args.force, try_sap=args.try_sap)

    try:
        outcomes = load(args.load or None, force=args.force, try_sap=args.try_sap)
    except ValueError as exc:
        print(exc)
        return 2

    print(f"\n{'TABLE':<12}{'SOURCE':<10}{'STATUS':<11}{'ROWS':>10}")
    failures = 0
    for o in outcomes:
        print(f"{o.table:<12}{o.source or '-':<10}{o.status:<11}{o.rows:>10,}")
        if o.detail:
            print(f"    {o.detail}")
        if not o.ok:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
