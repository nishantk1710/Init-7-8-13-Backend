"""How much memory one build batch needs, and what it writes -- without a database.

The production extract holds a handful of materials far larger than any batch
limit (measured 11 Oct 2026, work tables i13_w62_*):

    3000000052 @1500   510,373 movements, 74,187 PO lines, 71,275 GR lines
    5000017969 @1300   239,667 movements (221,565 of them issues against a
                       reservation), 224,081 reservations

A material is never split across batches, so each is a batch of its own, and
on the one-vCore App Service (about 400 MB free) those batches ran the
container out of memory. This module rebuilds both shapes synthetically behind
a fake session that answers the batch repositories' SQL, runs ``_build_batch``
under ``tracemalloc``, and reports the peak.

It also fingerprints every record the batch writes (per kind, in write order),
so a change to how a batch reads or writes can be proven not to change what it
stores: ``test_records_unchanged`` pins the fingerprints of a mixed tenant.

Full size, by hand (slow under tracemalloc, a few minutes)::

    python -m tests.i13.test_snapshot_store_memory
"""

from __future__ import annotations

import hashlib
import random
import tracemalloc
from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest
from sqlalchemy.sql.elements import TextClause

from app.core.config import get_settings
from app.core.criticality import CriticalityResult, CriticalitySource, CriticalityTier
from app.initiatives.i13.config import get_i13_config
from app.initiatives.i13.snapshot_store import builder
from app.initiatives.i13.snapshot_store.batch_repos import Batch
from app.initiatives.i13.snapshot_store.work_tables import WorkTables

AS_OF = date(2026, 10, 1)
WORK = WorkTables(0)


# --- the work tables' rows, as pyodbc hands them back ------------------------


class Marc(NamedTuple):
    material: str
    plant: str
    mrp_controller: str | None


class Mov(NamedTuple):
    material: str
    plant: str
    movement_type: str
    quantity: str
    posting_date: str
    purchase_order: str
    item: str
    reservation: str
    item_no_stock_transfer_reserv: str


class Stock(NamedTuple):
    material: str
    plant: str
    unrestricted: str


class Pr(NamedTuple):
    purchase_requisition: str
    item_of_requisition: str
    material: str
    plant: str
    quantity_requested: str
    requisition_date: str
    purchase_order: str | None
    purchase_order_item: str | None
    # EBAN-LOEKZ (app/initiatives/i13/po_lines.py): a live requisition here.
    deletion_indicator: str | None = None


class Po(NamedTuple):
    purchasing_document: str
    item: str
    purchase_requisition: str | None
    item_of_requisition: str
    material: str
    plant: str
    order_quantity: str
    # What the open-line rule reads (app/initiatives/i13/po_lines.py): an open
    # order here, as before.
    delivery_completed: str | None = None
    deletion_indicator: str | None = None
    doc_type: str | None = None


class Gr(NamedTuple):
    purchasing_document: str
    item: str
    movement_type: str
    quantity: str
    posting_date: str
    material: str


class Resb(NamedTuple):
    reservation: str
    item_no_stock_transfer_reserv: str
    item_deleted: str
    final_issue: str
    material: str
    plant: str
    storage_location: str
    requirement_date: str
    requirement_quantity: str
    base_unit_of_measure: str
    quantity_withdrawn: str
    value_withdrawn: str
    purchase_requisition: str | None
    item_of_requisition: str
    order: str
    movement_type: str
    receiving_plant: str
    receiving_stor_loc: str
    goods_recipient: str
    text: str


# A table is a function producing fresh rows each time it is read -- new
# strings every row, as a driver returns them -- so what a batch holds is
# what it allocated, not the fixture's own copy.
Table = Callable[[], Iterator[NamedTuple]]


def _s(value: Any) -> str:
    """A new string object, never an interned constant -- a driver's rows
    never share their strings."""
    return "".join(str(value))


def _day(n: int) -> str:
    return _s((date(2017, 1, 1) + timedelta(days=n % 3560)).isoformat())


# --- the two production shapes -----------------------------------------------


