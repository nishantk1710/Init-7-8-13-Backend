"""The pipeline stage that rebuilds the High Stockout Risk usage table.

The High Stockout Risk card reads ``i7_stockout_risk_input`` (each material-plant's last issue date and its
90-day consumption, read as of an anchor date). That table used to be rebuilt only by hand
(``python -m app.initiatives.i7.stockout_risk``), so the card went stale while every other card and chart moved
with each pipeline run. This stage runs the same rebuild as the last step of the pipeline, so the card is
recalculated every time the pipeline runs.

The anchor date is the one ``I7_STOCKOUT_RISK_AS_OF`` pins. Left BLANK (the default) it is the date of the run, so the
card's "As of" date is the date the pipeline rebuilt it and matches the rest of the run; a pinned date keeps the card on
that date.

The rebuild replaces the whole table, so a rerun is always safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.initiatives.i7.stockout_risk.history import build_stockout_risk_input
from app.models.i7_pipeline import STATUS_FAILED, STATUS_SUCCEEDED

logger = get_logger(__name__)


@dataclass
class StockoutRiskStageResult:
    """What the stage did, in the shape the pipeline reads (``status`` and, on failure, ``error``)."""

    status: str = STATUS_SUCCEEDED
    rows_written: int = 0
    as_of: date | None = None
    """The date the table was built as of (the pinned date, or the date of the run)."""
    error: str | None = None


def rebuild_stockout_risk_input() -> StockoutRiskStageResult:
    """Rebuild ``i7_stockout_risk_input`` as of the configured anchor. Never raises: a failure is returned."""
    with get_sessionmaker()() as session:
        try:
            anchor, written = build_stockout_risk_input(session)
            session.commit()
        except Exception as exc:  # noqa: BLE001 -- reported to the pipeline, not propagated
            session.rollback()
            logger.exception("stockout risk input build failed")
            return StockoutRiskStageResult(status=STATUS_FAILED, error=f"{type(exc).__name__}: {exc}")
    if anchor is None or written == 0:
        logger.warning("stockout risk: no issue movements on or before %s, the table was left empty", anchor)
    else:
        logger.info("stockout risk: %d material-plants as of %s", written, anchor)
    return StockoutRiskStageResult(rows_written=written, as_of=anchor)
