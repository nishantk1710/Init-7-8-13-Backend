"""CSV feed listener.

SAP will post CSV here. Which extract, with which columns, is not agreed yet
-- so this endpoint reads whatever arrives, reports what it understood, and
accepts it. It is the CSV counterpart of the PR event stub next door.

WHY THIS IS QUITE SO DEFENSIVE

The PR endpoint rejected live SAP events with 422 because ABAP's HTTP client
sends ``text/plain`` and FastAPI parses the body before the model is consulted.
The body was fine; a header refused it. CSV has strictly more ways to fail that
way than JSON does -- delimiter, encoding, byte order mark, line endings,
ragged rows, a field longer than the csv module's default limit -- and every
one of them is a way to refuse an extract over its packaging rather than its
contents.

So nothing here is assumed. The content type is not consulted, the delimiter is
sniffed, the encoding is detected, ragged rows are kept, and the only body that
is refused is one with nothing in it. What was understood comes back in the
response, so SAP can confirm the reading without asking us for logs.

Still a connectivity stub in what it *does*: it reads and reports. It does not
keep the file, validate against a schema, or route anywhere. The sha256 of the
bytes comes back so a re-send can be recognised as the same file.
"""

import asyncio
import codecs
import csv
import hashlib
import io
import re
import time
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.storage import get_storage
from app.ingest.csv_receipt import landing_keys, record_chunk

logger = get_logger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

# A field longer than this is still read. The csv module's default ceiling is
# 128 KB and it raises rather than truncating, which would turn one long free
# text column into a refused extract.
csv.field_size_limit(16 * 1024 * 1024)

# Enough of a body to identify the sender and the format, not so much that one
# bad post floods the log.
MAX_LOGGED_BODY_BYTES = 2_000

# What the delimiter sniffer looks at, and what it may choose. Kept narrow: SAP
# exports are comma, semicolon, tab or pipe, and a wider set makes the sniffer
# confidently wrong on short files.
SNIFF_BYTES = 16_384
CANDIDATE_DELIMITERS = ",;\t|"

# Which table a chunk belongs to, read from its header row.
#
# Nothing else in the request says. The push carries no table name, no request
# id and no sequence number -- the only thing that distinguishes an EKPO chunk
# from an MSEG one is the columns. Matched on the leading key fields, longest
# first, because EKKO's key is a prefix of EKPO's and MARA's of MARC's.
#
# A heuristic, and replaceable: if SAP can be persuaded to send the table name
# as a header or a query parameter, that becomes the source and this becomes a
# fallback. Until then an unrecognised header lands under a stable fingerprint
# rather than being guessed at or refused.
#
# TWO LAYOUTS, ONE SET OF SIGNATURES
#
# Until 30-Sep every table arrived WIDE: the full SAP table, the client column
# first (MANDT; MANDANT for change documents). Since SAP's 30-Sep rebuild some
# tables arrive NARROW: exactly the OData projection, no client column at all --
# MARA as MATNR,MTART,..., EKKO as EBELN,BSART,..., EKET as EBELN,EBELP,ETENR,...
# Signatures written with MANDT matched none of those, so they landed as
# UNKNOWN_<hash>, untracked, and their requests timed out. More tables are
# moving to the narrow layout, so the client column is stripped BEFORE
# matching and every signature below is written without it: one rule for both
# layouts, and a table converted tomorrow keeps resolving to its own name.
CLIENT_COLUMNS = ("MANDT", "MANDANT")

