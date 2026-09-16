"""WATCH: months of cover, insufficient history, 30-day GR-not-issued,
acquired-vs-plan -- all backend-computed, never inside a controller.
"""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.initiatives.i13.models import AcquiredVsPlanStatus
from app.initiatives.i13.watch import compute_watch_metrics
from tests.i13.conftest import FakeMovementRepository, FakeProcurementRepository, FakeReservationRepository, write_csv

AS_OF = date(2026, 1, 1)

PLAN_HEADER = [
    "plan_id", "session_id", "Rsnum", "Rspos", "Matnr", "Werks", "requester", "purpose",
    "planned_quantity", "planned_use_date", "status",
]

SCOPE_INDEX = {("MAT1", "1000"): "ND"}


def _days_ago(days: int) -> str:
    return (AS_OF - timedelta(days=days)).isoformat()


def _reservation_and_pr(qty: str = "10") -> tuple[dict, dict]:
    reservation = {"Rsnum": "1000000000", "Rspos": "0001", "Matnr": "MAT1", "Werks": "1000", "Bdmng": Decimal(qty), "Banfn": "2000000000", "Bnfpo": "0010"}
    pr = {"Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal(qty)}
    return reservation, pr


def _movement(bwart: str, menge: str, days_ago_value: int) -> dict:
    return {"Bwart": bwart, "Menge": Decimal(menge), "Matnr": "MAT1", "Werks": "1000", "BudatMkpf": AS_OF - timedelta(days=days_ago_value)}


def _write_plans(data_dir: Path, plan_rows: list[dict]) -> None:
    write_csv(data_dir / "platform" / "consumption_plans.csv", PLAN_HEADER, plan_rows)


def test_insufficient_history_when_no_movements(data_dir: Path, i13_config) -> None:
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, [])
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=[pr])
    reservation_repo = FakeReservationRepository(reservations=[reservation])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.months_of_cover is None
    assert metric.months_of_cover_reason == "INSUFFICIENT_HISTORY"


def test_months_of_cover_computed_from_consumption_and_stock(data_dir: Path, i13_config) -> None:
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, [])
    movements = [_movement("261", "4", days) for days in (30, 120, 200)]
    movement_repo = FakeMovementRepository(movements=movements, stock={("MAT1", "1000"): Decimal("6")})
    procurement_repo = FakeProcurementRepository(prs=[pr])
    reservation_repo = FakeReservationRepository(reservations=[reservation])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.consumed_qty_12m == 12
    assert metric.months_of_cover == 6  # stock 6 / (12 qty over 12 months = 1/month)
    assert metric.months_of_cover_reason is None


def test_gr_not_issued_flag_true_at_30_days(data_dir: Path, i13_config) -> None:
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, [])
    po_item = {"Ebeln": "4500000000", "Ebelp": "0010", "Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("10")}
    gr_row = {"Ebeln": "4500000000", "Ebelp": "0010", "Bwart": "101", "Menge": Decimal("10"), "BudatMkpf": AS_OF - timedelta(days=30)}
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=[pr], po_items=[po_item], gr_rows=[gr_row])
    reservation_repo = FakeReservationRepository(reservations=[reservation])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.gr_not_issued_flag is True
    assert metric.gr_not_issued_days_since_gr == 30
    assert metric.gr_not_issued_outstanding_quantity == 10


def test_gr_not_issued_flag_false_below_threshold(data_dir: Path, i13_config) -> None:
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, [])
    po_item = {"Ebeln": "4500000000", "Ebelp": "0010", "Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("10")}
    gr_row = {"Ebeln": "4500000000", "Ebelp": "0010", "Bwart": "101", "Menge": Decimal("10"), "BudatMkpf": AS_OF - timedelta(days=29)}
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=[pr], po_items=[po_item], gr_rows=[gr_row])
    reservation_repo = FakeReservationRepository(reservations=[reservation])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.gr_not_issued_flag is False


def test_acquired_vs_plan_no_plan(data_dir: Path, i13_config) -> None:
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, [])
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=[pr])
    reservation_repo = FakeReservationRepository(reservations=[reservation])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.NO_PLAN


def test_acquired_vs_plan_below_on_above(data_dir: Path, i13_config) -> None:
    reservation, pr = _reservation_and_pr()
    po_item = {"Ebeln": "4500000000", "Ebelp": "0010", "Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("10")}
    gr_row = {"Ebeln": "4500000000", "Ebelp": "0010", "Bwart": "101", "Menge": Decimal("5"), "BudatMkpf": AS_OF - timedelta(days=10)}
    _write_plans(
        data_dir,
        [
            {
                "plan_id": "PLAN-1", "session_id": "SESS-1", "Rsnum": "1000000000", "Rspos": "0001",
                "Matnr": "MAT1", "Werks": "1000", "requester": "REQ1", "purpose": "test",
                "planned_quantity": "10", "planned_use_date": "2026-06-01", "status": "OPEN",
            }
        ],
    )
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=[pr], po_items=[po_item], gr_rows=[gr_row])
    reservation_repo = FakeReservationRepository(reservations=[reservation])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.planned_quantity == 10
    assert metric.received_quantity == 5
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.BELOW_PLAN
