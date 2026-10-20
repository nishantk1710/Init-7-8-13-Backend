"""When the I13 store builds, re-ages or does nothing -- and what moves its fingerprint.

A full build of the production extract loads Azure SQL for over an hour. It
should run only when the SAP data (or the config) changed, and -- with
I13_AUTO_BUILD_WINDOW_UTC set -- only in the night window. A new day alone is
a re-age: minutes, no SAP read, never held back (lifecycle.decide).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.initiatives.i13 import snapshot
from app.initiatives.i13.config import get_i13_config
from app.initiatives.i13.snapshot_store.lifecycle import BUILD, REAGE, decide, in_window

QUIET = {"pull_running": False, "gap_holding": False, "in_window": True}


# --- decide ----------------------------------------------------------------------


def test_nothing_served_is_built_whatever_the_hour():
    assert decide(served=False, data_changed=True, day_turned=False, pull_running=True,
                  gap_holding=True, in_window=False) == BUILD


def test_a_new_day_alone_is_a_reage_not_a_build():
    assert decide(served=True, data_changed=False, day_turned=True, **QUIET) == REAGE


def test_nothing_changed_is_nothing():
    assert decide(served=True, data_changed=False, day_turned=False, **QUIET) is None


def test_a_data_change_in_the_window_is_a_build():
    assert decide(served=True, data_changed=True, day_turned=True, **QUIET) == BUILD


@pytest.mark.parametrize("held", [
    {"pull_running": True, "gap_holding": False, "in_window": True},
    {"pull_running": False, "gap_holding": True, "in_window": True},
    {"pull_running": False, "gap_holding": False, "in_window": False},
])
def test_a_held_back_data_change_still_re_ages_a_new_day(held):
    """The screens stay measured as of today while the full build waits."""
    assert decide(served=True, data_changed=True, day_turned=True, **held) == REAGE
    assert decide(served=True, data_changed=True, day_turned=False, **held) is None


# --- in_window -------------------------------------------------------------------


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 14, hour, minute, tzinfo=timezone.utc)


def test_no_window_is_always_open():
    assert in_window("", _at(9)) and in_window("  ", _at(23, 59))


def test_a_window_inside_one_day():
    assert in_window("01:00-03:30", _at(1)) and in_window("01:00-03:30", _at(3, 29))
    assert not in_window("01:00-03:30", _at(3, 30)) and not in_window("01:00-03:30", _at(0, 59))


def test_a_window_across_midnight():
    """18:30-23:30 UTC is 00:00-05:00 IST."""
    spec = "18:30-00:30"
    assert in_window(spec, _at(18, 30)) and in_window(spec, _at(23, 59)) and in_window(spec, _at(0, 29))
    assert not in_window(spec, _at(0, 30)) and not in_window(spec, _at(9)) and not in_window(spec, _at(18, 29))


def test_an_unreadable_window_does_not_stop_builds():
    assert in_window("tonight", _at(9))


# --- the fingerprint -------------------------------------------------------------


class _Db:
    """No ingestion runs: the fingerprint is then config, plans and (maybe) date."""

    def execute(self, statement):
        return SimpleNamespace(all=list, one=lambda: (0, None))


def _fingerprint(monkeypatch, store: str, day: date, **overrides) -> str:
    monkeypatch.setattr(snapshot, "reference_date_for", lambda settings=None: day)
    settings = get_settings().model_copy(update={"i13_snapshot_store": store, **overrides})
    return snapshot.compute_fingerprint(_Db(), settings=settings, config=get_i13_config())


def test_the_sql_store_does_not_rebuild_for_a_new_day(monkeypatch):
    assert _fingerprint(monkeypatch, "sql", date(2026, 10, 13)) == _fingerprint(monkeypatch, "sql", date(2026, 10, 14))


def test_the_memory_store_still_does(monkeypatch):
    """Its values are frozen at build time, so for it a new day is a rebuild."""
    assert _fingerprint(monkeypatch, "memory", date(2026, 10, 13)) != _fingerprint(monkeypatch, "memory", date(2026, 10, 14))


def test_moving_a_pinned_reference_date_still_rebuilds(monkeypatch):
    day = date(2026, 10, 14)
    assert _fingerprint(monkeypatch, "sql", day, i13_snapshot_reference_date="2026-10-01") != _fingerprint(
        monkeypatch, "sql", day, i13_snapshot_reference_date="2026-10-02"
    )


def test_changing_which_materials_are_oar_rebuilds(monkeypatch):
    """The OAR controller list and the deletion switch decide the population
    every screen reads, so a deploy that changes either must rebuild."""
    day = date(2026, 10, 20)
    base = _fingerprint(monkeypatch, "sql", day)
    assert base != _fingerprint(monkeypatch, "sql", day, i13_oar_mrp_controllers="OAR,POD")
    assert base != _fingerprint(monkeypatch, "sql", day, i13_exclude_deleted_materials=False)
    # Same codes, different spelling: the same population, so no rebuild.
    assert base == _fingerprint(monkeypatch, "sql", day, i13_oar_mrp_controllers=" oar ")


def test_a_new_record_layout_rebuilds_the_sql_store(monkeypatch):
    """A kind that starts storing a new column (kinds.RECORD_LAYOUT) needs a
    build to fill it; the served version stays readable meanwhile."""
    from app.initiatives.i13.snapshot_store import kinds

    day = date(2026, 10, 20)
    before = _fingerprint(monkeypatch, "sql", day)
    monkeypatch.setattr(kinds, "RECORD_LAYOUT", kinds.RECORD_LAYOUT + 1)
    assert _fingerprint(monkeypatch, "sql", day) != before


def test_uat_reservations_do_not_rebuild_the_sql_store(monkeypatch):
    """A UAT stamp refreshes its own material in place (uat._apply); counting it
    in the fingerprint started a full rebuild of over an hour per stamp."""

    class _UatDb(_Db):
        def execute(self, statement):  # would be asked for the UAT count
            if "uat_reservation_sgtxt" in str(statement):
                raise AssertionError("the sql store's fingerprint read the UAT overlay")
            return super().execute(statement)

    monkeypatch.setattr(snapshot, "reference_date_for", lambda settings=None: date(2026, 10, 14))
    settings = get_settings().model_copy(update={"i13_snapshot_store": "sql", "i13_uat_simulation_enabled": True})
    snapshot.compute_fingerprint(_UatDb(), settings=settings, config=get_i13_config())


# --- retrying a dropped connect (app/core/db.py) ---------------------------------


class _OdbcError(Exception):
    """Shaped like pyodbc's errors: (SQLSTATE, message)."""


