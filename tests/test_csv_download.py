"""Copying landed CSV extracts out of storage: which file, what name, what proof."""

from __future__ import annotations

import csv
import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ingest import csv_download
from app.integrations.storage.adls import AzureDataLakeStorage
from app.models.csv_extract import STATUS_COMPLETE, STATUS_TIMEOUT, CsvExtractRequest
from tests.fake_adls import FakeDataLakeServiceClient


@pytest.fixture
def store():
    return AzureDataLakeStorage(
        "abfss://landing@stvziaicomnonprod.dfs.core.windows.net/",
        service_client=FakeDataLakeServiceClient(),
    )


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", future=True)
    CsvExtractRequest.__table__.create(engine)
    maker = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr(csv_download, "get_sessionmaker", lambda: maker)
    return maker


def _land(store, key: str, text: str) -> bytes:
    data = text.encode("utf-8")
    with store.open_write(key) as sink:
        sink.write(data)
    return data


def _request(maker, request_id: str, table: str, *, status: str, fired: datetime, key: str | None) -> None:
    with maker() as session:
        session.add(CsvExtractRequest(
            request_id=request_id, sap_table=table, entity_set="X",
            from_date="19000101", to_date="20271231", status=status,
            fired_at=fired, data_key=key, received_rows=1, expected_rows=1,
        ))
        session.commit()


def test_the_newest_reconciled_request_is_copied_under_its_own_name(store, db, tmp_path) -> None:
    old = _land(store, "csv/EKKO/FEKKO11111111/EKKO.csv", "EBELN,BSART\n1,NB\n")
    new = _land(store, "csv/EKKO/FEKKO22222222/EKKO.csv", "EBELN,BSART\n2,NB\n")
    _request(db, "FEKKO11111111", "EKKO", status=STATUS_COMPLETE,
             fired=datetime(2026, 10, 5, tzinfo=timezone.utc), key="csv/EKKO/FEKKO11111111/EKKO.csv")
    _request(db, "FEKKO22222222", "EKKO", status=STATUS_COMPLETE,
             fired=datetime(2026, 10, 6, tzinfo=timezone.utc), key="csv/EKKO/FEKKO22222222/EKKO.csv")

    target, results = csv_download.download(["EKKO"], tmp_path, storage=store)

    [result] = results
    assert result.ok
    assert (tmp_path / "EKKO_FEKKO22222222.csv").read_bytes() == new != old
    assert result.sha256 == hashlib.sha256(new).hexdigest()
    assert result.bytes == len(new)


def test_a_reconciled_request_is_preferred_to_a_newer_failed_one(store, db, tmp_path) -> None:
    _land(store, "csv/MARA/FMARA11111111/MARA.csv", "MATNR\n1\n")
    _land(store, "csv/MARA/FMARA22222222/MARA.csv", "MATNR\n")
    _request(db, "FMARA11111111", "MARA", status=STATUS_COMPLETE,
             fired=datetime(2026, 10, 5, tzinfo=timezone.utc), key="csv/MARA/FMARA11111111/MARA.csv")
    _request(db, "FMARA22222222", "MARA", status=STATUS_TIMEOUT,
             fired=datetime(2026, 10, 6, tzinfo=timezone.utc), key="csv/MARA/FMARA22222222/MARA.csv")

    _, [result] = csv_download.download(["MARA"], tmp_path, storage=store)

    assert result.request_id == "FMARA11111111"


def test_without_a_tracked_request_the_newest_landed_file_is_used(store, db, tmp_path) -> None:
    """A chunk that arrived with nothing open lands under unattributed-<date>."""
    data = _land(store, "csv/ZMM_GP_HDR/unattributed-2026-10-06/ZMM_GP_HDR.csv", "MANDT,ZZGP_NO\n110,1\n")

    _, [result] = csv_download.download(["ZMM_GP_HDR"], tmp_path, storage=store)

    assert result.ok
    assert result.request_id == "unattributed-2026-10-06"
    assert (tmp_path / "ZMM_GP_HDR_unattributed-2026-10-06.csv").read_bytes() == data


def test_a_database_that_cannot_be_reached_does_not_stop_the_copy(store, monkeypatch, tmp_path) -> None:
    def broken():
        raise RuntimeError("no DATABASE_URL")

    monkeypatch.setattr(csv_download, "get_sessionmaker", broken)
    _land(store, "csv/MAKT/FMAKT12345678/MAKT.csv", "MANDT,MATNR,SPRAS\n")

    _, [result] = csv_download.download(["MAKT"], tmp_path, storage=store)

    assert result.ok and result.request_id == "FMAKT12345678"


def test_nothing_landed_is_reported_not_raised(store, db, tmp_path) -> None:
    _, [result] = csv_download.download(["EKET"], tmp_path, storage=store)

    assert not result.ok
    assert "nothing landed" in result.error


def test_the_manifest_lists_every_table_tried(store, db, tmp_path) -> None:
    _land(store, "csv/LFA1/FLFA112345678/LFA1.csv", "MANDT,LIFNR\n")

    csv_download.download(["LFA1", "EKET"], tmp_path, storage=store)

    with open(tmp_path / csv_download.MANIFEST_FILE, newline="", encoding="utf-8") as handle:
        rows = {r["sap_table"]: r for r in csv.DictReader(handle)}
    assert rows["LFA1"]["local_file"] == "LFA1_FLFA112345678.csv"
    assert rows["EKET"]["error"]
