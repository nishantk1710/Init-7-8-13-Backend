"""Command line entry point for the seed loader."""

from __future__ import annotations

import argparse
import sys

from app.core.logging import configure_logging, get_logger
from app.core.config import get_settings
from app.core.storage import StorageNotConfiguredError, get_storage
from app.seed.loader import (
    STATUS_FAILED,
    STATUS_SKIPPED,
    load_all,
    load_table,
    missing_files,
)
from app.seed.manifest import EXTRACTS, MISSING_FROM_DELIVERY, spec_for

logger = get_logger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed",
        description="Load the July SAP extracts into the local database.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="load every table in the manifest")
    group.add_argument("--table", metavar="NAME", help="load one table, e.g. marc")
    group.add_argument(
        "--list",
        action="store_true",
        help="show the manifest and whether each source file is present; loads nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="reload even when the source files are unchanged since the last load",
    )
    return parser


def _list_manifest() -> int:
    """Print the manifest, marking missing sources. Touches no database."""
    try:
        available = set(get_storage().list())
    except StorageNotConfiguredError as exc:
        print(f"STORAGE_URL is not set, so source files cannot be checked.\n  {exc}")
        available = set()

    print(f"{'TABLE':<14} {'SAP':<12} {'ROWS FROM':<44} INITIATIVES")
    missing_any = False
    for spec in EXTRACTS:
        marks = []
        for key in spec.files:
            present = key in available
            missing_any = missing_any or not present
            marks.append(key if present else f"{key} [MISSING]")
        print(
            f"{spec.table:<14} {spec.sap_table:<12} {', '.join(marks):<44} "
            f"{', '.join(spec.initiatives) or '-'}"
        )

    unmapped = sorted(available - {f for spec in EXTRACTS for f in spec.files})
    if unmapped:
        print("\nIn storage but not in the manifest:")
        for key in unmapped:
            print(f"  {key}")

    print("\nNamed in the brief but absent from the July delivery:")
    for item in MISSING_FROM_DELIVERY:
        print(f"  {item}")

    return 1 if missing_any else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(get_settings())

    if args.list:
        return _list_manifest()

    specs = EXTRACTS if args.all else (spec_for(args.table),)

    # Preflight. A wrong STORAGE_URL or a differently-laid-out delivery is a
    # setup problem, and it should be reported as one -- before any table is
    # dropped and rebuilt, not discovered a third of the way through.
    try:
        gaps = missing_files(specs)
    except StorageNotConfiguredError as exc:
        print(f"Cannot reach storage.\n  {exc}")
        return 1
    if gaps:
        print("Source files are missing from storage. Nothing was loaded.\n")
        for table, keys in gaps.items():
            for key in keys:
                print(f"  {table:<14} {key}")
        print(
            "\nCheck STORAGE_URL points at the folder that CONTAINS the delivery"
            " folders, and that they are named exactly as the manifest expects"
            " (see README, 'Seeding')."
        )
        return 1
    results = (
        load_all(specs, force=args.force)
        if args.all
        else [load_table(specs[0], force=args.force)]
    )

    loaded = [r for r in results if r.status not in (STATUS_FAILED, STATUS_SKIPPED)]
    skipped = [r for r in results if r.status == STATUS_SKIPPED]
    failed = [r for r in results if r.status == STATUS_FAILED]

    print()
    print(f"{'TABLE':<14} {'STATUS':<10} {'ROWS':>10}  {'SECONDS':>8}")
    for result in results:
        print(f"{result.table:<14} {result.status:<10} {result.rows:>10,}  {result.seconds:>8.1f}")

    total_rows = sum(r.rows for r in results)
    print(
        f"\n{len(loaded)} loaded, {len(skipped)} skipped, {len(failed)} failed; "
        f"{total_rows:,} rows"
    )

    if failed:
        print("\nFailures:")
        for result in failed:
            print(f"  {result.table}: {result.error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
