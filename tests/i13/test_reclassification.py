"""OAR -> Min-Max reclassification evidence: >4 consumption is the SOP flag."""

from datetime import date, timedelta
from decimal import Decimal

from app.initiatives.i13.reclassification import build_reclassification_candidates
from tests.i13.conftest import FakeMovementRepository

AS_OF = date(2026, 1, 1)


def _days_ago(days: int) -> str:
    return (AS_OF - timedelta(days=days)).isoformat()


def _movement(material: str, plant: str, days_ago_value: int, index: int) -> dict:
    return {
        "Bwart": "261",
        "Menge": Decimal("1"),
        "Matnr": material,
        "Werks": plant,
        "BudatMkpf": AS_OF - timedelta(days=days_ago_value),
    }


def test_more_than_four_consumptions_is_a_candidate(i13_config) -> None:
    movements = [_movement("MAT1", "1000", days, i) for i, days in enumerate([10, 40, 70, 100, 130])]
    repo = FakeMovementRepository(movements=movements)
    scope_index = {("MAT1", "1000"): "ND"}

    candidates = build_reclassification_candidates(repo, scope_index, i13_config, as_of=AS_OF)
    candidate = next(c for c in candidates if c.material == "MAT1")
    assert candidate.consumption_count_12m == 5
    assert candidate.consumed_more_than_threshold is True
    assert candidate.candidate_flag is True
    assert candidate.candidate_reasons


def test_four_or_fewer_consumptions_is_not_a_candidate(i13_config) -> None:
    movements = [_movement("MAT1", "1000", days, i) for i, days in enumerate([10, 40, 70, 100])]
    repo = FakeMovementRepository(movements=movements)
    scope_index = {("MAT1", "1000"): "PD"}

    candidates = build_reclassification_candidates(repo, scope_index, i13_config, as_of=AS_OF)
    candidate = next(c for c in candidates if c.material == "MAT1")
    assert candidate.consumption_count_12m == 4
    assert candidate.candidate_flag is False


def test_non_oar_material_is_excluded(i13_config) -> None:
    repo = FakeMovementRepository()
    scope_index = {("MAT2", "1000"): "VB"}

    candidates = build_reclassification_candidates(repo, scope_index, i13_config, as_of=AS_OF)
    assert all(c.material != "MAT2" for c in candidates)


def test_oar_material_with_no_movement_history_is_still_a_candidate_row() -> None:
    """An OAR material with zero movement history is a real zero-consumption
    candidate, not an omission (compute_all_movement_metrics never returns a
    key it has no movement rows for)."""
    from app.initiatives.i13.config import build_i13_config
    from app.core.config import Settings

    config = build_i13_config(Settings())
    repo = FakeMovementRepository()
    scope_index = {("MAT1", "1000"): "ND"}

    candidates = build_reclassification_candidates(repo, scope_index, config, as_of=AS_OF)
    candidate = next(c for c in candidates if c.material == "MAT1")
    assert candidate.consumption_count_12m == 0
    assert candidate.candidate_flag is False


def test_critical_and_hod_indicators_are_not_fabricated(i13_config) -> None:
    repo = FakeMovementRepository()
    scope_index = {("MAT1", "1000"): "ND"}

    candidate = build_reclassification_candidates(repo, scope_index, i13_config, as_of=AS_OF)[0]
    assert candidate.critical_impact_indicator is None
    assert candidate.hod_justified_request_indicator is None
    assert candidate.data_available is False
