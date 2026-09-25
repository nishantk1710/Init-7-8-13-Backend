"""Manual/scheduled entry point for the I07 Quarterly Deep-Dive Report.

    python -m app.reporting.generate_quarterly --quarter "Q3 2026"
    python -m app.reporting.generate_quarterly --auto

**No in-process scheduler exists in this codebase** (no celery, no
apscheduler, no cron trigger, no Azure Functions timer -- confirmed by
source-tree search). This module does not start one. It is intended to be
invoked by an external trigger -- Windows Task Scheduler, an Azure Logic App,
a WebJob, or a manual ops run -- on a quarterly cadence tied to the
Initiative 11 review cycle, exactly the way ``python -m app.seed --all``
(see ``app/seed/__main__.py``) is invoked externally rather than by anything
inside this process.

``--auto`` exists so the external trigger does not need its own copy of
quarter-boundary logic (e.g. a hardcoded string in a Task Scheduler XML that
would go stale every January): it resolves to
``period.most_recently_completed_quarter(today)`` and skips the run
entirely (exit 0) if that quarter already has a ``COMPLETED`` row, so a
misfired or re-run scheduled task is a no-op rather than a wasted 40-50s
regeneration. ``--quarter`` always runs (or reruns) the named quarter
unconditionally, which is what a manual ops regeneration wants.

This CLI calls the *same* service and repository functions the API's
``POST /v1/i7/reports/quarterly/generate`` endpoint calls --
``app.initiatives.i7.reporting.service.generate_quarterly_report`` and
``app.initiatives.i7.reporting.repository.save_report`` -- never a second,
parallel implementation. The manual/scheduled path and the API path are two
callers of one service, which is the whole point of sharing it.

Exits 0 on success (including an ``--auto`` no-op skip), non-zero on failure
(a bad ``--quarter`` format, a database error, or an aggregation failure), so
an external scheduler's own failure/retry/alerting logic can act on the
process exit code without parsing output.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from app.core.db import get_sessionmaker
from app.core.logging import configure_logging, get_logger
from app.core.config import get_settings
from app.initiatives.i7.reporting.period import most_recently_completed_quarter
from app.initiatives.i7.reporting.repository import get_report, save_report
from app.initiatives.i7.reporting.service import generate_quarterly_report

logger = get_logger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.reporting.generate_quarterly",
        description="Generate (or regenerate) the I07 Quarterly Deep-Dive "
        "Report for one calendar quarter and persist it. Intended to be "
        "invoked externally (Windows Task Scheduler / Azure Logic App / "
        "WebJob / manual ops run) on a quarterly cadence -- no scheduler "
        "runs inside this process.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--quarter",
        metavar="'Q<1-4> <year>'",
        help="Generate this specific quarter unconditionally, e.g. 'Q3 2026'.",
    )
    group.add_argument(
        "--auto",
        action="store_true",
        help="Resolve the most recently completed quarter and generate it "
        "only if it has no COMPLETED report yet. For unattended scheduled "
        "runs that should not hardcode a quarter string.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(get_settings())

    session = get_sessionmaker()()
    try:
        if args.auto:
            quarter = most_recently_completed_quarter(date.today())
            existing = get_report(session, quarter)
            if existing is not None and existing.status == "COMPLETED":
                print(f"{quarter} already has a completed report (report_id={existing.id}); skipping.")
                logger.info(
                    "i7.reporting.cli.generate_quarterly.auto_skip",
                    extra={"quarter": quarter, "report_id": existing.id},
                )
                return 0
        else:
            quarter = args.quarter

        logger.info("i7.reporting.cli.generate_quarterly.start", extra={"quarter": quarter})

        try:
            report = generate_quarterly_report(session, quarter)
        except ValueError as exc:
            # Malformed --quarter (see app.initiatives.i7.reporting.period.resolve_quarter)
            # is a usage error, not a data/database failure -- reported plainly,
            # no partial row written.
            print(f"Invalid --quarter value: {exc}", file=sys.stderr)
            logger.info(
                "i7.reporting.cli.generate_quarterly.invalid_quarter",
                extra={"quarter": quarter},
            )
            return 2

        try:
            row = save_report(session, quarter, report, status="COMPLETED")
            session.commit()
        except Exception as exc:
            session.rollback()
            try:
                save_report(session, quarter, report, status="FAILED", error=str(exc))
                session.commit()
            except Exception:
                session.rollback()
            print(f"Failed to persist report for {quarter}: {exc}", file=sys.stderr)
            logger.info(
                "i7.reporting.cli.generate_quarterly.persist_failed",
                extra={"quarter": quarter},
            )
            return 1
    finally:
        session.close()

    print(f"Generated and saved I07 Quarterly Deep-Dive Report for {quarter} (report_id={row.id}).")
    logger.info(
        "i7.reporting.cli.generate_quarterly.done",
        extra={"quarter": quarter, "report_id": row.id},
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
