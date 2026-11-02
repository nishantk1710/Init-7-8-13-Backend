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

SCOPE_INDEX = {("MAT1", "1000"): "OAR"}


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


def _plan(planned_quantity: str, **overrides) -> dict:
    plan = {
        "plan_id": "PLAN-1", "session_id": "SESS-1", "Rsnum": "1000000000", "Rspos": "0001",
        "Matnr": "MAT1", "Werks": "1000", "requester": "REQ1", "purpose": "test",
        "planned_quantity": planned_quantity, "planned_use_date": "2026-06-01", "status": "OPEN",
    }
    plan.update(overrides)
    return plan


def _gi(qty: str, *, rspos: str = "0001", bwart: str = "261", days_ago_value: int = 5) -> dict:
    return {
        "Rsnum": "1000000000", "Rspos": rspos, "Bwart": bwart, "Menge": Decimal(qty),
        "BudatMkpf": AS_OF - timedelta(days=days_ago_value),
    }


def _plan_metric(data_dir: Path, i13_config, plans: list[dict], *, gi_rows=(), reservations=None, **procurement):
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, plans)
    procurement_repo = FakeProcurementRepository(prs=[pr], **procurement)
    reservation_repo = FakeReservationRepository(reservations=reservations or [reservation], gi_rows=list(gi_rows))
    metrics = compute_watch_metrics(
        FakeMovementRepository(), procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF
    )
    return next(m for m in metrics if m.material == "MAT1")


def test_acquired_vs_plan_below_plan_on_a_partial_issue(data_dir: Path, i13_config) -> None:
    metric = _plan_metric(data_dir, i13_config, [_plan("10")], gi_rows=[_gi("5")])
    assert metric.planned_quantity == 10
    assert metric.issued_quantity == 5
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.BELOW_PLAN
    assert metric.acquired_vs_plan_variance_quantity == -5
    assert metric.acquired_vs_plan_variance_percentage == -50


def test_acquired_vs_plan_on_plan_when_issued_from_stock(data_dir: Path, i13_config) -> None:
    """The OAR case: the reservation is filled from stock, no PO behind it, so
    nothing is received against it -- yet the requester got all 10. That is on
    plan; measuring receipts called it 0 of 10."""
    reservation = {"Rsnum": "1000000000", "Rspos": "0001", "Matnr": "MAT1", "Werks": "1000", "Bdmng": Decimal("10")}
    metric = _plan_metric(data_dir, i13_config, [_plan("10")], gi_rows=[_gi("10")], reservations=[reservation])
    assert metric.received_quantity == 0
    assert metric.issued_quantity == 10
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.ON_PLAN
    assert metric.acquired_vs_plan_variance_quantity == 0
    assert metric.acquired_vs_plan_variance_percentage == 0


