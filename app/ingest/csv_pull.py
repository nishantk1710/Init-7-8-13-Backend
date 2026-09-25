"""Stage zero: ask SAP for a full table over the CSV extract route.

    fire()  ->  SAP acknowledges  ->  SAP pushes chunks to /api/events/csv
                                  ->  csv_upload appends them into one file
                                  ->  wait_for() sees the row go COMPLETE
                                  ->  csv_load files it into Azure SQL

THE REQUEST SHAPE, EXACTLY AS MEASURED

    sap/opu/odata/SAP/ZMM_GET_CSV_SRV/TableExtractSet(RequestId='...',
        TabName='EKPO',FromDate='20130101',ToDate='20131231',
        IsDelta='',MaxRows='')/$value

Four details in that string are load-bearing, all of them learned the hard way:

* ``/SAP/`` is upper case in this service's path, unlike ``/sap/`` everywhere
  else. The working call used it; nothing has tested the lower-case form.
* Dates are plain ``yyyymmdd`` DATS strings -- NOT ``datetime'...'``. These are
  key predicates, so the OData literal grammar that governs ``$filter`` does
  not apply here.
* ``IsDelta`` is sent empty. The field exists and may well work, but we have
  never set it, and a full pull is not the place to find out.
* ``RequestId`` must be unique. Refiring a used one returns the same cheerful
  acknowledgement and delivers nothing at all.

WHY ONE AT A TIME

The chunks SAP pushes carry no request id, no sequence number and no total.
The receiver attributes them to whichever request is OPEN. That is sound only
while exactly one is -- so ``fire`` refuses to start a second extract while any
row is still open, rather than letting two tables interleave into one file with
no way to separate them afterwards.

THE ACKNOWLEDGEMENT PROVES NOTHING

``Success: Table EKPO extraction started in background chunks of 50,000`` comes
back whether or not a single row will follow. We have fired requests that
returned exactly that and delivered nothing. Completion is decided by what
lands, never by what the trigger said.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.ingest.csv_tables import CsvTable, CSV_TABLES, csv_table
from app.integrations.sap.client import SapClient
from app.integrations.sap.errors import SapError
from app.integrations.sap.transport import CpiTransport
from app.models.csv_extract import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_OPEN,
    STATUS_TIMEOUT,
    CsvExtractRequest,
)

logger = get_logger(__name__)

CSV_SERVICE = "ZMM_GET_CSV_SRV"

# How long to wait for the first chunk before calling the extract dead. SAP
# starts a background job, so the gap is real -- the 23-Sep EKPO delivery
# landed in about a minute.
FIRST_CHUNK_TIMEOUT_SECONDS = 15 * 60

# How long after the LAST chunk to declare delivery finished. Nothing in the
# protocol says "that was the final chunk", so silence is the only signal
# available. Generous: a 50,000-row chunk of a 277-column table is ~64 MB.
QUIET_PERIOD_SECONDS = 5 * 60

POLL_SECONDS = 15


@dataclass
class PullResult:
    sap_table: str
    request_id: str
    status: str
    received_rows: int = 0
    received_chunks: int = 0
    expected_rows: int | None = None
    data_key: str | None = None
    seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETE


# RequestId length. $metadata declares MaxLength=20 and that is NOT the working
# limit: measured 25-Sep, every id of 16 characters acknowledged and delivered
# nothing, while every id of 9 to 11 characters delivered in seconds.
#
#   REQ163623         9   delivered
#   P17394600..20     9   delivered, all 21 tables
#   FMARA191610..    11   delivered, all 17 tables that answered
#   EKPO518A05164052 16   ack, no data
#   MARA00A78AE3BE9D 16   ack, no data
#   EKPOC4C06FFCD82D 16   ack, no data
#
# The declared maximum is therefore wrong, or something downstream truncates
# and then fails to match its own key. Twelve leaves headroom under the
# shortest failure seen without crowding the collision space.
REQUEST_ID_MAX = 12


def new_request_id(sap_table: str) -> str:
    """Unique per fire, and short enough that SAP actually honours it.

    Unique because SAP dedupes: refiring a used id returns the same cheerful
    acknowledgement and sends nothing. Short because of REQUEST_ID_MAX above --
    this was the cause of every failed pull on 25-Sep, and it reports as a
    15-minute timeout rather than as an error.
    """
    prefix = sap_table[:4].upper()
    suffix = uuid.uuid4().hex[: REQUEST_ID_MAX - len(prefix)].upper()
    return f"{prefix}{suffix}"


def extract_path(
    request_id: str,
    sap_table: str,
    from_date: str,
    to_date: str,
    max_rows: str = "",
) -> str:
    """The APIPath for one extract request. See the module docstring."""
    key = (
        f"TableExtractSet(RequestId='{request_id}',TabName='{sap_table}',"
        f"FromDate='{from_date}',ToDate='{to_date}',"
        f"IsDelta='',MaxRows='{max_rows}')"
    )
    return f"sap/opu/odata/SAP/{CSV_SERVICE}/{key}/$value"


def open_request(session) -> CsvExtractRequest | None:
    """The extract currently in flight, if any."""
    return session.scalars(
        select(CsvExtractRequest).where(CsvExtractRequest.status == STATUS_OPEN)
    ).first()


def _expected_rows(spec: CsvTable, client: SapClient | None) -> int | None:
    """Interface 1's $count for the same data, taken before firing.

    None is a normal answer, not a failure: several sets answer HTTP 500 to
    $count while serving rows fine, and one is deliberately not asked at all.
    A None here weakens the completeness check to "something arrived" -- which
    is recorded rather than hidden, so nobody mistakes it for a clean bill.
    """
    if client is None:
        return None
    try:
        return client.count(spec.entity_set)
    except SapError as exc:
        logger.warning(
            "%s: no $count for %s (%s); completeness will be unverified",
            spec.sap_table, spec.entity_set, exc,
        )
        return None


def fire(
    sap_table: str,
    *,
    max_rows: str = "",
    today: date | None = None,
    years: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    transport: CpiTransport | None = None,
    client: SapClient | None = None,
) -> PullResult:
    """Record the request, then ask SAP for it. Never raises."""
    spec = csv_table(sap_table)
    from_date, to_date = spec.window(
        today, years=years, from_date=from_date, to_date=to_date
    )
    started = time.monotonic()
    sessionmaker = get_sessionmaker()

    with sessionmaker() as session:
        blocking = open_request(session)
        if blocking is not None:
            detail = (
                f"{blocking.sap_table} (request {blocking.request_id}) is still "
                f"open, fired {blocking.fired_at:%Y-%m-%d %H:%M}Z. Chunks carry "
                "no request id, so a second extract now would interleave with "
                "it and neither could be separated afterwards. Wait for it, or "
                "close it with --abandon."
            )
            logger.error("%s: refusing to fire -- %s", spec.sap_table, detail)
            return PullResult(spec.sap_table, "", STATUS_FAILED, error=detail)

        request_id = new_request_id(spec.sap_table)
        expected = _expected_rows(spec, client or _default_client())

        # Written BEFORE the fire. A chunk that beats its own row cannot be
        # attributed to anything.
        record = CsvExtractRequest(
            request_id=request_id,
            sap_table=spec.sap_table,
            entity_set=spec.entity_set,
            from_date=from_date,
            to_date=to_date,
            max_rows=max_rows,
            status=STATUS_OPEN,
            reconcile=spec.reconcile.value,
            expected_rows=expected,
        )
        session.add(record)
        session.commit()

    path = extract_path(request_id, spec.sap_table, from_date, to_date, max_rows)
    logger.info(
        "%s: firing %s (window %s..%s, expecting %s rows)",
        spec.sap_table, request_id, from_date, to_date,
        expected if expected is not None else "an unverified number of",
    )
    # The whole APIPath, verbatim. Diagnosing a silent non-delivery means
    # comparing what we sent against a request known to have worked, and
    # reconstructing it from separate log fields wastes the one thing that
    # matters here -- being able to see the difference at a glance.
    logger.info("%s: APIPath %s", spec.sap_table, path)

    try:
        ack = (transport or CpiTransport()).get(path, context=f"csv extract {spec.sap_table}")
    except Exception as exc:
        detail = f"the trigger call failed: {exc}"
        _close(request_id, STATUS_FAILED, error=detail)
        logger.error("%s: %s", spec.sap_table, detail)
        return PullResult(
            spec.sap_table, request_id, STATUS_FAILED,
            expected_rows=expected, seconds=time.monotonic() - started, error=detail,
        )

    ack_text = (ack or "").strip()[:500]
    with sessionmaker() as session:
        record = session.get(CsvExtractRequest, request_id)
        if record is not None:
            record.ack = ack_text
            session.commit()

    logger.info("%s: SAP acknowledged -- %s", spec.sap_table, ack_text or "(empty)")
    return PullResult(
        spec.sap_table, request_id, STATUS_OPEN,
        expected_rows=expected, seconds=time.monotonic() - started,
    )


def _default_client() -> SapClient | None:
    try:
        return SapClient()
    except Exception as exc:  # configuration, not data
        logger.warning("no SAP client for the $count check: %s", exc)
        return None


def wait_for(
    request_id: str,
    *,
    first_chunk_timeout: int = FIRST_CHUNK_TIMEOUT_SECONDS,
    quiet_period: int = QUIET_PERIOD_SECONDS,
    poll: int = POLL_SECONDS,
    sleeper=time.sleep,
) -> PullResult:
    """Block until the extract completes, times out, or is declared dead.

    Two clocks, because two things can go wrong and they need different
    answers. Nothing at all within ``first_chunk_timeout`` means the extract
    never started -- almost always a reused RequestId or a blank date window.
    A gap of ``quiet_period`` after rows HAVE arrived means delivery finished,
    since no chunk is ever marked as the last one.
    """
    sessionmaker = get_sessionmaker()
    started = time.monotonic()

    while True:
        with sessionmaker() as session:
            record = session.get(CsvExtractRequest, request_id)
            if record is None:
                return PullResult(
                    "", request_id, STATUS_FAILED,
                    error=f"request {request_id} is not in the database",
                )

            if record.status in (STATUS_COMPLETE, STATUS_FAILED, STATUS_TIMEOUT):
                return _result(record, time.monotonic() - started)

            now = datetime.now(timezone.utc)
            fired_at = _aware(record.fired_at)
            last = _aware(record.last_chunk_at)

            if last is None:
                waited = (now - fired_at).total_seconds()
                if waited > first_chunk_timeout:
                    detail = (
                        f"no chunk arrived in {int(waited // 60)} minutes. SAP "
                        f"acknowledged the request ({record.ack or 'no ack recorded'}) "
                        "but delivered nothing -- the usual causes are a reused "
                        "RequestId or a blank date window. Neither reports an error."
                    )
                    record.status = STATUS_TIMEOUT
                    record.error = detail
                    record.completed_at = now
                    session.commit()
                    logger.error("%s: %s", record.sap_table, detail)
                    return _result(record, time.monotonic() - started)

            elif (now - last).total_seconds() > quiet_period:
                verdict, detail = _verdict(record)
                record.status = verdict
                record.error = detail
                record.completed_at = now
                session.commit()
                log = logger.info if verdict == STATUS_COMPLETE else logger.error
                log(
                    "%s: delivery finished -- %d row(s) in %d chunk(s); %s",
                    record.sap_table, record.received_rows,
                    record.received_chunks, detail or "reconciled",
                )
                return _result(record, time.monotonic() - started)

        sleeper(poll)


def _verdict(record: CsvExtractRequest) -> tuple[str, str | None]:
    """Did enough arrive to trust the file? See csv_tables.Reconcile."""
    received, expected = record.received_rows, record.expected_rows

    if received <= 0:
        return STATUS_FAILED, "chunks arrived but carried no data rows"

    if expected is None:
        return STATUS_COMPLETE, (
            "no $count was available for this set, so the row count is "
            "unverified -- the file is loadable but its completeness is not proven"
        )

    # A capped request cannot be expected to match the whole table, whatever
    # the set's usual rule. MaxRows is a probe -- asking for 100 rows of a
    # 2,040-row master table and then failing it for returning 100 would call
    # a working delivery broken, which is worse than not checking at all.
    cap = (record.max_rows or "").strip()
    if cap.isdigit() and int(cap) > 0:
        limit = int(cap)
        if received > limit:
            return STATUS_FAILED, (
                f"{received} row(s) landed against a cap of MaxRows={limit}. "
                "More arrived than was asked for -- chunks were delivered twice."
            )
        return STATUS_COMPLETE, (
            f"capped at MaxRows={limit}, so this is a probe and not a whole "
            f"extract; the set holds {expected:,} row(s)"
        )

    if record.reconcile == "exact":
        if received == expected:
            return STATUS_COMPLETE, None
        return STATUS_FAILED, (
            f"{received} row(s) landed against a $count of {expected}. This is a "
            "whole-table extract, so those must match; a short file means chunks "
            "were lost and loading it would look complete and not be."
        )

    # Windowed: the extract is a subset by design, so the count is a ceiling.
    if received > expected:
        return STATUS_FAILED, (
            f"{received} row(s) landed against a $count of {expected} for the "
            "whole set. A three-year window cannot return more than the table "
            "holds -- chunks were most likely delivered twice."
        )
    return STATUS_COMPLETE, None


def _aware(value: datetime | None) -> datetime | None:
    """SQL Server hands back naive datetimes; comparisons need tz-aware ones."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _result(record: CsvExtractRequest, seconds: float) -> PullResult:
    return PullResult(
        record.sap_table,
        record.request_id,
        record.status,
        received_rows=record.received_rows,
        received_chunks=record.received_chunks,
        expected_rows=record.expected_rows,
        data_key=record.data_key,
        seconds=seconds,
        error=record.error,
    )