TABLE_SIGNATURES: tuple[tuple[tuple[str, ...], str], ...] = (
    # Longest first: matching is a prefix test, so a shorter signature listed
    # earlier would swallow every table that begins the same way. That is not
    # hypothetical -- MBEW once matched MARA and MCHB matched MARD, and their
    # rows were appended into the wrong files without a word of complaint.
    (("OBJECTCLAS", "OBJECTID", "CHANGENR", "TABNAME"), "CDPOS"),
    (("OBJECTCLAS", "OBJECTID", "CHANGENR"), "CDHDR"),

    # LIS statistics. Wide: SSOUR/VRSIO first, then the period key (S031) or
    # the plant (S032). Narrow follows the OData key order instead:
    # Spmon,Werks,Matnr,... (S031) and Werks,Lgort,Matnr (S032).
    (("SSOUR", "VRSIO", "SPMON"), "S031"),
    (("SSOUR", "VRSIO", "WERKS"), "S032"),
    (("SPMON", "WERKS", "MATNR"), "S031"),
    (("WERKS", "LGORT", "MATNR"), "S032"),

    # Material master family. MARD and MCHB are identical for three columns and
    # diverge at the fourth, so both need four to be told apart.
    (("MATNR", "WERKS", "LGORT", "CHARG"), "MCHB"),
    (("MATNR", "WERKS", "LGORT"), "MARD"),
    (("MATNR", "WERKS"), "MARC"),
    (("MATNR", "BWKEY"), "MBEW"),
    (("MATNR", "SPRAS"), "MAKT"),
    (("MATNR",), "MARA"),

    # Purchasing. EKPO, EKBE and EKET share EBELN,EBELP and separate at the
    # third column; EKKO is EBELN followed by anything else (BSART when narrow).
    (("EBELN", "EBELP", "ZEKKN"), "EKBE"),
    (("EBELN", "EBELP", "ETENR"), "EKET"),
    (("EBELN", "EBELP"), "EKPO"),
    (("EBELN",), "EKKO"),

    # Movements.
    (("MBLNR", "MJAHR", "ZEILE"), "MSEG"),
    (("MBLNR", "MJAHR"), "MKPF"),

    # Requisitions and reservations.
    (("BANFN", "BNFPO"), "EBAN"),
    (("RSNUM", "RSPOS"), "RESB"),

    # Info records: EINE carries the purchasing org where EINA carries MATNR.
    (("INFNR", "EKORG"), "EINE"),
    (("INFNR", "MATNR"), "EINA"),

    (("LIFNR",), "LFA1"),
)


# Everything this endpoint writes lives under one prefix inside STORAGE_URL,
# which points at the `landing` container root.
CSV_PREFIX = "csv"

# Which table is mid-delivery. Read by a chunk that arrives with no header row
# and therefore nothing else to identify it.
OPEN_TABLE_KEY = f"{CSV_PREFIX}/_open_table.txt"

# A SAP field name: starts with a letter, no spaces. Data values in these
# tables are dominated by numeric keys -- MANDT is '800', EBELN '4500000001' --
# so a single purely numeric cell is enough to say this row is not a header.
_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_/]*$")


def looks_like_header(row: list[str]) -> bool:
    """Whether ``row`` is a column list rather than the first record.

    Needed because ``csv.reader`` will happily call anything row zero. If a
    continuation chunk's data row is taken for a header, that record is lost
    and the chunk is filed under a table that does not exist.
    """
    cells = [c.strip() for c in row if c.strip()]
    if not cells:
        return False
    if any(c.replace(".", "").replace("-", "").isdigit() for c in cells):
        return False
    named = sum(1 for c in cells if _FIELD_NAME.match(c))
    return named >= max(1, len(cells) // 2)


# Gate-pass Z-tables. All three begin ZZGP_NO, ZZYEAR (after the client column)
# and differ only in their key: the header has no item number, the items add
# ZZPOSNR, the returns add ZCOUNT as well (the OData keys of GatePassItemSet /
# GatePassReturnSet). Told apart by which key columns are PRESENT rather than
# by position, because only the header's full layout has been seen (06-Oct, 55
# columns). A wrong guess here is not silent: the chunk counts against the
# wrong request, and its exact $count check fails.
_GATE_PASS_PREFIX = ("ZZGP_NO", "ZZYEAR")


def _gate_pass_table(columns: tuple[str, ...]) -> str | None:
    if columns[: len(_GATE_PASS_PREFIX)] != _GATE_PASS_PREFIX:
        return None
    present = set(columns)
    if "ZCOUNT" in present:
        return "ZMM_GP_IN"
    if "ZZPOSNR" in present:
        return "ZMM_GP_ITEM"
    return "ZMM_GP_HDR"


def table_of(header: list[str]) -> str:
    """The SAP table a header row describes, or a fingerprint if unrecognised.

    Wide (client column first) and narrow (OData projection, no client column)
    layouts resolve to the same table: the client column is dropped first.
    """
    columns = tuple(c.strip().upper() for c in header)
    fields = columns[1:] if columns and columns[0] in CLIENT_COLUMNS else columns
    gate_pass = _gate_pass_table(fields)
    if gate_pass is not None:
        return gate_pass
    for signature, table in TABLE_SIGNATURES:
        if fields[: len(signature)] == signature:
            return table
    # Over the header as received, client column included, so a fingerprint
    # seen before this change still names the same file.
    fingerprint = hashlib.sha256(",".join(columns).encode()).hexdigest()[:8]
    return f"UNKNOWN_{fingerprint}"

# Byte order marks first, longest first -- the UTF-32 LE mark begins with the
# same two bytes as the UTF-16 LE one, so checking UTF-16 first would decode a
# UTF-32 file as UTF-16 and produce convincing nonsense.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

# Tried in order when there is no BOM. latin-1 maps every possible byte, so the
# list cannot be exhausted and decoding cannot fail.
_FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "cp1252", "latin-1")

