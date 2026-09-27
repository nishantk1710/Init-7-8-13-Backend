"""Workbook fallback for the tables the live SAP routes cannot fill. TEMPORARY.

    python -m app.ingest.fallback --list
    python -m app.ingest.fallback --upload "…/EKKO.XLSX" "…/EKET.XLSX" …
    python -m app.ingest.fallback --load                  # all five tables
    python -m app.ingest.fallback --load ekko eket --try-sap
    python -m app.ingest.fallback --load ekko --force     # overwrite live data

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
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
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
        size = 0
        with local.open("rb") as source, storage.open_write(key) as sink:
            while chunk := source.read(4 * 1024 * 1024):
                sink.write(chunk)
                size += len(chunk)
        logger.info("uploaded %s -> %s (%d bytes)", local.name, key, size)
        landed.append((str(local), key, size))
    return landed


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
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--list", action="store_true",
                      help="show the five tables, their workbook keys, whether each is in storage, and whether live data would block it")
    what.add_argument("--upload", nargs="+", metavar="FILE",
                      help="copy local workbook(s) to their manifest keys in storage, matched by file name")
    what.add_argument("--load", nargs="*", metavar="TABLE",
                      help="load from the workbooks: all five, or the ones named")
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
