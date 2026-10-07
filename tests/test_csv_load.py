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
from app.ingest.csv_tables import CSV_TABLES


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
    def test_seeds_one_day_back_as_a_day(self, monkeypatch) -> None:
        """A mark is a calendar day; one day back re-reads at most a day,
        which covers an extract that ran across midnight in SAP's zone."""
        written = []
        monkeypatch.setattr(csv_load, "set_watermark", lambda *a: written.append(a))

        mark = csv_load._seed_watermark("PurchaseOrderSet", "Aedat", "20260925", 3140)

        assert mark == "2026-09-24"
        assert written == [("PurchaseOrderSet", "Aedat", "2026-09-24", 3140)]

    def test_a_full_load_resets_the_mark_to_its_own_newest_date(self, monkeypatch) -> None:
        """The full pull replaced the table the delta merges into, so whatever
        the delta had measured before describes a table that no longer exists."""
        written = []
        monkeypatch.setattr(csv_load, "set_watermark", lambda *a: written.append(a))

        assert csv_load._seed_watermark("PurchaseOrderSet", "Aedat", "20260925", 1) == "2026-09-24"
        assert written == [("PurchaseOrderSet", "Aedat", "2026-09-24", 1)]

    def test_sap_no_date_seeds_nothing(self, monkeypatch) -> None:
        written = []
        monkeypatch.setattr(csv_load, "set_watermark", lambda *a: written.append(a))

        assert csv_load._seed_watermark("PurchaseOrderSet", "Aedat", "00000000", 1) is None
        assert written == []

    def test_the_seed_is_a_literal_every_delta_shape_can_send(self, monkeypatch) -> None:
        from app.ingest.fetch import delta_literal

        monkeypatch.setattr(csv_load, "set_watermark", lambda *a: None)
        mark = csv_load._seed_watermark("PurchaseOrderSet", "Aedat", "20260925", 1)

        assert delta_literal(mark, "datetime") == "datetime'2026-09-24T00:00:00'"
        assert delta_literal(mark, "dats") == "'20260924'"
        assert delta_literal(mark, "dotted") == "'24.09.2026'"

    def test_every_csv_table_with_a_delta_of_its_own_is_seeded_on_its_field(self) -> None:
        """Derived from manifest.DELTAS, so the seed cannot fall behind the
        delta again: MKPF was seeded on BUDAT after its delta moved to CPUDT,
        and EKPO, EKBE and MSEG were not seeded at all."""
        seeded = {t.sap_table: csv_load.watermark_field(t) for t in CSV_TABLES if csv_load.watermark_field(t)}

        assert seeded == {
            "EKKO": ("AEDAT", "Aedat"),
            "EKPO": ("AEDAT", "Aedat"),
            "EKBE": ("CPUDT", "Cpudt"),
            "MKPF": ("CPUDT", "Cpudt"),
            "MSEG": ("CPUDT_MKPF", "CpudtMkpf"),
            "CDHDR": ("UDATE", "Udate"),
        }


class TestHighestDate:
    def test_dd_mm_yyyy_is_ranked_by_year_not_by_day(self) -> None:
        """A MIN/MAX over the raw column once answered 01.01.2019 to 31.12.2018."""
        assert csv_load._highest(None, iter(["31.12.2018", "01.01.2019", "15.06.2018"])) == "20190101"

    def test_dats_still_works(self) -> None:
        assert csv_load._highest(None, iter(["20130927", "20260925", "20180101"])) == "20260925"

    def test_iso_dates_are_read_too(self) -> None:
        """The third shape the normalise views accept; the narrow layout is
        not promised to keep either of the other two."""
        assert csv_load._highest(None, iter(["2026-09-25", "2013-04-23", "15.06.2018"])) == "20260925"

    def test_sap_no_date_is_ignored_in_both_shapes(self) -> None:
        assert csv_load._highest(None, iter(["00000000", "00.00.0000", ""])) is None
