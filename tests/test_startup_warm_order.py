"""The start-up warm-up: a stored I08 snapshot is served ahead of the slow loaders.

On nonprod (4-Nov-2026) I08 answered 503 "building" for ~25 s after every
restart, although loading its stored snapshot takes a fraction of a second: the
warm-up ran the delivery-date map (~20 s) and the material index (~5 s) first.
With a stored snapshot ready, I08 now starts right after the material names;
with none (a new database, a codec bump) it still builds after the loaders,
because a build must not run beside them.

Every collaborator is replaced with a recorder, so this checks ORDER only and
needs neither the database nor the extracts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import main

I8_THREAD = object()  # stands in for the I08 build thread I13 waits on


def _settings(**over):
    base = dict(
        database_url="mssql+pyodbc://x",
        normalise_views_on_startup=True,
        i8_snapshot_warm_on_startup=True,
        i13_snapshot_enabled=True,
        i13_snapshot_warm_on_startup=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def run(monkeypatch):
    """``run(settings, stored)`` -> (the calls in order, what I13 was told to wait for)."""
    from app.assistant import departments, material_index
    from app.initiatives.i8 import service as i8_service
    from app.initiatives.i8.snapshot_store import lifecycle as i8_store
    from app.initiatives.i13 import expected_delivery
    from app.integrations.criticality import baseline
    from app.shared import material_names, sap_normalise

    calls: list[str] = []
    seen: dict[str, object] = {}

    def rec(name, result=None):
        def _fn(*args, **kwargs):
            calls.append(name)
            return result

        return _fn

    def i13_start(reason, *, after=None, **kwargs):
        calls.append("i13.start")
        seen["after"] = after

    monkeypatch.setattr(baseline, "run", rec("criticality"))
    monkeypatch.setattr(sap_normalise, "rebuild_read_layers", rec("views"))
    monkeypatch.setattr(material_names, "start_loading", rec("names.start"))
    monkeypatch.setattr(material_names, "wait_for_load", rec("names.wait"))
    monkeypatch.setattr(expected_delivery, "start_loading", rec("eket.start"))
    monkeypatch.setattr(expected_delivery, "wait_for_load", rec("eket.wait"))
    monkeypatch.setattr(material_index, "start_loading", rec("index.start"))
    monkeypatch.setattr(material_index, "wait_for_load", rec("index.wait"))
    monkeypatch.setattr(departments, "start_loading", rec("departments.start"))
    monkeypatch.setattr(i8_service, "start_background_build", rec("i8.start"))
    monkeypatch.setattr(i8_service, "build_thread", lambda: I8_THREAD)
    monkeypatch.setattr(main.i13_snapshot, "start_background_build", i13_start)

    def go(settings, stored):
        asked = []

        def stored_ready():
            asked.append(True)
            if isinstance(stored, Exception):
                raise stored
            return stored

        monkeypatch.setattr(i8_store, "stored_ready", stored_ready)
        main._warm_on_startup(settings)
        return calls, seen.get("after"), bool(asked)

    return go


LOADERS = ["eket.start", "eket.wait", "index.start", "index.wait", "departments.start"]


def test_a_stored_snapshot_starts_i8_before_the_slow_loaders(run):
    calls, after, _ = run(_settings(), stored=True)

    assert calls == ["criticality", "views", "names.start", "names.wait", "i8.start", *LOADERS, "i13.start"]
    assert calls.count("i8.start") == 1  # the fallback start must not run a second time
    assert after is I8_THREAD  # I13 still waits for I08


def test_with_nothing_stored_i8_builds_after_the_loaders(run):
    calls, after, _ = run(_settings(), stored=False)

    assert calls == ["criticality", "views", "names.start", "names.wait", *LOADERS, "i8.start", "i13.start"]
    assert after is I8_THREAD


def test_the_material_names_stay_ahead_of_i8(run):
    calls, _, _ = run(_settings(), stored=True)

    assert calls.index("names.wait") < calls.index("i8.start")


def test_i8_warm_up_switched_off_starts_nothing_and_asks_nothing(run):
    calls, after, asked = run(_settings(i8_snapshot_warm_on_startup=False), stored=True)

    assert "i8.start" not in calls
    assert asked is False
    assert after is None
    assert calls[-1] == "i13.start"


def test_without_a_database_the_snapshot_is_not_looked_for(run):
    calls, _, asked = run(_settings(database_url=""), stored=True)

    assert calls == ["i13.start"]
    assert asked is False


def test_a_failing_warm_up_is_logged_and_never_raised(run, monkeypatch, caplog):
    # Not the stored check (that answers False for itself): anything else breaking.
    from app.shared import material_names

    def boom(*args, **kwargs):
        raise RuntimeError("names failed")

    monkeypatch.setattr(material_names, "start_loading", boom)

    calls, _, _ = run(_settings(), stored=True)  # must return, not raise

    assert "i8.start" not in calls
    assert any("start-up warm-up failed" in r.getMessage() for r in caplog.records)


class _FakeSession:
    closed = False

    def close(self):
        self.closed = True


def _stored_ready_env(monkeypatch, *, enabled=True, latest):
    from app.core import db as core_db
    from app.initiatives.i8.snapshot_store import lifecycle, store

    session = _FakeSession()
    monkeypatch.setattr(lifecycle, "enabled", lambda: enabled)
    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: (lambda: session))
    monkeypatch.setattr(store, "latest_ready", latest)
    return lifecycle, session


def test_stored_ready_is_true_when_a_ready_run_exists(monkeypatch):
    lifecycle, session = _stored_ready_env(monkeypatch, latest=lambda db: object())

    assert lifecycle.stored_ready() is True
    assert session.closed


def test_stored_ready_is_false_with_no_ready_run(monkeypatch):
    lifecycle, session = _stored_ready_env(monkeypatch, latest=lambda db: None)

    assert lifecycle.stored_ready() is False
    assert session.closed


def test_stored_ready_is_false_when_the_store_is_off(monkeypatch):
    def never(db):
        raise AssertionError("must not touch the database when the store is off")

    lifecycle, _ = _stored_ready_env(monkeypatch, enabled=False, latest=never)

    assert lifecycle.stored_ready() is False


def test_stored_ready_is_false_when_the_database_cannot_be_read(monkeypatch):
    def down(db):
        raise OSError("database paused")

    lifecycle, session = _stored_ready_env(monkeypatch, latest=down)

    assert lifecycle.stored_ready() is False  # the old order then applies
    assert session.closed
