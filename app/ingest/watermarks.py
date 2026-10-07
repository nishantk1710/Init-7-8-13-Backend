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

WHAT A MARK IS

A calendar day, stored as ``YYYY-MM-DD``. Every delta field is an SAP date
(DATS), and SAP hands the same kind of value back in three shapes, measured
on 2026-10-07:

    Edm.DateTime   /Date(...)/ at SAP-local midnight, which the envelope
                   decodes to 22:00 UTC the evening before   EKKO/EKPO Aedat
    Edm.String     '20260916'                                EKBE Cpudt, CDHDR Udate
    Edm.String     '16.09.2026'                              MSEG CpudtMkpf

The highest value is taken as a DAY, not as text. Compared as text the third
shape ranks by day of month -- '31.12.2018' after '01.01.2026' -- and a mark
taken that way can sit years in the past or the future of the data. Rendering
the day back into the literal each field's $filter accepts is fetch.py's job
(``delta_literal``); this module only ever deals in days.

Marks written before this rule -- ``2026-09-14 22:00:00+00:00`` from a decoded
datetime, ``2026-10-05 00:00:00`` from a CSV or workbook seed -- are read as the
day they stand for, so no stored mark has to be rewritten by hand.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.models.ingest_watermark import IngestWatermark

logger = get_logger(__name__)

# SAP posts in its own zone, ahead of UTC (SAST, +2). Late in the UTC evening
# SAP's "today" is already tomorrow, so that is the latest day a mark can
# honestly be. Anything later is a bad value, and a mark taken from it would
# start the next window in the future and skip every change until then.
_LATEST_PLAUSIBLE = timedelta(days=1)


def as_day(value: Any) -> date | None:
    """The SAP calendar day a delta-field value stands for, or None.

    A timezone-aware moment is SAP-local midnight seen from UTC, so the day is
    the NEAREST midnight, not the UTC date: 22:00 UTC on the 14th is the 15th.
    A naive moment is already a calendar day. None, blanks and SAP's
    ``00000000`` ("no date") are None, as is anything not recognisably a date
    -- never guessed.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return (value.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(hours=12)).date()
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text or set(text) <= {"0", ".", "-"}:
        return None
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        if len(text) == 10 and text[2] == "." and text[5] == ".":
            return datetime.strptime(text, "%d.%m.%Y").date()
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return date.fromisoformat(text)
        return as_day(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def highest(rows: list[dict], field: str, *, today: date | None = None) -> str | None:
    """The latest day ``field`` holds across these rows, as ``YYYY-MM-DD``.

    Days, not text: see the module docstring. A value later than SAP's today
    is skipped and logged rather than allowed to become the mark.
    """
    ceiling = (today or datetime.now(timezone.utc).date()) + _LATEST_PLAUSIBLE
    best: date | None = None
    future = 0
    for row in rows:
        day = as_day(row.get(field))
        if day is None:
            continue
        if day > ceiling:
            future += 1
            continue
        if best is None or day > best:
            best = day
    if future:
        logger.warning(
            "%s: %d value(s) later than %s skipped when taking the mark",
            field, future, ceiling.isoformat(),
        )
    return best.isoformat() if best else None


def get_watermark(entity_set: str, field: str) -> str | None:
    """The mark for this set, as ``YYYY-MM-DD``, or None if there is none to use.

    Returns None when the stored mark was measured against a *different*
    field, or cannot be read as a day. A date compared against a position
    recorded in document numbers is silently nonsense, and the safe reading of
    "I do not understand this mark" is "pull everything".
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
        day = as_day(row.value)
        if day is None:
            logger.warning(
                "%s: stored watermark %r is not a date. Ignoring it and pulling "
                "in full; the mark will be rewritten.",
                entity_set,
                row.value,
            )
            return None
        return day.isoformat()


def set_watermark(entity_set: str, field: str, value: str, rows: int) -> None:
    """Record how far this set has been pulled. ``value`` is stored as a day."""
    day = as_day(value)
    if day is None:
        raise ValueError(f"{entity_set}.{field}: {value!r} is not a date, refusing to store it as a mark")
    stored = day.isoformat()
    session_factory = get_sessionmaker()
    with session_factory() as session:
        row = session.get(IngestWatermark, entity_set)
        if row is None:
            row = IngestWatermark(entity_set=entity_set, field=field, value=stored)
            session.add(row)
        row.field = field
        row.value = stored
        row.rows_last_run = rows
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
    logger.info("%s: watermark advanced to %s=%s", entity_set, field, stored)
