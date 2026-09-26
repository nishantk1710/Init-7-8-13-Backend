"""Command line entry point for live SAP ingestion.

    python -m app.ingest --list                  what would run, and what landed
    python -m app.ingest --fetch --all           CPI  -> storage
    python -m app.ingest --load  --all           storage -> Azure SQL
    python -m app.ingest --fetch --load --all    both, in order
    python -m app.ingest --fetch --set MaterialPlantSet

Fetch and load are separate flags rather than one command because they fail for
unrelated reasons and recover differently. A load that fails can be rerun
against bytes already on disk; re-fetching to fix a driver problem would ask
SAP for a hundred thousand rows it already gave us.
"""

from __future__ import annotations

import argparse
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.storage import StorageNotConfiguredError, get_storage
from app.ingest.fetch import MODE_DELTA, MODE_FULL, fetch_set, read_manifest
from app.ingest.load import STATUS_FAILED, latest_prefixes, load_set
from app.ingest.manifest import (
    IngestSpec,
    check_delta_filters,
    spec_for,
    specs,
)
from app.ingest.sweep import run_delta_sweep
from app.integrations.sap.client import SapClient

logger = get_logger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest",
        description="Pull entity sets from live SAP through CPI and load them.",
    )
    parser.add_argument("--fetch", action="store_true", help="pull from CPI into storage")
    parser.add_argument("--load", action="store_true", help="load landed files into Azure SQL")
    parser.add_argument(
        "--list",
        action="store_true",
        help="show every entity set and what has landed; touches nothing",
    )

    # --- The CSV route ----------------------------------------------------
    parser.add_argument(
        "--csv-pull",
        action="store_true",
        help=(
            "ask SAP for a full table over the CSV extract route, then wait for "
            "the chunks to land. Strictly one table at a time."
        ),
    )
    parser.add_argument(
        "--csv-load",
        action="store_true",
        help="file a reconciled CSV extract into raw_<table> in Azure SQL",
    )
    parser.add_argument(
        "--table",
        metavar="NAME",
        help="one SAP table for the CSV verbs, e.g. EKPO. Omit with --all.",
    )
    parser.add_argument(
        "--max-rows",
        metavar="N",
        default="",
        help=(
            "cap the extract, e.g. 100. Empty (the default) means NO CAP -- "
            "omit it entirely for a full pull. Use a small value to prove a "
            "table delivers before asking for all of it."
        ),
    )
    parser.add_argument(
        "--from-date",
        metavar="YYYYMMDD",
        help=(
            "override the window start, e.g. 20130101. The default is three "
            "years back for transaction tables, which is right for a live "
            "client and near-empty in this one -- 99.3%% of its purchase orders "
            "predate 2026, and the ZREP orders are from 2018. Use this to ask "
            "for the period the data actually occupies."
        ),
    )
    parser.add_argument(
        "--to-date",
        metavar="YYYYMMDD",
        help="override the window end, e.g. 20261231. Must be given with --from-date.",
    )
    parser.add_argument(
        "--years",
        type=int,
        metavar="N",
        help="widen the transaction window to N years back instead of three",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help=(
            "fire the extract(s) and return, instead of waiting for the "
            "chunks. Useful when the deliveries are being watched on the "
            "receiving side rather than here."
        ),
    )
    parser.add_argument(
        "--csv-status",
        action="store_true",
        help="show recent CSV extract requests and what landed for each",
    )
    parser.add_argument(
        "--csv-verify",
        action="store_true",
        help=(
            "walk the whole chain per table -- request, file in storage, rows "
            "in Azure SQL -- and report where each one stands"
        ),
    )
    parser.add_argument(
        "--abandon",
        action="store_true",
        help=(
            "close any open CSV request so a new one can be fired. For a "
            "delivery that died; it does not recover the rows."
        ),
    )

    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="every entity set")
    scope.add_argument("--set", metavar="NAME", help="one set, e.g. MaterialPlantSet")

    how = parser.add_mutually_exclusive_group()
    how.add_argument(
        "--full",
        action="store_true",
        help="read every row (the default)",
    )
    how.add_argument(
        "--delta",
        action="store_true",
        help=(
            "read only what changed since the stored watermark. With --all, "
            "only the sets whose delta can run (the rest belong to the CSV "
            "route); with --set, a set with no delta falls back to a full "
            "pull and says so. A set whose odata_ table does not exist yet is "
            "pulled in full once, to build the baseline."
        ),
    )
    parser.add_argument(
        "--since",
        metavar="VALUE",
        help=(
            "override the stored watermark for this run, e.g. 2026-09-01. "
            "Does not change what is stored unless the run succeeds."
        ),
    )

    parser.add_argument(
        "--allow-unstable",
        action="store_true",
        help=(
            "load a fetch whose duplicate-key check failed, or (with "
            "--csv-load) a CSV extract whose row count did not reconcile. For "
            "inspecting a bad pull deliberately -- never for getting a sweep "
            "to finish."
        ),
    )
    return parser