def _close(request_id: str, status: str, *, error: str | None = None) -> None:
    sessionmaker = get_sessionmaker()
    with sessionmaker() as session:
        record = session.get(CsvExtractRequest, request_id)
        if record is None:
            return
        record.status = status
        record.error = error
        record.completed_at = datetime.now(timezone.utc)
        session.commit()


def abandon_open(reason: str = "abandoned by operator") -> int:
    """Close any open request so a new extract can be fired. Returns how many."""
    sessionmaker = get_sessionmaker()
    with sessionmaker() as session:
        rows = list(
            session.scalars(
                select(CsvExtractRequest).where(CsvExtractRequest.status == STATUS_OPEN)
            )
        )
        for record in rows:
            record.status = STATUS_FAILED
            record.error = reason
            record.completed_at = datetime.now(timezone.utc)
        session.commit()
        for record in rows:
            logger.warning("abandoned %s (%s)", record.request_id, record.sap_table)
        return len(rows)


def pull_one(
    sap_table: str,
    *,
    max_rows: str = "",
    wait: bool = True,
    **kwargs,
) -> PullResult:
    """Fire one table and, unless told not to, wait for it to land."""
    fired = fire(sap_table, max_rows=max_rows, **kwargs)
    if not wait or fired.status != STATUS_OPEN:
        return fired
    return wait_for(fired.request_id)