# How many parsed rows come back in the response. Enough to see that the
# columns line up with the values; not enough to echo an extract.
PREVIEW_ROWS = 3


class CsvAccepted(BaseModel):
    """What we made of the upload.

    Returned so the sender can confirm the reading immediately. A delimiter
    sniffed wrong shows up here as one giant column, which is far easier to
    spot than it is to explain over email two days later.
    """

    status: str = "received"
    rows: int = Field(description="Data rows, not counting the header.")
    columns: list[str] = Field(description="First row, treated as the header.")
    delimiter: str
    delimiter_sniffed: bool = Field(
        description="False means sniffing failed and comma was assumed."
    )
    encoding: str
    preview: list[list[str]] = Field(
        default_factory=list, description="Up to PREVIEW_ROWS rows, as parsed."
    )
    stored: str | None = Field(
        default=None,
        description=(
            "Storage key this chunk was appended to, or null when STORAGE_URL "
            "is unset or the write failed. A null here never means the upload "
            "was refused."
        ),
    )
    sha256: str = Field(description="Of the bytes as received, before any decoding.")


def _decode(raw: bytes) -> tuple[str, str]:
    """Return the text and the encoding it was read with."""
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return raw[len(bom) :].decode(encoding, errors="replace"), encoding

    for encoding in _FALLBACK_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue

    # Unreachable while latin-1 is in the list above. Kept so that editing that
    # list cannot quietly introduce a path that raises.
    return raw.decode("latin-1", errors="replace"), "latin-1 (replaced)"


def _sniff_delimiter(sample: str) -> tuple[str, bool]:
    """Return the delimiter and whether it was sniffed or assumed."""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=CANDIDATE_DELIMITERS)
    except csv.Error:
        # A single-column file has no delimiter to find, and the sniffer says so
        # by raising. Comma then parses it as one column, which is correct.
        return ",", False
    return dialect.delimiter, True


