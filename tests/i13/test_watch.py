"""WATCH: months of cover, insufficient history, 30-day GR-not-issued,
acquired-vs-plan -- all backend-computed, never inside a controller.
"""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.initiatives.i13.models import AcquiredVsPlanStatus, MaterialScope
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
    # No plan to vary against -- None, not zero: "no plan" and "exactly on
    # plan" are different facts (see watch.py's _acquired_vs_plan_variance).
    assert metric.acquired_vs_plan_variance_quantity is None
    assert metric.acquired_vs_plan_variance_percentage is None


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
    assert metric.acquired_vs_plan_variance_quantity == -5
    assert metric.acquired_vs_plan_variance_percentage == -50


def _plan(planned_quantity: str, **overrides) -> dict:
    plan = {
        "plan_id": "PLAN-1", "session_id": "SESS-1", "Rsnum": "1000000000", "Rspos": "0001",
        "Matnr": "MAT1", "Werks": "1000", "requester": "REQ1", "purpose": "test",
        "planned_quantity": planned_quantity, "planned_use_date": "2026-06-01", "status": "OPEN",
    }
    plan.update(overrides)
    return plan


def test_acquired_vs_plan_aligned_when_gr_exactly_matches_plan(data_dir: Path, i13_config) -> None:
    """§12 example A: plan 10, GR 10 -> variance 0, ON_PLAN (this codebase's
    existing name for the FRS's "ALIGNED" -- see models.py's WatchMetric)."""
    reservation, pr = _reservation_and_pr()
    po_item = {"Ebeln": "4500000000", "Ebelp": "0010", "Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("10")}
    gr_row = {"Ebeln": "4500000000", "Ebelp": "0010", "Bwart": "101", "Menge": Decimal("10"), "BudatMkpf": AS_OF - timedelta(days=10)}
    _write_plans(data_dir, [_plan("10")])
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=[pr], po_items=[po_item], gr_rows=[gr_row])
    reservation_repo = FakeReservationRepository(reservations=[reservation])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.ON_PLAN
    assert metric.acquired_vs_plan_variance_quantity == 0
    assert metric.acquired_vs_plan_variance_percentage == 0


def test_acquired_vs_plan_above_plan_from_partial_receipts_summed(data_dir: Path, i13_config) -> None:
    """§12 example C, and §8's partial-receipt example: two GRs against the
    same PO line (6 then 4 later) must sum to one acquired quantity (10),
    never double-count, and a further GR (5 more, total 15) against plan 10
    is ABOVE_PLAN with +5/+50%."""
    reservation, pr = _reservation_and_pr()
    po_item = {"Ebeln": "4500000000", "Ebelp": "0010", "Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("15")}
    gr_rows = [
        {"Ebeln": "4500000000", "Ebelp": "0010", "Bwart": "101", "Menge": Decimal("6"), "BudatMkpf": AS_OF - timedelta(days=20)},
        {"Ebeln": "4500000000", "Ebelp": "0010", "Bwart": "101", "Menge": Decimal("4"), "BudatMkpf": AS_OF - timedelta(days=15)},
        {"Ebeln": "4500000000", "Ebelp": "0010", "Bwart": "101", "Menge": Decimal("5"), "BudatMkpf": AS_OF - timedelta(days=10)},
    ]
    _write_plans(data_dir, [_plan("10")])
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=[pr], po_items=[po_item], gr_rows=gr_rows)
    reservation_repo = FakeReservationRepository(reservations=[reservation])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.received_quantity == 15  # 6 + 4 + 5, never double-counted
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.ABOVE_PLAN
    assert metric.acquired_vs_plan_variance_quantity == 5
    assert metric.acquired_vs_plan_variance_percentage == 50


