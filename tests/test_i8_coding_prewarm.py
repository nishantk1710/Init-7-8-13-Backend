"""Repair Candidates: the screen is built in the background after a snapshot goes live.

Every snapshot swap drops the cached screen. The page then built it on first
open (~10 s), and the menu's badge -- which only READS a built screen -- showed
nothing until somebody opened the page. On real data, where SAP moves through
the day, that is most of the time.

The pieces are replaced with recorders, so this needs neither the database nor
a real snapshot.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from types import SimpleNamespace

import pytest

from app.initiatives.i8 import service
from app.initiatives.i8.config import I8Settings


class FakeTimer:
    started: list["FakeTimer"] = []

    def __init__(self, interval, function, args=None):
        self.interval, self.function, self.args = interval, function, tuple(args or ())
        self.daemon = False
        self.name = ""

    def start(self):
        FakeTimer.started.append(self)


@pytest.fixture(autouse=True)
def clean():
    service.reset_snapshot()
    FakeTimer.started = []
    yield
    service.reset_snapshot()


def _cfg(**over) -> I8Settings:
    return I8Settings(**over)


def test_a_swap_schedules_the_build_after_the_configured_delay(monkeypatch):
    monkeypatch.setattr(service, "get_i8_settings", lambda: _cfg(coding_prewarm_delay_seconds=45))
    monkeypatch.setattr(service.threading, "Timer", FakeTimer)
    snapshot = object()

    service._schedule_coding_prewarm(snapshot)

    (timer,) = FakeTimer.started
    assert timer.interval == 45
    assert timer.function is service._prewarm_coding_screen
    assert timer.args == (snapshot,)
    assert timer.daemon  # must never keep the server from stopping


def test_switched_off_schedules_nothing(monkeypatch):
    monkeypatch.setattr(service, "get_i8_settings", lambda: _cfg(coding_prewarm_enabled=False))
    monkeypatch.setattr(service.threading, "Timer", FakeTimer)

    service._schedule_coding_prewarm(object())

    assert FakeTimer.started == []


def test_the_setting_has_a_default_that_is_on():
    assert _cfg().coding_prewarm_enabled is True
    assert _cfg().coding_prewarm_delay_seconds == 60.0


def test_build_locked_schedules_the_prewarm_after_dropping_the_old_screen(monkeypatch):
    """The hook sits at the one line that makes a snapshot live."""
    from app.initiatives.i8.snapshot_store import lifecycle

    live = object()
    order: list[str] = []
    monkeypatch.setattr(lifecycle, "enabled", lambda: False)
    monkeypatch.setattr(service, "exclusive_build", lambda name: contextlib.nullcontext())
    monkeypatch.setattr(service, "build_snapshot", lambda db, cfg: live)
    monkeypatch.setattr(service, "reset_attestation_view", lambda: order.append("attestation reset"))
    monkeypatch.setattr(service, "reset_coding_screen", lambda: order.append("screen reset"))
    monkeypatch.setattr(service, "_schedule_coding_prewarm", lambda snap: order.append(("prewarm", snap)))

    result = service._build_locked(object(), None, "test")

    assert result is live
    assert order == ["attestation reset", "screen reset", ("prewarm", live)]


def _fake_db(monkeypatch):
    sessions = []

    class Db:
        closed = False

        def close(self):
            self.closed = True

    def maker():
        db = Db()
        sessions.append(db)
        return db

    monkeypatch.setattr(service, "get_sessionmaker", lambda: maker)
    return sessions


def test_the_background_build_asks_for_the_default_screen(monkeypatch):
    snapshot = object()
    service._state.snapshot = snapshot
    sessions = _fake_db(monkeypatch)
    calls = []
    monkeypatch.setattr(service, "get_coding_screen", lambda db, snap, *a, **k: calls.append((db, snap, a, k)))

    service._prewarm_coding_screen(snapshot)

    ((db, snap, args, kwargs),) = calls
    assert snap is snapshot
    assert args == () and kwargs == {}  # the defaults are the key the page and the badge read
    assert db is sessions[0] and db.closed


def test_a_snapshot_replaced_in_the_meantime_is_not_built_for(monkeypatch):
    service._state.snapshot = object()  # a newer one went live while the timer ran
    _fake_db(monkeypatch)
    calls = []
    monkeypatch.setattr(service, "get_coding_screen", lambda *a, **k: calls.append(a))

    service._prewarm_coding_screen(object())

    assert calls == []


def test_a_failed_background_build_is_logged_never_raised(monkeypatch, caplog):
    snapshot = object()
    service._state.snapshot = snapshot
    sessions = _fake_db(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("database paused")

    monkeypatch.setattr(service, "get_coding_screen", boom)
    caplog.set_level(logging.INFO, logger=service.logger.name)

    service._prewarm_coding_screen(snapshot)  # must return

    assert sessions[0].closed
    assert any("background build failed" in r.getMessage() for r in caplog.records)


def test_the_badge_reads_a_built_screen():
    cfg = _cfg()
    unscreened = SimpleNamespace(verdict="UNSCREENED")
    judged = SimpleNamespace(verdict="MISCODED_REPAIRABLE")
    service._screens[(False, None, cfg.coding_candidate_source_list)] = SimpleNamespace(
        candidates=(unscreened, unscreened, judged)
    )
    try:
        assert service.peek_unscreened_count(cfg) == 2
    finally:
        service.reset_coding_screen()


def test_the_badge_answers_none_at_once_while_a_screen_is_being_built():
    """It is asked on every page; it must not queue behind a ~10 s build."""
    cfg = _cfg()
    holding, release = threading.Event(), threading.Event()

    def build():  # what get_coding_screen does: holds the lock for the whole build
        with service._screen_lock:
            holding.set()
            release.wait(5)

    builder = threading.Thread(target=build)
    builder.start()
    try:
        assert holding.wait(2)
        answered = []
        asker = threading.Thread(target=lambda: answered.append(service.peek_unscreened_count(cfg)))
        asker.start()
        asker.join(1.0)  # a blocking peek would still be waiting here
        assert not asker.is_alive(), "peek_unscreened_count waited for the build"
        assert answered == [None]
    finally:
        release.set()
        builder.join(2)
