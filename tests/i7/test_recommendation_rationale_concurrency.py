"""Recommendation explanations run several at a time, outside any database session.

Each explanation is one AI call (about 4 s on Foundry). One at a time, a full
catalogue took most of a day, and the session held open across that wait was
dropped by Azure SQL before the final write. These tests pin the two fixes:
the calls run side by side, and no session is open while they do.
"""

import logging
import threading
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import rationale, service
from app.initiatives.i7.recommendations.rationale import RationaleResult


def fake_built(n: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(sap_material_number=f"M{i:03d}") for i in range(n)]


def by_material(item) -> RationaleResult:
    return RationaleResult(text=f"why {item.sap_material_number}", source="AI_GENERATED")


def test_results_come_back_in_the_same_order_as_the_recommendations(monkeypatch):
    monkeypatch.setattr(rationale, "generate_rationale", by_material)
    built = fake_built(40)

    results = service._generate_rationales(built, workers=8)

    assert [r.text for r in results] == [f"why M{i:03d}" for i in range(40)]


def test_the_calls_really_run_at_the_same_time(monkeypatch):
    """Four calls that each wait for the other three can only finish if all
    four are in flight together -- one at a time, the barrier times out."""
    barrier = threading.Barrier(4, timeout=5)

    def waits_for_the_others(item):
        barrier.wait()
        return by_material(item)

    monkeypatch.setattr(rationale, "generate_rationale", waits_for_the_others)

    results = service._generate_rationales(fake_built(4), workers=4)

    assert len(results) == 4


def test_one_worker_still_works(monkeypatch):
    monkeypatch.setattr(rationale, "generate_rationale", by_material)
    assert len(service._generate_rationales(fake_built(3), workers=1)) == 3


def test_no_recommendations_means_no_calls(monkeypatch):
    def must_not_be_called(item):
        raise AssertionError("called with nothing to explain")

    monkeypatch.setattr(rationale, "generate_rationale", must_not_be_called)
    assert service._generate_rationales([], workers=8) == []


def test_progress_is_logged_with_the_sources(monkeypatch, caplog):
    monkeypatch.setattr(rationale, "generate_rationale", by_material)
    monkeypatch.setattr(service, "RATIONALE_PROGRESS_EVERY", 5)

    with caplog.at_level(logging.INFO, logger=service.logger.name):
        service._generate_rationales(fake_built(12), workers=3)

    progress = [r.getMessage() for r in caplog.records if "explanations:" in r.getMessage()]
    assert progress[0].startswith("recommendation explanations: 5 of 12 done")
    assert progress[-1] == "recommendation explanations: 12 of 12 done (AI_GENERATED 12)"


def test_concurrency_defaults_to_eight_and_refuses_zero():
    assert Settings(_env_file=None).i7_rationale_concurrency == 8
    with pytest.raises(ValidationError):
        Settings(_env_file=None, i7_rationale_concurrency=0)


def test_no_database_session_is_open_while_explanations_are_written(monkeypatch):
    """Read in one session, explain with none open, write in a fresh one."""
    open_sessions = 0
    events: list[str] = []

    class FakeSession:
        def __enter__(self):
            nonlocal open_sessions
            open_sessions += 1
            events.append("open")
            return self

        def __exit__(self, *exc):
            nonlocal open_sessions
            open_sessions -= 1
            events.append("close")
            return False

        def execute(self, *args, **kwargs):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, row):
            events.append("add")

        def commit(self):
            events.append("commit")

    target = SimpleNamespace(
        sap_material_number="4000000001",
        sap_plant_code="1300",
        history_status="NO_HISTORY",
        criticality=None,
        unit_price=None,
        feature_run_id=1,
    )
    monkeypatch.setattr(service, "get_sessionmaker", lambda: FakeSession)
    monkeypatch.setattr(service.repository, "latest_inventory_run", lambda s: None)
    monkeypatch.setattr(service.repository, "latest_oar_run", lambda s: 7)
    monkeypatch.setattr(service.repository, "load_oar_inputs", lambda s, run: [])
    monkeypatch.setattr(service.repository, "load_uncovered_oar_targets", lambda s, run: [target])

    def explain(built, workers):
        assert open_sessions == 0, "a database session was held open during the AI calls"
        events.append("explain")
        return [RationaleResult(text="why", source="DETERMINISTIC_FALLBACK") for _ in built]

    monkeypatch.setattr(service, "_generate_rationales", explain)

    result = service.generate_recommendations(PolicyDocument())

    assert result.error is None
    assert result.recommendations_written == 1
    assert events == ["open", "close", "explain", "open", "add", "commit", "close"]