def test_acquired_vs_plan_receipts_alone_do_not_count(data_dir: Path, i13_config) -> None:
    """Goods received on the reservation's PO but not yet issued: the
    requester has not had them, so the plan is not met."""
    po_item = {"Ebeln": "4500000000", "Ebelp": "0010", "Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("10")}
    gr_row = {"Ebeln": "4500000000", "Ebelp": "0010", "Bwart": "101", "Menge": Decimal("10"), "BudatMkpf": AS_OF - timedelta(days=10)}
    metric = _plan_metric(data_dir, i13_config, [_plan("10")], po_items=[po_item], gr_rows=[gr_row])
    assert metric.received_quantity == 10
    assert metric.issued_quantity == 0
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.BELOW_PLAN


def test_acquired_vs_plan_above_plan_with_a_reversal_netted(data_dir: Path, i13_config) -> None:
    """Issued 6 + 4 + 5, one issue of 5 reversed (262) and issued again: 15
    against a plan of 10 is +5 / +50%."""
    gi_rows = [_gi("6", days_ago_value=20), _gi("4", days_ago_value=15), _gi("5", days_ago_value=12),
               _gi("5", bwart="262", days_ago_value=11), _gi("5", days_ago_value=10)]
    metric = _plan_metric(data_dir, i13_config, [_plan("10")], gi_rows=gi_rows)
    assert metric.issued_quantity == 15
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.ABOVE_PLAN
    assert metric.acquired_vs_plan_variance_quantity == 5
    assert metric.acquired_vs_plan_variance_percentage == 50


def test_a_split_po_reservation_counts_its_issue_once(data_dir: Path, i13_config) -> None:
    """A PR split over two PO lines gives the reservation two ledger entries,
    each carrying the item's whole issued quantity. Counted once."""
    po_items = [
        {"Ebeln": "4500000000", "Ebelp": "0010", "Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("5")},
        {"Ebeln": "4500000001", "Ebelp": "0010", "Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("5")},
    ]
    metric = _plan_metric(data_dir, i13_config, [_plan("10")], gi_rows=[_gi("10")], po_items=po_items)
    assert metric.issued_quantity == 10
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.ON_PLAN


def test_only_the_plans_own_reservation_counts(data_dir: Path, i13_config) -> None:
    """Issues on another reservation of the same material and plant -- one
    no plan stands for -- are not this plan's."""
    reservations = [
        {"Rsnum": "1000000000", "Rspos": "0001", "Matnr": "MAT1", "Werks": "1000", "Bdmng": Decimal("10")},
        {"Rsnum": "1000000000", "Rspos": "0002", "Matnr": "MAT1", "Werks": "1000", "Bdmng": Decimal("50")},
    ]
    metric = _plan_metric(
        data_dir, i13_config, [_plan("10")], gi_rows=[_gi("4"), _gi("50", rspos="0002")], reservations=reservations
    )
    assert metric.issued_quantity == 4
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.BELOW_PLAN


def test_a_plan_with_no_reservation_is_awaiting_one(data_dir: Path, i13_config) -> None:
    """The plan names a reservation the extract does not hold (yet): nothing
    could have been issued, which is not the same as being short."""
    metric = _plan_metric(data_dir, i13_config, [_plan("10", Rsnum="1999999999")], gi_rows=[_gi("10")])
    assert metric.planned_quantity == 10
    assert metric.issued_quantity == 0
    assert metric.acquired_vs_plan_status is AcquiredVsPlanStatus.NO_RESERVATION
    assert metric.acquired_vs_plan_variance_quantity is None
    assert metric.acquired_vs_plan_variance_percentage is None


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


def _po_line(ebeln: str, menge: str, *, elikz: str = "", loekz: str = "") -> dict:
    return {"Ebeln": ebeln, "Ebelp": "0010", "Banfn": None, "Bnfpo": None, "Matnr": "MAT1", "Werks": "1000",
            "Menge": Decimal(menge), "Elikz": elikz, "Loekz": loekz}


def _gr(ebeln: str, menge: str) -> dict:
    return {"Ebeln": ebeln, "Ebelp": "0010", "Bwart": "101", "Menge": Decimal(menge), "BudatMkpf": AS_OF - timedelta(days=5)}


def _open_po(data_dir: Path, i13_config, po_items: list[dict], gr_rows: list[dict]) -> Decimal:
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, [])
    procurement_repo = FakeProcurementRepository(prs=[pr], po_items=po_items, gr_rows=gr_rows)
    metrics = compute_watch_metrics(
        FakeMovementRepository(), procurement_repo, FakeReservationRepository(reservations=[reservation]),
        SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF,
    )
    return next(m for m in metrics if m.material == "MAT1").open_po_quantity


def test_a_closed_po_line_has_nothing_open(data_dir: Path, i13_config) -> None:
    """UAT, 21 Oct: delivery completed (ELIKZ = X) -- even short-closed, 10
    ordered and 6 received -- and a deleted line (LOEKZ = L) add nothing; an
    open line still counts what is unreceived."""
    po_items = [
        _po_line("4500000010", "10", elikz="X"),
        _po_line("4500000011", "5", loekz="L"),
        _po_line("4500000012", "8"),
    ]
    gr_rows = [_gr("4500000010", "6"), _gr("4500000012", "3")]
    assert _open_po(data_dir, i13_config, po_items, gr_rows) == 5


def test_closed_lines_count_again_with_the_switch_off(data_dir: Path, i13_config, monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "i13_open_po_respect_closed", False)
    po_items = [_po_line("4500000010", "10", elikz="X"), _po_line("4500000011", "5", loekz="L")]
    assert _open_po(data_dir, i13_config, po_items, [_gr("4500000010", "6")]) == 9


def test_material_scope_is_exposed_for_oar_filtering(data_dir: Path, i13_config) -> None:
    reservation, pr = _reservation_and_pr()
    _write_plans(data_dir, [])
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=[pr])
    reservation_repo = FakeReservationRepository(reservations=[reservation])

    metrics = compute_watch_metrics(movement_repo, procurement_repo, reservation_repo, SCOPE_INDEX, i13_config, data_dir, as_of=AS_OF)
    metric = next(m for m in metrics if m.material == "MAT1")
    assert metric.material_scope is MaterialScope.OAR


def test_a_contract_or_a_request_for_quotation_is_never_on_order(data_dir: Path, i13_config) -> None:
    """UAT, 23 Oct: a contract line orders nothing (MENGE 0, its quantity is
    a target) and a request for quotation is not an order, whatever quantity
    it asks about. Only the real order counts."""
    po_items = [
        {**_po_line("4600000926", "0"), "Bsart": "WK"},
        {**_po_line("6000000001", "4"), "Bsart": "AN"},
        {**_po_line("4100000001", "3"), "Bsart": "FPA"},
        {**_po_line("4100000002", "2"), "Bsart": "ZDOM"},
    ]
    assert _open_po(data_dir, i13_config, po_items, []) == 2
