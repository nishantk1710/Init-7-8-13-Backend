"""The staged I08 snapshot build answers exactly what the view-backed one does.

Seeds a small SAP tenant into raw_* tables (SAP field names, as the CSV route
loads them; ZMM065 in its workbook labels), builds the normalise and I08 views,
then builds the snapshot twice -- ``staged=False`` reading the views over the
whole extract, ``staged=True`` reading the narrowed copies ``staged.py`` makes
-- and compares every repair line, universe row, vendor, acquisition and
statistic.

The tenant is shaped to cross every staging filter: a repair line on a non-80
material (its lead time only reachable through the staged PO materials), keys
zero-padded differently in EKPO, EKBE and MSEG, a deleted and a blocked line, a
reversed receipt, a 541 with its plant-side twin and one with item '0', lines
and stock at an out-of-scope plant, and ordinary lines the staging leaves out
but the register still counts as scanned.

Needs a database (skipped without DATABASE_URL); creates and drops its own raw
tables, so it runs on the empty CI database.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.criticality import CriticalityResult, CriticalitySource, CriticalityTier

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")
pytestmark = [needs_db]

TODAY = date(2026, 10, 1)

A, B, C = "8000000001", "8000000002", "8000000003"  # 80-series
X = "1000000777"  # a non-80 material on a repair line
N = "5000000123"  # an ordinary material, never repairable

RAW: dict[str, list[str]] = {
    "raw_mara": ["MATNR", "MTART"],
    "raw_makt": ["MATNR", "SPRAS", "MAKTX"],
    "raw_marc": ["MATNR", "WERKS", "DISMM", "PLIFZ", "MINBE"],
    "raw_mard": ["MATNR", "WERKS", "LGORT", "LABST"],
    "raw_ekko": ["EBELN", "BSART", "LOEKZ", "AEDAT", "LIFNR"],
    "raw_ekpo": [
        "EBELN", "EBELP", "LOEKZ", "AEDAT", "TXZ01", "MATNR", "WERKS", "MENGE", "NETPR",
        "ELIKZ", "PSTYP", "MEINS", "BANFN", "BNFPO", "MTART", "AFNAM", "CREATIONDATE",
    ],
    "raw_eket": ["EBELN", "EBELP", "ETENR", "EINDT", "MENGE"],
    "raw_ekbe": ["EBELN", "EBELP", "GJAHR", "BELNR", "BUZEI", "BEWTP", "BWART", "BUDAT", "MENGE", "SHKZG",
                 "MATNR", "WERKS"],
    "raw_mseg": ["MBLNR", "MJAHR", "ZEILE", "BWART", "MATNR", "WERKS", "LGORT", "MENGE", "BUDAT_MKPF",
                 "SOBKZ", "LIFNR", "SHKZG", "EBELN", "EBELP"],
    "raw_lfa1": ["LIFNR", "NAME1", "LAND1"],
    "raw_zmm065_bmm": ["mat_code", "plant", "criticality", "material_description", "mattyp", "mrp_type"],
    "raw_zmm065_gb": ["mat_code", "plant", "criticality", "material_description", "mattyp", "mrp_type"],
}

ROWS: dict[str, list[tuple]] = {
    "raw_mara": [(A, "ZREP"), (B, "ZREP"), (C, "ZREP"), (N, "ERSA"), (X, "ERSA")],
    "raw_makt": [(A, "E", "PUMP, CENTRIFUGAL"), (B, "E", "VALVE"), (N, "E", "GLOVES")],
    "raw_marc": [
        (A, "1300", "ND", "30", "1"), (B, "1500", "PD", "0", ""), (X, "1500", "PD", "45", ""),
        (N, "1300", "VB", "10", "5"), (A, "3000", "ND", "99", ""),
    ],
    "raw_mard": [
        (A, "1300", "0001", "3"), (A, "1300", "0002", "4,5"), (C, "1500", "0001", "2"),
        (N, "1300", "0001", "9"), (A, "3000", "0001", "50"),
    ],
    "raw_ekko": [
        ("4100000001", "ZREP", "", "05.06.2026", "0000100234"),
        ("4100000004", "ZREP", "", "10.06.2026", "100235"),
        ("4500000010", "NB", "", "01.09.2026", "0000100236"),
        ("4500000011", "NB", "", "02.09.2026", "0000100237"),
    ],
    "raw_ekpo": [
        # Repair lines.
        ("4100000001", "10", "", "06.06.2026", "PUMP REPAIR", "000000008000000001", "1300", "1", "1.200,50",
         "X", "3", "EA", "0010000001", "10", "ZREP", "MILLERJ", "05.06.2026"),
        ("4100000001", "20", "", "06.06.2026", "VALVE REPAIR", B, "1300", "2", "300", "", "3", "EA", "", "",
         "ZREP", "MILLERJ", "05.06.2026"),
        ("4100000002", "10", "", "07.06.2026", "GEARBOX REPAIR", X, "1500", "1", "900", "", "3", "EA", "", "",
         "ERSA", "SMITHK", "07.06.2026"),
        ("4100000003", "10", "L", "08.06.2026", "DELETED REPAIR", A, "1300", "1", "100", "", "3", "EA", "", "",
         "ZREP", "MILLERJ", "08.06.2026"),
        ("4100000004", "10", "S", "10.06.2026", "BLOCKED REPAIR", B, "1500", "1", "100", "", "3", "EA", "", "",
         "ZREP", "SMITHK", "10.06.2026"),
        ("4100000005", "10", "", "11.06.2026", "OUT OF SCOPE", A, "3000", "1", "100", "", "3", "EA", "", "",
         "ZREP", "MILLERJ", "11.06.2026"),
        # A new 80-series purchase, and lines the staging drops but the register still scans.
        ("4500000010", "10", "", "01.09.2026", "NEW VALVE", C, "1300", "1", "5.000", "", "0", "EA", "", "",
         "ZREP", "JONESP", "01.09.2026"),
        ("4500000011", "10", "", "02.09.2026", "GLOVES", N, "1300", "100", "2", "", "0", "PR", "", "",
         "ERSA", "JONESP", "02.09.2026"),
        ("4500000012", "10", "", "02.09.2026", "GLOVES", N, "3000", "100", "2", "", "0", "PR", "", "",
         "ERSA", "JONESP", "02.09.2026"),
        ("4500000013", "", "", "02.09.2026", "NO ITEM", N, "1300", "1", "2", "", "0", "PR", "", "",
         "ERSA", "JONESP", "02.09.2026"),
    ],
    "raw_eket": [
        ("4100000001", "10", "1", "15.07.2026", "1"),
        ("4100000001", "20", "1", "01.08.2026", "2"),
        ("0004100000004", "10", "1", "20.07.2026", "1"),
    ],
    "raw_ekbe": [
        ("4100000001", "10", "2026", "5000000001", "1", "E", "101", "20.07.2026", "1", "S", A, "1300"),
        ("004100000004", "10", "2026", "5000000002", "1", "E", "101", "25.07.2026", "1", "S", B, "1500"),
        ("004100000004", "10", "2026", "5000000003", "1", "E", "102", "26.07.2026", "1", "H", B, "1500"),
        ("4500000011", "10", "2026", "5000000004", "1", "E", "101", "05.09.2026", "100", "S", N, "1300"),
    ],
    "raw_mseg": [
        # Dispatch of 4100000001/10: the vendor-side row, its plant-side twin, and a second shipment.
        ("4900000001", "2026", "1", "541", A, "1300", "0001", "1", "01.07.2026", "O", "0000100234", "S",
         "0004100000001", "10"),
        ("4900000001", "2026", "2", "541", A, "1300", "0001", "1", "01.07.2026", "", "", "H",
         "0004100000001", "10"),
        ("4900000002", "2026", "1", "541", A, "1300", "0001", "1", "03.07.2026", "O", "0000100234", "S",
         "4100000001", "10"),
        # A 541 with item '0' (no PO line), a receipt, and movements on other POs and plants.
        ("4900000003", "2026", "1", "541", B, "1300", "0001", "2", "04.07.2026", "O", "100235", "S",
         "4100000001", "0"),
        ("4900000004", "2026", "1", "101", A, "1300", "0001", "1", "20.07.2026", "", "", "S", "4100000001", "10"),
        ("4900000005", "2026", "1", "541", A, "3000", "0001", "1", "05.07.2026", "O", "", "S", "4100000005", "10"),
        ("4900000006", "2026", "1", "261", N, "1300", "0001", "5", "06.07.2026", "", "", "H", "", ""),
    ],
    "raw_lfa1": [("0000100234", "ACME ROTATING", "ZA"), ("100235", "BETA VALVES", "ZA"),
                 ("0000100236", "GAMMA SUPPLY", "ZA"), ("0000999999", "NOBODY", "ZA")],
    "raw_zmm065_bmm": [(A, "1300", "critical", "PUMP", "ZREP", "ND"), (N, "1300", "normal", "GLOVES", "ERSA", "VB")],
    "raw_zmm065_gb": [(B, "1500", "IMPACT", "VALVE", "ZREP", "PD")],
}


class _Criticality(CriticalitySource):
    """The tiers, without the shared source's own database reads."""

    name = "fake"
    tiers = {(A, "1300"): CriticalityTier.CRITICAL, (B, "1500"): CriticalityTier.IMPACT}

    def get(self, sap_material_number, sap_plant_code=None):
        tier = self.tiers.get((sap_material_number, sap_plant_code))
        return CriticalityResult(sap_material_number, sap_plant_code, tier, "fake")

    def get_many(self, keys):
        return {key: self.get(*key) for key in keys}

    def check_connection(self):
        return None


