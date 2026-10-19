"""The assistant reads the latest built I13 snapshot, whatever day it was built.

With ``I13_SNAPSHOT_STORE=sql`` the served version lives in Azure SQL and the
in-memory slot is always empty, so the assistant asks ``serving_snapshot`` and
reads one row with ``watch_row`` -- the one call both stores answer.

It used to accept only a snapshot built *today* and otherwise computed WATCH
live for the one material. On the production extract that live path scans the
unindexable ``n_*`` views for minutes, so from midnight until the day's rebuild
landed every I13 session hung on "Checking..." and timed out. Now a stored row
from an earlier day is aged to today (``age_watch_row``) and the assistant says
which day the quantities are from; the live compute is gone from the request.

The stored snapshot is faked here by its interface only: ``reference_date`` and
``watch_row``, and a ``watch`` that is a method, as on ``SqlSnapshot``, so any
return to ``snapshot.watch.get(...)`` fails loudly.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

import app.initiatives.i13.snapshot as i13_snapshot
from app.assistant import session
from app.assistant.router import Flow, RoutedFlow
from app.core.config import get_settings
from app.initiatives.i13.config import get_i13_config
from app.initiatives.i13.models import AgingBand
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
KEY = ("1000000123", "1300")


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


@pytest.fixture(autouse=True)
def no_cross_plant_reads(monkeypatch):
    monkeypatch.setattr(session, "PostgresCrossPlantStockProvider", _NoCrossPlantStock)


def _serve(monkeypatch, snapshot):
    monkeypatch.setattr(i13_snapshot, "serving_snapshot", lambda db: snapshot)


def _assess():
    return session._assessment_for(
        None,
        ROUTED,
        requested_quantity=Decimal("5"),
        today=TODAY,
        settings=get_settings(),
        i13_config=get_i13_config(),
    )


def test_the_live_compute_is_gone_from_the_request_path():
    """The session module no longer imports it, so nothing can call it."""
    assert not hasattr(session, "compute_watch_metrics")


def test_todays_snapshot_is_read_as_is(monkeypatch):
    metric = watch_metric(stock_on_hand=Decimal("17"))
    stored = StoredSnapshot(TODAY, {KEY: metric})
    _serve(monkeypatch, stored)

    assessment = _assess()

    assert stored.asked == [KEY]
    assert assessment.metric is metric
    assert assessment.figures_as_of is None
    assert "as of" not in assessment.headline


def test_yesterdays_snapshot_is_used_aged_and_dated(monkeypatch):
    """The case that used to hang: served at once, days moved on by one, and
    the requester told which day the quantities are from."""
    yesterday = TODAY - timedelta(days=1)
    stored = StoredSnapshot(yesterday, {KEY: watch_metric(stock_on_hand=Decimal("17"))})
    _serve(monkeypatch, stored)

    assessment = _assess()

    assert stored.asked == [KEY]
    assert assessment.metric.stock_on_hand == Decimal("17")
    # last movement/issue 2026-06-01, TODAY 2026-07-31.
    assert assessment.metric.days_since_last_movement == 60
    assert assessment.figures_as_of == yesterday
    assert "Stock and usage as of 30 Jul 2026." in assessment.headline
    assert assessment.as_record(TODAY)["figuresAsOf"] == "2026-07-30"


def test_a_never_moved_part_is_still_refused(monkeypatch):
    _serve(monkeypatch, StoredSnapshot(TODAY - timedelta(days=1), {}))

    with pytest.raises(session.SessionError, match="no recorded stock movement or usage"):
        _assess()


def test_no_snapshot_yet_is_refused_at_once(monkeypatch):
    """First start, nothing built: said immediately, never computed live."""
    _serve(monkeypatch, None)

    with pytest.raises(session.SessionError, match="not available yet"):
        _assess()


def test_in_memory_snapshot_answers_watch_row_too():
    """``I13Snapshot.watch_row`` reads its mapping, so callers need not know
    which store served them."""
    metric = watch_metric()
    snapshot = object.__new__(i13_snapshot.I13Snapshot)
    object.__setattr__(snapshot, "watch", {KEY: metric})

    assert snapshot.watch_row("1000000123", "1300") is metric
    assert snapshot.watch_row("1000000123", "1500") is None


def test_aging_moves_the_band_across_a_threshold(monkeypatch):
    """A part last issued 365 days before the snapshot day is FAST that day and
    SLOW the next -- the band follows the reading day, not the build day."""
    issued = TODAY - timedelta(days=366)
    yesterday = TODAY - timedelta(days=1)
    stored = StoredSnapshot(
        yesterday,
        {KEY: watch_metric(last_issue_date=issued, days_since_last_issue=365, aging_band=AgingBand.FAST)},
    )
    _serve(monkeypatch, stored)

    assessment = _assess()

    assert assessment.metric.days_since_last_issue == 366
    assert assessment.metric.aging_band is AgingBand.SLOW


def test_unused_dates_stay_none(monkeypatch):
    stored = StoredSnapshot(
        TODAY - timedelta(days=2),
        {KEY: watch_metric(last_issue_date=None, days_since_last_issue=None, aging_band=AgingBand.NON_MOVING)},
    )
    _serve(monkeypatch, stored)

    metric = _assess().metric

    assert metric.days_since_last_issue is None
    assert metric.aging_band is AgingBand.NON_MOVING


def test_grni_age_and_flag_follow_the_reading_day(monkeypatch):
    received = date(2026, 7, 2)  # 29 days before the snapshot day, 30 before TODAY
    stored = StoredSnapshot(
        TODAY - timedelta(days=1),
        {
            KEY: watch_metric(
                gr_not_issued_relevant_gr_date=received,
                gr_not_issued_days_since_gr=28,
                gr_not_issued_outstanding_quantity=Decimal("3"),
                gr_not_issued_flag=False,
            )
        },
    )
    _serve(monkeypatch, stored)

    metric = _assess().metric

    assert metric.gr_not_issued_days_since_gr == 29
    assert metric.gr_not_issued_flag is False

    # One more day and it crosses the 30-day threshold.
    stored.reference_date = TODAY - timedelta(days=2)
    received_older = date(2026, 7, 1)
    stored._rows[KEY] = watch_metric(
        gr_not_issued_relevant_gr_date=received_older,
        gr_not_issued_days_since_gr=28,
        gr_not_issued_outstanding_quantity=Decimal("3"),
    )
    metric = _assess().metric
    assert metric.gr_not_issued_days_since_gr == 30
    assert metric.gr_not_issued_flag is True