def giant_procurement(scale: float = 1.0) -> dict[str, Table]:
    """3000000052's shape: issues with no reference, receipts against
    tens of thousands of PO lines that carry no PR, almost no reservations."""
    m, p = "3000000052", "1500"
    issues, receipts = int(440_000 * scale), int(70_000 * scale)
    po_lines, gr_lines, resb = int(74_187 * scale), int(71_275 * scale), max(int(93 * scale), 3)

    def mov():
        for i in range(issues):
            yield Mov(_s(m), _s(p), _s("261"), _s(f"{i % 17 + 1}.000"), _day(i * 7), _s(""), _s(""), _s(""), _s(""))
        for i in range(receipts):
            yield Mov(_s(m), _s(p), _s("101"), _s(f"{i % 9 + 1}.000"), _day(i * 11), _s(f"45{i % po_lines:08d}"),
                      _s("10"), _s(""), _s(""))

    def po():
        for i in range(po_lines):
            yield Po(_s(f"45{i:08d}"), _s("10"), None, _s("0"), _s(m), _s(p), _s(f"{i % 9 + 1}.000"))

    def gr():
        for i in range(gr_lines):
            yield Gr(_s(f"45{i % po_lines:08d}"), _s("10"), _s("101"), _s(f"{i % 9 + 1}.000"), _day(i * 11), _s(m))

    def reservations():
        for i in range(resb):
            yield _resb(f"9{i:09d}", m, p, i)

    return {
        "marc": lambda: iter([Marc(m, p, "OAR")]),
        "mov": mov,
        "stock": lambda: iter([Stock(m, p, "120.000")]),
        "pr": lambda: iter(()),
        "po": po,
        "gr": gr,
        "resb": reservations,
    }


def giant_reservations(scale: float = 1.0) -> dict[str, Table]:
    """5000017969's shape: a reservation per issue, a quarter of a million
    of each, a few hundred PR/PO lines."""
    m, p = "5000017969", "1300"
    resb, issued, other = int(224_081 * scale), int(221_565 * scale), int(18_102 * scale)
    prs, pos, grs = max(int(630 * scale), 3), max(int(638 * scale), 3), max(int(1_122 * scale), 3)

    def mov():
        for i in range(issued):
            yield Mov(_s(m), _s(p), _s("261"), _s(f"{i % 5 + 1}.000"), _day(i * 3), _s(""), _s(""),
                      _s(f"{i:010d}"), _s("1"))
        for i in range(other):
            yield Mov(_s(m), _s(p), _s("101"), _s(f"{i % 40 + 1}.000"), _day(i * 13), _s(f"46{i % pos:08d}"),
                      _s("10"), _s(""), _s(""))

    def reservations():
        for i in range(resb):
            yield _resb(f"{i:010d}", m, p, i, pr=f"10{i % prs:08d}" if i % 50 == 0 else None)

    def pr():
        for i in range(prs):
            yield Pr(_s(f"10{i:08d}"), _s("10"), _s(m), _s(p), _s("50.000"), _day(i * 5), _s(f"46{i % pos:08d}"), _s("10"))

    def po():
        for i in range(pos):
            yield Po(_s(f"46{i:08d}"), _s("10"), _s(f"10{i % prs:08d}"), _s("10"), _s(m), _s(p), _s("50.000"))

    def gr():
        for i in range(grs):
            yield Gr(_s(f"46{i % pos:08d}"), _s("10"), _s("101"), _s("25.000"), _day(i * 5), _s(m))

    return {
        "marc": lambda: iter([Marc(m, p, "OAR")]),
        "mov": mov,
        "stock": lambda: iter([Stock(m, p, "3400.000")]),
        "pr": pr,
        "po": po,
        "gr": gr,
        "resb": reservations,
    }


def _resb(rsnum: str, m: str, p: str, i: int, pr: str | None = None) -> Resb:
    return Resb(
        _s(rsnum), _s("1"), _s("X" if i % 97 == 0 else ""), _s("X" if i % 3 == 0 else ""), _s(m), _s(p),
        _s("0001"), _day(i * 3 + 30), _s(f"{i % 5 + 1}.000"), _s("EA"), _s(f"{i % 5 + 1}.000" if i % 3 == 0 else "0.000"),
        _s(f"{(i % 5 + 1) * 120}.00"), _s(pr) if pr else None, _s("10" if pr else "0"), _s(f"7{i % 400:07d}"),
        _s("261"), _s(""), _s(""), _s(f"USER{i % 60}"), _s(f"S{i:09X}" if i % 11 == 0 else ""),
    )


