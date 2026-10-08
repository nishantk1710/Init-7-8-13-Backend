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

        assert body["stored"].startswith("csv/EKPO/")
        assert body["stored"].endswith("/EKPO.csv")

    def test_narrow_files_land_under_their_own_table_names(self, landing) -> None:
        """MARA, EKKO and EKET have arrived without MANDT since 30-Sep. They
        used to land as UNKNOWN_<hash>, where no request could count them."""
        cases = {
            "MARA": "MATNR,MTART,MATKL,MEINS,BISMT,LVORM,MSTAE\n000000000000000011,ERSA,M01,EA,,,\n",
            "EKKO": "EBELN,BSART,BEDAT,AEDAT,LIFNR,EKORG,EKGRP,WAERS\n4000000000,NB,20130419,20130419,100001,1000,001,ZAR\n",
            "EKET": "EBELN,EBELP,ETENR,EINDT,MENGE,WEMNG\n4000000000,00010,0001,20130423,1.000,0.000\n",
        }
        for table, body in cases.items():
            stored = post_chunk(body).json()["stored"]
            assert stored.startswith(f"csv/{table}/"), stored
            assert stored.endswith(f"/{table}.csv"), stored

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

    def test_a_lost_race_on_the_open_table_marker_does_not_lose_the_chunk(
        self, landing, monkeypatch
    ) -> None:
        """Two workers rewriting _open_table.txt at once: ADLS refuses one
        (ConditionNotMet, 2026-10-08). That came AFTER the rows were appended,
        and used to fail the landing -- the rows sat in the file uncounted."""
        counted: list[tuple] = []
        monkeypatch.setattr(csv_upload, "record_chunk", lambda *a, **k: counted.append((a, k)))
        real = landing.open_write

        def racing(key):
            if key == csv_upload.OPEN_TABLE_KEY:
                raise RuntimeError("ConditionNotMet: the condition specified is not met")
            return real(key)

        monkeypatch.setattr(landing, "open_write", racing)

        first = post_chunk(f"{EKPO_HEADER}\n800,4500000001,00010,\n").json()["stored"]
        second = post_chunk(f"{EKPO_HEADER}\n800,4500000002,00020,\n").json()["stored"]

        assert first and first == second
        assert len(counted) == 2, "both chunks counted"
        assert read_stored(landing, first).splitlines()[1:] == [
            "800,4500000001,00010,", "800,4500000002,00020,",
        ]

    def test_parsing_and_landing_run_off_the_event_loop(self, landing, monkeypatch) -> None:
        """Blocking work on the event loop stalled every other upload on the
        worker until SAP's sender gave up (ClientDisconnect, 2026-10-08)."""
        import threading

        threads: dict[str, int] = {}
        real_read, real_land = csv_upload._read_body, csv_upload._land

        async def read(request):
            threads["read"] = threading.get_ident()
            return await real_read(request)

        def land(*args, **kwargs):
            threads["land"] = threading.get_ident()
            return real_land(*args, **kwargs)

        monkeypatch.setattr(csv_upload, "_read_body", read)
        monkeypatch.setattr(csv_upload, "_land", land)

        assert post_chunk(f"{EKPO_HEADER}\n800,4500000001,00010,\n").status_code == 202
        assert threads["land"] != threads["read"]

    def test_a_sender_that_hangs_up_is_one_warning_not_a_crash(self, landing, monkeypatch, caplog) -> None:
        from starlette.requests import ClientDisconnect

        async def gone(request):
            raise ClientDisconnect()

        monkeypatch.setattr(csv_upload, "_read_body", gone)

        with caplog.at_level(logging.WARNING):
            response = post_chunk(f"{EKPO_HEADER}\n800,4500000001,00010,\n")

        assert response.status_code == 400
        assert "ABANDONED" in caplog.text and "resend" in caplog.text
        assert "Unhandled error" not in caplog.text
        assert list(landing.list("csv")) == [], "an incomplete body is never landed"

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


# ---------------------------------------------------------------------------
# How many chunks are processed at once.
#
# SAP pushes every table of a sweep together. On the shared B1 plan twenty
# chunks decoded and landed side by side were an out-of-memory kill
# (2026-10-08 08:12), so a worker processes a few at a time -- and never makes
# a chunk wait so long that the request outlives App Service's time limit.
# ---------------------------------------------------------------------------

import asyncio
import threading
import time as _time


