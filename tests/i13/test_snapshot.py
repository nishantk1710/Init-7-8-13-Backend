"""The I13 in-memory snapshot and what it changed.

Unit tests (no database) cover the pure pieces: captured-plan matching (gaps
G3/G4), the exception queue computed from components, monthly consumption
netting, and the 503 a snapshot route answers while the first build runs.

The Postgres tests prove the point of the whole change -- **a snapshot answer
is the same answer the live compute gives** -- on real data, for a scoped
material so they run in seconds. Skipped without ``DATABASE_URL``.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.initiatives.i13.exceptions import build_exception_queue, exception_queue_from
from app.initiatives.i13.models import ExceptionType
from app.initiatives.i13.plans import ConsumptionPlan, PlanMatcher, PlanSource
from tests.i13.conftest import FakeMovementRepository, FakeProcurementRepository, FakeReservationRepository

TODAY = date(2026, 9, 24)


class _Entry:
    """The fields PlanMatcher reads off a ReservationLedgerEntry."""

    def __init__(self, reservation_number, reservation_item, material, plant, requirement_date, issued=Decimal("0")):
        self.reservation_number = reservation_number
        self.reservation_item = reservation_item
        self.material = material
        self.plant = plant
        self.requirement_date = requirement_date
        self.issued_quantity = issued


def _plan(**overrides) -> ConsumptionPlan:
    values = dict(
        plan_id="PLN-1",
        session_id="S1",
        reservation_number="",
        reservation_item="",
        material="M1",
        plant="1300",
        requester="req",
        purpose="p",
        planned_quantity=Decimal("5"),
        planned_use_date=date(2026, 10, 1),
        status="OPEN",
        source=PlanSource.CAPTURED,
        window_end=date(2026, 10, 31),
        captured_on=date(2026, 9, 20),
    )
    values.update(overrides)
    return ConsumptionPlan(**values)


class TestCapturedPlanMatching:
    def test_an_unlinked_capture_covers_a_reservation_required_inside_its_window(self) -> None:
        plan = _plan()
        assert plan.covers(_Entry("R1", "1", "M1", "1300", date(2026, 10, 15)))

    def test_it_does_not_cover_outside_the_window_or_another_material(self) -> None:
        plan = _plan()
        assert not plan.covers(_Entry("R1", "1", "M1", "1300", date(2026, 9, 30)))
        assert not plan.covers(_Entry("R1", "1", "M1", "1300", date(2026, 11, 1)))
        assert not plan.covers(_Entry("R1", "1", "M2", "1300", date(2026, 10, 15)))
        assert not plan.covers(_Entry("R1", "1", "M1", "1500", date(2026, 10, 15)))

    def test_no_requirement_date_is_never_matched(self) -> None:
        assert not _plan().covers(_Entry("R1", "1", "M1", "1300", None))

    def test_no_window_falls_back_to_the_capture_date(self) -> None:
        plan = _plan(planned_use_date=None, window_end=None)
        assert plan.covers(_Entry("R1", "1", "M1", "1300", date(2026, 9, 21)))
        assert not plan.covers(_Entry("R1", "1", "M1", "1300", date(2026, 9, 19)))

    def test_reference_plans_still_match_by_reservation_only(self) -> None:
        reference = _plan(
            reservation_number="R9", reservation_item="2", source=PlanSource.REFERENCE_CSV, window_end=None
        )
        assert not reference.covers(_Entry("R9", "2", "M1", "1300", date(2026, 10, 15)))
        matcher = PlanMatcher([reference], [_Entry("R9", "2", "M1", "1300", None)])
        assert matcher.plan_for(_Entry("R9", "2", "M1", "1300", None)) is reference

    def test_unlinked_captures_no_longer_collide_on_an_empty_key(self) -> None:
        """Gap G3: every unlinked capture used to be keyed on ("", "")."""
        a = _plan(plan_id="A", material="M1")
        b = _plan(plan_id="B", material="M2")
        entry_a = _Entry("R1", "1", "M1", "1300", date(2026, 10, 10))
        entry_b = _Entry("R2", "1", "M2", "1300", date(2026, 10, 10))
        matcher = PlanMatcher([a, b], [entry_a, entry_b])
        assert matcher.plan_for(entry_a) is a
        assert matcher.plan_for(entry_b) is b
        assert matcher.entries_for(a) == [entry_a]

    def test_a_direct_reservation_match_wins_over_a_window_match(self) -> None:
        direct = _plan(plan_id="D", reservation_number="R1", reservation_item="1", source=PlanSource.CAPTURED)
        window = _plan(plan_id="W")
        entry = _Entry("R1", "1", "M1", "1300", date(2026, 10, 10))
        assert PlanMatcher([window, direct], [entry]).plan_for(entry) is direct

    def test_breach_is_measured_from_the_window_end(self) -> None:
        """Gap G4: a captured plan breached from its window START before."""
        assert _plan().breach_reference_date == date(2026, 10, 31)
        assert _plan(window_end=None).breach_reference_date == date(2026, 10, 1)


class TestExceptionQueueFromComponents:
    """``exception_queue_from`` is what the snapshot serves; it must give the
    same queue ``build_exception_queue`` builds from the repositories."""

    def _fixture(self, i13_config):
        movements = FakeMovementRepository(
            movements=[
                {"Matnr": "M1", "Werks": "1300", "Bwart": "261", "Menge": Decimal("2"), "BudatMkpf": date(2026, 3, 1)},
            ],
            stock={("M1", "1300"): Decimal("10")},
        )
        procurement = FakeProcurementRepository()
        reservations = FakeReservationRepository(
            reservations=[
                {
                    "Rsnum": "R1",
                    "Rspos": "1",
                    "Matnr": "M1",
                    "Werks": "1300",
                    "Bdmng": Decimal("3"),
                    "Bdter": date(2026, 10, 10),
                    "Banfn": None,
                    "Bnfpo": None,
                    "Wempf": "someone",
                }
            ]
        )
        scope = {("M1", "1300"): "PD"}
        return movements, procurement, reservations, scope

    def test_same_queue_as_the_repository_build(self, i13_config, data_dir) -> None:
        from app.initiatives.i13.reservation_ledger import build_reservation_ledger
        from app.initiatives.i13.watch import compute_watch_metrics

        movements, procurement, reservations, scope = self._fixture(i13_config)
        built = build_exception_queue(movements, procurement, reservations, scope, i13_config, data_dir, as_of=TODAY)

        ledger = build_reservation_ledger(
            reservations, procurement, material_scope_index=scope, include_out_of_scope=True
        )
        watch = compute_watch_metrics(movements, procurement, reservations, scope, i13_config, data_dir, as_of=TODAY)
        composed = exception_queue_from(ledger, [], watch, i13_config, as_of=TODAY)

        assert [(e.type, e.material, e.reservation_number) for e in composed] == [
            (e.type, e.material, e.reservation_number) for e in built
        ]

    def test_a_captured_plan_now_clears_no_plan(self, i13_config, data_dir) -> None:
        from app.initiatives.i13.reservation_ledger import build_reservation_ledger

        _, procurement, reservations, scope = self._fixture(i13_config)
        ledger = build_reservation_ledger(
            reservations, procurement, material_scope_index=scope, include_out_of_scope=True
        )
        without = exception_queue_from(ledger, [], [], i13_config, as_of=TODAY)
        assert any(e.type is ExceptionType.NO_PLAN for e in without)

        plan = _plan(planned_use_date=date(2026, 10, 1), window_end=date(2026, 10, 31))
        with_plan = exception_queue_from(ledger, [plan], [], i13_config, as_of=TODAY)
        assert not any(e.type is ExceptionType.NO_PLAN for e in with_plan)
        # ...and it is not already breached: its window has not closed.
        assert not any(e.type is ExceptionType.PLAN_BREACH for e in with_plan)


class TestMonthlyConsumption:
    def test_issues_net_reversals_per_month(self) -> None:
        from app.initiatives.i13.snapshot import _monthly_consumption

        rows = [
            {"Matnr": "M", "Werks": "P", "Bwart": "261", "Menge": Decimal("5"), "BudatMkpf": date(2026, 1, 5)},
            {"Matnr": "M", "Werks": "P", "Bwart": "262", "Menge": Decimal("2"), "BudatMkpf": date(2026, 1, 9)},
            {"Matnr": "M", "Werks": "P", "Bwart": "101", "Menge": Decimal("7"), "BudatMkpf": date(2026, 2, 1)},
            {"Matnr": "M", "Werks": "P", "Bwart": "201", "Menge": Decimal("1"), "BudatMkpf": None},
        ]
        series = _monthly_consumption(rows)[("M", "P")]
        assert [(m.month, m.issued_quantity, m.issue_count, m.received_quantity) for m in series] == [
            ("2026-01", Decimal("3"), 0, Decimal("0")),
            ("2026-02", Decimal("0"), 0, Decimal("7")),
        ]


class TestSnapshotUnavailable:
    def test_a_route_answers_503_while_the_first_build_runs(self, monkeypatch) -> None:
        from datetime import datetime, timezone

        from app.api.i13 import deps
        from app.initiatives.i13.snapshot import SnapshotBuilding

        def building():
            raise SnapshotBuilding(datetime(2026, 9, 24, tzinfo=timezone.utc))

        monkeypatch.setattr(deps, "get_i13_snapshot", building)
        with pytest.raises(HTTPException) as raised:
            deps.snapshot_or_live(live=False)
        assert raised.value.status_code == 503
        assert raised.value.headers["Retry-After"]
        assert raised.value.detail["status"] == "building"

    def test_live_true_bypasses_the_snapshot(self) -> None:
        from app.api.i13 import deps

        assert deps.snapshot_or_live(live=True) is None


# --- Postgres: snapshot answers == live answers -----------------------------

from app.core.config import get_settings  # noqa: E402

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture(scope="module")
def sample_key(client) -> tuple[str, str]:
    rows = client.get("/api/i13/act/utilisation", params={"limit": 50, "aging_band": "FAST"}).json()
    if not rows:
        pytest.skip("no FAST OAR WATCH rows in this extract")
    return rows[len(rows) // 2]["material"], rows[len(rows) // 2]["plant"]


@needs_db
@pytest.mark.needs_seed_data
class TestSnapshotMatchesLive:
    def test_watch_row(self, client, sample_key) -> None:
        material, plant = sample_key
        params = {"material": material, "plant": plant}
        snapshot = client.get("/api/i13/watch", params=params).json()
        live = client.get("/api/i13/watch", params={**params, "live": "true"}).json()
        drop = {"calculated_at"}
        assert [{k: v for k, v in r.items() if k not in drop} for r in snapshot] == [
            {k: v for k, v in r.items() if k not in drop} for r in live
        ]

    def test_reservation_ledger(self, client, sample_key) -> None:
        material, plant = sample_key
        params = {"material": material, "plant": plant, "limit": 1000}
        snapshot = client.get("/api/i13/utilisation-ledger", params=params).json()
        live = client.get("/api/i13/utilisation-ledger", params={**params, "live": "true"}).json()
        assert snapshot == live

    def test_reclassification(self, client, sample_key) -> None:
        material, plant = sample_key
        params = {"material": material, "plant": plant}
        drop = {"generated_at"}
        snapshot = client.get("/api/i13/reclassification", params=params).json()
        live = client.get("/api/i13/reclassification", params={**params, "live": "true"}).json()
        assert [{k: v for k, v in r.items() if k not in drop} for r in snapshot] == [
            {k: v for k, v in r.items() if k not in drop} for r in live
        ]

    def test_list_routes_report_a_total(self, client) -> None:
        for path in ("/api/i13/act/exceptions", "/api/i13/act/utilisation", "/api/i13/grni", "/api/i13/ledger"):
            response = client.get(path, params={"limit": 5})
            assert response.status_code == 200, (path, response.text)
            assert int(response.headers["x-total-count"]) >= len(response.json()), path

    def test_snapshot_status_and_headers(self, client) -> None:
        response = client.get("/api/i13/summary")
        assert response.status_code == 200
        assert response.headers["x-i13-data-as-of"]
        status = client.get("/api/i13/snapshot").json()
        assert status["status"] == "ready"
        assert status["version"] >= 1

    def test_usage_pattern_months_cover_the_history(self, client) -> None:
        response = client.get("/api/i13/usage-patterns", params={"limit": 1})
        assert response.status_code == 200
        body = response.json()
        first, last = response.headers["x-i13-history-months"].split("..")
        assert body[0]["months"][0]["month"] == first
        assert body[0]["months"][-1]["month"] == last