@pytest.fixture(scope="module")
def tenant():
    from app.core.db import get_engine
    from app.initiatives.i8 import views
    from app.shared import sap_normalise

    engine = get_engine()
    with engine.begin() as conn:
        for table, columns in RAW.items():
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            conn.execute(text(f"CREATE TABLE {table} ({', '.join(f'{c} NVARCHAR(200)' for c in columns)})"))
            marks = ", ".join(f":p{i}" for i in range(len(columns)))
            conn.execute(
                text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})"),
                [{f"p{i}": v for i, v in enumerate(row)} for row in ROWS[table]],
            )
    sap_normalise.ensure_views(engine)
    views.ensure_views(engine)
    yield
    with engine.begin() as conn:
        for table in RAW:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
    sap_normalise.ensure_views(engine)
    views.ensure_views(engine)


@pytest.fixture()
def built(tenant, monkeypatch):
    """(view-backed snapshot, staged snapshot) over the same tenant."""
    import app.initiatives.i8.universe as universe
    from app.core.db import get_sessionmaker
    from app.initiatives.i8.service import build_snapshot

    monkeypatch.setattr(universe, "get_criticality_source", _Criticality)
    db = get_sessionmaker()()
    try:
        direct = build_snapshot(db, today=TODAY, staged=False)
        staged = build_snapshot(db, today=TODAY, staged=True)
        # Built twice on one session: the staging cleans up after itself.
        again = build_snapshot(db, today=TODAY, staged=True)
    finally:
        db.rollback()
        db.close()
    return direct, staged, again


