"""Tally an arriving CSV chunk against the extract request that asked for it.

Kept apart from the endpoint for one reason: **the endpoint must never fail
because of this.** ``/api/events/csv`` exists so SAP's push is never refused
over its packaging; refusing it because our own database is unreachable would
be the same mistake with a different cause. Every function here swallows its
failures and says so in the log. SAP gets its 202 regardless.

The attribution rule is the one ``csv_pull`` enforces from the other side:
exactly one request is OPEN at a time, so an arriving chunk belongs to it. The
chunk itself carries nothing -- no request id, no sequence number, no total.

A chunk that arrives with no request open is still written to storage by the
caller; it simply has nothing to be counted against. That is logged as a
warning rather than dropped, because the bytes are real and somebody will want
to know where they came from.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.models.csv_extract import STATUS_OPEN, CsvExtractRequest

logger = get_logger(__name__)

# Everything the CSV route writes lives under this prefix inside STORAGE_URL,
# which points at the `landing` container root.
CSV_PREFIX = "csv"


def _open_request_id(table: str) -> str | None:
    """The id of the request currently collecting chunks for ``table``."""
    try:
        with get_sessionmaker()() as session:
            record = session.scalars(
                select(CsvExtractRequest)
                .where(
                    CsvExtractRequest.status == STATUS_OPEN,
                    CsvExtractRequest.sap_table == table,
                )
                .order_by(CsvExtractRequest.fired_at.desc())
            ).first()
            return record.request_id if record else None
    except Exception:
        logger.exception("could not look up the open request for %s", table)
        return None


def landing_keys(table: str) -> tuple[str, str]:
    """``(data_key, header_key)`` for this table's chunks.

    Scoped to the REQUEST, not to the calendar day. A date-keyed path split an
    extract that ran across midnight UTC into two files -- and since the row
    tally lives in the database and counted every chunk, reconciliation passed
    while the loader read only the first file. A short load that reports
    success is the failure this whole route is built to avoid.

    Request scoping also keeps a retry separate. A retried pull must carry a
    new RequestId because SAP dedupes on it, so a same-day date key would have
    appended two different requests into one file with no way to tell them
    apart afterwards.

    The dated fallback is for a chunk that arrives with nothing open. Those
    bytes are real and are kept rather than dropped; the log says they are
    unattributed.
    """
    request_id = _open_request_id(table)
    if request_id is None:
        stamp = date.today().strftime("%Y-%m-%d")
        folder = f"{CSV_PREFIX}/{table}/unattributed-{stamp}"
    else:
        folder = f"{CSV_PREFIX}/{table}/{request_id}"
    return f"{folder}/{table}.csv", f"{folder}/_header.csv"


def record_chunk(table: str, data_key: str, *, rows: int, raw_bytes: int) -> None:
    """Add one chunk to the open request's running totals. Never raises."""
    try:
        sessionmaker = get_sessionmaker()
    except Exception as exc:
        logger.warning("no database for chunk accounting (%s); bytes are landed", exc)
        return

    try:
        with sessionmaker() as session:
            record = session.scalars(
                select(CsvExtractRequest)
                .where(CsvExtractRequest.status == STATUS_OPEN)
                .order_by(CsvExtractRequest.fired_at.desc())
            ).first()

            if record is None:
                logger.warning(
                    "chunk for %s landed at %s with no extract request open -- "
                    "the bytes are stored but nothing is tracking completeness",
                    table, data_key,
                )
                return

            if record.sap_table != table:
                # The header said one table, the open request asked for another.
                # Counting it would corrupt the tally the completeness check
                # rests on, so it is refused loudly and the bytes left alone.
                logger.error(
                    "chunk header says %s but the open request %s asked for %s. "
                    "Not counting it: two extracts in flight would explain this, "
                    "and csv_pull is meant to make that impossible.",
                    table, record.request_id, record.sap_table,
                )
                return

            record.received_rows += max(rows, 0)
            record.received_chunks += 1
            record.received_bytes += max(raw_bytes, 0)
            record.last_chunk_at = datetime.now(timezone.utc)
            if record.data_key is None:
                record.data_key = data_key
            session.commit()

            logger.info(
                "%s: chunk %d recorded, %d row(s) so far%s",
                record.sap_table,
                record.received_chunks,
                record.received_rows,
                f" of {record.expected_rows}" if record.expected_rows else "",
            )
    except Exception:
        logger.exception(
            "could not record the chunk for %s; the bytes are landed at %s",
            table, data_key,
        )
