"""The CSV feed listener.

These read as a list of ways an extract could have been refused for its
packaging rather than its contents. Each one is a way the PR endpoint's 422
could have repeated itself in a new place, so each is pinned.
"""

import codecs
import logging

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

URL = "/api/events/csv"

CSV = b"MATNR,WERKS,EISBE\n000000000011,1500,0.000\n000000000012,1300,2.000\n"


def post(body: bytes, content_type: str | None = None):
    headers = {"Content-Type": content_type} if content_type else None
    return client.post(URL, content=body, headers=headers)


# --- The content type is not consulted -------------------------------------
#
# This is the whole lesson from the PR endpoint. ABAP's HTTP client sends
# whatever it sends; the body is what matters.


def test_accepts_text_csv() -> None:
    assert post(CSV, "text/csv").status_code == 202


def test_accepts_no_content_type() -> None:
    assert post(CSV).status_code == 202


def test_accepts_text_plain() -> None:
    assert post(CSV, "text/plain").status_code == 202


def test_accepts_octet_stream() -> None:
    assert post(CSV, "application/octet-stream").status_code == 202


def test_accepts_csv_mislabelled_as_json() -> None:
    """A wrong header must not cost us the extract."""
    assert post(CSV, "application/json").status_code == 202


# --- Delimiters -------------------------------------------------------------


def test_sniffs_semicolon() -> None:
    body = post(CSV.replace(b",", b";"), "text/csv").json()

    assert body["delimiter"] == ";"
    assert body["columns"] == ["MATNR", "WERKS", "EISBE"]


def test_sniffs_tab() -> None:
    body = post(CSV.replace(b",", b"\t")).json()

    assert body["delimiter"] == "\t"
    assert body["rows"] == 2


def test_sniffs_pipe() -> None:
    body = post(CSV.replace(b",", b"|")).json()

    assert body["delimiter"] == "|"
    assert body["rows"] == 2


def test_single_column_file_is_not_a_failure() -> None:
    """Sniffing raises when there is no delimiter. Comma is then correct."""
    body = post(b"MATNR\n000011\n000012\n").json()

    assert body["rows"] == 2
    assert body["columns"] == ["MATNR"]
    assert body["delimiter_sniffed"] is False


# --- Encodings and line endings --------------------------------------------


def test_accepts_crlf() -> None:
    assert post(CSV.replace(b"\n", b"\r\n")).json()["rows"] == 2


def test_strips_utf8_bom_from_the_first_column_name() -> None:
    """Left in, the BOM becomes part of the first header and no lookup matches."""
    body = post(codecs.BOM_UTF8 + CSV).json()

    assert body["encoding"] == "utf-8-sig"
    assert body["columns"][0] == "MATNR"


def test_accepts_utf16() -> None:
    """A Windows export can be UTF-16; read as 8-bit it is full of NULs."""
    body = post(codecs.BOM_UTF16_LE + CSV.decode().encode("utf-16-le")).json()

    assert body["encoding"] == "utf-16-le"
    assert body["columns"] == ["MATNR", "WERKS", "EISBE"]
    assert body["rows"] == 2


def test_accepts_cp1252_accents() -> None:
    """Not valid UTF-8; rejecting it would lose the extract over one umlaut."""
    body = post("NAME,CITY\nBjörn,Köln\n".encode("cp1252")).json()

    assert body["encoding"] == "cp1252"
    assert body["rows"] == 1


# --- Shapes we cannot control ----------------------------------------------


def test_accepts_ragged_rows() -> None:
    """Rows with more or fewer cells than the header are kept, not refused."""
    body = post(b"A,B,C\n1,2\n3,4,5,6\n").json()

    assert body["rows"] == 2


def test_accepts_a_quoted_field_containing_a_newline() -> None:
    body = post(b'A,B\n"line1\nline2",x\n').json()

    assert body["rows"] == 1
    assert body["columns"] == ["A", "B"]


def test_accepts_a_header_with_no_data_rows() -> None:
    body = post(b"A,B,C\n").json()

    assert body["rows"] == 0
    assert body["columns"] == ["A", "B", "C"]


def test_accepts_a_body_that_is_not_csv_at_all() -> None:
    """We cannot tell SAP they sent the wrong thing by dropping it."""
    assert post(b'{"a": 1}').status_code == 202


