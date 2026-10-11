"""The assistant reads the served I13 snapshot, whichever store holds it.

With ``I13_SNAPSHOT_STORE=sql`` the served version lives in Azure SQL and the
in-memory slot is always empty. The assistant used to ask only the in-memory
slot (``peek_i13_snapshot``), so on Azure every session fell through to the
single-material live compute -- full scans of the unindexable ``n_*`` views,
minutes per session on the production extract. It now asks
``serving_snapshot``, which returns the SQL version, and reads the row with
``watch_row`` -- the one call both stores answer.

The stored snapshot is faked here by its interface only: ``reference_date`` and
``watch_row``, and a ``watch`` that is a method, as on ``SqlSnapshot``, so any
return to ``snapshot.watch.get(...)`` fails loudly.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import app.initiatives.i13.snapshot as i13_snapshot
from app.assistant import session
from app.assistant.router import Flow, RoutedFlow
from app.core.config import get_settings
from app.initiatives.i13.config import get_i13_config
from app.shared.material_scope import MaterialScope
from tests.assistant.conftest import TODAY, watch_metric

ROUTED = RoutedFlow(
    flow=Flow.I13,
    material_id="1000000123",
    plant="1300",
    eighty_series=False,
    material_scope=MaterialScope.OAR,
    mrp_type="PD",
)


class StoredSnapshot:
    """``SqlSnapshot``'s surface, as far as the assistant reads it."""

    def __init__(self, reference_date, rows):
        self.reference_date = reference_date
        self._rows = rows
        self.asked: list[tuple[str, str]] = []

    def watch(self, **filters):  # a paged query on SqlSnapshot, not a mapping
        raise AssertionError("the assistant must read one row with watch_row, not page WATCH")

    def watch_row(self, material, plant):
        self.asked.append((material, plant))
        return self._rows.get((material, plant))


class _NoCrossPlantStock:
    def __init__(self, _repository):
        pass

    def get_other_plant_stock(self, *, material, exclude_plant):
        return ()


@pytest.fixture
def no_live_compute(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("served a snapshot for today, yet the assistant recomputed WATCH live")

    monkeypatch.setattr(session, "compute_watch_metrics", refuse)
    monkeypatch.setattr(session, "PostgresCrossPlantStockProvider", _NoCrossPlantStock)


def _assess():
    return session._assessment_for(
        None,
        ROUTED,
        requested_quantity=Decimal("5"),
        today=TODAY,
        settings=get_settings(),
        i13_config=get_i13_config(),
    )


def test_reads_the_stored_watch_row_and_does_not_recompute(monkeypatch, no_live_compute):
    metric = watch_metric(stock_on_hand=Decimal("17"))
    stored = StoredSnapshot(TODAY, {("1000000123", "1300"): metric})
    monkeypatch.setattr(i13_snapshot, "serving_snapshot", lambda db: stored)

    assessment = _assess()

    assert stored.asked == [("1000000123", "1300")]
    assert assessment.metric is metric


def test_a_snapshot_from_another_day_sends_it_live(monkeypatch):
    """Unchanged behaviour: a version measured as of yesterday is not today's
    cover position, so the live compute answers instead."""
    from datetime import timedelta

    stored = StoredSnapshot(TODAY - timedelta(days=1), {})
    monkeypatch.setattr(i13_snapshot, "serving_snapshot", lambda db: stored)
    live = watch_metric(stock_on_hand=Decimal("3"))
    calls = []

    def compute(*args, **kwargs):
        calls.append(kwargs)
        return [live]

    monkeypatch.setattr(session, "compute_watch_metrics", compute)
    monkeypatch.setattr(session, "PostgresCrossPlantStockProvider", _NoCrossPlantStock)
    monkeypatch.setattr(session, "fetch_material_scope_index", lambda db, **kw: {})

    assessment = _assess()

    assert stored.asked == []
    assert len(calls) == 1 and assessment.metric is live


def test_in_memory_snapshot_answers_watch_row_too():
    """``I13Snapshot.watch_row`` reads its mapping, so callers need not know
    which store served them."""
    metric = watch_metric()
    snapshot = object.__new__(i13_snapshot.I13Snapshot)
    object.__setattr__(snapshot, "watch", {("1000000123", "1300"): metric})

    assert snapshot.watch_row("1000000123", "1300") is metric
    assert snapshot.watch_row("1000000123", "1500") is None
