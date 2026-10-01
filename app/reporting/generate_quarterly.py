"""Manual/scheduled entry point for the I07 Quarterly Deep-Dive Report.

    python -m app.reporting.generate_quarterly --quarter "Q3 2026"
    python -m app.reporting.generate_quarterly              # latest closed quarter

**No in-process scheduler exists in this codebase** (no celery, no
apscheduler, no cron trigger, no Azure Functions timer -- confirmed by
source-tree search). This module does not start one. It is intended to be
invoked by an external trigger -- an Azure Logic App, a WebJob, or a manual
ops run -- on a quarterly cadence tied to the Initiative 11 review cycle,
exactly the way ``python -m app.seed --all`` (see ``app/seed/__main__.py``)
is invoked externally rather than by anything inside this process. See
``docs/quarterly-report-scheduling.md`` for exactly what that external
trigger requires and whether it has actually been provisioned.

This CLI calls the *same* service and repository functions the API's
``POST /v1/i7/reports/quarterly/generate`` endpoint calls --
``app.initiatives.i7.reporting.service.generate_quarterly_report`` and
``app.initiatives.i7.reporting.repository.save_report`` -- never a second,
parallel implementation. The manual/scheduled path and the API path are two
callers of one service, which is the whole point of sharing it.

``--quarter`` is optional. Omitted, the latest *closed* calendar quarter is
resolved via ``app.initiatives.i7.reporting.period.latest_closed_quarter`` --
the same resolution the API endpoint falls back to -- so a scheduler invoking
this with no arguments on, say, the 1st of every January/April/July/October
always generates the quarter that just closed, never a hardcoded one.

Exits 0 on success, non-zero on failure (a bad ``--quarter`` format, a
database error, or an aggregation failure), so an external scheduler's own
failure/retry/alerting logic can act on the process exit code without
parsing output. Safe to retry/re-run: generation is idempotent per quarter
(see ``repository.save_report``'s docstring).
"""

from __future__ import annotations

import argparse
import sys

from app.core.db import get_sessionmaker
from app.core.logging import configure_logging, get_logger
from app.core.config import get_settings
from app.initiatives.i7.reporting.period import latest_closed_quarter
from app.initiatives.i7.reporting.repository import save_report
from app.initiatives.i7.reporting.service import generate_quarterly_report

logger = get_logger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.reporting.generate_quarterly",
        description="Generate (or regenerate) the I07 Quarterly Deep-Dive "
        "Report for one calendar quarter and persist it. Intended to be "
        "invoked externally (Azure Logic App / WebJob / manual ops run) on a "
        "quarterly cadence -- no scheduler runs inside this process.",
    )
    parser.add_argument(
        "--quarter",
        required=False,
        default=None,
        metavar="'Q<1-4> <year>'",
        help="e.g. 'Q3 2026'. Omit to generate the latest closed calendar "
        "quarter -- the correct choice for a recurring scheduled call.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(get_settings())
    if args.quarter is None:
        quarter = latest_closed_quarter()
        quarter_source = "latest_closed"
    else:
        quarter = args.quarter
        quarter_source = "explicit"

    logger.info(
        "i7.reporting.cli.generate_quarterly.start",
        extra={"quarter": quarter, "quarter_source": quarter_source},
    )

    session = get_sessionmaker()()
    try:
        try:
            report = generate_quarterly_report(session, quarter)
        except ValueError as exc:
            # Malformed --quarter (see app.initiatives.i7.reporting.period.resolve_quarter)
            # is a usage error, not a data/database failure -- reported plainly,
            # no partial row written.
            print(f"Invalid --quarter value: {exc}", file=sys.stderr)
            logger.info(
                "i7.reporting.cli.generate_quarterly.invalid_quarter",
                extra={"quarter": quarter, "quarter_source": quarter_source},
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
                extra={"quarter": quarter, "quarter_source": quarter_source},
            )
            return 1
    finally:
        session.close()

    print(f"Generated and saved I07 Quarterly Deep-Dive Report for {quarter} (report_id={row.id}).")
    logger.info(
        "i7.reporting.cli.generate_quarterly.done",
        extra={"quarter": quarter, "quarter_source": quarter_source, "report_id": row.id},
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
