"""Preview the I07 Quarterly Report as it would read on a chosen day -- without saving anything.

    python -m app.reporting.preview_quarterly --as-of 2027-01-01 --json q4-preview.json --xlsx q4-preview.xlsx
    python -m app.reporting.preview_quarterly --quarter "Q4 2026" --as-of 2027-01-01
    python -m app.reporting.preview_quarterly                      # the latest closed quarter, as of today

This runs the **real** report generation (``generate_quarterly_report``, the same function the API, the CLI and the
nightly step call) with a simulated "today" passed down to every date-dependent part of it -- whether the period is
complete, the reporting cutoff, which months have ended, stockout staleness, the status distribution's date and
``generated_at`` -- not just the quarter resolution. Nothing here changes the system clock.

**Nothing is written.** This module does not import ``save_report`` and never commits. The session is also fenced:
any attempt to flush or commit raises, and the transaction is rolled back at the end, so even a future change that made
generation write would fail loudly here instead of altering shared data. Use it on a database you would not want
written to (the shared non-production one included): it only reads.

What a preview is not: it reads the data as it stands today with only the *date* moved. It cannot invent November's
recommendations, and a month-end snapshot that was never captured is still missing -- the report says so, as it would
for real. To see three populated months, use the synthetic fixtures in ``tests/i7/reporting/test_completed_quarter.py``
(an isolated in-memory database), never the shared one.

Exit codes: 0 done, 2 for a bad ``--quarter`` / ``--as-of``, 1 for a failure while generating or exporting.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timezone

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.logging import configure_logging, get_logger
from app.initiatives.i7.reporting.auto_generate import DEFAULT_WINDOW_DAYS, assess
from app.initiatives.i7.reporting.period import latest_closed_period
from app.initiatives.i7.reporting.report_export import build_quarterly_report_workbook
from app.initiatives.i7.reporting.service import generate_quarterly_report

logger = get_logger(__name__)


class ReadOnlyViolation(RuntimeError):
    """The preview session was asked to write."""


def fence_read_only(session: Session) -> Session:
    """Make ``session`` refuse every write: a flush with pending changes, or any commit, raises."""

    @event.listens_for(session, "before_flush")
    def _no_flush(sess, flush_context, instances):  # noqa: ANN001
        if sess.new or sess.dirty or sess.deleted:
            raise ReadOnlyViolation("the quarterly report preview is read-only; it tried to write")

    @event.listens_for(session, "before_commit")
    def _no_commit(sess):  # noqa: ANN001
        raise ReadOnlyViolation("the quarterly report preview is read-only; it tried to commit")

    return session


def _as_of(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a date; expected YYYY-MM-DD, e.g. 2027-01-01") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.reporting.preview_quarterly",
        description="Run the real Quarterly Report generation for a simulated date and export it, saving NOTHING.",
    )
    parser.add_argument("--as-of", type=_as_of, default=None, metavar="YYYY-MM-DD",
                        help="the day to simulate (default: today), e.g. 2027-01-01 to see Q4 2026 as a completed quarter")
    parser.add_argument("--quarter", default=None, metavar="'Q<1-4> <year>' or '<Mon> <year>'",
                        help="a quarter ('Q4 2026') or a single month ('Oct 2026'); default: the latest closed period "
                        "of --period as of --as-of")
    parser.add_argument("--period", choices=("quarter", "month"), default="quarter",
                        help="with --quarter omitted: preview the latest closed 'quarter' (default) or 'month'")
    parser.add_argument("--json", dest="json_path", default=None, metavar="FILE", help="write the report as JSON")
    parser.add_argument("--xlsx", dest="xlsx_path", default=None, metavar="FILE", help="write the Excel export")
    return parser


def _summary(report, quarter: str) -> list[str]:
    management = report.management
    lines = [
        f"  quarter ............ {quarter} ({report.metadata.period_start} to {report.metadata.period_end})",
        f"  period status ...... {management.period.status.value if management else 'n/a'}",
        f"  recommendations .... {report.recommendations.total}",
        f"  forecasts .......... {report.forecasting.total_forecasts}",
        f"  material-plants .... {report.scope_and_data_quality.total_records}",
    ]
    if management is not None:
        lines.append(f"  months ............. " + ", ".join(f"{m.label} [{m.state}]" for m in management.period.months))
        lines.append(f"  position basis ..... {management.position_basis}")
        lines.append(f"  note ............... {management.period.note}")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(get_settings())
    as_of = args.as_of or datetime.now(timezone.utc).date()
    quarter = args.quarter or latest_closed_period(args.period, as_of)
    # The simulated moment, UTC midnight of the chosen day -- what ``generated_at`` will say.
    simulated_now = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc)

    print(f"PREVIEW -- nothing will be saved. Simulated date: {as_of}")
    session = fence_read_only(get_sessionmaker()())
    try:
        try:
            decision = assess(session, simulated_now, DEFAULT_WINDOW_DAYS, args.period)
            print(f"  the nightly {args.period} step, on {as_of}, would target {decision.quarter}: {decision.state} -- {decision.reason}")

            started = time.monotonic()
            report = generate_quarterly_report(session, quarter, today=as_of, generated_at=simulated_now)
        except ValueError as exc:
            print(f"Invalid --quarter value: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 -- reported plainly for the caller
            print(f"Preview failed while generating: {type(exc).__name__}: {exc}", file=sys.stderr)
            logger.info("i7.reporting.preview.failed", extra={"quarter": quarter, "as_of": str(as_of)})
            return 1
        finally:
            session.rollback()  # the preview never leaves a transaction behind

        print(f"  generated in {time.monotonic() - started:.1f}s")
        print("\n".join(_summary(report, quarter)))

        try:
            if args.json_path:
                with open(args.json_path, "w", encoding="utf-8") as handle:
                    handle.write(report.model_dump_json(indent=2))
                print(f"  JSON  -> {args.json_path}")
            if args.xlsx_path:
                with open(args.xlsx_path, "wb") as handle:
                    handle.write(build_quarterly_report_workbook(report).getvalue())
                print(f"  Excel -> {args.xlsx_path}")
        except OSError as exc:
            print(f"Preview generated, but a file could not be written: {exc}", file=sys.stderr)
            return 1
    finally:
        session.close()

    print("Done. Nothing was saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