def wait_for_clear(*, timeout: int | None = None, poll: int = POLL_SECONDS) -> bool:
    """Block until no extract is open. True if the way is clear.

    A sweep that finds a request already open used to fail all 21 tables in
    under a second -- one refusal per table, none of them the real problem, and
    the actual cause buried at the top. An earlier run still finishing is a
    reason to wait, not to give up: waiting costs minutes, and the alternative
    is a report that reads like total failure when nothing is wrong.

    The wait is bounded by the same clocks the delivery itself runs on, so a
    genuinely stuck request still ends the sweep rather than holding it open
    for ever.
    """
    limit = timeout if timeout is not None else FIRST_CHUNK_TIMEOUT_SECONDS + QUIET_PERIOD_SECONDS
    sessionmaker = get_sessionmaker()
    started = time.monotonic()
    announced = False

    while True:
        with sessionmaker() as session:
            blocking = open_request(session)
            if blocking is None:
                return True
            request_id, table = blocking.request_id, blocking.sap_table

        if not announced:
            logger.info(
                "waiting for %s (request %s) to finish before starting the sweep",
                table, request_id,
            )
            announced = True

        if time.monotonic() - started > limit:
            logger.error(
                "%s (request %s) is still open after %d minute(s). Close it with "
                "--abandon, then re-run.",
                table, request_id, int(limit // 60),
            )
            return False

        # Let the open request run its own clocks out; wait_for closes it as
        # timed out or complete, which clears the way here.
        wait_for(request_id)

    return False


def pull_all(
    *,
    max_rows: str = "",
    tables: list[str] | None = None,
    wait_for_open: bool = True,
    **window,
) -> list[PullResult]:
    """Every table in turn, strictly one at a time.

    Sequential by necessity, not by caution: see the module docstring. A table
    that fails does not stop the run -- the others are independent, and a
    partial refresh beats no refresh.
    """
    names = tables or [t.sap_table for t in CSV_TABLES]

    if wait_for_open and not wait_for_clear():
        detail = (
            "an earlier extract is still open and did not clear. Nothing was "
            "fired. Run --csv-status to see it, then --abandon to close it."
        )
        logger.error(detail)
        return [PullResult(name, "", STATUS_FAILED, error=detail) for name in names]

    results: list[PullResult] = []
    for index, name in enumerate(names, 1):
        logger.info("[%d/%d] %s", index, len(names), name)
        result = pull_one(name, max_rows=max_rows, **window)
        results.append(result)
        if not result.ok:
            # fire() and wait_for() have already logged the detail; repeating
            # the whole message per table is what made one blocked sweep print
            # the same paragraph twenty-one times.
            logger.error("%s: %s", name, result.status)
    return results
