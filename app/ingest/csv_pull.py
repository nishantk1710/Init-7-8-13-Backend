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

WHY THE WHOLE SWEEP IS FIRED AT ONCE

Serialised -- fire one, wait for it, fire the next -- this route delivered
nothing, a dozen attempts running. Fired as a batch it has never failed:

    21 tables, 3s apart, twice from a laptop   38 deliveries
    3 tables, 5s apart                          3 deliveries
    2 tables, 20s apart                         2 deliveries
    1 at a time, waiting in between             0 deliveries, every time

The request id, the date window, ``MaxRows``, the preceding ``$count``, the
transport class and the calling host were each varied on their own and each
cleared. What is left is the firing pattern, and the evidence for it is 43
deliveries against a dozen silences.

Serialising was never actually required. It existed because the chunks SAP
pushes carry no request id, no sequence number and no total -- so the receiver
attributed each one to whichever request was OPEN, which is sound only while
exactly one is. But a chunk DOES carry a header naming its table, and
``csv_upload.table_of`` resolves all 21 from it. Attribution by table needs no
serialisation: several tables may be in flight together, and only a second
request for the SAME table is ambiguous, because those two would be
indistinguishable. ``fire`` refuses that one case and nothing else.

THE ACKNOWLEDGEMENT PROVES NOTHING