def _list() -> int:
    settings = get_settings()
    root = settings.ingest_prefix
    try:
        storage = get_storage()
    except StorageNotConfiguredError as exc:
        storage = None
        print(f"STORAGE_URL is not set, so landed files cannot be checked.\n  {exc}\n")

    # One listing for all 21, rather than a round trip per set.
    found: dict[str, str] = {}
    listing_error: str | None = None
    if storage is not None:
        try:
            found = latest_prefixes(storage, root=root)
        except Exception as exc:  # a listing problem must not stop the listing
            listing_error = f"{type(exc).__name__}: {exc}"

    print(f"{'ENTITY SET':<30} {'TABLE':<28} {'DELTA':<30} LANDED")
    for spec in specs():
        landed = "-" if listing_error is None else "?"
        prefix = found.get(spec.name)
        if prefix:
            try:
                manifest = read_manifest(storage, prefix)
                mark = "" if manifest.get("usable") else "  [UNUSABLE]"
                landed = (
                    f"{manifest.get('run_date')} "
                    f"{manifest.get('rows')} rows{mark}"
                )
            except Exception as exc:
                landed = f"? ({type(exc).__name__})"

        delta = spec.runnable_delta
        if delta is None:
            # Nothing incremental will run for this set; say why rather than
            # promise an increment the run will not perform.
            how = spec.why_not_runnable or "full pull only"
        elif delta.field:
            how = f"{delta.field} ge ..."
        else:
            how = f"via {delta.via}.{delta.via_key}"

        note = "" if spec.expects_rows else "  (empty in this client)"
        print(f"{spec.name:<30} {spec.raw_table:<28} {how:<30} {landed}{note}")

    if listing_error:
        print(f"\nCould not read the landing area: {listing_error}")

    problems = check_delta_filters()
    print(f"\n{len(specs())} entity sets. Landing area: {root}/ inside STORAGE_URL.")
    print(
        f"{sum(1 for s in specs() if s.delta)} have a delta; the rest pull in full."
    )
    if problems:
        print("\nDeltas whose filter is NOT verified honoured against live SAP:")
        for problem in problems:
            print(f"  {problem}")
    return 0


def _report(title: str, rows: list[tuple[str, str, int, float]]) -> None:
    print()
    print(f"{title:<30} {'STATUS':<12} {'ROWS':>10}  {'SECONDS':>8}")
    for name, status, count, seconds in rows:
        print(f"{name:<30} {status:<12} {count:>10,}  {seconds:>8.1f}")


def _csv(args) -> int:
    """The CSV route: pull, load, or both, for one table or all of them."""
    from app.ingest.csv_load import load_all, load_table
    from app.ingest.csv_pull import pull_all, pull_one
    from app.ingest.csv_tables import CSV_TABLES

    if not (args.all or args.table):
        print("Nothing selected: pass --all or --table NAME. See --help.")
        return 2

    names = (
        [t.sap_table for t in CSV_TABLES] if args.all else [args.table.upper()]
    )
    failures = 0

    if args.csv_pull:
        # Both or neither: a start with no end silently falls back to the
        # computed window, which is the behaviour the flag exists to avoid.
        if bool(args.from_date) != bool(args.to_date):
            print("--from-date and --to-date must be given together.")
            return 2

        window = {
            "from_date": args.from_date,
            "to_date": args.to_date,
            "years": args.years,
        }
        if args.from_date:
            span = f"{args.from_date}..{args.to_date} (explicit)"
        elif args.years:
            span = f"{args.years} year(s) back"
        else:
            span = "default (3 years for transaction tables)"

        shape = "fired together, then collected" if len(names) > 1 else "single table"
        if args.all:
            blocked = [t for t in CSV_TABLES if t.blocked]
            names = [n for n in names if n not in {t.sap_table for t in blocked}]
            for t in blocked:
                print(f"  {t.sap_table}: left out -- {t.blocked}")
        print(f"CSV pull: {len(names)} table(s), {shape}")
        print(f"  window : {span}")
        print(f"  rows   : {args.max_rows or 'no cap -- full pull'}")
        print()

        results = (
            pull_all(
                max_rows=args.max_rows,
                tables=names,
                wait=not args.no_wait,
                **window,
            )
            if len(names) > 1
            else [
                pull_one(
                    names[0],
                    max_rows=args.max_rows,
                    wait=not args.no_wait,
                    **window,
                )
            ]
        )
        for result in results:
            mark = "ok " if result.ok else "FAIL"
            expected = result.expected_rows
            print(
                f"  [{mark}] {result.sap_table:<8} {result.status:<9} "
                f"{result.received_rows:>9,} row(s)"
                + (f" of {expected:,}" if expected else " (count unverified)")
                + f"  {result.received_chunks} chunk(s)"
            )
            if result.error:
                print(f"         {result.error}")
            if not result.ok:
                failures += 1

    if args.csv_load:
        print(f"\nCSV load into Azure SQL\n")
        results = (
            load_all(names, allow_unreconciled=args.allow_unstable)
            if len(names) > 1
            else [load_table(names[0], allow_unreconciled=args.allow_unstable)]
        )
        for result in results:
            mark = "ok " if result.ok else "FAIL"
            print(
                f"  [{mark}] {result.sap_table:<8} {result.rows:>9,} row(s), "
                f"{result.columns} column(s) -> {result.table}"
                + (f"  watermark={result.watermark}" if result.watermark else "")
            )
            if result.error:
                print(f"         {result.error}")
            if not result.ok:
                failures += 1

    if args.csv_verify:
        failures += _csv_verify(names)

    return 1 if failures else 0


