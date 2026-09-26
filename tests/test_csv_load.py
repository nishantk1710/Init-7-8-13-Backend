"""csv_load's read path, against storage that behaves like the real adapter.

The ADLS adapter's ``open_read`` is a generator-based context manager whose
``finally`` closes the spooled file. That one property is what the fake here
reproduces, because the bug it guards against only exists with it: take the
handle out of the manager, let the manager go, and the generator is finalised
before the first row is read.
"""

from __future__ import annotations

import contextlib
import gc
import io

import pytest

from app.ingest import csv_load


class _FinalisingStorage:
    """open_read exactly as the adapter does it: yield a buffer, close it after."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.closed: list[str] = []

    @contextlib.contextmanager
    def open_read(self, key: str):
        buffer = io.BytesIO(self.objects[key])
        try:
            yield buffer
        finally:
            buffer.close()
            self.closed.append(key)


BODY = b"MANDT,MATNR,MAKTX\r\n100,000000000010000001,Bearing 6205\r\n100,000000000010000002,Seal kit\r\n"


class TestOpenCsv:
    def test_rows_are_readable_for_the_whole_block(self) -> None:
        """18 tables failed with 'I/O operation on closed file' on the real
        adapter because the handle was taken out of its context manager."""
        storage = _FinalisingStorage({"csv/MAKT/R1/MAKT.csv": BODY})

        with csv_load._open_csv(storage, "csv/MAKT/R1/MAKT.csv") as (header, rows):
            gc.collect()   # anything unreferenced gets finalised here, as in CPython
            assert header == ["MANDT", "MATNR", "MAKTX"]
            assert [r[2] for r in rows] == ["Bearing 6205", "Seal kit"]

    def test_the_object_is_closed_once_the_block_ends(self) -> None:
        storage = _FinalisingStorage({"k": BODY})

        with csv_load._open_csv(storage, "k") as (header, rows):
            list(rows)
            assert storage.closed == []
        assert storage.closed == ["k"]

    def test_an_empty_object_yields_no_header(self) -> None:
        storage = _FinalisingStorage({"k": b""})

        with csv_load._open_csv(storage, "k") as (header, rows):
            assert header == []
            assert list(rows) == []

    def test_nul_bytes_are_stripped_before_the_csv_module_sees_them(self) -> None:
        storage = _FinalisingStorage({"k": b"A,B\r\n1,x\x00y\r\n"})

        with csv_load._open_csv(storage, "k") as (header, rows):
            assert list(rows) == [["1", "xy"]]

    def test_a_missing_object_raises_at_open(self) -> None:
        storage = _FinalisingStorage({})

        with pytest.raises(KeyError):
            with csv_load._open_csv(storage, "missing"):
                pass


# --- Seeding the OData delta's starting point ------------------------------


class TestSeedWatermark:
    def test_seeds_one_day_back_in_the_odata_shape(self, monkeypatch) -> None:
        """SAP serialises a DATS as midnight in its own zone, decoded as 22:00
        UTC the evening before; a literal at midnight of the same date could
        sit past every row of that day. One day back re-reads at most a day."""
        written = []
        monkeypatch.setattr(csv_load, "get_watermark", lambda *a: None)
        monkeypatch.setattr(csv_load, "set_watermark", lambda *a: written.append(a))

        mark = csv_load._seed_watermark("PurchaseOrderSet", "Aedat", "20260925", 3140)

        assert mark == "2026-09-24 00:00:00"
        assert written == [("PurchaseOrderSet", "Aedat", "2026-09-24 00:00:00", 3140)]

    def test_never_overwrites_a_mark_the_delta_measured_itself(self, monkeypatch) -> None:
        """That mark is a position in odata_<table>; this load filled raw_<table>."""
        written = []
        monkeypatch.setattr(csv_load, "get_watermark", lambda *a: "2026-09-14 22:00:00+00:00")
        monkeypatch.setattr(csv_load, "set_watermark", lambda *a: written.append(a))

        assert csv_load._seed_watermark("PurchaseOrderSet", "Aedat", "20260925", 1) is None
        assert written == []

    def test_sap_no_date_seeds_nothing(self, monkeypatch) -> None:
        written = []
        monkeypatch.setattr(csv_load, "get_watermark", lambda *a: None)
        monkeypatch.setattr(csv_load, "set_watermark", lambda *a: written.append(a))

        assert csv_load._seed_watermark("PurchaseOrderSet", "Aedat", "00000000", 1) is None
        assert written == []

    def test_the_seed_is_a_literal_the_delta_can_send(self, monkeypatch) -> None:
        from app.ingest.fetch import odata_literal

        monkeypatch.setattr(csv_load, "get_watermark", lambda *a: None)
        monkeypatch.setattr(csv_load, "set_watermark", lambda *a: None)
        mark = csv_load._seed_watermark("PurchaseOrderSet", "Aedat", "20260925", 1)

        assert odata_literal(mark, "Edm.DateTime") == "datetime'2026-09-24T00:00:00'"
