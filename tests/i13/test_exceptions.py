"""Plan-breach, grace period, no-plan, and GR-not-issued-30-day exceptions."""

from datetime import date, timedelta
from pathlib import Path

from app.initiatives.i13.exceptions import build_exception_queue
from app.initiatives.i13.models import ExceptionType
from app.integrations.sap.gateway import SapGateway
from tests.i13.conftest import write_csv

AS_OF = date(2026, 1, 1)

PR_HEADER = ["Banfn", "Bnfpo", "Matnr", "Werks", "Menge", "Bsmng", "Ebeln", "Ebelp"]
PO_ITEM_HEADER = ["Ebeln", "Ebelp", "Banfn", "Bnfpo", "Matnr", "Werks", "Menge"]
MOVEMENT_HEADER = ["Mblnr", "Bwart", "Matnr", "Werks", "Menge", "Ebeln", "Ebelp", "Rsnum", "Rspos", "BudatMkpf"]
RESERVATION_HEADER = ["Rsnum", "Rspos", "Banfn", "Bnfpo"]
MATERIAL_PLANT_HEADER = ["Matnr", "Werks", "Dismm"]
PLAN_HEADER = [
    "plan_id", "session_id", "Rsnum", "Rspos", "Matnr", "Werks", "requester", "purpose",
    "planned_quantity", "planned_use_date", "status",
]


def _setup(
    data_dir: Path,
    *,
    pr_rows=(),
    po_item_rows=(),
    movement_rows=(),
    reservation_rows=(),
    material_plant_rows=(),
    plan_rows=(),
) -> SapGateway:
    write_csv(data_dir / "sap" / "PurchaseRequisitionSet.csv", PR_HEADER, list(pr_rows))
    write_csv(data_dir / "sap" / "PurchaseOrderItemSet.csv", PO_ITEM_HEADER, list(po_item_rows))
    write_csv(data_dir / "sap" / "GoodsMovementItemSet.csv", MOVEMENT_HEADER, list(movement_rows))
    write_csv(data_dir / "sap" / "ReservationItemSet.csv", RESERVATION_HEADER, list(reservation_rows))
    write_csv(data_dir / "sap" / "MaterialPlantSet.csv", MATERIAL_PLANT_HEADER, list(material_plant_rows))
    write_csv(data_dir / "platform" / "consumption_plans.csv", PLAN_HEADER, list(plan_rows))
    return SapGateway(data_dir)


def _plan(planned_use_date: date, **overrides) -> dict:
    plan = {
        "plan_id": "PLAN-1",
        "session_id": "SESS-1",
        "Rsnum": "1000000000",
        "Rspos": "0001",
        "Matnr": "MAT1",
        "Werks": "1000",
        "requester": "REQ1",
        "purpose": "test",
        "planned_quantity": "10",
        "planned_use_date": planned_use_date.isoformat(),
        "status": "OPEN",
    }
    plan.update(overrides)
    return plan


def test_within_plan_is_not_a_breach(data_dir: Path, i13_config) -> None:
    gateway = _setup(data_dir, plan_rows=[_plan(AS_OF - timedelta(days=5))])
    exceptions = build_exception_queue(gateway, i13_config, data_dir, as_of=AS_OF)
    assert not [e for e in exceptions if e.type is ExceptionType.PLAN_BREACH]


def test_plan_breach_after_grace_period_expires(data_dir: Path, i13_config) -> None:
    # grace_days=14 by default; planned 20 days ago -> due 6 days ago -> breached.
    gateway = _setup(data_dir, plan_rows=[_plan(AS_OF - timedelta(days=20))])
    exceptions = build_exception_queue(gateway, i13_config, data_dir, as_of=AS_OF)
    breaches = [e for e in exceptions if e.type is ExceptionType.PLAN_BREACH]
    assert len(breaches) == 1
    assert breaches[0].material == "MAT1"