def test_which_connect_failures_are_retried():
    from app.core.db import is_transient

    reset = Exception("08001", "[08001] [Microsoft][ODBC Driver 18 for SQL Server]TCP Provider: Error code 0x68 (104)")
    waking = Exception("42000", "Database 'sqldb-aicom' on server is not currently available. (40613)")
    login = Exception("28000", "Login failed for user 'app'. (18456)")
    assert is_transient(reset) and is_transient(waking)
    assert not is_transient(login) and not is_transient(ValueError("bad"))


def test_a_transient_connect_is_retried_then_succeeds(monkeypatch):
    from app.core import db

    calls = []

    class _Dialect:
        def connect(self, *args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise _OdbcError("08001", "TCP Provider: Error code 0x68 (104)")
            return "connection"

    monkeypatch.setattr(db.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        db, "get_settings",
        lambda: SimpleNamespace(database_connect_attempts=3, database_connect_retry_seconds=2.0),
    )
    assert db._connect_with_retry(_Dialect(), None, (), {}) == "connection"
    assert len(calls) == 3


def test_a_non_transient_connect_is_not_retried(monkeypatch):
    from app.core import db

    calls = []

    class _Dialect:
        def connect(self, *args, **kwargs):
            calls.append(1)
            raise _OdbcError("28000", "Login failed. (18456)")

    monkeypatch.setattr(db.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        db, "get_settings",
        lambda: SimpleNamespace(database_connect_attempts=3, database_connect_retry_seconds=2.0),
    )
    with pytest.raises(Exception, match="Login failed"):
        db._connect_with_retry(_Dialect(), None, (), {})
    assert len(calls) == 1