``Success: Table EKPO extraction started in background chunks of 50,000`` comes
back whether or not a single row will follow. We have fired requests that
returned exactly that and delivered nothing. Completion is decided by what
lands, never by what the trigger said.
"""

from __future__ import annotations

import threading
import time
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
    TERMINAL_STATUSES,
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


# RequestId SHAPE. Every id that ever failed and every id that delivered,
# 25-Sep, with the window, cap and transport held constant in each probe:
#
#   delivered   REQ163623 (9)  P17394600 (9)  FMARA224319 (11)  X1790357579 (11)
#               FEINA90357571 (13)  F<TABLE>224559 x 17 (11-12)
#   silent      MARA8XR2WS (10)  MAKTVO2QGB (10)  MARA55AD19FF (12)
#               FEINA790357145 (14)  FEINA790357363 (14)
#               MARA00A78AE3BE9D (16)  EKPOC4C06FFCD82D (16)
#
# Two rules, each shown by a side-by-side on the same table:
#
#   * TOTAL LENGTH OF AT MOST 13. FEINA90357571 delivered in six seconds;
#     FEINA790357145 and FEINA790357363, one character longer, never did.
#     $metadata declares MaxLength=20; that is the field, not the rule.
#   * LETTERS THEN DIGITS, and not the bare table name in front. MARA8XR2WS
#     and MAKTVO2QGB are short enough and never delivered; ids that mixed
#     letters back in after the digits never did either. Ten digits after the
#     letter are fine (X1790357579), so it is the mixing, not a digit count.
#
# Both failures look identical from here: SAP acknowledges and sends nothing,
# and the pull reports a 15-minute timeout, not an error.
REQUEST_ID_MAX = 13

# Epoch seconds modulo 10^8: unique per second, cycle of 3.17 years.
REQUEST_ID_DIGITS = 8

# Letters first. Anything but the table name itself, and not a digit.
_ID_PREFIX = "F"

# 1 + 4 + 8 = 13. CDHDR and CDPOS lose their fifth letter; FCDHD and FCDPO
# still tell them apart, and the row records the full table name anyway.
_TABLE_LETTERS = REQUEST_ID_MAX - len(_ID_PREFIX) - REQUEST_ID_DIGITS

# Last second issued per table, so a burst within one second stays unique.
_issued: dict[str, int] = {}
_issued_lock = threading.Lock()


def new_request_id(sap_table: str, *, now: float | None = None) -> str:
    """``F<TABL><8 digits>`` -- thirteen characters, letters then digits.

    Unique because SAP dedupes: refiring a used id returns the same cheerful
    acknowledgement and sends nothing, exactly like a mis-shaped one. The
    digits are epoch seconds, so a scheduled sweep firing at the same
    wall-clock second every day still gets a fresh id. Within one sweep the
    table letters keep the ids apart. Across processes, two fires of ONE
    table in ONE second would collide, and fire() refuses a second open
    request for a table.
    """
    table = sap_table.upper()[:_TABLE_LETTERS]
    seconds = int(time.time() if now is None else now) % 10**REQUEST_ID_DIGITS
    with _issued_lock:
        # Never re-issue a (table, second) this process has already used: a
        # second fire of one table inside the same second steps forward one
        # second instead of colliding. Ids may run marginally ahead of the
        # clock under a burst; they only ever have to be unique and shaped.
        last = _issued.get(table)
        if last is not None and seconds <= last:
            seconds = last + 1
        _issued[table] = seconds
    return f"{_ID_PREFIX}{table}{seconds:0{REQUEST_ID_DIGITS}d}"


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


def open_request(
    session, sap_table: str | None = None
) -> CsvExtractRequest | None:
    """The extract in flight for one table, or for any table at all.

    Per table by default in the callers that matter, because the sweep now
    fires all 21 together. Two requests for the SAME table are genuinely
    inseparable -- their chunks carry identical headers and nothing else --
    but two for different tables are told apart by that header.
    """
    query = select(CsvExtractRequest).where(CsvExtractRequest.status == STATUS_OPEN)
    if sap_table is not None:
        query = query.where(CsvExtractRequest.sap_table == sap_table)
    return session.scalars(query.order_by(CsvExtractRequest.fired_at)).first()


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
        # Only this table blocks. Another table being in flight is fine --
        # its chunks name themselves in their header.
        blocking = open_request(session, spec.sap_table)
        if blocking is not None:
            detail = (
                f"{spec.sap_table} already has request {blocking.request_id} "
                f"open, fired {blocking.fired_at:%Y-%m-%d %H:%M}Z. Two extracts "
                "of one table push chunks with identical headers and nothing to "
                "tell them apart. Wait for it, or close it with --abandon."
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

            if record.status not in TERMINAL_STATUSES:
                _judge(
                    record,
                    datetime.now(timezone.utc),
                    first_chunk_timeout,
                    quiet_period,
                )
                session.commit()

            if record.status in TERMINAL_STATUSES:
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


def wait_for_clear(
    tables: list[str] | None = None,
    *,
    timeout: int | None = None,
    poll: int = POLL_SECONDS,
) -> bool:
    """Block until nothing is open for ``tables``. True if the way is clear.

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

    wanted = set(tables) if tables else None

    while True:
        with sessionmaker() as session:
            blocking = None
            for candidate in session.scalars(
                select(CsvExtractRequest)
                .where(CsvExtractRequest.status == STATUS_OPEN)
                .order_by(CsvExtractRequest.fired_at)
            ):
                if wanted is None or candidate.sap_table in wanted:
                    blocking = candidate
                    break
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


# Seconds between fires within a batch. Every batch that delivered used 2-20s.
# Nothing suggests the exact value matters; the gap exists so 21 requests do
# not arrive at CPI as a single burst.
FIRE_GAP_SECONDS = 3


