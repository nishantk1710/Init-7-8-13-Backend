"""The delta sweep: the order of things, and when a watermark is allowed to move.

Everything below the sweep is stubbed -- fetch, load, the watermark table, the
table-existence check -- because what is being tested is the sequencing, which
is where the losses were: a parent advanced before its children resolved the
window, a child that failed while its parent moved on, an increment merged
into a table that did not exist.
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

        # The family is fully runnable here whatever SAP is doing today: these
        # tests are about sequencing, and a set blocked in known_conditions
        # would simply be left out of it.
        monkeypatch.setattr(manifest_mod, "READ_BROKEN_SETS", {})
        monkeypatch.setattr(sweep_mod, "resolve_windows", self.resolve_windows)
        monkeypatch.setattr(sweep_mod, "table_exists", lambda t: t in self.tables)
        monkeypatch.setattr(sweep_mod, "collect_parent_keys", self.collect)
        monkeypatch.setattr(sweep_mod, "fetch_set", self.fetch)
        monkeypatch.setattr(sweep_mod, "load_set", self.load)
        monkeypatch.setattr(sweep_mod, "set_watermark", self.advance)

    def resolve_windows(self, chosen, *, since=None):
        self.log.append(("windows",))
        return {PO: since or self.marks.get(PO)}

    def collect(self, client, delta, since):
        self.log.append(("keys", delta.via, since))
        return ["4500000001", "4500000002"]

    def fetch(self, spec, *, mode, since, parent_keys, advance_watermark, **_):
        self.log.append(("fetch", spec.name, mode, since,
                         None if parent_keys is None else len(parent_keys)))
        assert advance_watermark is False, "the sweep advances marks, not fetch_set"
        result = FetchResult(entity_set=spec.name, prefix=f"p/{spec.name}", mode=mode,
                             rows=self.rows.get(spec.name, 0))
        if spec.name in self.fail_fetch:
            result.error = "boom"
        if spec.delta is not None and spec.delta.field is not None and result.ok:
            result.watermark = "2026-09-14 22:00:00+00:00"
        return result

    def load(self, spec, *, root, prefix):
        self.log.append(("load", spec.name))
        if spec.name in self.fail_load:
            return LoadResult(spec.name, spec.raw_table, STATUS_FAILED, error="disk")
        return LoadResult(spec.name, spec.raw_table, STATUS_SUCCEEDED, self.rows[spec.name])

    def advance(self, entity_set, field, value, rows):
        self.log.append(("advance", entity_set, value))
        self.marks[entity_set] = value


FAMILY = (spec_for(ITEMS), spec_for(HIST), spec_for(PO), spec_for(SCHED))
STORED = "2026-09-01 22:00:00+00:00"


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch, marks={PO: STORED})


def _run(chosen=FAMILY, **kw):
    return sweep_mod.run_delta_sweep(chosen, root="odata", client=object(), storage=object(), **kw)


def test_the_window_is_read_before_anything_is_fetched(h) -> None:
    _run()
    kinds = [entry[0] for entry in h.log]
    assert kinds[0] == "windows"
    assert kinds.index("fetch") > kinds.index("windows")


def test_the_parent_is_fetched_first_and_its_keys_collected_once(h) -> None:
    _run()
    fetched = [e[1] for e in h.log if e[0] == "fetch"]
    assert fetched[0] == PO
    assert [e for e in h.log if e[0] == "keys"] == [("keys", PO, STORED)]


def test_children_use_the_window_the_parent_was_read_with(h) -> None:
    """Not the mark the parent's own fetch would have advanced to."""
    _run()
    for name in (ITEMS, HIST, SCHED):
        entry = next(e for e in h.log if e[0] == "fetch" and e[1] == name)
        assert entry[3] == STORED
        assert entry[4] == 2, "handed the parent's keys rather than reading it again"


def test_the_mark_moves_only_after_every_load(h) -> None:
    _run()
    kinds = [e[0] for e in h.log]
    assert kinds.index("advance") > max(i for i, k in enumerate(kinds) if k == "load")
    assert h.marks[PO] == "2026-09-14 22:00:00+00:00"


def test_a_failed_child_holds_the_parents_mark(h) -> None:
    """Advancing it would leave the child no way back to the window it missed."""
    h.fail_fetch.add(HIST)
    report = _run()
    assert not any(e[0] == "advance" for e in h.log)
    assert h.marks[PO] == STORED
    assert HIST in report.held[PO]
    assert report.failures == 1


def test_a_failed_child_load_holds_the_parents_mark_too(h) -> None:
    h.fail_load.add(ITEMS)
    report = _run()
    assert h.marks[PO] == STORED
    assert ITEMS in report.held[PO]


def test_a_failed_parent_never_advances(h) -> None:
    h.fail_fetch.add(PO)
    report = _run()
    assert h.marks[PO] == STORED
    assert "fetch" in report.held[PO]
    # Children still ran against the stored window; a parent failing to fetch
    # does not stop increments that are independently correct.
    assert sorted(e[1] for e in h.log if e[0] == "load") == sorted([ITEMS, HIST, SCHED])


def test_a_missing_table_is_pulled_in_full_to_build_the_baseline(h) -> None:
    h.tables.discard("odata_po_history")
    report = _run()
    entry = next(e for e in h.log if e[0] == "fetch" and e[1] == HIST)
    assert entry[2] == "full"
    assert report.baselined == [HIST]
    # The others are still increments.
    assert next(e for e in h.log if e[0] == "fetch" and e[1] == ITEMS)[2] == "delta"


def test_a_child_alone_does_not_advance_its_parent(h) -> None:
    """--set PurchaseOrderItemSet --delta reads the parent's window but never
    the parent itself; there is nothing measured to advance."""
    _run(chosen=(spec_for(ITEMS),))
    assert not any(e[0] == "advance" for e in h.log)
    assert h.marks[PO] == STORED


def test_fetch_only_advances_on_fetch_success(h) -> None:
    """With no load in the sweep, 'landed' means in storage, as before."""
    _run(load=False)
    assert not any(e[0] == "load" for e in h.log)
    assert h.marks[PO] == "2026-09-14 22:00:00+00:00"


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
