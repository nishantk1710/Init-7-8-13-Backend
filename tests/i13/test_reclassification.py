"""OAR -> Min-Max reclassification evidence (SOP 3.1.1, W6.5).

Three indicators, OR'd together: >4 trailing-12-month consumptions,
Critical criticality tier, and HOD-justified request. Every test injects
fake ``CriticalitySource``/``HodJustificationProvider`` implementations
(``tests/i13/conftest.py``) so this module never touches a database --
the real ``get_criticality_source()`` default is exercised only by the
Postgres-gated route/mart tests.
"""

from datetime import date, timedelta
from decimal import Decimal

from app.initiatives.i13.config import build_i13_config
from app.initiatives.i13.reclassification import build_reclassification_candidates
from tests.i13.conftest import FakeCriticalitySource, FakeHodJustificationProvider, FakeMovementRepository

AS_OF = date(2026, 1, 1)


def _movement(material: str, plant: str, days_ago_value: int, index: int) -> dict:
    return {
        "Bwart": "261",
        "Menge": Decimal("1"),
        "Matnr": material,
        "Werks": plant,
        "BudatMkpf": AS_OF - timedelta(days=days_ago_value),
    }


def _candidates(repo, scope_index, config, *, criticality=None, hod=None, **kwargs):
    return build_reclassification_candidates(
        repo,
        scope_index,
        config,
        as_of=AS_OF,
        criticality_source=criticality if criticality is not None else FakeCriticalitySource(),
        hod_provider=hod if hod is not None else FakeHodJustificationProvider(),
        **kwargs,
    )


def test_more_than_four_consumptions_is_a_candidate(i13_config) -> None:
    movements = [_movement("MAT1", "1000", days, i) for i, days in enumerate([10, 40, 70, 100, 130])]
    repo = FakeMovementRepository(movements=movements)
    scope_index = {("MAT1", "1000"): "ND"}

    candidate = _candidates(repo, scope_index, i13_config)[0]
    assert candidate.consumption_count_12m == 5
    assert candidate.consumed_more_than_threshold is True
    assert candidate.candidate_flag is True
    assert candidate.candidate_reasons == ["FREQUENT_CONSUMPTION"]


def test_four_consumptions_does_not_satisfy_greater_than_four(i13_config) -> None:
    """The SOP rule is strictly '> 4', not '>= 4' -- exactly 4 must not flag."""
    movements = [_movement("MAT1", "1000", days, i) for i, days in enumerate([10, 40, 70, 100])]
    repo = FakeMovementRepository(movements=movements)
    scope_index = {("MAT1", "1000"): "PD"}

    candidate = _candidates(repo, scope_index, i13_config)[0]
    assert candidate.consumption_count_12m == 4
    assert candidate.consumed_more_than_threshold is False
    assert candidate.candidate_flag is False


def test_non_oar_material_is_excluded(i13_config) -> None:
    repo = FakeMovementRepository()
    scope_index = {("MAT2", "1000"): "VB"}

    candidates = _candidates(repo, scope_index, i13_config)
    assert all(c.material != "MAT2" for c in candidates)


def test_oar_material_with_no_movement_history_is_still_a_candidate_row() -> None:
    """An OAR material with zero movement history is a real zero-consumption
    candidate, not an omission (compute_all_movement_metrics never returns a
    key it has no movement rows for)."""
    from app.core.config import Settings

    config = build_i13_config(Settings())
    repo = FakeMovementRepository()
    scope_index = {("MAT1", "1000"): "ND"}

    candidate = _candidates(repo, scope_index, config)[0]
    assert candidate.consumption_count_12m == 0
    assert candidate.candidate_flag is False


def test_critical_material_is_a_candidate_despite_low_consumption(i13_config) -> None:
    repo = FakeMovementRepository(movements=[_movement("MAT1", "1000", d, i) for i, d in enumerate([10, 40])])
    scope_index = {("MAT1", "1000"): "ND"}
    criticality = FakeCriticalitySource({("MAT1", "1000"): "CRITICAL"})

    candidate = _candidates(repo, scope_index, i13_config, criticality=criticality)[0]
    assert candidate.consumption_count_12m == 2
    assert candidate.critical_impact_indicator is True
    assert candidate.candidate_flag is True
    assert candidate.candidate_reasons == ["CRITICAL"]


def test_hod_justified_material_is_a_candidate_despite_low_consumption(i13_config) -> None:
    repo = FakeMovementRepository(movements=[_movement("MAT1", "1000", d, i) for i, d in enumerate([10, 40])])
    scope_index = {("MAT1", "1000"): "ND"}
    hod = FakeHodJustificationProvider({("MAT1", "1000"): True})

    candidate = _candidates(repo, scope_index, i13_config, hod=hod)[0]
    assert candidate.critical_impact_indicator is None
    assert candidate.hod_justified_request_indicator is True
    assert candidate.candidate_flag is True
    assert candidate.candidate_reasons == ["HOD_JUSTIFIED"]


def test_all_three_indicators_retain_every_applicable_reason(i13_config) -> None:
    movements = [_movement("MAT1", "1000", d, i) for i, d in enumerate([10, 40, 70, 100, 130, 160])]
    repo = FakeMovementRepository(movements=movements)
    scope_index = {("MAT1", "1000"): "ND"}
    criticality = FakeCriticalitySource({("MAT1", "1000"): "CRITICAL"})
    hod = FakeHodJustificationProvider({("MAT1", "1000"): True})

    candidate = _candidates(repo, scope_index, i13_config, criticality=criticality, hod=hod)[0]
    assert candidate.consumption_count_12m == 6
    assert candidate.candidate_flag is True
    assert set(candidate.candidate_reasons) == {"FREQUENT_CONSUMPTION", "CRITICAL", "HOD_JUSTIFIED"}
    assert candidate.data_available is True


