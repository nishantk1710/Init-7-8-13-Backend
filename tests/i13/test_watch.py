"""WATCH: months of cover, insufficient history, 30-day GR-not-issued,
acquired-vs-plan -- all backend-computed, never inside a controller.
"""

from datetime import date, timedelta
from pathlib import Path

from app.initiatives.i13.models import AcquiredVsPlanStatus
from app.initiatives.i13.watch import compute_watch_metrics
from app.integrations.sap.gateway import SapGateway
from tests.i13.conftest import write_csv

AS_OF = date(2026, 1, 1)

PR_HEADER = ["Banfn", "Bnfpo", "Matnr", "Werks", "Menge", "Bsmng", "Ebeln", "Ebelp", "Rsnum"]
PO_ITEM_HEADER = ["Ebeln", "Ebelp", "Banfn", "Bnfpo", "Matnr", "Werks", "Menge"]
MOVEMENT_HEADER = ["Mblnr", "Bwart", "Matnr", "Werks", "Menge", "Ebeln", "Ebelp", "Rsnum", "Rspos", "BudatMkpf"]
STOCK_HEADER = ["Matnr", "Werks", "Lgort", "Labst"]
PLAN_HEADER = ["plan_id", "session_id", "Rsnum", "Rspos", "Matnr", "Werks", "requester", "purpose", "planned_quantity", "planned_use_date", "status"]


def _days_ago(days: int) -> str:
    return (AS_OF - timedelta(days=days)).isoformat()


def _setup(
    data_dir: Path,
    *,
    pr_rows=(),
    po_item_rows=(),
    movement_rows=(),
    stock_rows=(),
    plan_rows=(),
) -> SapGateway:
    write_csv(data_dir / "sap" / "PurchaseRequisitionSet.csv", PR_HEADER, list(pr_rows))
    write_csv(data_dir / "sap" / "PurchaseOrderItemSet.csv", PO_ITEM_HEADER, list(po_item_rows))
    write_csv(data_dir / "sap" / "GoodsMovementItemSet.csv", MOVEMENT_HEADER, list(movement_rows))
    write_csv(data_dir / "sap" / "ReservationItemSet.csv", ["Rsnum", "Rspos", "Banfn", "Bnfpo"], [])
    write_csv(data_dir / "sap" / "StorageLocationStockSet.csv", STOCK_HEADER, list(stock_rows))
    write_csv(data_dir / "platform" / "consumption_plans.csv", PLAN_HEADER, list(plan_rows))
    return SapGateway(data_dir)


def test_insufficient_history_when_no_movements(data_dir: Path, i13_config) -> None:
    gateway = _setup(
        data_dir, pr_rows=[{"Banfn": "2000000000", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}]
    )
    metrics = compute_watch_metrics(gateway, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.months_of_cover is None
    assert metric.months_of_cover_reason == "INSUFFICIENT_HISTORY"


def test_months_of_cover_computed_from_consumption_and_stock(data_dir: Path, i13_config) -> None:
    movement_rows = [
        {"Mblnr": str(i), "Bwart": "261", "Matnr": "MAT1", "Werks": "1000", "Menge": "4", "BudatMkpf": _days_ago(days)}
        for i, days in enumerate([30, 120, 200])
    ]
    gateway = _setup(
        data_dir,
        pr_rows=[{"Banfn": "2000000000", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}],
        movement_rows=movement_rows,
        stock_rows=[{"Matnr": "MAT1", "Werks": "1000", "Lgort": "SP01", "Labst": "6"}],
    )
    metrics = compute_watch_metrics(gateway, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.consumed_qty_12m == 12
    assert metric.months_of_cover == 6  # stock 6 / (12 qty over 12 months = 1/month)
    assert metric.months_of_cover_reason is None


def test_gr_not_issued_flag_true_at_30_days(data_dir: Path, i13_config) -> None:
    gateway = _setup(
        data_dir,
        pr_rows=[{"Banfn": "2000000000", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}],
        po_item_rows=[{"Ebeln": "4500000000", "Ebelp": "00010", "Banfn": "2000000000", "Bnfpo": "00010", "Menge": "10"}],
        movement_rows=[
            {"Mblnr": "1", "Bwart": "101", "Ebeln": "4500000000", "Ebelp": "00010", "Menge": "10", "BudatMkpf": _days_ago(30)}
        ],
    )
    metrics = compute_watch_metrics(gateway, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.gr_not_issued_flag is True
    assert metric.gr_not_issued_days_since_gr == 30
    assert metric.gr_not_issued_outstanding_quantity == 10


def test_gr_not_issued_flag_false_below_threshold(data_dir: Path, i13_config) -> None:
    gateway = _setup(
        data_dir,
        pr_rows=[{"Banfn": "2000000000", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}],
        po_item_rows=[{"Ebeln": "4500000000", "Ebelp": "00010", "Banfn": "2000000000", "Bnfpo": "00010", "Menge": "10"}],
        movement_rows=[
            {"Mblnr": "1", "Bwart": "101", "Ebeln": "4500000000", "Ebelp": "00010", "Menge": "10", "BudatMkpf": _days_ago(29)}
        ],
    )
    metrics = compute_watch_metrics(gateway, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.gr_not_issued_flag is False


def test_acquired_vs_plan_no_plan(data_dir: Path, i13_config) -> None:
    gateway = _setup(
        data_dir, pr_rows=[{"Banfn": "2000000000", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}]
    )
    metrics = compute_watch_metrics(gateway, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.NO_PLAN


def test_acquired_vs_plan_below_on_above(data_dir: Path, i13_config) -> None:
    gateway = _setup(
        data_dir,
        pr_rows=[{"Banfn": "2000000000", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}],
        po_item_rows=[{"Ebeln": "4500000000", "Ebelp": "00010", "Banfn": "2000000000", "Bnfpo": "00010", "Menge": "10"}],
        movement_rows=[
            {"Mblnr": "1", "Bwart": "101", "Ebeln": "4500000000", "Ebelp": "00010", "Menge": "5", "BudatMkpf": _days_ago(10)}
        ],
        plan_rows=[
            {
                "plan_id": "PLAN-1",
                "session_id": "SESS-1",
                "Rsnum": "1",
                "Rspos": "0001",
                "Matnr": "MAT1",
                "Werks": "1000",
                "requester": "REQ1",
                "purpose": "test",
                "planned_quantity": "10",
                "planned_use_date": "2026-06-01",
                "status": "OPEN",
            }
        ],
    )
    metrics = compute_watch_metrics(gateway, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.planned_quantity == 10
    assert metric.received_quantity == 5
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.BELOW_PLAN