def test_gr_not_issued_partial_issue_leaves_unissued_remainder_flagged(data_dir: Path, i13_config) -> None:
    """§13 example C: GR 10, GI 4, age 31 -> unissued 6, flag true. Also
    checks the relevant-GR-date and configured-threshold fields the mart
    exposes alongside the flag."""
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, [])
    po_item = {"Ebeln": "4500000000", "Ebelp": "0010", "Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("10")}
    gr_date = AS_OF - timedelta(days=31)
    gr_row = {"Ebeln": "4500000000", "Ebelp": "0010", "Bwart": "101", "Menge": Decimal("10"), "BudatMkpf": gr_date}
    gi_row = {"Rsnum": "1000000000", "Rspos": "0001", "Bwart": "261", "Menge": Decimal("4"), "BudatMkpf": AS_OF - timedelta(days=5)}
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=[pr], po_items=[po_item], gr_rows=[gr_row])
    reservation_repo = FakeReservationRepository(reservations=[reservation], gi_rows=[gi_row])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.gr_not_issued_flag is True
    assert metric.gr_not_issued_received_quantity == 10
    assert metric.gr_not_issued_issued_quantity == 4
    assert metric.gr_not_issued_outstanding_quantity == 6
    assert metric.gr_not_issued_days_since_gr == 31
    assert metric.gr_not_issued_relevant_gr_date == gr_date
    assert metric.gr_not_issued_threshold_days == i13_config.watch.gr_not_issued_threshold_days


def test_gr_not_issued_clears_once_fully_issued(data_dir: Path, i13_config) -> None:
    """§13 example B: GR 10, GI 10 (all of it, even well past the
    threshold) -> flag false, zero unissued."""
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, [])
    po_item = {"Ebeln": "4500000000", "Ebelp": "0010", "Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("10")}
    gr_row = {"Ebeln": "4500000000", "Ebelp": "0010", "Bwart": "101", "Menge": Decimal("10"), "BudatMkpf": AS_OF - timedelta(days=50)}
    gi_row = {"Rsnum": "1000000000", "Rspos": "0001", "Bwart": "261", "Menge": Decimal("10"), "BudatMkpf": AS_OF - timedelta(days=5)}
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=[pr], po_items=[po_item], gr_rows=[gr_row])
    reservation_repo = FakeReservationRepository(reservations=[reservation], gi_rows=[gi_row])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.gr_not_issued_flag is False
    assert metric.gr_not_issued_outstanding_quantity == 0


def test_projected_months_of_cover_includes_open_po_quantity(data_dir: Path, i13_config) -> None:
    """§12 example C: stock 20, open PO 12, trailing-12m consumption 48
    (avg 4/month) -> current MoC 5, projected MoC 8. Open PO quantity comes
    from W6.1's own procurement chain (ordered - received per PO line,
    clamped at zero), never a new SAP read."""
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, [])
    movements = [_movement("261", "48", 30)]
    open_po_item = {"Ebeln": "4500000001", "Ebelp": "0010", "Banfn": None, "Bnfpo": None, "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("12")}
    movement_repo = FakeMovementRepository(movements=movements, stock={("MAT1", "1000"): Decimal("20")})
    procurement_repo = FakeProcurementRepository(prs=[pr], po_items=[open_po_item])
    reservation_repo = FakeReservationRepository(reservations=[reservation])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.average_monthly_consumption == 4
    assert metric.months_of_cover == 5
    assert metric.open_po_quantity == 12
    assert metric.projected_months_of_cover == 8


def test_open_po_quantity_clamps_at_zero_when_over_received(data_dir: Path, i13_config) -> None:
    """§8 edge case 14: a PO line where received exceeds ordered (a source
    anomaly/over-receipt) must never contribute a *negative* open-PO
    quantity to the total."""
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, [])
    po_item = {"Ebeln": "4500000002", "Ebelp": "0010", "Banfn": None, "Bnfpo": None, "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("5")}
    gr_row = {"Ebeln": "4500000002", "Ebelp": "0010", "Bwart": "101", "Menge": Decimal("8"), "BudatMkpf": AS_OF - timedelta(days=5)}
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=[pr], po_items=[po_item], gr_rows=[gr_row])
    reservation_repo = FakeReservationRepository(reservations=[reservation])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.open_po_quantity == 0


def test_material_scope_is_exposed_for_oar_filtering(data_dir: Path, i13_config) -> None:
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, [])
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=[pr])
    reservation_repo = FakeReservationRepository(reservations=[reservation])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.material_scope is MaterialScope.OAR
