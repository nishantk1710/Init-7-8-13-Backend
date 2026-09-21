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
from app.ingest.fetch import (
    MODE_DELTA,
    MODE_FULL,
    collect_parent_keys,
    fetch_set,
    read_manifest,
)
from app.ingest.load import STATUS_FAILED, load_set, latest_prefix
from app.ingest.manifest import (
    IngestSpec,
    check_delta_filters,
    spec_for,
    specs,
)
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
            "read only what changed since the stored watermark. Sets with no "
            "declared delta fall back to a full pull."
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
            "load a fetch whose duplicate-key check failed. For inspecting a bad "
            "pull deliberately -- never for getting a sweep to finish."
        ),
    )
    return parser


def _fetch_order(chosen: tuple[IngestSpec, ...]) -> list[IngestSpec]:
    """Parents before the children that are read through them.

    Only matters for the key cache below: a child fetched before its parent
    would read that parent itself, and the sweep would then read it again for
    the next child.
    """
    parents = {s.delta.via for s in chosen if s.delta and s.delta.via}
    return sorted(chosen, key=lambda s: (s.name not in parents, s.name))


def _list() -> int:
    settings = get_settings()
    root = settings.ingest_prefix
    try:
        storage = get_storage()
    except StorageNotConfiguredError as exc:
        storage = None
        print(f"STORAGE_URL is not set, so landed files cannot be checked.\n  {exc}\n")

    print(f"{'ENTITY SET':<30} {'TABLE':<28} {'DELTA':<30} LANDED")
    for spec in specs():
        landed = "-"
        if storage is not None:
            try:
                prefix = latest_prefix(storage, spec, root=root)
                if prefix:
                    manifest = read_manifest(storage, prefix)
                    mark = "" if manifest.get("usable") else "  [UNUSABLE]"
                    landed = (
                        f"{manifest.get('run_date')} "
                        f"{manifest.get('rows')} rows{mark}"
                    )
            except Exception as exc:  # a listing problem must not stop the listing
                landed = f"? ({type(exc).__name__})"

        delta = spec.delta
        if delta is None:
            how = "full pull only"
        elif delta.field:
            how = f"{delta.field} ge ..."
        else:
            how = f"via {delta.via}.{delta.via_key}"

        note = "" if spec.expects_rows else "  (empty in this client)"
        print(f"{spec.name:<30} {spec.raw_table:<28} {how:<30} {landed}{note}")

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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)

    if args.list:
        return _list()

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

    if args.fetch:
        try:
            storage = get_storage()
        except StorageNotConfiguredError as exc:
            print(f"Cannot reach storage.\n  {exc}")
            return 1

        client = SapClient()
        # One read of each parent, shared by its children. PurchaseOrderSet has
        # three; reading EKKO four times a run would be pure waste.
        key_cache: dict[tuple[str, str], list[str]] = {}
        results = []

        for spec in _fetch_order(chosen):
            parent_keys = None
            delta = spec.delta if mode == MODE_DELTA else None
            if delta is not None and delta.via:
                cache_key = (delta.via, delta.via_key or "")
                if cache_key not in key_cache:
                    try:
                        key_cache[cache_key] = collect_parent_keys(
                            client, delta, args.since
                        )
                    except Exception as exc:
                        print(f"{spec.name}: could not read parent {delta.via}: {exc}")
                        failures += 1
                        results.append((spec.name, "FAILED", 0, 0.0))
                        continue
                parent_keys = key_cache[cache_key]

            outcome = fetch_set(
                spec,
                root=root,
                client=client,
                storage=storage,
                mode=mode,
                since=args.since,
                parent_keys=parent_keys,
            )
            status = (
                "ok" if outcome.ok
                else ("UNSTABLE" if outcome.error is None else "FAILED")
            )
            failures += 0 if outcome.ok else 1
            results.append((spec.name, status, outcome.rows, outcome.seconds))
        _report("FETCHED", results)

    if args.load:
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