def test_plan_breach_grace_period_boundary(data_dir: Path, i13_config) -> None:
    grace_days = i13_config.exceptions.plan_breach_grace_days
    not_yet_due = _plan(AS_OF - timedelta(days=grace_days), Rsnum="1", plan_id="PLAN-A")
    just_expired = _plan(AS_OF - timedelta(days=grace_days + 1), Rsnum="2", plan_id="PLAN-B")
    gateway = _setup(data_dir, plan_rows=[not_yet_due, just_expired])
    exceptions = build_exception_queue(gateway, i13_config, data_dir, as_of=AS_OF)
    breach_plan_ids = {e.id for e in exceptions if e.type is ExceptionType.PLAN_BREACH}
    assert "EXC-PLAN_BREACH-PLAN-A" not in breach_plan_ids
    assert "EXC-PLAN_BREACH-PLAN-B" in breach_plan_ids


def test_plan_breach_skipped_when_issuance_evidence_exists(data_dir: Path, i13_config) -> None:
    gateway = _setup(
        data_dir,
        pr_rows=[{"Banfn": "2000000000", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}],
        movement_rows=[
            {"Mblnr": "1", "Bwart": "261", "Matnr": "MAT1", "Werks": "1000", "Menge": "1", "Rsnum": "1000000000", "Rspos": "0001", "BudatMkpf": AS_OF.isoformat()}
        ],
        reservation_rows=[{"Rsnum": "1000000000", "Rspos": "0001", "Banfn": "2000000000", "Bnfpo": "00010"}],
        plan_rows=[_plan(AS_OF - timedelta(days=20))],
    )
    exceptions = build_exception_queue(gateway, i13_config, data_dir, as_of=AS_OF)
    assert not [e for e in exceptions if e.type is ExceptionType.PLAN_BREACH]


def test_no_plan_for_oar_reservation_without_a_plan(data_dir: Path, i13_config) -> None:
    gateway = _setup(
        data_dir,
        pr_rows=[{"Banfn": "2000000000", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}],
        reservation_rows=[{"Rsnum": "1000000000", "Rspos": "0001", "Banfn": "2000000000", "Bnfpo": "00010"}],
        material_plant_rows=[{"Matnr": "MAT1", "Werks": "1000", "Dismm": "ND"}],
        plan_rows=[],
    )
    exceptions = build_exception_queue(gateway, i13_config, data_dir, as_of=AS_OF)
    no_plan = [e for e in exceptions if e.type is ExceptionType.NO_PLAN]
    assert len(no_plan) == 1
    assert no_plan[0].reservation_number == "1000000000"


def test_no_plan_not_raised_for_non_oar_material(data_dir: Path, i13_config) -> None:
    gateway = _setup(
        data_dir,
        pr_rows=[{"Banfn": "2000000000", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}],
        reservation_rows=[{"Rsnum": "1000000000", "Rspos": "0001", "Banfn": "2000000000", "Bnfpo": "00010"}],
        material_plant_rows=[{"Matnr": "MAT1", "Werks": "1000", "Dismm": "VB"}],
        plan_rows=[],
    )
    exceptions = build_exception_queue(gateway, i13_config, data_dir, as_of=AS_OF)
    assert not [e for e in exceptions if e.type is ExceptionType.NO_PLAN]


def test_gr_not_issued_30_day_exception(data_dir: Path, i13_config) -> None:
    gateway = _setup(
        data_dir,
        pr_rows=[{"Banfn": "2000000000", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}],
        po_item_rows=[{"Ebeln": "4500000000", "Ebelp": "00010", "Banfn": "2000000000", "Bnfpo": "00010", "Menge": "10"}],
        movement_rows=[
            {
                "Mblnr": "1",
                "Bwart": "101",
                "Ebeln": "4500000000",
                "Ebelp": "00010",
                "Menge": "10",
                "BudatMkpf": (AS_OF - timedelta(days=45)).isoformat(),
            }
        ],
    )
    exceptions = build_exception_queue(gateway, i13_config, data_dir, as_of=AS_OF)
    gr_not_issued = [e for e in exceptions if e.type is ExceptionType.GR_NOT_ISSUED_30_DAY]
    assert len(gr_not_issued) == 1
    assert gr_not_issued[0].material == "MAT1"
