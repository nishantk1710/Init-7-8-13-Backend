"""Switching SAP systems (DEV <-> QA) is one setting: CPI_PATH.

What has to follow it, and what has to refuse when it cannot:

* every call goes to the configured iFlow path;
* the recorded contract is the snapshot captured FROM that path, and there is
  no silent fall back to another system's;
* the database keeps one system's data, and refuses a load from another.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import DEFAULT_CPI_PATH, Settings, normalise_cpi_path
from app.ingest import sap_system
from app.integrations.sap.errors import ContractError
from app.integrations.sap.transport import CpiTransport
from app.models.ingest_watermark import IngestWatermark

# The module, not the function the package re-exports under the same name.
contract_mod = importlib.import_module("app.integrations.sap.contract")

DEV = "/http/SAPECC/OdataConsumption"
QA = "/http/SAPECCQA/OdataConsumption"


def settings(**overrides) -> Settings:
    base = {"cpi_base_url": "https://cpi.example.test", "_env_file": None, "sap_discovery_dir": ""}
    base.update(overrides)
    return Settings(**base)


# --- The setting ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", DEFAULT_CPI_PATH),
        (None, DEFAULT_CPI_PATH),
        (QA, QA),
        (f"  {QA}/ ", QA),
        ("http/SAPECCQA/OdataConsumption", QA),
    ],
)
def test_cpi_path_is_one_canonical_string(raw, expected) -> None:
    """It is compared, not only sent: a stray space must not be another system."""
    assert normalise_cpi_path(raw) == expected
    assert settings(cpi_path=raw or "").cpi_path == expected


def test_dev_is_the_default() -> None:
    assert DEFAULT_CPI_PATH == DEV
    assert Settings(_env_file=None).cpi_path == DEV


def test_every_call_goes_to_the_configured_path() -> None:
    """OData reads, $count, $metadata and CSV extract requests all go through
    CpiTransport, and its one URL is CPI_BASE_URL + CPI_PATH."""
    assert CpiTransport(settings(cpi_path=QA), tokens=object()).endpoint == f"https://cpi.example.test{QA}"
    assert CpiTransport(settings(cpi_path=DEV), tokens=object()).endpoint == f"https://cpi.example.test{DEV}"


# --- The contract follows the path ----------------------------------------------


@pytest.fixture
def snapshot_root(tmp_path, monkeypatch):
    def make(name: str, path: str | None) -> Path:
        folder = tmp_path / name
        folder.mkdir()
        if path is not None:
            (folder / contract_mod.SNAPSHOT_FILE).write_text(json.dumps({"cpi_path": path}), encoding="utf-8")
        return folder

    monkeypatch.setattr(contract_mod, "SNAPSHOT_ROOT", tmp_path)

    def point(**overrides):
        monkeypatch.setattr(contract_mod, "get_settings", lambda: settings(**overrides))
        contract_mod.discovery_dir.cache_clear()

    yield make, point
    contract_mod.discovery_dir.cache_clear()


def test_the_snapshot_captured_from_the_configured_path_is_used(snapshot_root) -> None:
    make, point = snapshot_root
    dev, qa = make("discovery", DEV), make("discovery_qa", QA + "/")

    point(cpi_path=DEV)
    assert contract_mod.discovery_dir() == dev
    point(cpi_path=QA)
    assert contract_mod.discovery_dir() == qa


def test_no_snapshot_for_the_path_is_an_error_not_another_systems_contract(snapshot_root) -> None:
    make, point = snapshot_root
    make("discovery", DEV)
    make("discovery_qa", None)  # swept once, never recorded which system: not eligible

    point(cpi_path=QA)
    with pytest.raises(ContractError) as raised:
        contract_mod.discovery_dir()
    assert f"CPI_PATH={QA}" in str(raised.value)
    assert "cpi_discovery.py" in str(raised.value) and "delta_probe" in str(raised.value)


def test_two_snapshots_claiming_one_path_is_an_error(snapshot_root) -> None:
    make, point = snapshot_root
    make("discovery", DEV)
    make("discovery_old", DEV)

    point(cpi_path=DEV)
    with pytest.raises(ContractError, match="2 discovery snapshots"):
        contract_mod.discovery_dir()


def test_a_forced_folder_wins(snapshot_root, tmp_path) -> None:
    make, point = snapshot_root
    make("discovery", DEV)
    forced = make("anything", None)

    point(cpi_path=QA, sap_discovery_dir=str(forced))
    assert contract_mod.discovery_dir() == forced


def test_the_committed_dev_snapshot_records_dev() -> None:
    info = contract_mod.snapshot_info(Path(contract_mod.SNAPSHOT_ROOT) / "discovery")
    assert normalise_cpi_path(info["cpi_path"]) == DEV


# --- One system per database ---------------------------------------------------


def test_the_verdict() -> None:
    assert sap_system.verdict(None, QA) is None, "an unrecorded database is claimed, not refused"
    assert sap_system.verdict(DEV, DEV) is None
    problem = sap_system.verdict(DEV, QA)
    assert DEV in problem and QA in problem and "--adopt-sap-system" in problem


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://", future=True)
    IngestWatermark.__table__.create(engine)
    maker = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr(sap_system, "get_sessionmaker", lambda: maker)

    def point(path: str) -> None:
        monkeypatch.setattr(sap_system, "get_settings", lambda: settings(cpi_path=path))

    return point


@pytest.mark.sap_system_guard
def test_the_first_load_records_the_system_and_a_second_system_is_refused(database) -> None:
    database(DEV)
    sap_system.ensure()
    assert sap_system.recorded() == DEV

    database(QA)
    with pytest.raises(sap_system.SapSystemMismatch):
        sap_system.ensure()
    assert sap_system.recorded() == DEV, "a refusal changes nothing"


@pytest.mark.sap_system_guard
def test_adopting_re_records_the_database(database) -> None:
    database(DEV)
    sap_system.ensure()
    database(QA)

    assert sap_system.adopt() == (DEV, QA)
    sap_system.ensure()  # no longer refused


@pytest.mark.sap_system_guard
def test_the_record_is_not_a_watermark_any_delta_can_read(database, monkeypatch) -> None:
    from app.ingest import watermarks

    database(DEV)
    sap_system.ensure()
    monkeypatch.setattr(watermarks, "get_sessionmaker", sap_system.get_sessionmaker)

    assert watermarks.get_watermark("PurchaseOrderSet", "Aedat") is None


@pytest.mark.sap_system_guard
def test_a_load_into_another_systems_database_is_refused_before_any_write(monkeypatch) -> None:
    from app.ingest import load as load_mod
    from app.ingest.manifest import spec_for
    from tests.test_ingest import ROWS, RUN_DATE, MemoryStorage, StubClient, StubExtract
    from app.ingest.fetch import fetch_set

    spec = spec_for("MaterialPlantSet")
    storage = MemoryStorage()
    fetch_set(spec, root="odata", client=StubClient(StubExtract(ROWS)), storage=storage, run_date=RUN_DATE)
    monkeypatch.setattr(sap_system, "recorded", lambda: DEV)
    monkeypatch.setattr(sap_system, "configured", lambda: QA)

    def no_database():
        raise AssertionError("nothing may be written")

    monkeypatch.setattr(load_mod, "get_engine", no_database)
    monkeypatch.setattr(load_mod.raw_merge, "merge_landed", lambda *a, **k: no_database())

    result = load_mod.load_set(spec, root="odata", storage=storage)

    assert result.status == load_mod.STATUS_FAILED and "refused" in result.error and QA in result.error


def test_the_timer_refuses_before_calling_sap(monkeypatch) -> None:
    from app.ingest import manifest, scheduler

    monkeypatch.setattr(sap_system, "recorded", lambda: DEV)
    monkeypatch.setattr(sap_system, "configured", lambda: QA)

    def must_not_run():
        raise AssertionError("no SAP work after a refusal")

    monkeypatch.setattr(manifest, "check_delta_filters", must_not_run)

    summary = scheduler.run_delta_cycle()

    assert summary["failed"] == 1 and "refused" in summary["errors"][0]