# --- a mixed tenant: every path, for the fingerprints -------------------------


def mixed_tenant(seed: int = 13, materials: int = 120) -> dict[str, Table]:
    """Many small materials in both plants with every kind of link: PR -> PO
    -> GR chains, issues against reservations and against POs, reversals,
    deleted and final-issued reservations, OAR and non-OAR MRP types, stock
    and no stock, recent and ancient movement."""
    rng = random.Random(seed)
    marc, mov, stock, prs, pos, grs, resb = [], [], [], [], [], [], []
    for n in range(materials):
        m = f"{2000000000 + n * 37}"
        for p in ("1300", "1500"):
            if rng.random() < 0.3 and p == "1500":
                continue
            marc.append(Marc(m, p, rng.choice(["OAR", "OAR", "POD", "101", None])))
            if rng.random() < 0.7:
                stock.append(Stock(m, p, f"{rng.randint(0, 40)}.000"))
            for k in range(rng.randint(0, 3)):
                banfn = f"10{n:05d}{k}{p[1]}"
                ebeln = f"45{n:05d}{k}{p[1]}" if rng.random() < 0.8 else None
                prs.append(Pr(banfn, "10", m, p, f"{rng.randint(1, 20)}.000", _day(rng.randint(0, 3500)),
                              ebeln, "10" if ebeln else "0"))
                if ebeln:
                    pos.append(Po(ebeln, "10", banfn, "10", m, p, f"{rng.randint(1, 20)}.000"))
                    for g in range(rng.randint(0, 2)):
                        grs.append(Gr(ebeln, "10", rng.choice(["101", "101", "102"]), f"{rng.randint(1, 10)}.000",
                                      _day(rng.randint(0, 3559)), m))
                    if rng.random() < 0.5:
                        mov.append(Mov(m, p, "261", f"{rng.randint(1, 5)}.000", _day(rng.randint(0, 3559)),
                                       ebeln, "10", "", ""))
                for r in range(rng.randint(0, 2)):
                    rsnum = f"{n:05d}{k}{r}{p[1]}"
                    resb.append(_resb(rsnum, m, p, rng.randint(0, 10_000), pr=banfn if rng.random() < 0.6 else None))
                    for _ in range(rng.randint(0, 2)):
                        mov.append(Mov(m, p, rng.choice(["261", "201", "262"]), f"{rng.randint(1, 5)}.000",
                                       _day(rng.randint(0, 3559)), "", "", rsnum, "1"))
            for _ in range(rng.choice([0, 1, 3, 12])):
                mov.append(Mov(m, p, rng.choice(["261", "201", "262", "101", "311"]), f"{rng.randint(1, 9)}.000",
                               _day(rng.randint(0, 3559)), "", "", "", ""))
    tables = {"marc": marc, "mov": mov, "stock": stock, "pr": prs, "po": pos, "gr": grs, "resb": resb}
    return {name: (lambda rows=rows: iter(rows)) for name, rows in tables.items()}


# --- a session that answers the batch repositories' SQL ------------------------