def _plain(items) -> list[dict]:
    return sorted((dataclasses.asdict(i) for i in items), key=repr)


def test_the_tenant_crosses_every_filter(built) -> None:
    """Guards the comparison below against passing on an empty build."""
    direct, _, _ = built
    keys = {line.key for line in direct.lines}
    assert keys == {("4100000001", "10"), ("4100000001", "20"), ("4100000002", "10"), ("4100000004", "10")}
    by_key = {line.key: line for line in direct.lines}
    assert by_key[("4100000002", "10")].lead_time_days == 45  # non-80 repair, MARC via staged materials
    assert by_key[("4100000001", "10")].dispatched_qty == 2  # vendor side only, twin excluded
    assert by_key[("4100000001", "10")].vendor_name == "ACME ROTATING"
    assert by_key[("4100000004", "10")].reversals == 1 and by_key[("4100000004", "10")].po_blocked
    assert direct.register_stats.excluded_deleted_lines == 1
    assert direct.register_stats.candidates_scanned == 7  # every in-scope line with a PO and an item
    assert {(a.purchasing_document, a.item) for a in direct.acquisitions} == {("4500000010", "10")}
    assert {(r.material_id, r.plant) for r in direct.universe} >= {(A, "1300"), (B, "1500"), (C, "1500")}


