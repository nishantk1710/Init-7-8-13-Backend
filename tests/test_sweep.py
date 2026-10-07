"""The delta sweep: the order of things, and when a watermark is allowed to move.

Everything below the sweep is stubbed -- fetch, load, the watermark table, the
table check -- because what is being tested is the sequencing, which is where
the losses were: a parent advanced before its children resolved the window, a
child that failed while its parent moved on, an increment merged into a table
that did not exist or held nothing.

The family, as declared in manifest.DELTAS since 2026-10-07:

    POHistorySet          own window (Cpudt)
    PurchaseOrderItemSet  own window (Aedat)  + keys from POHistorySet
    PurchaseOrderSet      own window (Aedat)  + keys from PurchaseOrderItemSet
    POScheduleLineSet                           keys from both
"""

from __future__ import annotations

import pytest

from app.ingest import manifest as manifest_mod
from app.ingest import sweep as sweep_mod
from app.ingest.fetch import FetchResult
from app.ingest.load import STATUS_FAILED, STATUS_SUCCEEDED, LoadResult
from app.ingest.manifest import spec_for

PO, ITEMS, HIST, SCHED = (
    "PurchaseOrderSet", "PurchaseOrderItemSet", "POHistorySet", "POScheduleLineSet",
)
OWNERS = (PO, ITEMS, HIST)


class Harness:
    """Stand-ins for everything the sweep talks to, recording the order."""

    def __init__(self, monkeypatch, *, marks=None, tables=None):
        self.marks = dict(marks or {})
        self.tables = set(tables if tables is not None else
                          {"odata_purchase_order", "odata_purchase_order_item",
                           "odata_po_history", "odata_po_schedule_line"})
        self.log: list[tuple] = []
        self.fail_fetch: set[str] = set()
        self.fail_load: set[str] = set()
        self.rows = {PO: 3, ITEMS: 7, HIST: 2, SCHED: 4}
        self.keys = {HIST: ["4500000001", "4500000002"], ITEMS: ["4500000002", "4500000003"]}

        # The family is fully runnable here whatever SAP is doing today: these
        # tests are about sequencing, and a set blocked in known_conditions
        # would simply be left out of it.
        monkeypatch.setattr(manifest_mod, "READ_BROKEN_SETS", {})
        monkeypatch.setattr(sweep_mod, "resolve_windows", self.resolve_windows)
        monkeypatch.setattr(sweep_mod, "table_has_rows", lambda t: t in self.tables)
        monkeypatch.setattr(sweep_mod, "collect_parent_keys", self.collect)
        monkeypatch.setattr(sweep_mod, "fetch_set", self.fetch)
        monkeypatch.setattr(sweep_mod, "load_set", self.load)
        monkeypatch.setattr(sweep_mod, "set_watermark", self.advance)

    def resolve_windows(self, chosen, *, since=None):
        self.log.append(("windows",))
        return {name: since or self.marks.get(name) for name in OWNERS}

    def collect(self, client, parent, via_key, since):
        self.log.append(("keys", parent, since))
        return list(self.keys[parent])

    def fetch(self, spec, *, mode, since, parent_keys, advance_watermark, **_):
        self.log.append(("fetch", spec.name, mode, since,
                         None if parent_keys is None else sorted(parent_keys)))
        assert advance_watermark is False, "the sweep advances marks, not fetch_set"
        result = FetchResult(entity_set=spec.name, prefix=f"p/{spec.name}", mode=mode,
                             rows=self.rows.get(spec.name, 0))
        if spec.name in self.fail_fetch:
            result.error = "boom"
        if spec.delta is not None and spec.delta.direct and result.ok:
            result.watermark = "2026-09-15"
        return result

    def load(self, spec, *, root, prefix):
        self.log.append(("load", spec.name))
        if spec.name in self.fail_load:
            return LoadResult(spec.name, spec.raw_table, STATUS_FAILED, error="disk")
        return LoadResult(spec.name, spec.raw_table, STATUS_SUCCEEDED, self.rows[spec.name])

    def advance(self, entity_set, field, value, rows):
        self.log.append(("advance", entity_set, value))
        self.marks[entity_set] = value

    def fetched(self, name):
        return next(e for e in self.log if e[0] == "fetch" and e[1] == name)


FAMILY = (spec_for(ITEMS), spec_for(HIST), spec_for(PO), spec_for(SCHED))
STORED = "2026-09-01"
ADVANCED = "2026-09-15"


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch, marks={PO: STORED, ITEMS: STORED, HIST: STORED})


def _run(chosen=FAMILY, **kw):
    return sweep_mod.run_delta_sweep(chosen, root="odata", client=object(), storage=object(), **kw)


def test_the_window_is_read_before_anything_is_fetched(h) -> None:
    _run()
    kinds = [entry[0] for entry in h.log]
    assert kinds[0] == "windows"
    assert kinds.index("fetch") > kinds.index("windows")