class _Result:
    def __init__(self, rows: Iterator[Any]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def fetchall(self) -> list:
        return list(self._rows)

    def partitions(self, size: int | None = None):
        rows = list(self._rows)
        step = size or len(rows) or 1
        for start in range(0, len(rows), step):
            yield rows[start : start + step]


class FakeSession:
    """Just enough of a Session for ``_build_batch``: the work-table SELECTs
    (filtered as the SQL would filter them) and the record INSERTs, which are
    fingerprinted per kind and then dropped."""

    def __init__(self, tables: dict[str, Table]) -> None:
        self.tables = tables
        self.digests: dict[str, Any] = defaultdict(hashlib.sha256)
        self.counts: dict[str, int] = defaultdict(int)

    def connection(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="fake"))

    def execute(self, statement, params=None):
        if not isinstance(statement, TextClause):  # the RecordWriter's INSERT
            for row in params or []:
                self.counts[row["kind"]] += 1
                self.digests[row["kind"]].update(repr(tuple(row.values())).encode())
            return _Result(iter(()))
        return _Result(self._select(str(statement), params or {}))

    def _select(self, sql: str, params: dict[str, str]) -> Iterator[Any]:
        name = sql.split(f"FROM {WORK.name('')}", 1)[1].split()[0]
        lo, hi, plant = params.get("b_lo"), params.get("b_hi"), params.get("b_plant")
        unknown = set(params) - {"b_lo", "b_hi", "b_plant", "material", "plant"}
        if unknown:
            raise NotImplementedError(f"fake session: filter {unknown} on {name}")
        issues = "movement_type IN ('201', '261')" in sql
        issues_or_reversals = "movement_type IN ('201', '261', '202', '262')" in sql
        by_reservation = "reservation <> ''" in sql
        by_po = "purchase_order <> ''" in sql
        for row in self.tables[name]():
            if lo is not None and not (lo <= row.material <= hi):
                continue
            if plant and getattr(row, "plant", plant) != plant:
                continue
            if params.get("material") and row.material != params["material"]:
                continue
            if params.get("plant") and row.plant != params["plant"]:
                continue
            if issues and row.movement_type not in ("201", "261"):
                continue
            if issues_or_reversals and row.movement_type not in ("201", "261", "202", "262"):
                continue
            if by_reservation and row.reservation in ("", "0"):
                continue
            if by_po and (row.purchase_order == "" or row.item == ""):
                continue
            yield row

    def fingerprints(self) -> dict[str, tuple[int, str]]:
        return {kind: (self.counts[kind], d.hexdigest()[:16]) for kind, d in sorted(self.digests.items())}


# --- running one batch -------------------------------------------------------