def test_repair_lines_are_identical(built) -> None:
    direct, staged, again = built
    assert _plain(staged.lines) == _plain(direct.lines)
    assert _plain(again.lines) == _plain(direct.lines)


def test_register_statistics_are_identical(built) -> None:
    direct, staged, _ = built
    assert dataclasses.asdict(staged.register_stats) == dataclasses.asdict(direct.register_stats)


def test_universe_is_identical(built) -> None:
    direct, staged, _ = built
    assert _plain(staged.universe) == _plain(direct.universe)
    assert dataclasses.asdict(staged.universe_stats) == dataclasses.asdict(direct.universe_stats)


def test_vendors_and_acquisitions_are_identical(built) -> None:
    direct, staged, _ = built
    assert _plain(staged.vendors) == _plain(direct.vendors)
    assert _plain(staged.acquisitions) == _plain(direct.acquisitions)


# --- the stored snapshot (I8_SNAPSHOT_STORE=sql) --------------------------------


@pytest.fixture()
def no_runs(tenant):
    from app.core.db import get_engine

    def clear():
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM i8_snapshot_run"))

    clear()
    yield
    clear()


def _without_clocks(snapshot) -> dict:
    plain = dataclasses.asdict(snapshot)
    plain.pop("built_at"), plain.pop("build_seconds")
    return {k: (sorted(map(repr, v)) if isinstance(v, (list, tuple)) else v) for k, v in plain.items()}


def test_a_build_process_stores_what_a_direct_build_returns(no_runs, monkeypatch) -> None:
    """The process's work in this process: lock, staged build, store; then the
    stored payload decoded against a direct build of the same data and day."""
    import app.initiatives.i8.universe as universe
    from app.core.db import get_sessionmaker
    from app.initiatives.i8.service import build_snapshot
    from app.initiatives.i8.snapshot_store import store
    from app.initiatives.i8.snapshot_store.__main__ import EXIT_BUILT, build

    monkeypatch.setattr(universe, "get_criticality_source", _Criticality)
    assert build("parity test") == EXIT_BUILT

    db = get_sessionmaker()()
    try:
        run = store.latest_ready(db)
        assert run is not None and run.payload_bytes and run.fingerprint
        stored = store.load(run)
        direct = build_snapshot(db, staged=False)
        assert _without_clocks(stored) == _without_clocks(direct)
        assert stored.source_fingerprint == run.fingerprint

        # Unchanged data: a second process finds it current and builds nothing.
        assert build("again") == EXIT_BUILT
        db.rollback()
        assert store.latest_ready(db).version == run.version
        # A forced one builds a new version and supersedes the old.
        assert build("manual refresh", force=True) == EXIT_BUILT
        db.rollback()
        newer = store.latest_ready(db)
        assert newer.version == run.version + 1
        old = db.get(type(run), run.version)
        db.refresh(old)
        assert old.status == store.SUPERSEDED and old.payload is None
    finally:
        db.rollback()
        db.close()


def test_a_failed_build_is_recorded_on_its_run(no_runs, monkeypatch) -> None:
    import app.initiatives.i8.service as service
    from app.core.db import get_sessionmaker
    from app.initiatives.i8.snapshot_store import store
    from app.initiatives.i8.snapshot_store.__main__ import EXIT_FAILED, build

    def broken(*args, **kwargs):
        raise RuntimeError("v_ekpo went away\n[SQL: select secret from v_ekpo]")

    monkeypatch.setattr(service, "build_snapshot", broken)
    assert build("will fail") == EXIT_FAILED

    db = get_sessionmaker()()
    try:
        last = store.latest(db)
        assert last.status == store.FAILED
        assert last.error == "RuntimeError: v_ekpo went away"
        assert store.latest_ready(db) is None
        assert store.recent_failures(db) == 1
    finally:
        db.close()
