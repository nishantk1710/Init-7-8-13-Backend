"""Plan-breach, grace period, no-plan, and GR-not-issued-30-day exceptions."""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.initiatives.i13.exceptions import build_exception_queue
from app.initiatives.i13.models import ExceptionType
from tests.i13.conftest import FakeMovementRepository, FakeProcurementRepository, FakeReservationRepository, write_csv

AS_OF = date(2026, 1, 1)

PLAN_HEADER = [
    "plan_id", "session_id", "Rsnum", "Rspos", "Matnr", "Werks", "requester", "purpose",
    "planned_quantity", "planned_use_date", "status",
]


def _plan(planned_use_date: date, **overrides) -> dict:
    plan = {
        "plan_id": "PLAN-1", "session_id": "SESS-1", "Rsnum": "1000000000", "Rspos": "0001",
        "Matnr": "MAT1", "Werks": "1000", "requester": "REQ1", "purpose": "test",
        "planned_quantity": "10", "planned_use_date": planned_use_date.isoformat(), "status": "OPEN",
    }
    plan.update(overrides)
    return plan


def _write_plans(data_dir: Path, plan_rows: list[dict]) -> None:
    write_csv(data_dir / "platform" / "consumption_plans.csv", PLAN_HEADER, plan_rows)


def _reservation(rsnum="1000000000", rspos="0001") -> dict:
    return {"Rsnum": rsnum, "Rspos": rspos, "Matnr": "MAT1", "Werks": "1000", "Bdmng": Decimal("10"), "Banfn": "2000000000", "Bnfpo": "0010"}


def _pr() -> dict:
    return {"Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("10")}


def _build(*, reservations=(), prs=(), po_items=(), gr_rows=(), gi_rows=(), scope_index=None, plan_rows=(), data_dir, i13_config):
    _write_plans(data_dir, list(plan_rows))
    movement_repo = FakeMovementRepository()
    procurement_repo = FakeProcurementRepository(prs=list(prs), po_items=list(po_items), gr_rows=list(gr_rows))
    reservation_repo = FakeReservationRepository(reservations=list(reservations), gi_rows=list(gi_rows))
    scope_index = scope_index if scope_index is not None else {("MAT1", "1000"): "ND"}
    return build_exception_queue(movement_repo, procurement_repo, reservation_repo, scope_index, i13_config, data_dir, as_of=AS_OF)


def test_within_plan_is_not_a_breach(data_dir: Path, i13_config) -> None:
    exceptions = _build(
        reservations=[_reservation()], prs=[_pr()],
        plan_rows=[_plan(AS_OF - timedelta(days=5))], data_dir=data_dir, i13_config=i13_config,
    )
    assert not [e for e in exceptions if e.type is ExceptionType.PLAN_BREACH]


def test_plan_breach_after_grace_period_expires(data_dir: Path, i13_config) -> None:
    # grace_days=14 by default; planned 20 days ago -> due 6 days ago -> breached.
    exceptions = _build(
        reservations=[_reservation()], prs=[_pr()],
        plan_rows=[_plan(AS_OF - timedelta(days=20))], data_dir=data_dir, i13_config=i13_config,
    )
    breaches = [e for e in exceptions if e.type is ExceptionType.PLAN_BREACH]
    assert len(breaches) == 1
    assert breaches[0].material == "MAT1"


def test_plan_breach_grace_period_boundary(data_dir: Path, i13_config) -> None:
    grace_days = i13_config.exceptions.plan_breach_grace_days
    not_yet_due = _plan(AS_OF - timedelta(days=grace_days), Rsnum="1", plan_id="PLAN-A")
    just_expired = _plan(AS_OF - timedelta(days=grace_days + 1), Rsnum="2", plan_id="PLAN-B")
    exceptions = _build(plan_rows=[not_yet_due, just_expired], data_dir=data_dir, i13_config=i13_config)
    breach_plan_ids = {e.id for e in exceptions if e.type is ExceptionType.PLAN_BREACH}
    assert "EXC-PLAN_BREACH-PLAN-A" not in breach_plan_ids
    assert "EXC-PLAN_BREACH-PLAN-B" in breach_plan_ids


def test_plan_breach_skipped_when_issuance_evidence_exists(data_dir: Path, i13_config) -> None:
    gi_row = {"Rsnum": "1000000000", "Rspos": "0001", "Bwart": "261", "Menge": Decimal("1"), "BudatMkpf": AS_OF}
    exceptions = _build(
        reservations=[_reservation()], prs=[_pr()], gi_rows=[gi_row],
        plan_rows=[_plan(AS_OF - timedelta(days=20))], data_dir=data_dir, i13_config=i13_config,
    )
    assert not [e for e in exceptions if e.type is ExceptionType.PLAN_BREACH]


def test_no_plan_for_oar_reservation_without_a_plan(data_dir: Path, i13_config) -> None:
    exceptions = _build(
        reservations=[_reservation()], prs=[_pr()], plan_rows=[],
        scope_index={("MAT1", "1000"): "ND"}, data_dir=data_dir, i13_config=i13_config,
    )
    no_plan = [e for e in exceptions if e.type is ExceptionType.NO_PLAN]
    assert len(no_plan) == 1
    assert no_plan[0].reservation_number == "1000000000"


def test_no_plan_not_raised_for_non_oar_material(data_dir: Path, i13_config) -> None:
    exceptions = _build(
        reservations=[_reservation()], prs=[_pr()], plan_rows=[],
        scope_index={("MAT1", "1000"): "VB"}, data_dir=data_dir, i13_config=i13_config,
    )
    assert not [e for e in exceptions if e.type is ExceptionType.NO_PLAN]


def test_gr_not_issued_30_day_exception(data_dir: Path, i13_config) -> None:
    po_item = {"Ebeln": "4500000000", "Ebelp": "0010", "Banfn": "2000000000", "Bnfpo": "0010", "Matnr": "MAT1", "Werks": "1000", "Menge": Decimal("10")}
    gr_row = {"Ebeln": "4500000000", "Ebelp": "0010", "Bwart": "101", "Menge": Decimal("10"), "BudatMkpf": AS_OF - timedelta(days=45)}
    exceptions = _build(
        reservations=[_reservation()], prs=[_pr()], po_items=[po_item], gr_rows=[gr_row],
        plan_rows=[], data_dir=data_dir, i13_config=i13_config,
    )
    gr_not_issued = [e for e in exceptions if e.type is ExceptionType.GR_NOT_ISSUED_30_DAY]
    assert len(gr_not_issued) == 1
    assert gr_not_issued[0].material == "MAT1"
