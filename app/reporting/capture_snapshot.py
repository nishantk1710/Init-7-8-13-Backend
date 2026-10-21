"""Capture the I07 monthly KPI snapshots by hand (or from an external trigger).

    python -m app.reporting.capture_snapshot                    # this month's rolling reading, and last month's if not final
    python -m app.reporting.capture_snapshot --month 2026-10    # one named month
    python -m app.reporting.capture_snapshot --month 2026-10 --force   # replace a FINAL snapshot (logged)

What a snapshot is, and why a past month cannot be rebuilt, is in ``app/models/i7_kpi_snapshot.py``; the capture rules
(idempotent, OPEN / FINAL, nothing replaced silently, failures logged) are in
``app/initiatives/i7/reporting/snapshots.py``. This command is a thin caller of that module -- the nightly job in
``pipeline_scheduler`` calls the same functions, so there is one implementation.

Run it after the migration ``a3c9e7f15b42`` has been applied. Capturing right after a month ends (the first days of
the next month) gives the closest thing to a month-end position; a snapshot taken much later is still saved, but is
marked as not valid for month-end.

Exits 0 on success (including a refusal to replace a final snapshot, which is reported, not a failure), 1 when a capture
failed (the failure is in ``i7_kpi_snapshot_log`` and the command can simply be run again), 2 for a bad ``--month``.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.logging import configure_logging, get_logger
from app.initiatives.i7.reporting import snapshots

logger = get_logger(__name__)


def _month(value: str) -> date:
    """``2026-10`` -> ``date(2026, 10, 1)``."""
    try:
        year, month = value.split("-")
        return date(int(year), int(month), 1)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"'{value}' is not a month; expected YYYY-MM, e.g. 2026-10") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.reporting.capture_snapshot",
        description="Capture the I07 monthly KPI snapshot(s) the Quarterly Report's trends and quarter-end position read.",
    )
    parser.add_argument(
        "--month", type=_month, action="append", metavar="YYYY-MM",
        help="a month to capture; repeat for several. Omit for the current month and the previous one.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="replace a FINAL snapshot. The replacement is logged with the fingerprint it replaced.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(get_settings())

    lines: list[str] = []
    with get_sessionmaker()() as session:
        try:
            if args.month:
                results = [snapshots.capture_month(session, month, force=args.force) for month in args.month]
            else:
                results = snapshots.capture_due(session)
        except ValueError as exc:
            print(f"Invalid --month: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 -- logged by capture_month; reported plainly for the caller
            print(f"Snapshot capture failed: {type(exc).__name__}: {exc} (logged; run again to retry)", file=sys.stderr)
            return 1
        # Read while the session is still open: a committed row is expired, and reloading it needs the session.
        for result in results:
            row = result.snapshot
            detail = f" ({result.detail})" if result.detail else ""
            measured = f", measured {row.measured_on}, month-end valid: {row.month_end_valid}" if row else ""
            lines.append(f"{result.month:%Y-%m}: {result.action}{detail}{measured}")

    print("\n".join(lines) if lines else "Snapshots are up to date: nothing to capture.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