def _csv_verify(names: list[str]) -> int:
    """Per table: did the request complete, is the file there, are rows in SQL?

    Returns the number of tables that did not make it the whole way. A table
    nobody has pulled yet is reported, not counted as a failure -- absence of a
    pull is not the same thing as a broken one.
    """
    from sqlalchemy import select, text

    from app.core.db import get_engine, get_sessionmaker
    from app.core.storage import get_storage
    from app.ingest.csv_tables import csv_table
    from app.models.csv_extract import CsvExtractRequest

    storage = get_storage()
    engine = get_engine()
    gaps = 0

    print("\n" + "=" * 96)
    print("VERIFY: request -> file in storage -> rows in Azure SQL")
    print("=" * 96)
    print(f"{'TABLE':<8}{'REQUEST':<12}{'RECEIVED':>10}{'EXPECTED':>10}"
          f"{'FILE':>14}{'SQL ROWS':>10}{'COLS':>6}  VERDICT")

    with get_sessionmaker()() as session:
        for name in names:
            spec = csv_table(name)

            record = session.scalars(
                select(CsvExtractRequest)
                .where(CsvExtractRequest.sap_table == spec.sap_table)
                .order_by(CsvExtractRequest.fired_at.desc())
            ).first()

            if record is None:
                print(f"{spec.sap_table:<8}{'-':<12}{'-':>10}{'-':>10}"
                      f"{'-':>14}{'-':>10}{'-':>6}  never pulled")
                continue

            # Storage
            size = "-"
            if record.data_key:
                try:
                    size = f"{storage.stat(record.data_key).size:,}"
                except Exception:
                    size = "MISSING"

            # Azure SQL
            rows = cols = "-"
            try:
                with engine.connect() as connection:
                    cols = connection.execute(
                        text(
                            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
                            "WHERE TABLE_NAME = :t"
                        ),
                        {"t": spec.raw_table},
                    ).scalar()
                    if cols:
                        # Table name comes from our own register, never input.
                        rows = connection.execute(
                            text(f"SELECT COUNT(*) FROM {spec.raw_table}")
                        ).scalar()
                    else:
                        cols, rows = 0, 0
            except Exception as exc:
                rows, cols = "ERR", "ERR"
                logger.debug("%s: could not read SQL (%s)", spec.sap_table, exc)

            verdict = _verdict_for(record, size, rows)
            if verdict != "ok":
                gaps += 1

            expected = f"{record.expected_rows:,}" if record.expected_rows else "?"
            print(
                f"{spec.sap_table:<8}{record.status:<12}"
                f"{record.received_rows:>10,}{expected:>10}"
                f"{size:>14}{rows if isinstance(rows, str) else f'{rows:,}':>10}"
                f"{cols:>6}  {verdict}"
            )

    print("=" * 96)
    print(
        f"{len(names) - gaps} of {len(names)} table(s) made it to Azure SQL."
        if not gaps
        else f"{gaps} of {len(names)} table(s) did not reach Azure SQL -- see above."
    )
    return gaps


def _verdict_for(record, size, rows) -> str:
    """One phrase saying where this table stopped."""
    if record.status == "open":
        return "delivery still open"
    if record.status == "timeout":
        return "SAP acked, delivered nothing"
    if record.status == "failed":
        return f"pull failed ({(record.error or '')[:40]})"
    if size == "MISSING":
        return "complete but the file is gone"
    if record.loaded_at is None:
        return "landed, not loaded -- run --csv-load"
    if isinstance(rows, str):
        return "loaded, SQL unreadable"
    if rows == 0:
        return "loaded 0 rows"
    return "ok"


