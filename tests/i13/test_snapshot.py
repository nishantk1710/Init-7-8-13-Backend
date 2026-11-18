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
    def test_an_unlinked_capture_matches_no_reservation_even_inside_its_window(self) -> None:
        """Same material, plant and window, but no item text carries its ID:
        someone else's reservation, never the plan's (5-Nov-2026)."""
        plan = _plan()
        entry = _Entry("R1", "1", "M1", "1300", date(2026, 10, 15))
        matcher = PlanMatcher([plan], [entry])
        assert matcher.plan_for(entry) is None
        assert matcher.entries_for(plan) == []

    def test_a_linked_capture_matches_only_its_linked_reservations(self) -> None:
        plan = _plan(linked_reservations=(("R1", "1"),))
        own = _Entry("R1", "1", "M1", "1300", date(2026, 12, 1))
        other = _Entry("R2", "1", "M1", "1300", date(2026, 10, 15))
        matcher = PlanMatcher([plan], [own, other])
        assert matcher.plan_for(own) is plan
        assert matcher.plan_for(other) is None
        assert matcher.entries_for(plan) == [own]

    def test_reference_plans_still_match_by_reservation_only(self) -> None:
        reference = _plan(
            reservation_number="R9", reservation_item="2", source=PlanSource.REFERENCE_CSV, window_end=None
        )
        matcher = PlanMatcher([reference], [_Entry("R9", "2", "M1", "1300", None)])
        assert matcher.plan_for(_Entry("R9", "2", "M1", "1300", None)) is reference
        assert matcher.plan_for(_Entry("R8", "1", "M1", "1300", date(2026, 10, 15))) is None

    def test_unlinked_captures_never_collide_on_an_empty_key(self) -> None:
        """Gap G3: every unlinked capture used to be keyed on ("", "")."""
        a = _plan(plan_id="A", material="M1")
        b = _plan(plan_id="B", material="M2")
        blank = _Entry("", "", "M1", "1300", date(2026, 10, 10))
        assert PlanMatcher([a, b], [blank]).plan_for(blank) is None

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
        scope = {("M1", "1300"): "OAR"}
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

    def test_only_a_linked_captured_plan_clears_no_plan(self, i13_config, data_dir) -> None:
        from app.initiatives.i13.reservation_ledger import build_reservation_ledger

        _, procurement, reservations, scope = self._fixture(i13_config)
        ledger = build_reservation_ledger(
            reservations, procurement, material_scope_index=scope, include_out_of_scope=True
        )
        without = exception_queue_from(ledger, [], [], i13_config, as_of=TODAY)
        assert any(e.type is ExceptionType.NO_PLAN for e in without)

        # A plan for the same part and window does not cover it: no item text
        # links the two, so the reservation is still Non-AI.
        unlinked = _plan(planned_use_date=date(2026, 10, 1), window_end=date(2026, 10, 31))
        assert any(
            e.type is ExceptionType.NO_PLAN
            for e in exception_queue_from(ledger, [unlinked], [], i13_config, as_of=TODAY)
        )

        plan = _plan(planned_use_date=date(2026, 10, 1), window_end=date(2026, 10, 31), linked_reservations=(("R1", "1"),))
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
            # 2 of the 5 taken back: the issue still happened, once.
            ("2026-01", Decimal("3"), 1, Decimal("0")),
            ("2026-02", Decimal("0"), 0, Decimal("7")),
        ]

    def test_a_reversal_is_taken_off_the_month_of_the_issue_it_cancels(self) -> None:
        """8000004187 (7-Nov-2026): each issue reversed the month after. The
        Usage tab showed +1 then -1 and a last issue in October 2025; the
        Consumption tab, correctly, April 2024."""
        from app.initiatives.i13.snapshot import _monthly_consumption

        def mv(bwart, day):
            return {"Matnr": "M", "Werks": "P", "Bwart": bwart, "Menge": Decimal("1"), "BudatMkpf": day}

        rows = [
            mv("261", date(2024, 4, 18)),
            mv("261", date(2025, 1, 10)), mv("262", date(2025, 3, 5)),
            mv("261", date(2025, 10, 2)), mv("262", date(2025, 11, 4)),
        ]
        series = _monthly_consumption(rows)[("M", "P")]
        assert all(m.issued_quantity >= 0 for m in series)
        assert [(m.month, m.issued_quantity, m.issue_count) for m in series] == [
            ("2024-04", Decimal("1"), 1),
            ("2025-01", Decimal("0"), 0),
            ("2025-10", Decimal("0"), 0),
        ]
        assert max(m.month for m in series if m.issue_count) == "2024-04"

    def test_a_reversal_of_an_older_issue_does_not_cancel_this_months(self) -> None:
        """8000005907 (7-Nov-2026): April 2026 held an issue and a 262 for an
        earlier one. Netted by month it read as no issue at all."""
        from app.initiatives.i13.snapshot import _monthly_consumption

        def mv(bwart, day):
            return {"Matnr": "M", "Werks": "P", "Bwart": bwart, "Menge": Decimal("1"), "BudatMkpf": day}

        rows = [mv("261", date(2025, 12, 1)), mv("262", date(2026, 4, 2)), mv("261", date(2026, 4, 13))]
        series = {m.month: (m.issued_quantity, m.issue_count) for m in _monthly_consumption(rows)[("M", "P")]}
        assert series == {"2025-12": (Decimal("0"), 0), "2026-04": (Decimal("1"), 1)}

    def test_a_reversal_of_an_issue_before_the_history_is_ignored(self) -> None:
        from app.initiatives.i13.snapshot import _monthly_consumption

        rows = [{"Matnr": "M", "Werks": "P", "Bwart": "262", "Menge": Decimal("2"), "BudatMkpf": date(2026, 3, 1)}]
        assert _monthly_consumption(rows) == {}


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
        # The Usage Pattern tab carries WATCH's turns and days since movement (no Metrics tab).
        assert "inventory_turns" in body[0] and "days_since_last_movement" in body[0]


