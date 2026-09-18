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

import codecs
import csv
import hashlib
import io

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.logging import get_logger

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
    raw = await _read_body(request)
    content_type = request.headers.get("content-type", "(none)")

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
    if "\x00" in text:
        logger.warning("CSV upload contains NUL bytes; stripping them before parsing")
        text = text.replace("\x00", "")

    delimiter, sniffed = _sniff_delimiter(text[:SNIFF_BYTES])

    # newline="" leaves quoted fields containing line breaks intact, and the
    # reader handles CR, LF and CRLF alike -- so SAP's line endings do not
    # matter either.
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)

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

    return CsvAccepted(
        status="received",
        rows=rows,
        columns=header,
        delimiter=delimiter,
        delimiter_sniffed=sniffed,
        encoding=encoding,
        preview=preview,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