def test_accepts_a_field_longer_than_the_csv_default_limit() -> None:
    """The csv module's 128 KB ceiling raises rather than truncating."""
    long_value = b"x" * 200_000
    body = post(b"A,B\n" + long_value + b",y\n").json()

    assert body["rows"] == 1


def test_accepts_a_multipart_file_upload() -> None:
    """CPI may wrap the extract as a file part rather than posting it raw."""
    response = client.post(URL, files={"file": ("extract.csv", CSV, "text/csv")})

    assert response.status_code == 202
    assert response.json()["columns"] == ["MATNR", "WERKS", "EISBE"]


# --- The one refusal --------------------------------------------------------


def test_empty_body_is_refused_with_a_reason(caplog) -> None:
    """Nothing arrived, so there is nothing to lose by saying so."""
    with caplog.at_level(logging.WARNING, logger="app.api.events.csv_upload"):
        response = post(b"")

    assert response.status_code == 400
    assert "Empty request body" in response.json()["detail"]
    assert any("REJECTED" in message for message in caplog.messages)


# --- What comes back --------------------------------------------------------


def test_response_reports_what_was_understood() -> None:
    """So the sender can confirm the reading without asking us for logs."""
    body = post(CSV, "text/csv").json()

    assert body["status"] == "received"
    assert body["rows"] == 2
    assert body["columns"] == ["MATNR", "WERKS", "EISBE"]
    assert body["delimiter"] == ","
    assert body["encoding"] == "utf-8"
    assert body["preview"][0] == ["000000000011", "1500", "0.000"]


def test_columns_are_logged(caplog) -> None:
    """The schema is unknown, so the log is how we learn what SAP sends."""
    with caplog.at_level(logging.INFO, logger="app.api.events.csv_upload"):
        post(CSV, "text/csv")

    logged = " | ".join(caplog.messages)
    assert "MATNR" in logged
    assert "2 rows" in logged


# ---------------------------------------------------------------------------
# Landing SAP's chunked push as one file per table.
#
# SAP sends a table as chunks of 50,000 records, each its own POST, and they
# have to end up as a single readable CSV. Whether a chunk after the first
# repeats the header row is NOT yet confirmed with SAP, so both are covered:
# assuming either one costs a data row per chunk or duplicates a header.
# ---------------------------------------------------------------------------

import pytest

from app.api.events import csv_upload
from app.core import storage as storage_module
from app.integrations.storage.adls import AzureDataLakeStorage
from tests.fake_adls import FakeDataLakeServiceClient

EKPO_HEADER = "MANDT,EBELN,EBELP,LOEKZ"
MSEG_HEADER = "MANDT,MBLNR,MJAHR,ZEILE"


@pytest.fixture
def landing(monkeypatch):
    """A real adapter over the in-memory fake, wired in as the app's storage."""
    adapter = AzureDataLakeStorage(
        "abfss://landing@stvziaicomnonprod.dfs.core.windows.net/",
        service_client=FakeDataLakeServiceClient(),
    )
    monkeypatch.setattr(csv_upload, "get_storage", lambda: adapter)
    monkeypatch.setattr(
        csv_upload,
        "get_settings",
        lambda: type("S", (), {"storage_url": "abfss://landing@a.dfs.core.windows.net/"})(),
    )
    return adapter


def post_chunk(body: str):
    return client.post(
        "/api/events/csv", content=body.encode("utf-8"),
        headers={"Content-Type": "text/csv"},
    )


def read_stored(adapter, key: str) -> str:
    with adapter.open_read(key) as handle:
        return handle.read().decode("utf-8")


