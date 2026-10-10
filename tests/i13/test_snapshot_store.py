"""The SQL snapshot store answers exactly what the in-memory snapshot answers.

Seeds a small SAP tenant into raw_* tables (SAP field names, as the CSV route
loads them), builds the snapshot both ways -- in memory, and into Azure SQL in
batches of TWO materials so every batch boundary is crossed -- and compares
every collection a route reads, item by item. Then exercises what only the
store does: paging and totals in SQL, a one-material refresh, an interrupted
build, and a version switch.

Needs a database (skipped without DATABASE_URL); creates and drops its own
raw tables, so it runs on an empty CI database.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.config import get_settings

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")
pytestmark = [needs_db]

AS_OF = "2026-10-01"

M1, M2, M3, M4, M5, M6 = (f"{n:018d}" for n in range(1000000001, 1000000007))

# Every raw column a view or builder reads, SAP vocabulary.
RAW = {
    "raw_marc": ["MATNR", "WERKS", "DISMM"],
    "raw_mard": ["MATNR", "WERKS", "LGORT", "LABST"],
    "raw_mkpf": ["MBLNR", "MJAHR", "BUDAT"],
    "raw_mseg": ["MBLNR", "MJAHR", "ZEILE", "BWART", "MATNR", "WERKS", "MENGE", "EBELN", "EBELP", "RSNUM", "RSPOS"],
    "raw_resb": ["RSNUM", "RSPOS", "XLOEK", "KZEAR", "MATNR", "WERKS", "BDTER", "BDMNG", "BANFN", "BNFPO", "SGTXT", "WEMPF"],
    "raw_eban": ["BANFN", "BNFPO", "MATNR", "WERKS", "MENGE", "BADAT", "EBELN", "EBELP"],
    "raw_ekpo": ["EBELN", "EBELP", "MATNR", "WERKS", "MENGE", "BANFN", "BNFPO"],
    "raw_ekbe": ["EBELN", "EBELP", "BEWTP", "BWART", "BUDAT", "MENGE"],
    # Criticality, for reclassification's Critical impact (workbook labels).
    "raw_zmm065_bmm": ["mat_code", "plant", "criticality"],
    "raw_zmm065_gb": ["mat_code", "plant", "criticality"],
}

ROWS = {
    "raw_marc": [
        (M1, "1300", "PD"), (M1, "1500", "ND"), (M2, "1500", "PD"), (M3, "1300", "VB"),
        (M4, "1300", ""), (M5, "1300", "PD"), (M6, "1500", "V1"), (M1, "3000", "PD"),
    ],
    "raw_mard": [
        (M1, "1300", "0001", "10"), (M1, "1300", "0002", "2.5"), (M2, "1500", "0001", "4"),
        (M5, "1300", "0001", "0"), (M3, "1300", "0001", "7"), (M1, "3000", "0001", "99"),
    ],
    "raw_mkpf": [
        ("4900000001", "2026", "20.08.2026"), ("4900000002", "2026", "21.08.2026"),
        ("4900000003", "2024", "15.03.2024"), ("4900000004", "2023", "10.03.2023"),
        ("4900000005", "2026", "01.08.2026"), ("4900000006", "2026", "05.09.2026"),
        ("4900000007", "2025", "11.11.2025"), ("4900000008", "2026", "12.09.2026"),
    ],
    "raw_mseg": [
        # M1@1300: a reservation issue, an issue and its reversal, an old issue.
        ("4900000001", "2026", "1", "261", M1, "1300", "2", "", "", "0000000101", "1"),
        ("4900000002", "2026", "1", "261", M1, "1300", "3", "", "", "", ""),
        ("4900000002", "2026", "2", "262", M1, "1300", "3", "", "", "", ""),
        ("4900000003", "2024", "1", "201", M1, "1300", "1", "", "", "", ""),
        # M1@1500, M2@1500, M3@1300: issues of different ages.
        ("4900000007", "2025", "1", "261", M1, "1500", "4", "", "", "0000000104", "1"),
        ("4900000004", "2023", "1", "261", M2, "1500", "6", "", "", "", ""),
        ("4900000008", "2026", "1", "201", M3, "1300", "1", "", "", "", ""),
        # Receipts against the POs.
        ("4900000005", "2026", "1", "101", M1, "1300", "5", "4500000001", "10", "", ""),
        ("4900000006", "2026", "1", "101", M5, "1300", "3", "4500000002", "10", "", ""),
        # Out of scope plant: never counted.
        ("4900000006", "2026", "2", "261", M1, "3000", "8", "", "", "", ""),
    ],
    "raw_resb": [
        ("0000000101", "1", "", "", M1, "1300", "15.09.2026", "5", "0010000001", "10", "S0000ABCDE", "JDOE"),
        ("0000000102", "1", "", "", M2, "1500", "01.10.2026", "1", "", "", "", ""),
        ("0000000103", "1", "X", "", M5, "1300", "01.09.2026", "2", "", "", "", ""),
        ("0000000104", "1", "", "", M1, "1500", "10.11.2025", "4", "", "", "", ""),
        ("0000000105", "1", "", "", M5, "1300", "20.09.2026", "3", "", "", "", ""),
        ("0000000106", "1", "", "", M1, "3000", "20.09.2026", "3", "", "", "", ""),
    ],
    "raw_eban": [
        ("0010000001", "10", M1, "1300", "5", "01.07.2026", "4500000001", "10"),
    ],
    "raw_ekpo": [
        ("4500000001", "10", M1, "1300", "5", "0010000001", "10"),
        ("4500000002", "10", M5, "1300", "3", "", ""),
    ],
    "raw_ekbe": [
        ("4500000001", "10", "E", "101", "01.08.2026", "5"),
        ("4500000002", "10", "E", "101", "25.09.2026", "3"),
    ],
    "raw_zmm065_bmm": [("1000000001", "1300", "CRITICAL"), ("1000000005", "1300", "NORMAL")],
    "raw_zmm065_gb": [("1000000002", "1500", "IMPACT")],
}


@pytest.fixture(scope="module")
def tenant():
    from app.core.db import get_engine
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
    # The n_* views only: the I08 views need tables this tenant does not seed.
    sap_normalise.ensure_views(engine)
    yield
    with engine.begin() as conn:
        for table in RAW:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        conn.execute(text("DELETE FROM i13_snapshot_record"))
        conn.execute(text("DELETE FROM i13_snapshot_run"))
        conn.execute(text("DELETE FROM session_reservation_link"))
    with engine.begin() as conn:
        from app.initiatives.i13.snapshot_store import work_tables

        work_tables.drop_versions_except(conn, set())
    sap_normalise.ensure_views(engine)


@pytest.fixture()
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("I13_SNAPSHOT_REFERENCE_DATE", AS_OF)
    monkeypatch.setenv("I13_BUILD_BATCH_MATERIALS", "2")
    monkeypatch.setenv("I13_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("I13_REFERENCE_PLANS_ENABLED", "false")
    monkeypatch.setenv("I13_UAT_SIMULATION_ENABLED", "false")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture()
def db(tenant, settings):
    from app.core.db import get_sessionmaker

    session = get_sessionmaker()()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def both(db, settings):
    """(in-memory snapshot, stored SqlSnapshot) over the same tenant."""
    from app.initiatives.i13.snapshot import build_i13_snapshot
    from app.initiatives.i13.snapshot_store import builder, lifecycle
    from app.initiatives.i13.snapshot_store.reader import SqlSnapshot

    memory = build_i13_snapshot(db)
    db.commit()
    builder.build(db, reason="parity test", settings=settings)
    run = lifecycle.ready_run(db)
    assert run is not None
    return memory, SqlSnapshot(db, run)


_CLOCKS = {"calculated_at", "generated_at", "attributed_at", "created_at"}


def plain(items) -> list[dict]:
    """Items as dicts without build-time clocks, in a stable order."""
    rows = []
    for item in items:
        d = dataclasses.asdict(item) if dataclasses.is_dataclass(item) else dict(item)
        for clock in _CLOCKS & d.keys():
            d[clock] = None
        rows.append(d)
    return sorted(rows, key=repr)


# --- parity -----------------------------------------------------------------


def test_it_crosses_batches(both) -> None:
    _, stored = both
    assert stored.run.batches and stored.run.batches >= 3


def test_watch(both) -> None:
    memory, stored = both
    rows, total = stored.watch()
    assert total == len(memory.watch_sorted) > 0
    assert plain(rows) == plain(memory.watch_sorted)


def test_movement_metrics(both) -> None:
    memory, stored = both
    rows, total = stored.movement_metrics()
    assert total == len(memory.movement_metrics) > 0
    assert plain(rows) == plain(memory.movement_metrics)


def test_ledgers_chain_and_attribution(both) -> None:
    memory, stored = both
    rows, total = stored.reservation_ledger(oar_only=False)
    assert total == len(memory.reservation_ledger) > 0
    assert plain(e for e, _ in rows) == plain(memory.reservation_ledger)
    assert {(e.reservation_number, e.reservation_item): s for e, s in rows if s} == dict(memory.sgtxt_by_reservation)

    legacy, total = stored.legacy_ledger(oar_only=False)
    assert total == len(memory.legacy_ledger) > 0
    assert plain(legacy) == plain(memory.legacy_ledger)

    chain, total = stored.procurement_chain()
    assert total == len(memory.procurement_chain) > 0
    assert plain(chain) == plain(memory.procurement_chain)
    assert dataclasses.asdict(stored.chain_diagnostics) == dataclasses.asdict(memory.chain_diagnostics)

    attribution, total = stored.attribution(oar_only=False)
    assert total == len(memory.consumption_attribution) > 0
    assert plain(attribution) == plain(memory.consumption_attribution)


def test_reclassification(both) -> None:
    memory, stored = both
    rows, total = stored.reclassification()
    assert total == len(memory.reclassification) > 0
    assert plain(rows) == plain(memory.reclassification)


def test_grni(both) -> None:
    memory, stored = both
    rows, total = stored.grni(oar_only=False)
    assert total == len(memory.grni_entries) > 0, "the seed has one GR-not-issued line"
    as_tuples = lambda items: sorted(
        (i.ledger.ledger_id, i.outstanding_quantity, i.days_since_gr) for i in items
    )
    assert as_tuples(rows) == as_tuples(memory.grni_entries)


def test_usage_within_the_kept_months(both, settings) -> None:
    from app.initiatives.i13.snapshot_store.builder import _month_start

    memory, stored = both
    first = _month_start(date.fromisoformat(AS_OF), settings.i13_usage_history_months - 1)
    expected = {
        key: tuple(m for m in series if m.month >= first)
        for key, series in memory.monthly_consumption.items()
    }
    expected = {k: v for k, v in expected.items() if v}
    rows, total = stored.usage(oar_only=False)
    assert total == len(expected) > 0
    assert {key: series for key, series, _, _ in rows} == expected
    # The 2023 issue is older than 36 months, so M2@1500 has no usage row.
    assert (M2.lstrip("0"), "1500") not in {key for key, *_ in rows}


def test_exceptions_and_summary(both, db) -> None:
    from app.initiatives.i13.snapshot import current_plans, exception_queue
    from app.initiatives.i13.summary import summary_from_snapshot

    memory, stored = both
    plans = current_plans(db, memory)
    rows, total = stored.exception_queue(stored.current_plans())
    expected = exception_queue(memory, plans)
    assert total == len(expected) > 0
    assert plain(rows) == plain(expected)
    # The queue's order: plan breaches, no-plan, then GRNI.
    ranks = [r.type for r in rows]
    assert ranks == sorted(ranks, key=lambda t: ["PLAN_BREACH", "NO_PLAN", "GR_NOT_ISSUED_30_DAY"].index(t.value))

    assert stored.summary(stored.current_plans()) == summary_from_snapshot(memory, plans)
    for plant in ("1300", "1500"):
        assert stored.summary(stored.current_plans(), plant=plant) == summary_from_snapshot(memory, plans, plant=plant)


# --- what only the store does -------------------------------------------------


def test_filters_pages_and_totals(both) -> None:
    memory, stored = both
    everything, total = stored.watch()
    page_one, total_one = stored.watch(limit=2, offset=0)
    page_two, _ = stored.watch(limit=2, offset=2)
    assert total_one == total
    assert [(r.material, r.plant) for r in page_one + page_two] == [(r.material, r.plant) for r in everything[:4]]

    oar, oar_total = stored.watch(oar_only=True, plant="1300")
    assert oar_total == sum(
        1 for m in memory.watch_sorted if m.material_scope.value == "OAR" and m.plant == "1300"
    )
    assert all(m.plant == "1300" and m.material_scope.value == "OAR" for m in oar)

    band = everything[0].aging_band.value
    banded, _ = stored.watch(aging_band=band.lower())
    assert banded and all(m.aging_band.value == band for m in banded)


def test_a_version_switch_keeps_one_version(both, db, settings) -> None:
    from sqlalchemy import func, select

    from app.initiatives.i13.snapshot_store import builder, lifecycle
    from app.models.i13_snapshot_store import I13SnapshotRecord

    _, stored = both
    first = stored.version
    second = builder.build(db, reason="second", settings=settings)
    assert second == first + 1
    assert lifecycle.ready_run(db).version == second
    versions = db.execute(select(I13SnapshotRecord.version).distinct()).scalars().all()
    assert versions == [second]
    tables = db.execute(text("SELECT name FROM sys.tables WHERE name LIKE 'i13[_]w[0-9]%'")).scalars().all()
    assert tables and all(name.startswith(f"i13_w{second}_") for name in tables)
    assert db.execute(select(func.count()).select_from(I13SnapshotRecord)).scalar_one() > 0


def test_an_interrupted_build_is_cleared_and_the_ready_one_kept(both, db) -> None:
    from datetime import datetime, timezone

    from app.initiatives.i13.snapshot_store import builder, kinds, lifecycle
    from app.models.i13_snapshot_store import I13SnapshotRecord, I13SnapshotRun

    _, stored = both
    ready = stored.version
    db.add(
        I13SnapshotRun(
            version=ready + 1, status="building", schema_signature=kinds.signature(),
            reference_date=date.fromisoformat(AS_OF), started_at=datetime.now(timezone.utc),
        )
    )
    db.add(
        I13SnapshotRecord(
            version=ready + 1, kind="watch", seq=1, material="X", plant="1300", oar=True, payload="[]"
        )
    )
    db.commit()
    assert builder.abandon_unfinished(db) == 1
    assert db.get(I13SnapshotRun, ready + 1).status == "failed"
    assert lifecycle.ready_run(db).version == ready
    assert not db.execute(
        text("SELECT 1 FROM i13_snapshot_record WHERE version = :v"), {"v": ready + 1}
    ).first()


def test_refreshing_one_material_replaces_only_its_rows(both, db) -> None:
    from app.initiatives.i13.snapshot_store.refresh import refresh_material

    _, stored = both
    before, total = stored.watch()
    key = (M1.lstrip("0"), "1300")
    assert refresh_material(db, *key, reservations=True)
    db.commit()
    after, after_total = stored.watch()
    assert after_total == total
    strip = lambda rows: plain(r for r in rows if (r.material, r.plant) == key)
    assert strip(after) == strip(before)
    ledger, _ = stored.reservation_ledger(material=key[0], plant=key[1])
    assert ledger, "the material's ledger rows were rewritten, not lost"


def test_a_layout_change_is_never_decoded(both, db, monkeypatch) -> None:
    from app.initiatives.i13.snapshot_store import kinds, lifecycle

    monkeypatch.setattr(kinds, "signature", lambda: "different")
    assert lifecycle.ready_run(db) is None


def test_the_codec_round_trips_every_stored_type() -> None:
    from datetime import datetime, timezone

    from app.initiatives.i13.models import AcquiredVsPlanStatus, AgingBand, WatchMetric
    from app.initiatives.i13.snapshot_store import codec
    from app.shared.material_scope import MaterialScope

    item = WatchMetric(
        material="1", plant="1300", material_scope=MaterialScope.OAR, stock_on_hand=Decimal("29.000"),
        open_po_quantity=Decimal(0), average_monthly_consumption=Decimal("1.5"), months_of_cover=None,
        projected_months_of_cover=None, months_of_cover_reason="INSUFFICIENT_HISTORY",
        last_movement_date=date(2026, 8, 1), days_since_last_movement=61, last_issue_date=None,
        days_since_last_issue=None, consumption_count_12m=0, consumed_qty_12m=Decimal(0),
        inventory_turns=None, inventory_turns_reason=None, aging_band=AgingBand.NON_MOVING,
        gr_not_issued_flag=False, gr_not_issued_days_since_gr=None, gr_not_issued_relevant_gr_date=None,
        gr_not_issued_threshold_days=30, gr_not_issued_received_quantity=Decimal(0),
        gr_not_issued_issued_quantity=Decimal(0), gr_not_issued_outstanding_quantity=Decimal(0),
        acquired_vs_plan_status=AcquiredVsPlanStatus.NO_PLAN, planned_quantity=None,
        received_quantity=Decimal(0), issued_quantity=Decimal(0),
        acquired_vs_plan_variance_quantity=None, acquired_vs_plan_variance_percentage=None,
        calculated_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    import json

    assert codec.decode(WatchMetric, json.loads(codec.dumps(codec.encode(item)))) == item


# --- captured plans are applied when the queue is read ------------------------


def _captured(**overrides):
    from app.initiatives.i13.plans import ConsumptionPlan, PlanSource

    fields = {
        "plan_id": "CAP-1", "session_id": "S0000ABCDE", "reservation_number": "", "reservation_item": "",
        "material": M1.lstrip("0"), "plant": "1300", "requester": "JDOE", "purpose": "test",
        "planned_quantity": Decimal(2), "planned_use_date": date(2026, 9, 1), "status": "OPEN",
        "source": PlanSource.CAPTURED, "window_end": date(2026, 9, 1), "captured_on": date(2026, 8, 25),
    }
    fields.update(overrides)
    return ConsumptionPlan(**fields)


@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(lambda: [_captured()], id="covers-a-reservation"),
        pytest.param(lambda: [_captured(window_end=date(2026, 6, 1), planned_use_date=date(2026, 6, 1))], id="breached"),
        pytest.param(lambda: [_captured(material=M5.lstrip("0")), _captured(plan_id="CAP-2", plant="1500")], id="two-keys"),
        pytest.param(lambda: [_captured(material="999999")], id="material-with-no-ledger"),
    ],
)
def test_captured_plans_overlay_matches_the_in_memory_queue(both, plans) -> None:
    from app.initiatives.i13 import snapshot as snapshot_module
    from app.initiatives.i13.snapshot import exception_queue
    from app.initiatives.i13.summary import summary_from_snapshot

    memory, stored = both
    plans = plans()
    # The in-memory queue caches by plan id/status/quantity, not dates.
    snapshot_module._queue_cache.clear()
    expected = exception_queue(memory, plans)
    rows, total = stored.exception_queue(plans)
    assert total == len(expected)
    assert plain(rows) == plain(expected)
    assert stored.summary(plans) == summary_from_snapshot(memory, plans)
    # Paged across the overlay boundary, the pages join up to the whole.
    pages = []
    for offset in range(0, total, 2):
        page, _ = stored.exception_queue(plans, limit=2, offset=offset)
        pages.extend(page)
    assert [r.id for r in pages] == [r.id for r in rows]


# --- the routes answer the same from either store ------------------------------

ROUTES = [
    "/api/i13/summary",
    "/api/i13/summary?plant=1300",
    "/api/i13/watch",
    "/api/i13/watch?plant=1500&limit=2",
    "/api/i13/act/utilisation",
    "/api/i13/act/utilisation?aging_band=NON_MOVING",
    "/api/i13/act/utilisation?grni=true",
    "/api/i13/movement-metrics",
    f"/api/i13/materials/{M1.lstrip('0')}/plants/1300/movement-metrics",
    f"/api/i13/act/utilisation/{M1.lstrip('0')}/1300",
    "/api/i13/reclassification",
    "/api/i13/reclassification?candidates_only=true",
    "/api/i13/exceptions",
    "/api/i13/exceptions?exception_type=NO_PLAN",
    "/api/i13/utilisation-ledger",
    "/api/i13/utilisation-ledger?include_out_of_scope=true",
    "/api/i13/utilisation-ledger?lifecycle_status=received",
    "/api/i13/utilisation-ledger/101/1",
    "/api/i13/ledger",
    "/api/i13/ledger?include_out_of_scope=true",
    "/api/i13/utilisation-ledger/partial",
    "/api/i13/utilisation-ledger/partial/diagnostics",
    "/api/i13/consumption-attribution",
    "/api/i13/consumption-attribution?include_out_of_scope=true",
    "/api/i13/grni?include_out_of_scope=true",
    "/api/i13/session-compliance",
]

# Usage keeps I13_USAGE_HISTORY_MONTHS in the store, so these differ from the
# in-memory answer by design (test_usage_within_the_kept_months compares them
# under that rule); here they only have to answer.
USAGE_ROUTES = [
    "/api/i13/usage-patterns?include_out_of_scope=true",
    f"/api/i13/usage-patterns/{M1.lstrip('0')}/1300",
]


def _strip_clocks(value):
    if isinstance(value, dict):
        return {k: (None if k in _CLOCKS or k in {"calculatedAt"} else _strip_clocks(v)) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((_strip_clocks(v) for v in value), key=repr)
    return value


def test_every_route_answers_the_same_from_either_store(both, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.initiatives.i13 import snapshot as snapshot_module
    from app.main import app

    memory, _ = both
    client = TestClient(app)

    def fetch(store: str) -> dict:
        monkeypatch.setenv("I13_SNAPSHOT_STORE", store)
        get_settings.cache_clear()
        answers = {}
        for url in ROUTES:
            response = client.get(url)
            answers[url] = (response.status_code, response.headers.get("x-total-count"), _strip_clocks(response.json()))
        return answers

    with snapshot_module._lock:
        snapshot_module._state.snapshot = memory
        snapshot_module._state.status = "ready"
    try:
        from_memory = fetch("memory")
        from_sql = fetch("sql")
    finally:
        snapshot_module.reset_i13_snapshot()
    for url in ROUTES:
        assert from_memory[url][0] == 200, (url, from_memory[url])
        assert from_sql[url] == from_memory[url], url

    monkeypatch.setenv("I13_SNAPSHOT_STORE", "sql")
    get_settings.cache_clear()
    listed = client.get(USAGE_ROUTES[0])
    assert listed.status_code == 200 and listed.json()
    assert listed.headers["x-i13-history-months"].endswith("2026-09")
    one = client.get(USAGE_ROUTES[1])
    assert one.status_code == 200 and one.json()["months"]


# --- when the store builds ---------------------------------------------------------


@pytest.fixture()
def no_thread(monkeypatch):
    """Record build starts instead of running them."""
    from app.initiatives.i13.snapshot_store import lifecycle

    started: list[str] = []
    monkeypatch.setattr(lifecycle, "start_background_build", lambda reason, after=None: started.append(reason) or True)
    lifecycle._state.last_check = 0.0
    return started


def test_start_up_serves_a_current_version_without_building(both, no_thread) -> None:
    from app.initiatives.i13.snapshot_store import lifecycle

    lifecycle.start_up()
    assert no_thread == []


def test_start_up_builds_when_the_data_changed(both, db, no_thread, monkeypatch) -> None:
    from app.initiatives.i13 import snapshot as snapshot_module
    from app.initiatives.i13.snapshot_store import lifecycle

    monkeypatch.setattr(snapshot_module, "compute_fingerprint", lambda *a, **k: "changed")
    monkeypatch.setattr(lifecycle, "_pull_in_progress", lambda db: None)
    lifecycle.start_up()
    assert no_thread == ["start-up"]


def test_a_running_pull_is_waited_out(both, db, no_thread, monkeypatch) -> None:
    from app.initiatives.i13 import snapshot as snapshot_module
    from app.initiatives.i13.snapshot_store import lifecycle
    from app.models.csv_extract import CsvExtractRequest

    monkeypatch.setattr(snapshot_module, "compute_fingerprint", lambda *a, **k: "changed")
    request = CsvExtractRequest(
        request_id="FTEST1", sap_table="MSEG", entity_set="GoodsMovementItemSet", from_date="20260101", to_date="20261001"
    )
    db.add(request)
    db.commit()
    try:
        assert lifecycle._pull_in_progress(db)
        assert lifecycle.check_fingerprint(db, min_interval_seconds=0) is False
        assert no_thread == []
    finally:
        db.delete(request)
        db.commit()
    lifecycle._state.last_check = 0.0
    monkeypatch.setattr(lifecycle, "_pull_in_progress", lambda db: None)
    assert lifecycle.check_fingerprint(db, min_interval_seconds=0) is True
    assert no_thread == ["data changed"]
