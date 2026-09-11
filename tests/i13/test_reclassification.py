"""OAR -> Min-Max reclassification evidence: >4 consumption is the SOP flag."""

from datetime import date, timedelta
from pathlib import Path

from app.initiatives.i13.reclassification import build_reclassification_candidates
from app.integrations.sap.gateway import SapGateway
from tests.i13.conftest import write_csv

AS_OF = date(2026, 1, 1)
MATERIAL_PLANT_HEADER = ["Matnr", "Werks", "Dismm"]
MOVEMENT_HEADER = ["Mblnr", "Bwart", "Matnr", "Werks", "Menge", "BudatMkpf"]


def _days_ago(days: int) -> str:
    return (AS_OF - timedelta(days=days)).isoformat()


def _setup(data_dir: Path, *, material_plant_rows, movement_rows) -> SapGateway:
    write_csv(data_dir / "sap" / "MaterialPlantSet.csv", MATERIAL_PLANT_HEADER, material_plant_rows)
    write_csv(data_dir / "sap" / "GoodsMovementItemSet.csv", MOVEMENT_HEADER, movement_rows)
    return SapGateway(data_dir)


def test_more_than_four_consumptions_is_a_candidate(data_dir: Path, i13_config) -> None:
    movements = [
        {"Mblnr": str(i), "Bwart": "261", "Matnr": "MAT1", "Werks": "1000", "Menge": "1", "BudatMkpf": _days_ago(days)}
        for i, days in enumerate([10, 40, 70, 100, 130])
    ]
    gateway = _setup(
        data_dir, material_plant_rows=[{"Matnr": "MAT1", "Werks": "1000", "Dismm": "ND"}], movement_rows=movements
    )
    candidates = build_reclassification_candidates(gateway, i13_config, as_of=AS_OF)
    candidate = next(c for c in candidates if c.material == "MAT1")
    assert candidate.consumption_count_12m == 5
    assert candidate.consumed_more_than_threshold is True
    assert candidate.candidate_flag is True
    assert candidate.candidate_reasons


def test_four_or_fewer_consumptions_is_not_a_candidate(data_dir: Path, i13_config) -> None:
    movements = [
        {"Mblnr": str(i), "Bwart": "261", "Matnr": "MAT1", "Werks": "1000", "Menge": "1", "BudatMkpf": _days_ago(days)}
        for i, days in enumerate([10, 40, 70, 100])
    ]
    gateway = _setup(
        data_dir, material_plant_rows=[{"Matnr": "MAT1", "Werks": "1000", "Dismm": "PD"}], movement_rows=movements
    )
    candidates = build_reclassification_candidates(gateway, i13_config, as_of=AS_OF)
    candidate = next(c for c in candidates if c.material == "MAT1")
    assert candidate.consumption_count_12m == 4
    assert candidate.candidate_flag is False


def test_non_oar_material_is_excluded(data_dir: Path, i13_config) -> None:
    gateway = _setup(
        data_dir, material_plant_rows=[{"Matnr": "MAT2", "Werks": "1000", "Dismm": "VB"}], movement_rows=[]
    )
    candidates = build_reclassification_candidates(gateway, i13_config, as_of=AS_OF)
    assert all(c.material != "MAT2" for c in candidates)


def test_critical_and_hod_indicators_are_not_fabricated(data_dir: Path, i13_config) -> None:
    gateway = _setup(
        data_dir, material_plant_rows=[{"Matnr": "MAT1", "Werks": "1000", "Dismm": "ND"}], movement_rows=[]
    )
    candidate = build_reclassification_candidates(gateway, i13_config, as_of=AS_OF)[0]
    assert candidate.critical_impact_indicator is None
    assert candidate.hod_justified_request_indicator is None
    assert candidate.data_available is False