class _Gate:
    """Stands in for _accept: records how many run at once."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.running = 0
        self.peak = 0
        self.lock = threading.Lock()

    def __call__(self, raw, content_type):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        _time.sleep(self.seconds)
        with self.lock:
            self.running -= 1
        return "landed"


def _settings(**values):
    base = {"storage_url": "", "csv_upload_max_concurrent": 2, "csv_upload_max_wait_seconds": 120}
    base.update(values)
    return type("S", (), base)()


def _run_together(count: int):
    async def main():
        return await asyncio.gather(*(csv_upload._process(b"x", "text/csv") for _ in range(count)))

    return asyncio.run(main())


class TestProcessingSlots:
    def test_no_more_than_the_limit_are_processed_at_once(self, monkeypatch) -> None:
        gate = _Gate(0.2)
        monkeypatch.setattr(csv_upload, "_accept", gate)
        monkeypatch.setattr(csv_upload, "get_settings", lambda: _settings(csv_upload_max_concurrent=2))

        results = _run_together(6)

        assert results == ["landed"] * 6, "every chunk is still landed"
        assert gate.peak == 2

    def test_a_chunk_never_waits_past_the_limit(self, monkeypatch, caplog) -> None:
        """Past the wait limit it goes ahead without a slot: a request held
        past 230 seconds is a failure to the sender even when we land it."""
        gate = _Gate(1.5)
        monkeypatch.setattr(csv_upload, "_accept", gate)
        monkeypatch.setattr(
            csv_upload, "get_settings",
            lambda: _settings(csv_upload_max_concurrent=1, csv_upload_max_wait_seconds=1),
        )

        with caplog.at_level(logging.WARNING):
            results = _run_together(2)

        assert results == ["landed", "landed"]
        assert gate.peak == 2, "the second went ahead after its wait ran out"
        assert "landing it anyway" in caplog.text

    def test_waiting_does_not_hold_a_thread(self, monkeypatch) -> None:
        """Synchronous routes share the thread pool; queued chunks must not
        occupy it. While one chunk holds the only slot, the waiting ones run
        no code at all on a worker thread."""
        gate = _Gate(0.2)
        pool = {"active": 0, "peak": 0}
        real = csv_upload.run_in_threadpool

        async def counting(fn, *args):
            pool["active"] += 1
            pool["peak"] = max(pool["peak"], pool["active"])
            try:
                return await real(fn, *args)
            finally:
                pool["active"] -= 1

        monkeypatch.setattr(csv_upload, "_accept", gate)
        monkeypatch.setattr(csv_upload, "run_in_threadpool", counting)
        monkeypatch.setattr(csv_upload, "get_settings", lambda: _settings(csv_upload_max_concurrent=1))

        _run_together(4)

        # Gated on threads, all four would sit in the pool at once.
        assert pool["peak"] == 1


# ---------------------------------------------------------------------------
# Lean handling: what one chunk costs.
#
# A 6 MB push used to be held about seven times over (a StringIO at four bytes
# a character, a padded copy, its remainder, that remainder re-encoded). The
# parse is now line by line and a UTF-8 push is stored as the bytes SAP sent.
# ---------------------------------------------------------------------------

import csv
import io as _io


class TestLeanHandling:
    @pytest.mark.parametrize(
        "text",
        [
            'A,B\r\n1,"two\nlines"\r\n3,"x,y"\r\n4,last',
            "A,B\n1,2\n",
            "A,B\n1,2",
            'A;B\n"q""uote";2\n',
            "only one line",
            "",
        ],
    )
    def test_line_by_line_parses_exactly_as_the_stringio_did(self, text) -> None:
        old = list(csv.reader(_io.StringIO(text, newline="")))
        new = list(csv.reader(csv_upload._lines(text)))
        assert new == old

    def test_bare_cr_line_endings_still_parse(self) -> None:
        """No \n to split on: those take the old route, which knows \r."""
        text = "A,B\r1,2\r3,4\r"
        assert list(csv.reader(csv_upload._lines(text))) == [["A", "B"], ["1", "2"], ["3", "4"]]

    def test_a_utf8_push_is_stored_as_the_bytes_sap_sent(self, landing) -> None:
        body = f"{EKPO_HEADER}\n800,4500000001,00010,Müller Ørsted ☂\n".encode("utf-8")

        key = client.post("/api/events/csv", content=body, headers={"Content-Type": "text/csv"}).json()["stored"]

        with landing.open_read(key) as handle:
            assert handle.read() == body

    def test_a_non_utf8_push_is_still_stored_as_utf8(self, landing) -> None:
        body = f"{EKPO_HEADER}\n800,4500000001,00010,Müller\n".encode("cp1252")

        key = client.post("/api/events/csv", content=body, headers={"Content-Type": "text/csv"}).json()["stored"]

        assert read_stored(landing, key).splitlines()[1] == "800,4500000001,00010,Müller"

    def test_the_defaults_keep_the_sender_waiting_briefly(self) -> None:
        """Six at once is ~150 MB of chunks in flight now that each peaks at
        ~24 MB (54 MB before); the wait stays short because SAP's push waits
        with it."""
        from app.core.config import Settings

        settings = Settings(_env_file=None)
        assert settings.csv_upload_max_concurrent == 6
        assert settings.csv_upload_max_wait_seconds == 30
