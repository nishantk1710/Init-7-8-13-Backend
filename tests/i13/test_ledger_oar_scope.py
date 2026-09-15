"""W2.4 OAR scope applied at the I13 ledger API boundary.

``build_utilisation_ledger`` itself stays scope-agnostic (other consumers,
e.g. exceptions.py, read the full ledger and apply scope on their own terms);
these tests cover the filtering that ``app.api.i13.ledger`` adds on top,
using the same small-synthetic-CSV style as ``test_ledger.py``.
"""

from pathlib import Path

from app.api.i13.ledger import get_ledger_entry, list_ledger_entries
from app.integrations.sap.gateway import SapGateway
from tests.i13.conftest import write_csv

PR_HEADER = ["Banfn", "Bnfpo", "Matnr", "Werks", "Menge", "Bsmng", "Ebeln", "Ebelp", "Rsnum"]
MATERIAL_PLANT_HEADER = ["Matnr", "Werks", "Lvorm", "Dismm", "Dispo", "Plifz", "Webaz", "Minbe", "Eisbe", "Bstmi", "Bstma", "Mabst"]


def _gateway(data_dir: Path) -> SapGateway:
    write_csv(
        data_dir / "sap" / "PurchaseRequisitionSet.csv",
        PR_HEADER,
        [
            {"Banfn": "2000000010", "Bnfpo": "00010", "Matnr": "MAT-OAR", "Werks": "1300"},
            {"Banfn": "2000000011", "Bnfpo": "00010", "Matnr": "MAT-MINMAX", "Werks": "1300"},
            {"Banfn": "2000000012", "Bnfpo": "00010", "Matnr": "MAT-UNSCOPED", "Werks": "1300"},
        ],
    )
    write_csv(data_dir / "sap" / "PurchaseOrderItemSet.csv", ["Ebeln", "Ebelp", "Banfn", "Bnfpo", "Matnr", "Werks", "Menge"], [])
    write_csv(
        data_dir / "sap" / "GoodsMovementItemSet.csv",
        ["Mblnr", "Bwart", "Matnr", "Werks", "Menge", "Ebeln", "Ebelp", "Rsnum", "Rspos", "BudatMkpf"],
        [],
    )
    write_csv(data_dir / "sap" / "ReservationItemSet.csv", ["Rsnum", "Rspos", "Banfn", "Bnfpo", "Matnr", "Werks"], [])
    write_csv(
        data_dir / "sap" / "MaterialPlantSet.csv",
        MATERIAL_PLANT_HEADER,
        [
            {"Matnr": "MAT-OAR", "Werks": "1300", "Dismm": "ND"},
            {"Matnr": "MAT-MINMAX", "Werks": "1300", "Dismm": "VB"},
            {"Matnr": "MAT-UNSCOPED", "Werks": "1300", "Dismm": "M0"},
        ],
    )
    return SapGateway(data_dir)


def test_ledger_excludes_non_oar_materials_by_default(data_dir: Path) -> None:
    gateway = _gateway(data_dir)
    entries = list_ledger_entries(plant=None, material=None, include_out_of_scope=False, gateway=gateway)
    assert {entry.material for entry in entries} == {"MAT-OAR"}


def test_ledger_include_out_of_scope_returns_everything(data_dir: Path) -> None:
    gateway = _gateway(data_dir)
    entries = list_ledger_entries(plant=None, material=None, include_out_of_scope=True, gateway=gateway)
    assert {entry.material for entry in entries} == {"MAT-OAR", "MAT-MINMAX", "MAT-UNSCOPED"}


def test_get_ledger_entry_404s_for_non_oar_material_by_default(data_dir: Path) -> None:
    gateway = _gateway(data_dir)
    non_oar_id = "LEDG-2000000011-00010"
    from fastapi import HTTPException

    try:
        get_ledger_entry(non_oar_id, include_out_of_scope=False, gateway=gateway)
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404

    entry = get_ledger_entry(non_oar_id, include_out_of_scope=True, gateway=gateway)
    assert entry.material == "MAT-MINMAX"
