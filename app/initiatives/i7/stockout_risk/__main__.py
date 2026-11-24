"""Rebuild the High Stockout Risk usage table.

    python -m app.initiatives.i7.stockout_risk                    # as of I7_STOCKOUT_RISK_AS_OF, or today when blank
    python -m app.initiatives.i7.stockout_risk --as-of 2026-05-31 # one-off date

Run it after changing I7_STOCKOUT_RISK_AS_OF. The I07 pipeline now runs the same rebuild as
its last stage (``stockout_risk.stage``), so the card is recalculated on every pipeline run; this
command is for rebuilding it by hand in between. It replaces the whole table, so it is safe to
rerun. Until the table has been built once, the card reads "Not available".

Exits 0 on success, non-zero on failure.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.logging import configure_logging, get_logger
from app.initiatives.i7.stockout_risk.history import build_stockout_risk_input

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.initiatives.i7.stockout_risk",
        description="Rebuild i7_stockout_risk_input, the usage the High Stockout Risk card reads.",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Read the history as of this date instead of I7_STOCKOUT_RISK_AS_OF.",
    )
    args = parser.parse_args(argv)
    configure_logging(get_settings())

    with get_sessionmaker()() as session:
        try:
            anchor, written = build_stockout_risk_input(session, as_of=args.as_of)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("stockout risk input build failed")
            return 1
    if written == 0:
        logger.warning("no issue movements on or before %s: the table was left empty", anchor)
    else:
        logger.info("done: %d material-plants as of %s", written, anchor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