class TestChunkLanding:
    def test_the_table_is_identified_from_the_header(self, landing) -> None:
        body = post_chunk(f"{EKPO_HEADER}\n800,4500000001,00010,\n").json()

        assert body["stored"].startswith("EKPO/")
        assert body["stored"].endswith("/EKPO.csv")

    def test_two_tables_land_in_separate_files(self, landing) -> None:
        ekpo = post_chunk(f"{EKPO_HEADER}\n800,4500000001,00010,\n").json()["stored"]
        mseg = post_chunk(f"{MSEG_HEADER}\n800,4900000001,2026,0001\n").json()["stored"]

        assert ekpo != mseg
        assert "/EKPO.csv" in ekpo and "/MSEG.csv" in mseg

    def test_chunks_repeating_the_header_keep_only_the_first(self, landing) -> None:
        key = post_chunk(f"{EKPO_HEADER}\n800,4500000001,00010,\n").json()["stored"]
        post_chunk(f"{EKPO_HEADER}\n800,4500000002,00020,\n")
        post_chunk(f"{EKPO_HEADER}\n800,4500000003,00030,\n")

        assert read_stored(landing, key) == (
            f"{EKPO_HEADER}\n"
            "800,4500000001,00010,\n"
            "800,4500000002,00020,\n"
            "800,4500000003,00030,\n"
        )

    def test_headerless_chunks_do_not_lose_their_first_row(self, landing) -> None:
        """If SAP omits the header after chunk one, row one is DATA.

        Stripping it unconditionally would discard one real record per chunk --
        99 records across a 100-chunk batch, with a 202 on every one.
        """
        key = post_chunk(f"{EKPO_HEADER}\n800,4500000001,00010,\n").json()["stored"]
        post_chunk("800,4500000002,00020,\n800,4500000003,00030,\n")

        assert read_stored(landing, key) == (
            f"{EKPO_HEADER}\n"
            "800,4500000001,00010,\n"
            "800,4500000002,00020,\n"
            "800,4500000003,00030,\n"
        )

    def test_a_chunk_without_a_trailing_newline_still_joins_cleanly(self, landing) -> None:
        """Otherwise the last row of one chunk and the first of the next merge."""
        key = post_chunk(f"{EKPO_HEADER}\n800,4500000001,00010,").json()["stored"]
        post_chunk(f"{EKPO_HEADER}\n800,4500000002,00020,")

        assert read_stored(landing, key).splitlines() == [
            EKPO_HEADER,
            "800,4500000001,00010,",
            "800,4500000002,00020,",
        ]

    def test_an_unrecognised_header_still_lands_under_a_stable_name(self, landing) -> None:
        first = post_chunk("ZZONE,ZZTWO\n1,2\n").json()["stored"]
        second = post_chunk("ZZONE,ZZTWO\n3,4\n").json()["stored"]

        assert "UNKNOWN_" in first
        assert first == second, "the same header must always resolve to one file"

    def test_a_storage_failure_never_refuses_the_upload(self, monkeypatch) -> None:
        """The whole point of this endpoint: packaging never loses an extract.

        Our storage being broken is our problem, not a reason to hand SAP a
        failure it will not retry.
        """
        def boom():
            raise RuntimeError("storage is down")

        monkeypatch.setattr(csv_upload, "get_storage", boom)
        monkeypatch.setattr(
            csv_upload, "get_settings",
            lambda: type("S", (), {"storage_url": "abfss://x@y.dfs.core.windows.net/"})(),
        )

        response = post_chunk(f"{EKPO_HEADER}\n800,4500000001,00010,\n")

        assert response.status_code == 202
        assert response.json()["stored"] is None
        assert response.json()["rows"] == 1

    def test_nothing_is_written_when_storage_is_not_configured(self, monkeypatch) -> None:
        monkeypatch.setattr(
            csv_upload, "get_settings",
            lambda: type("S", (), {"storage_url": ""})(),
        )

        response = post_chunk(f"{EKPO_HEADER}\n800,4500000001,00010,\n")

        assert response.status_code == 202
        assert response.json()["stored"] is None


class TestTableOf:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (["MANDT", "EBELN", "EBELP", "LOEKZ"], "EKPO"),
            (["MANDT", "EBELN", "BUKRS"], "EKKO"),
            (["MANDT", "EBELN", "EBELP", "ZEKKN", "VGABE"], "EKBE"),
            (["MANDT", "MBLNR", "MJAHR", "ZEILE"], "MSEG"),
            (["MANDT", "MBLNR", "MJAHR", "VGART"], "MKPF"),
            (["MANDT", "MATNR", "ERSDA"], "MARA"),
            (["MANDT", "MATNR", "WERKS", "PSTAT"], "MARC"),
            (["MANDT", "MATNR", "WERKS", "LGORT"], "MARD"),
            (["MANDT", "BANFN", "BNFPO"], "EBAN"),
            (["MANDT", "RSNUM", "RSPOS"], "RESB"),
        ],
    )
    def test_longest_signature_wins(self, header, expected) -> None:
        """EKKO's key is a prefix of EKPO's and MARA's of MARC's.

        Matched shortest-first, every EKPO chunk would land in EKKO's file.
        """
        assert csv_upload.table_of(header) == expected

    def test_case_and_whitespace_do_not_change_the_answer(self) -> None:
        assert csv_upload.table_of([" mandt ", "ebeln", "ebelp"]) == "EKPO"