def test_parents_are_fetched_first_and_each_parents_keys_collected_once(h) -> None:
    _run()
    fetched = [e[1] for e in h.log if e[0] == "fetch"]
    assert set(fetched[:2]) == {HIST, ITEMS}
    keys = sorted(e[1:] for e in h.log if e[0] == "keys")
    assert keys == [(HIST, STORED), (ITEMS, STORED)], "one read per parent, however many children"


def test_a_set_with_several_parents_gets_the_union_of_their_keys(h) -> None:
    _run()
    entry = h.fetched(SCHED)
    assert entry[2] == "delta"
    assert entry[4] == ["4500000001", "4500000002", "4500000003"]


def test_a_set_with_its_own_window_and_parents_gets_both(h) -> None:
    """EKPO reads its own Aedat window and the purchase orders with new history."""
    _run()
    entry = h.fetched(ITEMS)
    assert entry[3] == STORED
    assert entry[4] == ["4500000001", "4500000002"]
    assert h.fetched(PO)[4] == ["4500000002", "4500000003"]


def test_children_use_the_windows_the_parents_were_read_with(h) -> None:
    """Not the marks the parents' own fetches would have advanced to."""
    _run()
    assert [e for e in h.log if e[0] == "keys" and e[2] != STORED] == []


def test_the_marks_move_only_after_every_load(h) -> None:
    _run()
    kinds = [e[0] for e in h.log]
    assert kinds.index("advance") > max(i for i, k in enumerate(kinds) if k == "load")
    assert {name: h.marks[name] for name in OWNERS} == {name: ADVANCED for name in OWNERS}


def test_a_failed_child_holds_every_parent_it_reads_through(h) -> None:
    """EKET reads through EKPO and EKBE; advancing either would leave it no
    way back to the keys it missed."""
    h.fail_fetch.add(SCHED)
    report = _run()
    assert h.marks[ITEMS] == STORED and h.marks[HIST] == STORED
    assert SCHED in report.held[ITEMS] and SCHED in report.held[HIST]
    assert h.marks[PO] == ADVANCED, "nothing reads through EKKO"
    assert report.failures == 1


def test_a_failed_child_load_holds_its_parent_and_its_own_mark(h) -> None:
    h.fail_load.add(ITEMS)
    report = _run()
    assert h.marks[ITEMS] == STORED and "load" in report.held[ITEMS]
    assert h.marks[HIST] == STORED and ITEMS in report.held[HIST]


def test_a_failed_parent_holds_its_own_mark_only(h) -> None:
    h.fail_fetch.add(HIST)
    report = _run()
    assert h.marks[HIST] == STORED
    assert "fetch" in report.held[HIST]
    # The sets reading through EKBE took its keys from its window, read
    # separately; they are independently correct and still load.
    assert {e[1] for e in h.log if e[0] == "load"} == {ITEMS, PO, SCHED}


def test_a_missing_table_is_pulled_in_full_to_build_the_baseline(h) -> None:
    h.tables.discard("odata_po_history")
    report = _run()
    assert h.fetched(HIST)[2] == "full"
    assert report.baselined == [HIST]
    # The others are still increments.
    assert h.fetched(ITEMS)[2] == "delta"


def test_a_parent_with_no_mark_sends_its_children_to_a_full_pull(h) -> None:
    """No window, no keys -- and reading the parent whole to collect them
    would be the child set fifty keys at a time."""
    del h.marks[HIST]
    _run()
    assert h.fetched(SCHED)[2] == "full"
    assert h.fetched(ITEMS)[2] == "full"
    assert h.fetched(PO)[2] == "delta", "EKKO reads through EKPO only, which has its mark"


def test_a_child_alone_does_not_advance_its_parents(h) -> None:
    """--set POScheduleLineSet --delta reads its parents' windows but never
    the parents themselves; there is nothing measured to advance."""
    _run(chosen=(spec_for(SCHED),))
    assert not any(e[0] == "advance" for e in h.log)
    assert h.marks[ITEMS] == STORED and h.marks[HIST] == STORED


def test_fetch_only_advances_on_fetch_success(h) -> None:
    """With no load in the sweep, 'landed' means in storage, as before."""
    _run(load=False)
    assert not any(e[0] == "load" for e in h.log)
    assert h.marks[PO] == ADVANCED


def test_no_rows_leaves_the_mark_where_it_was(h, monkeypatch) -> None:
    """Nothing changed in the window: nothing was measured, and the next run
    re-reads the same window, which is cheap."""
    h.rows = {PO: 0, ITEMS: 0, HIST: 0, SCHED: 0}
    real = h.fetch

    def fetch(spec, **kw):
        result = real(spec, **kw)
        result.watermark = None
        return result

    monkeypatch.setattr(sweep_mod, "fetch_set", fetch)
    _run()
    assert not any(e[0] == "advance" for e in h.log)
    assert h.marks[PO] == STORED


def test_the_summary_counts_what_the_scheduler_logs(h) -> None:
    h.fail_load.add(SCHED)
    summary = _run().summary()
    assert summary["fetched"] == 4
    assert summary["loaded"] == 3
    assert summary["rows"] == 3 + 7 + 2
    assert summary["failed"] == 1
    assert any(SCHED in e for e in summary["errors"])
    assert any("held" in e for e in summary["errors"])