async def _read_body(request: Request) -> bytes:
    """The upload's bytes, whether posted raw or as a form file."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except Exception as exc:  # python-multipart missing, or a malformed part
            logger.warning(
                "CSV upload looked like multipart but could not be parsed (%s); "
                "falling back to the raw body",
                exc,
            )
            return await request.body()
        for value in form.values():
            read = getattr(value, "read", None)
            if read is not None:
                data = await value.read()
                return data if isinstance(data, bytes) else str(data).encode()
        # A form with no file part: take the first value that has content.
        for value in form.values():
            if isinstance(value, str) and value:
                return value.encode()
    return await request.body()


@router.post(
    "/csv",
    response_model=CsvAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Accept a CSV upload (stub)",
    responses={400: {"description": "The body was empty."}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": (
                "CSV, in any encoding and with any delimiter. Sent either as the "
                "raw request body or as a multipart file part. The Content-Type "
                "header is not inspected."
            ),
            "content": {
                "text/csv": {"schema": {"type": "string", "format": "binary"}},
                "text/plain": {"schema": {"type": "string", "format": "binary"}},
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                },
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                },
            },
        }
    },
)
async def receive_csv(request: Request) -> CsvAccepted:
    """Read the upload on the event loop; do everything else on a thread.

    Everything after the read -- decoding megabytes, parsing 50,000 rows,
    the ADLS appends and the database tally -- is blocking work. Done on the
    event loop, as it was until 2026-10-08, it stalled the worker for the
    length of each chunk, and SAP QA's push (many 50,000-row chunks at once,
    into an app also building its snapshots after a restart) waited, gave up
    and hung up: ClientDisconnect, and a chunk that was never landed. On a
    thread, the loop keeps reading the other uploads while one is stored.
    """
    content_type = request.headers.get("content-type", "(none)")
    try:
        raw = await _read_body(request)
    except ClientDisconnect:
        # The sender gave up before the body arrived. Nothing was read, so
        # nothing is landed or counted, and the request's reconciliation
        # will show the chunk missing. One line, not a crash traceback.
        logger.warning(
            "CSV upload ABANDONED by the sender before its body arrived "
            "(Content-Length %s, Content-Type %s). Nothing was landed; SAP has "
            "to resend this chunk.",
            request.headers.get("content-length", "?"),
            content_type,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The sender disconnected before the body arrived.",
        ) from None
    return await _process(raw, content_type)


# One gate per event loop: an asyncio semaphore belongs to the loop it first
# waits on, and a test client runs a loop of its own. The app has one.
_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _processing_slots() -> asyncio.Semaphore:
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        limit = getattr(get_settings(), "csv_upload_max_concurrent", 2)
        _slots = (loop, asyncio.Semaphore(max(1, int(limit))))
    return _slots[1]


async def _process(raw: bytes, content_type: str) -> CsvAccepted:
    """Land one chunk, at most ``csv_upload_max_concurrent`` at a time.

    The wait is on the event loop, NOT on a worker thread: FastAPI serves every
    synchronous route from the same thread pool, so twenty chunks queued on
    threads would leave the I07/I08/I13 pages waiting behind them. And it is
    bounded -- past ``csv_upload_max_wait_seconds`` the chunk goes ahead
    without a slot, because a request held past App Service's 230 seconds is
    a failure to the sender even when we land it.
    """
    slots = _processing_slots()
    limit = getattr(get_settings(), "csv_upload_max_wait_seconds", 120)
    queued = time.monotonic()
    acquired = False
    try:
        await asyncio.wait_for(slots.acquire(), timeout=max(1, int(limit)))
        acquired = True
    except asyncio.TimeoutError:
        logger.warning(
            "CSV chunk waited %ds for a processing slot; landing it anyway so the "
            "request finishes inside App Service's time limit", limit,
        )
    waited = time.monotonic() - queued
    if acquired and waited >= 1:
        logger.info("CSV chunk waited %.1fs for a processing slot", waited)
    try:
        return await run_in_threadpool(_accept, raw, content_type)
    finally:
        if acquired:
            slots.release()


def _accept(raw: bytes, content_type: str) -> CsvAccepted:
    """Decode, parse and land one upload. Blocking; runs on a worker thread."""
    if not raw.strip():
        logger.warning(
            "CSV upload REJECTED (empty body). Content-Type: %s, %d bytes.",
            content_type,
            len(raw),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty request body; expected CSV content.",
        )

    text, encoding = _decode(raw)

    # A stray NUL makes the csv module raise rather than skip. That happens for
    # real -- a UTF-16 export mislabelled as 8-bit decodes to text peppered with
    # them -- and losing the extract to it would be the wrong outcome.
    nul_stripped = "\x00" in text
    if nul_stripped:
        logger.warning("CSV upload contains NUL bytes; stripping them before parsing")
        text = text.replace("\x00", "")

    delimiter, sniffed = _sniff_delimiter(text[:SNIFF_BYTES])

    # newline="" leaves quoted fields containing line breaks intact, and the
    # reader handles CR, LF and CRLF alike -- so SAP's line endings do not
    # matter either.
    reader = csv.reader(_lines(text), delimiter=delimiter)

    header: list[str] = []
    preview: list[list[str]] = []
    rows = 0
    try:
        for index, row in enumerate(reader):
            if index == 0:
                header = row
                continue
            rows += 1
            if len(preview) < PREVIEW_ROWS:
                preview.append(row)
    except csv.Error as exc:
        # Whatever was read before this point still counts. Reporting a partial
        # read beats discarding the upload over a malformed row near the end.
        logger.warning(
            "CSV upload stopped early at row %d (%s); accepting what parsed", rows, exc
        )

    logger.info(
        "CSV received: %d rows, %d columns, delimiter=%r (%s), encoding=%s, "
        "Content-Type: %s, %d bytes",
        rows,
        len(header),
        delimiter,
        "sniffed" if sniffed else "assumed",
        encoding,
        content_type,
        len(raw),
    )
    logger.info("CSV columns: %s", header)
    logger.debug("CSV first rows: %s", preview)

    # One column across a multi-column file is what a wrongly sniffed delimiter
    # looks like. Say so here rather than leaving it to be discovered later.
    if len(header) == 1 and rows and any(len(row) == 1 for row in preview):
        logger.warning(
            "CSV parsed as a single column %r -- if that is wrong, the delimiter "
            "was sniffed as %r and the file may use something else",
            header[0][:80] if header else "",
            delimiter,
        )

    # SAP's own bytes are stored as they came when they ARE the UTF-8 the
    # file is kept in: decoded strictly (no BOM, nothing replaced) and nothing
    # stripped. Re-encoding the text would only make a second 6 MB copy of
    # identical bytes. Anything else is stored from the decoded text, as UTF-8.
    utf8 = raw if encoding == "utf-8" and not nul_stripped else None
    stored = _land(text, header, data_rows=rows, raw_bytes=len(raw), utf8=utf8)

    return CsvAccepted(
        status="received",
        rows=rows,
        columns=header,
        delimiter=delimiter,
        delimiter_sniffed=sniffed,
        encoding=encoding,
        preview=preview,
        stored=stored,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _lines(text: str) -> Iterator[str]:
    """The text a line at a time, line endings kept -- what csv.reader needs.

    In place of ``io.StringIO(text)``, which holds the whole chunk again at
    four bytes a character: a 6 MB push became a 24 MB buffer, and twenty
    chunks arriving together were an out-of-memory kill on the B1 plan
    (2026-10-08). Splits on ``\n`` only, as the csv module expects, so CRLF
    endings and line breaks inside quoted fields parse exactly as before. A
    file using bare ``\r`` endings has no ``\n`` to split on and takes the
    old route, which does recognise them.
    """
    if "\n" not in text and "\r" in text:
        yield from io.StringIO(text, newline="")
        return
    start, find = 0, text.find
    while True:
        end = find("\n", start)
        if end < 0:
            if start < len(text):
                yield text[start:]
            return
        yield text[start : end + 1]
        start = end + 1


def _land(
    text: str,
    first_row: list[str],
    *,
    data_rows: int = 0,
    raw_bytes: int = 0,
    utf8: bytes | None = None,
) -> str | None:
    """Append this chunk to its table's file, best effort.

    SAP sends a table as chunks of 50,000 records, each its own POST. Three
    cases have to be told apart, and only the first is obvious:

    * a chunk whose first row is a header -- names the table and opens it;
    * a later chunk that repeats that header -- the header is dropped;
    * a later chunk with no header at all -- every row is data, and the chunk
      carries nothing identifying it, so it is attributed to the table
      currently open.

    Which of the last two SAP does is not yet confirmed. Assuming headers
    repeat would eat one real record per chunk; assuming they do not would
    duplicate a header per chunk. Both are handled instead.

    The open table is recorded in storage rather than in memory because chunks
    may land on any worker and a restart mid-batch must not orphan the rest.

    Every failure is swallowed. This endpoint refuses nothing but an empty
    body; losing an extract because OUR storage is unset or unwritable would be
    that same mistake wearing a different hat. SAP gets its 202 either way.
    """
    if not get_settings().storage_url:
        logger.debug("STORAGE_URL is not set; CSV not landed")
        return None

    # Bytes from here on: the file is stored as UTF-8, and working in bytes
    # once costs one copy of the chunk instead of three (a padded text, its
    # remainder, and that remainder encoded).
    payload = utf8 if utf8 is not None else text.encode("utf-8")
    if not payload.endswith(b"\n"):
        payload += b"\n"
    newline = payload.index(b"\n")
    first_line = payload[:newline].decode("utf-8", errors="replace").rstrip("\r")
    key = "(undetermined)"

    try:
        storage = get_storage()

        if not looks_like_header(first_row):
            return _append_continuation(
                storage, payload, data_rows=data_rows, raw_bytes=raw_bytes
            )

        table = table_of(first_row)
        key, header_key = _keys_for(table)

        if not storage.exists(header_key):
            with storage.open_write(header_key) as sink:
                sink.write(first_line.encode("utf-8"))
            with storage.open_write(key) as sink:
                sink.write(payload)
            logger.info("CSV landed at %s (new file, table=%s)", key, table)
            record_chunk(table, key, rows=data_rows, raw_bytes=raw_bytes)
            _set_open_table(storage, table)
            return key

        with storage.open_read(header_key) as source:
            known = source.read().decode("utf-8")

        repeats_header = first_line == known
        body = payload[newline + 1 :] if repeats_header else payload
        if body:
            size = storage.append(key, body)
            logger.info(
                "CSV appended to %s (table=%s, header repeated=%s, now %d bytes)",
                key, table, repeats_header, size,
            )
        # A repeated header row is not a record. Counting it would inflate the
        # tally the completeness check depends on. Counted before the open-
        # table marker is touched: the rows are in the file at this point, and
        # nothing after it may stop them being counted.
        record_chunk(
            table, key,
            rows=data_rows if repeats_header else data_rows + 1,
            raw_bytes=raw_bytes,
        )
        _set_open_table(storage, table)
        return key
    except Exception:
        logger.exception("CSV not landed at %s", key)
        return None


def _append_continuation(
    storage, payload: bytes, *, data_rows: int = 0, raw_bytes: int = 0
) -> str | None:
    """A chunk with no header. Every row is data; attribute it to the open table."""
    table = _get_open_table(storage)
    if table is None:
        logger.warning(
            "CSV chunk has no header row and no table is open; it cannot be "
            "attributed and has not been landed"
        )
        return None

    key, _ = _keys_for(table)
    size = storage.append(key, payload)
    logger.info(
        "CSV appended to %s (table=%s, headerless chunk, now %d bytes)",
        key, table, size,
    )
    # Headerless: csv.reader took row zero for a header, so the count is one
    # short of the records actually in this chunk.
    record_chunk(table, key, rows=data_rows + 1, raw_bytes=raw_bytes)
    return key


def _keys_for(table: str) -> tuple[str, str]:
    """Where this table's assembled file lives.

    Scoped to the open extract request rather than to the date -- see
    ``csv_receipt.landing_keys`` for why the calendar version lost rows.
    """
    return landing_keys(table)


def _set_open_table(storage, table: str) -> None:
    """Remember which table is arriving, for a later headerless chunk. Best effort.

    One small file every worker rewrites, so two chunks landing at the same
    moment race on it, and ADLS refuses the loser (ConditionNotMet, measured
    2026-10-08 with SAP QA pushing CDHDR and EKBE at once). It used to raise
    out of ``_land`` AFTER the chunk's rows were appended -- logged as "not
    landed" and never counted, so a complete file read as a short one. The
    marker only matters for a chunk without a header, and SAP repeats the
    header on every chunk, so losing one update costs nothing worth a chunk.
    """
    try:
        with storage.open_write(OPEN_TABLE_KEY) as sink:
            sink.write(table.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- the chunk is already landed and counted
        logger.warning(
            "could not record %s as the open table (%s: %s); its chunk is landed "
            "and counted regardless",
            table, type(exc).__name__, str(exc).splitlines()[0][:120],
        )


def _get_open_table(storage) -> str | None:
    if not storage.exists(OPEN_TABLE_KEY):
        return None
    with storage.open_read(OPEN_TABLE_KEY) as source:
        return source.read().decode("utf-8").strip() or None