def pull_all(
    *,
    max_rows: str = "",
    tables: list[str] | None = None,
    wait: bool = True,
    wait_for_open: bool = True,
    gap: int = FIRE_GAP_SECONDS,
    sleeper=time.sleep,
    **window,
) -> list[PullResult]:
    """Fire every table, then collect whatever comes back.

    Fired as a batch, not one at a time -- see the module docstring. A table
    that fails does not stop the run: the others are independent, and a partial
    refresh beats no refresh.
    """
    names = tables or [t.sap_table for t in CSV_TABLES]

    if wait_for_open and not wait_for_clear(names):
        detail = (
            "an earlier extract is still open and did not clear. Nothing was "
            "fired. Run --csv-status to see it, then --abandon to close it."
        )
        logger.error(detail)
        return [PullResult(name, "", STATUS_FAILED, error=detail) for name in names]

    logger.info("firing %d extract(s), %ds apart", len(names), gap)
    fired: list[PullResult] = []
    for index, name in enumerate(names, 1):
        logger.info("[%d/%d] %s", index, len(names), name)
        result = fire(name, max_rows=max_rows, **window)
        fired.append(result)
        if result.status != STATUS_OPEN:
            logger.error("%s: %s -- %s", name, result.status, result.error or "")
        elif index < len(names):
            sleeper(gap)

    accepted = [r.request_id for r in fired if r.status == STATUS_OPEN]
    if not wait:
        # Fired and left open on purpose: the deliveries are being watched
        # somewhere else -- a receiver's own logs, or --csv-status later.
        logger.info("%d of %d accepted; not waiting", len(accepted), len(names))
        return fired

    logger.info("%d of %d accepted; now waiting for deliveries",
                len(accepted), len(names))
    if not accepted:
        return fired

    collected = {r.request_id: r for r in wait_for_all(accepted, sleeper=sleeper)}
    return [collected.get(r.request_id, r) for r in fired]


def wait_for_all(
    request_ids: list[str],
    *,
    first_chunk_timeout: int = FIRST_CHUNK_TIMEOUT_SECONDS,
    quiet_period: int = QUIET_PERIOD_SECONDS,
    poll: int = POLL_SECONDS,
    sleeper=time.sleep,
) -> list[PullResult]:
    """Wait on a whole batch, judging each request by the clocks wait_for uses.

    One shared poll, not one wait per table. Waiting on each in turn would
    charge a five-minute quiet period per table and stretch a 21-table sweep
    past two hours, when the deliveries themselves overlap.
    """
    sessionmaker = get_sessionmaker()
    pending = list(request_ids)
    done: dict[str, PullResult] = {}
    started = time.monotonic()

    while pending:
        still_pending: list[str] = []
        with sessionmaker() as session:
            now = datetime.now(timezone.utc)
            for request_id in pending:
                record = session.get(CsvExtractRequest, request_id)
                if record is None:
                    done[request_id] = PullResult(
                        "", request_id, STATUS_FAILED,
                        error=f"request {request_id} is not in the database",
                    )
                    continue

                if record.status not in TERMINAL_STATUSES:
                    _judge(record, now, first_chunk_timeout, quiet_period)

                if record.status in TERMINAL_STATUSES:
                    done[request_id] = _result(record, time.monotonic() - started)
                else:
                    still_pending.append(request_id)
            session.commit()

        pending = still_pending
        if pending:
            waiting = ", ".join(sorted(pending)[:6])
            logger.info(
                "waiting on %d extract(s): %s%s",
                len(pending), waiting, " ..." if len(pending) > 6 else "",
            )
            sleeper(poll)

    return [done[r] for r in request_ids if r in done]


def _judge(
    record: CsvExtractRequest,
    now: datetime,
    first_chunk_timeout: int,
    quiet_period: int,
) -> None:
    """Close the record if either clock has run out. Caller commits."""
    last = _aware(record.last_chunk_at)

    if last is None:
        waited = (now - _aware(record.fired_at)).total_seconds()
        if waited <= first_chunk_timeout:
            return
        detail = (
            f"no chunk arrived in {int(waited // 60)} minutes. SAP "
            f"acknowledged the request ({record.ack or 'no ack recorded'}) "
            "but delivered nothing -- the usual causes are a reused "
            "RequestId or a blank date window. Neither reports an error."
        )
        record.status = STATUS_TIMEOUT
        record.error = detail
        record.completed_at = now
        logger.error("%s: %s", record.sap_table, detail)
        return

    if (now - last).total_seconds() <= quiet_period:
        return

    verdict, detail = _verdict(record)
    record.status = verdict
    record.error = detail
    record.completed_at = now
    log = logger.info if verdict == STATUS_COMPLETE else logger.error
    log(
        "%s: delivery finished -- %d row(s) in %d chunk(s); %s",
        record.sap_table, record.received_rows,
        record.received_chunks, detail or "reconciled",
    )