class TestValidationIndexes:
    """FR-6 inputs the snapshot keeps: issue events per key, receipt dates per PO line."""

    def test_issue_events_keep_issues_and_their_reversals_and_find_the_history_start(self) -> None:
        from datetime import date

        from app.initiatives.i13.snapshot import _issue_events

        rows = [
            {"Matnr": "M", "Werks": "1300", "Bwart": "101", "BudatMkpf": date(2025, 8, 8)},
            {"Matnr": "M", "Werks": "1300", "Bwart": "261", "BudatMkpf": date(2026, 1, 5)},
            {"Matnr": "M", "Werks": "1300", "Bwart": "262", "BudatMkpf": date(2026, 1, 6)},
            {"Matnr": "N", "Werks": "1500", "Bwart": "201", "BudatMkpf": date(2026, 2, 1)},
        ]
        events, start = _issue_events(rows)
        assert start == date(2025, 8, 8)
        assert [r["Bwart"] for r in events[("M", "1300")]] == ["261", "262"]
        assert [r["Bwart"] for r in events[("N", "1500")]] == ["201"]

    def test_receipt_dates_are_101_postings_per_po_line(self) -> None:
        from datetime import date

        from app.initiatives.i13.snapshot import _receipt_dates

        rows = [
            {"Ebeln": "41", "Ebelp": "10", "Bwart": "101", "BudatMkpf": date(2026, 8, 1)},
            {"Ebeln": "41", "Ebelp": "10", "Bwart": "102", "BudatMkpf": date(2026, 8, 2)},
            {"Ebeln": "41", "Ebelp": "10", "Bwart": "101", "BudatMkpf": date(2026, 8, 5)},
        ]
        assert _receipt_dates(rows) == {("41", "10"): frozenset({date(2026, 8, 1), date(2026, 8, 5)})}