class _FixedClock(datetime):
    """``datetime`` whose ``now`` never moves: items carry a computed-at stamp,
    and a fingerprint must not change with the wall clock."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 1, 6, 0, tzinfo=tz)


class _Criticality(CriticalitySource):
    """Every fifth material-plant CRITICAL, the rest NORMAL -- ZMM065 lives in SQL."""

    def get(self, sap_material_number, sap_plant_code=None):
        tier = CriticalityTier.CRITICAL if int(sap_material_number) % 5 == 0 else CriticalityTier.NORMAL
        return CriticalityResult(sap_material_number, sap_plant_code, tier, "fake")

    def get_many(self, keys):
        return {key: self.get(*key) for key in keys}

    def check_connection(self):
        return None


def run_batch(
    tables: dict[str, Table], batch: Batch, *, measure: bool = False, as_of: date = AS_OF, dated_only: bool = False
):
    """``_build_batch`` over ``tables``. Returns (peak bytes or None, the
    session -- its fingerprints say what was written)."""
    import app.initiatives.i13.consumption_attribution as attribution
    import app.initiatives.i13.exceptions as exceptions
    import app.initiatives.i13.movement_metrics as movement_metrics
    import app.initiatives.i13.reclassification as reclassification
    import app.initiatives.i13.watch as watch

    session = FakeSession(tables)
    settings = get_settings()
    patches = [
        (watch, "load_consumption_plans", lambda data_dir, db=None: []),  # captured plans live in SQL
        (reclassification, "get_criticality_source", _Criticality),
        *((module, "datetime", _FixedClock)
          for module in (attribution, exceptions, movement_metrics, reclassification, watch)),
    ]
    originals = [(module, name, getattr(module, name)) for module, name, _ in patches]
    for module, name, value in patches:
        setattr(module, name, value)
    try:
        if measure:
            tracemalloc.start()
        builder._build_batch(
            session, WORK, batch,
            config=get_i13_config(), settings=settings, data_dir=Path(settings.i13_data_dir), as_of=as_of,
            reference_plans=[], all_plans=[],
            writer=builder.RecordWriter(session, 0), stats=builder.BuildStats(), diagnostics=[], sgtxt_rows=[],
            dated_only=dated_only,
        )
        peak = tracemalloc.get_traced_memory()[1] if measure else None
    finally:
        if measure:
            tracemalloc.stop()
        for module, name, value in originals:
            setattr(module, name, value)
    return peak, session


ALL = Batch("0", "9999999999")


# --- tests ---------------------------------------------------------------------


# What the batch writes for the mixed tenant and the two giant shapes at 2 %,
# recorded on the code before the memory work. A change that alters a single
# stored byte changes a fingerprint. "mixed" re-recorded 2026-10-12 when a
# blank DISMM became OAR: its None MARC rows moved oarkey/reclass 87 -> 124 and
# exception 137 -> 203; every other kind kept its row count.
# "rledger" re-recorded 2026-10-19 in all three: a reservation with no PO and
# nothing issued now reads RESERVED / PR_NOT_FOUND / PR_SHARED by its PR
# reference instead of PR_CREATED (same rows, corrected lifecycle).
# "mixed" re-recorded 2026-10-20 when OAR moved to MRP controller (DISPO = OAR,
# blank not OAR): oarkey/reclass 124 -> 87 and exception 203 -> 137 (the None
# MARC rows are no longer OAR); every kind's oar flag or scope label moved with
# it (the old VB rows now read EXCLUDED, not MIN_MAX). Row counts otherwise kept.
# "lledger" and "watch" re-recorded 2026-10-20 in all three: they now store
# their filter/sort columns beside the payload (kinds.RECORD_LAYOUT 2). Same rows.
# "lledger", "grni", "watch" and "exception" re-recorded 2026-10-28: goods issued
# per PO line now include stock issued out of its deliveries, oldest first
# (stock_issue_matching), and GRNI is per PO line, not per reservation. lledger
# keeps its rows; grni 35 -> 81 (mixed) and 0 -> 6 (giant_reservations), and the
# GR-not-issued exceptions follow WATCH's flag: 137 -> 152 and 4434 -> 4435.
# "mixed" re-recorded 2026-10-30: a reservation's issues now carry their 202/262
# reversals, netted and never below zero, so rledger/lledger/watch quantities
# move; a 262 against a reservation no longer sits in the oldest-first stock
# pool undoing another line's issue, so one estimated line leaves GRNI
# (81 -> 80) and its exception goes with it (152 -> 151). The giants keep theirs.
# "watch" re-recorded 2026-10-30 in "mixed" and "giant_reservations":
# acquired-vs-plan measures what was issued on each plan's own reservations
# (plans.plan_outcomes), so a row's issued quantity and plan status move; the
# row counts, and every other kind, are unchanged.
EXPECTED: dict[str, dict[str, tuple[int, str]]] = {
    "mixed": {
        "attrib": (294, "214738b080d76802"), "chain": (286, "636c21f9a8b8679e"),
        "exception": (151, "46ce4895f8e71b5c"), "grni": (80, "cdbe217240794569"),
        "lledger": (286, "18121a087fdb0045"), "movement": (186, "085dd35e7ee606ce"),
        "oarkey": (87, "cafeb8032728b5cb"), "reclass": (87, "4b90da3bcd2f2450"),
        "rledger": (294, "747cdb56819da766"), "usage": (134, "a4ef4d00cc908924"),
        "watch": (192, "e4800991a50f3707"),
    },
    "giant_procurement": {
        "attrib": (3, "f77f2830ef44df9f"), "chain": (1483, "17e55edc51ed3706"),
        "exception": (2, "635263fdeb4ddacc"), "lledger": (1483, "a93eeb4dccddc3db"),
        "movement": (1, "e888732abb0b5eb0"), "oarkey": (1, "34de90e00df89cf2"),
        "reclass": (1, "796ae91ff3f6fa23"), "rledger": (3, "3e636498cefa3ea8"),
        "usage": (1, "ed89b5283468661c"), "watch": (1, "5ed9bb4c2ae856d7"),
    },
    "giant_reservations": {
        "attrib": (4481, "172a43e538fb60e0"), "chain": (12, "441a698e4314a2ad"),
        "exception": (4435, "2a46b5743a731d76"), "grni": (6, "9dd86a35f35ae9f6"),
        "lledger": (12, "ce8c39b941ae9377"),
        "movement": (1, "145d27f46ec8471e"), "oarkey": (1, "702ce6717c803e00"),
        "reclass": (1, "99733b38e83d2030"), "rledger": (4481, "906ccc961d424b9e"),
        "usage": (1, "4db03c5965f3dd07"), "watch": (1, "aff23366b80b2fe5"),
    },
}


@pytest.mark.parametrize(
    "name, tables",
    [
        ("mixed", lambda: mixed_tenant()),
        ("giant_procurement", lambda: giant_procurement(0.02)),
        ("giant_reservations", lambda: giant_reservations(0.02)),
    ],
)
def test_records_unchanged(name, tables):
    _, session = run_batch(tables(), ALL)
    assert session.fingerprints() == EXPECTED[name]


# --- a new day: re-age, not rebuild (builder.reage) ------------------------------
#
# A re-age copies the undated kinds from yesterday's version and recomputes the
# dated ones. That is only right if (1) the undated kinds really do not move
# with the date, and (2) the dated-only batch writes exactly what a full batch
# writes for those kinds on the new date. Both are checked here on the mixed
# tenant, across 400 days -- past the 30-day GRNI threshold, the 14-day plan
# grace, the 365/730-day bands and the 12-month window.

LATER = AS_OF + timedelta(days=400)


def _kinds(session, names) -> dict[str, tuple[int, str]]:
    return {k: v for k, v in session.fingerprints().items() if k in names}


@pytest.mark.parametrize("make", [mixed_tenant, lambda: giant_reservations(0.02)])
def test_undated_kinds_do_not_move_with_the_date(make):
    from app.initiatives.i13.snapshot_store import kinds

    _, today = run_batch(make(), ALL)
    _, later = run_batch(make(), ALL, as_of=LATER)
    assert _kinds(today, kinds.UNDATED_KINDS) == _kinds(later, kinds.UNDATED_KINDS)
    assert _kinds(today, kinds.UNDATED_KINDS)  # they were written at all
    # ...while the dated ones do move: the test can tell the dates apart.
    assert _kinds(today, kinds.DATED_KINDS) != _kinds(later, kinds.DATED_KINDS)


@pytest.mark.parametrize("make", [mixed_tenant, lambda: giant_reservations(0.02)])
def test_a_dated_only_batch_writes_what_a_full_batch_writes(make):
    from app.initiatives.i13.snapshot_store import kinds

    _, full = run_batch(make(), ALL, as_of=LATER)
    _, reaged = run_batch(make(), ALL, as_of=LATER, dated_only=True)
    assert set(reaged.fingerprints()) <= set(kinds.DATED_KINDS)
    assert reaged.fingerprints() == _kinds(full, kinds.DATED_KINDS)


def test_every_kind_is_dated_or_undated():
    from app.initiatives.i13.snapshot_store import kinds

    _, session = run_batch(mixed_tenant(), ALL)
    assert set(session.fingerprints()) <= set(kinds.DATED_KINDS) | set(kinds.UNDATED_KINDS)
    assert not set(kinds.DATED_KINDS) & set(kinds.UNDATED_KINDS)


# Peak bytes for one batch of each giant shape at 5 % of its production size.
# Measured on the code before the memory work: 44 MB and 92 MB (full size:
# 888 MB and 1,836 MB). After it: 8 MB and 22 MB. The limits leave room for
# interpreter differences and catch any return to holding rows twice.
PEAK_LIMIT_AT_5_PERCENT = {"giant_procurement": 12 * 2**20, "giant_reservations": 30 * 2**20}


@pytest.mark.parametrize("name, make", [("giant_procurement", giant_procurement), ("giant_reservations", giant_reservations)])
def test_peak_memory_of_one_giant_batch(name, make):
    peak, session = run_batch(make(0.05), ALL, measure=True)
    assert session.counts  # it did build the batch
    assert peak < PEAK_LIMIT_AT_5_PERCENT[name], f"{name}: {peak / 2**20:.1f} MB at 5 % scale"


if __name__ == "__main__":  # pragma: no cover - a measurement, by hand
    import sys

    scale = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    for label, make in (("mixed", lambda: mixed_tenant()),
                        ("giant_procurement@2%", lambda: giant_procurement(0.02)),
                        ("giant_reservations@2%", lambda: giant_reservations(0.02))):
        print(label, run_batch(make(), ALL)[1].fingerprints())
    for label, make in (("giant_procurement", giant_procurement), ("giant_reservations", giant_reservations)):
        peak, session = run_batch(make(scale), ALL, measure=True)
        print(f"{label} @ scale {scale}: peak {peak / 2**20:.0f} MB, records {dict(session.counts)}")