def test_no_evidence_on_any_indicator_is_not_a_candidate(i13_config) -> None:
    repo = FakeMovementRepository()
    scope_index = {("MAT1", "1000"): "ND"}
    criticality = FakeCriticalitySource({("MAT1", "1000"): "NORMAL"})
    hod = FakeHodJustificationProvider({("MAT1", "1000"): False})

    candidate = _candidates(repo, scope_index, i13_config, criticality=criticality, hod=hod)[0]
    assert candidate.consumption_count_12m == 0
    assert candidate.critical_impact_indicator is False
    assert candidate.hod_justified_request_indicator is False
    assert candidate.candidate_flag is False
    assert candidate.data_available is True


def test_unavailable_hod_source_does_not_block_a_candidate_from_other_evidence(i13_config) -> None:
    """count=6, HOD source unavailable -> still a candidate on
    FREQUENT_CONSUMPTION alone, but data_available reflects the gap."""
    movements = [_movement("MAT1", "1000", d, i) for i, d in enumerate([10, 40, 70, 100, 130, 160])]
    repo = FakeMovementRepository(movements=movements)
    scope_index = {("MAT1", "1000"): "ND"}
    criticality = FakeCriticalitySource({("MAT1", "1000"): "NORMAL"})

    candidate = _candidates(repo, scope_index, i13_config, criticality=criticality)[0]
    assert candidate.consumption_count_12m == 6
    assert candidate.candidate_flag is True
    assert candidate.hod_justified_request_indicator is None
    assert candidate.data_available is False


def test_unavailable_hod_source_with_no_other_evidence_is_not_confidently_negative(i13_config) -> None:
    """count=2, NORMAL, HOD unavailable -> not a candidate today, but this
    must never be reported as proven complete negative evidence."""
    repo = FakeMovementRepository(movements=[_movement("MAT1", "1000", d, i) for i, d in enumerate([10, 40])])
    scope_index = {("MAT1", "1000"): "ND"}
    criticality = FakeCriticalitySource({("MAT1", "1000"): "NORMAL"})

    candidate = _candidates(repo, scope_index, i13_config, criticality=criticality)[0]
    assert candidate.candidate_flag is False
    assert candidate.hod_justified_request_indicator is None
    assert candidate.data_available is False


def test_unknown_criticality_is_not_silently_treated_as_critical(i13_config) -> None:
    repo = FakeMovementRepository()
    scope_index = {("MAT1", "1000"): "ND"}
    criticality = FakeCriticalitySource({})  # material entirely absent from the source

    candidate = _candidates(repo, scope_index, i13_config, criticality=criticality)[0]
    assert candidate.critical_impact_indicator is None
    assert candidate.candidate_flag is False
    assert candidate.data_available is False


def test_default_hod_provider_is_unknown_not_a_fabricated_false(i13_config) -> None:
    """No hod_provider given -- NullHodJustificationProvider (W6.6 not built
    yet) answers UNKNOWN, never a fabricated False."""
    repo = FakeMovementRepository()
    scope_index = {("MAT1", "1000"): "ND"}

    candidates = build_reclassification_candidates(
        repo, scope_index, i13_config, as_of=AS_OF, criticality_source=FakeCriticalitySource()
    )
    assert candidates[0].hod_justified_request_indicator is None
    assert candidates[0].data_available is False


def test_repeated_execution_with_same_input_is_deterministic(i13_config) -> None:
    movements = [_movement("MAT1", "1000", d, i) for i, d in enumerate([10, 40, 70, 100, 130])]
    repo = FakeMovementRepository(movements=movements)
    scope_index = {("MAT1", "1000"): "ND"}
    criticality = FakeCriticalitySource({("MAT1", "1000"): "CRITICAL"})
    hod = FakeHodJustificationProvider({("MAT1", "1000"): True})

    first = _candidates(repo, scope_index, i13_config, criticality=criticality, hod=hod)[0]
    second = _candidates(repo, scope_index, i13_config, criticality=criticality, hod=hod)[0]

    # generated_at is a wall-clock audit stamp (same convention as
    # MovementMetrics/WatchMetric's calculated_at) -- every other field
    # must match exactly across runs.
    assert first.material == second.material
    assert first.consumption_count_12m == second.consumption_count_12m
    assert first.critical_impact_indicator == second.critical_impact_indicator
    assert first.hod_justified_request_indicator == second.hod_justified_request_indicator
    assert first.candidate_flag == second.candidate_flag
    assert first.candidate_reasons == second.candidate_reasons
    assert first.data_available == second.data_available


def test_multiple_plants_do_not_double_count_consumption(i13_config) -> None:
    movements = [
        *[_movement("MAT1", "1000", d, i) for i, d in enumerate([10, 40, 70])],
        *[_movement("MAT1", "1500", d, i) for i, d in enumerate([20, 50])],
    ]
    repo = FakeMovementRepository(movements=movements)
    scope_index = {("MAT1", "1000"): "ND", ("MAT1", "1500"): "ND"}

    candidates = {c.plant: c for c in _candidates(repo, scope_index, i13_config)}
    assert candidates["1000"].consumption_count_12m == 3
    assert candidates["1500"].consumption_count_12m == 2


def test_candidate_carries_as_of_date_threshold_and_generated_at(i13_config) -> None:
    repo = FakeMovementRepository()
    scope_index = {("MAT1", "1000"): "ND"}

    candidate = _candidates(repo, scope_index, i13_config)[0]
    assert candidate.as_of_date == AS_OF
    assert candidate.consumption_threshold == i13_config.reclassification.min_consumption_count
    assert candidate.generated_at is not None
