"""Reading and advancing the delta high-water mark.

Two rules here are worth stating, because both are about preferring a slower
correct answer to a faster wrong one.

**The mark advances only after the rows are safely landed.** If the fetch
half-succeeds, the mark stays where it was and the next run re-reads the same
window. Re-reading costs time; advancing past rows that were never landed
loses them permanently and silently.

**The window is inclusive at its lower bound.** ``field ge watermark`` re-reads
the boundary value every run. That is deliberate: SAP's dates have day
granularity, so an exclusive bound would drop anything created later on the
same day as the previous run's last row. The overlap is absorbed by the load,
which merges on the entity key rather than appending.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.models.ingest_watermark import IngestWatermark

logger = get_logger(__name__)


def get_watermark(entity_set: str, field: str) -> str | None:
    """The mark for this set, or None if there is none to use.

    Returns None when the stored mark was measured against a *different*
    field. A date compared against a position recorded in document numbers is
    silently nonsense, and the safe reading of "I do not understand this mark"
    is "pull everything".
    """
    session_factory = get_sessionmaker()
    with session_factory() as session:
        row = session.get(IngestWatermark, entity_set)
        if row is None:
            return None
        if row.field != field:
            logger.warning(
                "%s: stored watermark is on %r but the delta now uses %r. "
                "Ignoring it and pulling in full; the mark will be rewritten.",
                entity_set,
                row.field,
                field,
            )
            return None
        return row.value


def set_watermark(entity_set: str, field: str, value: str, rows: int) -> None:
    """Record how far this set has been pulled."""
    session_factory = get_sessionmaker()
    with session_factory() as session:
        row = session.get(IngestWatermark, entity_set)
        if row is None:
            row = IngestWatermark(entity_set=entity_set, field=field, value=value)
            session.add(row)
        row.field = field
        row.value = value
        row.rows_last_run = rows
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
    logger.info("%s: watermark advanced to %s=%s", entity_set, field, value)


def highest(rows: list[dict], field: str) -> str | None:
    """The largest value of ``field`` across these rows, as text.

    Compared as strings, which is correct for the two shapes this sees: OData
    hands back dates in a form that sorts lexically, and document numbers are
    zero-padded. It would be wrong for a bare integer, which is why a delta
    field has to be one SAP filters on rather than anything numeric-looking.
    """
    values = [str(row[field]) for row in rows if row.get(field) not in (None, "")]
    return max(values) if values else None