def _csv_status() -> int:
    """Recent extract requests, newest first."""
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.csv_extract import CsvExtractRequest

    with get_sessionmaker()() as session:
        rows = list(
            session.scalars(
                select(CsvExtractRequest)
                .order_by(CsvExtractRequest.fired_at.desc())
                .limit(25)
            )
        )

    if not rows:
        print("No CSV extract requests recorded yet.")
        return 0

    print(f"{'REQUEST':<22}{'TABLE':<8}{'STATUS':<10}{'ROWS':>12}"
          f"{'EXPECTED':>12}{'CHUNKS':>8}  LOADED")
    for row in rows:
        print(
            f"{row.request_id:<22}{row.sap_table:<8}{row.status:<10}"
            f"{row.received_rows:>12,}"
            f"{(f'{row.expected_rows:,}' if row.expected_rows else '-'):>12}"
            f"{row.received_chunks:>8}  "
            f"{'yes' if row.loaded_at else 'no'}"
        )
        if row.error:
            print(f"    {row.error}")
    return 0


def _abandon() -> int:
    from app.ingest.csv_pull import abandon_open

    closed = abandon_open()
    print(
        f"Closed {closed} open request(s)."
        if closed
        else "No open request; nothing to abandon."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)

    if args.list:
        return _list()

    if args.abandon:
        return _abandon()
    if args.csv_status:
        return _csv_status()
    if args.csv_pull or args.csv_load or args.csv_verify:
        return _csv(args)

    if not (args.fetch or args.load):
        print("Nothing to do: pass --fetch, --load, or both. See --help.")
        return 2
    if not (args.all or args.set):
        print("Nothing selected: pass --all or --set NAME. See --help.")
        return 2

    try:
        chosen: tuple[IngestSpec, ...] = specs() if args.all else (spec_for(args.set),)
    except KeyError as exc:
        print(exc)
        return 2

    root = settings.ingest_prefix
    mode = MODE_DELTA if args.delta else MODE_FULL
    failures = 0

    if mode == MODE_DELTA:
        # Refuse before the first request rather than after a "delta" has
        # quietly pulled whole sets. See manifest.check_delta_filters.
        problems = check_delta_filters()
        if problems:
            print("Delta filters are not all verified against live SAP:")
            for problem in problems:
                print(f"  {problem}")
            print("\nRe-probe them, or run --full.")
            return 1

        if args.all:
            # The same rule the scheduler applies. A set with no runnable
            # delta is not "pulled in full as a fallback" here: CDPOS is
            # 940,000 rows over OData, and the CSV route already covers it.
            chosen = tuple(s for s in specs() if s.runnable_delta is not None)
            blocked = [s for s in specs() if s.delta is not None and s.blocked]
            left_out = [
                s.name for s in specs()
                if s.runnable_delta is None and not (s.delta is not None and s.blocked)
            ]
            print(
                f"--delta --all: {len(chosen)} set(s) have a delta that can run "
                f"({', '.join(s.name for s in chosen)})."
            )
            for s in blocked:
                print(f"  {s.name}: delta declared but {s.blocked}")
            print(f"Left to the CSV route: {', '.join(left_out)}.\n")

    loaded_by_sweep = False

    if args.fetch:
        try:
            storage = get_storage()
        except StorageNotConfiguredError as exc:
            print(f"Cannot reach storage.\n  {exc}")
            return 1

        client = SapClient()

        if mode == MODE_DELTA:
            report = run_delta_sweep(
                chosen, root=root, client=client, storage=storage,
                since=args.since, load=args.load,
            )
            _report("FETCHED", report.fetch_rows())
            if args.load:
                _report("LOADED", report.load_rows())
                loaded_by_sweep = True
            for name in report.baselined:
                print(f"  {name}: no odata_ table yet, pulled in full to build it")
            for name, mark in report.advanced.items():
                print(f"  {name}: watermark -> {mark}")
            for name, why in report.held.items():
                print(f"  {name}: watermark HELD -- {why}")
            for outcome in report.outcomes:
                if outcome.status != "ok" and outcome.detail:
                    print(f"  {outcome.name}: {outcome.detail}")
            failures += report.failures
        else:
            results = []
            for spec in chosen:
                outcome = fetch_set(
                    spec, root=root, client=client, storage=storage, mode=MODE_FULL
                )
                status = (
                    "ok" if outcome.ok
                    else ("UNSTABLE" if outcome.error is None else "FAILED")
                )
                failures += 0 if outcome.ok else 1
                results.append((spec.name, status, outcome.rows, outcome.seconds))
            _report("FETCHED", results)

    if args.load and not loaded_by_sweep:
        results = []
        for spec in chosen:
            outcome = load_set(
                spec, root=root, allow_unstable=args.allow_unstable
            )
            failures += 1 if outcome.status == STATUS_FAILED else 0
            results.append((spec.name, outcome.status, outcome.rows, outcome.seconds))
        _report("LOADED", results)

    if failures:
        print(f"\n{failures} set(s) did not complete. See the log above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
