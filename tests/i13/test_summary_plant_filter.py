"""The summary KPIs follow the dashboard's plant and material filters.

They used to be the combined 1300 + 1500 totals whatever the filter said, so
the KPI cards disagreed with the charts beneath them. Filtered, every count is
narrowed to the scope; summed over the plants, the filtered counts equal the
unfiltered ones.
"""

from types import SimpleNamespace

import pytest

from app.initiatives.i13 import summary as summary_module
from app.initiatives.i13.models import AgingBand, ExceptionType
from app.initiatives.i13.summary import summary_from_snapshot

# DISMM per material-plant: ND/PD are OAR, VB is Min-Max (never counted).
SCOPE = {
    ("M1", "1300"): "PD",
    ("M2", "1300"): "ND",
    ("M3", "1300"): "VB",
    ("M1", "1500"): "PD",
    ("M4", "1500"): "PD",
}
# M4/1500 has no movement history, so it is NON_MOVING by definition.
METRICS = (
    SimpleNamespace(material="M1", plant="1300", aging_band=AgingBand.FAST),
    SimpleNamespace(material="M2", plant="1300", aging_band=AgingBand.SLOW),
    SimpleNamespace(material="M3", plant="1300", aging_band=AgingBand.FAST),
    SimpleNamespace(material="M1", plant="1500", aging_band=AgingBand.FAST),
)
EXCEPTIONS = (
    SimpleNamespace(type=ExceptionType.NO_PLAN, material="M1", plant="1300"),
    SimpleNamespace(type=ExceptionType.NO_PLAN, material="M4", plant="1500"),
    SimpleNamespace(type=ExceptionType.GR_NOT_ISSUED_30_DAY, material="M2", plant="1300"),
    SimpleNamespace(type=ExceptionType.PLAN_BREACH, material="M1", plant="1500"),
)


def _snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        # The build-time totals, counted as the snapshot build counts them.
        oar_position_count=4,
        band_counts={AgingBand.FAST.value: 2, AgingBand.SLOW.value: 1, AgingBand.NON_MOVING.value: 1},
        material_scope_index=SCOPE,
        movement_metrics=METRICS,
        reclassification=(
            SimpleNamespace(material="M1", plant="1300", candidate_flag=True),
            SimpleNamespace(material="M2", plant="1300", candidate_flag=False),
            SimpleNamespace(material="M4", plant="1500", candidate_flag=True),
        ),
        reference_plans=(),
    )


@pytest.fixture(autouse=True)
def exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summary_module, "exception_queue", lambda snapshot, plans: EXCEPTIONS)


def _counts(summary) -> tuple:
    return (
        summary.total_oar_positions,
        summary.fast_moving_count,
        summary.slow_moving_count,
        summary.non_moving_count,
        summary.no_plan_count,
        summary.gr_not_issued_30_day_count,
        summary.plan_breach_count,
        summary.reclassification_candidate_count,
    )


def test_one_plant_counts_only_that_plant() -> None:
    s1300 = summary_from_snapshot(_snapshot(), plans=[], plant="1300")
    s1500 = summary_from_snapshot(_snapshot(), plans=[], plant="1500")

    # 1300: M1 FAST, M2 SLOW (M3 is Min-Max, not OAR).
    assert _counts(s1300) == (2, 1, 1, 0, 1, 1, 0, 1)
    # 1500: M1 FAST, M4 never moved -> NON_MOVING.
    assert _counts(s1500) == (2, 1, 0, 1, 1, 0, 1, 1)


def test_the_plants_add_up_to_the_unfiltered_summary() -> None:
    combined = summary_from_snapshot(_snapshot(), plans=[])
    per_plant = [summary_from_snapshot(_snapshot(), plans=[], plant=p) for p in ("1300", "1500")]

    assert _counts(combined) == tuple(sum(values) for values in zip(*(_counts(s) for s in per_plant)))


def test_a_material_filter_narrows_across_both_plants() -> None:
    summary = summary_from_snapshot(_snapshot(), plans=[], material="M1")

    assert (summary.total_oar_positions, summary.fast_moving_count) == (2, 2)
    assert (summary.no_plan_count, summary.plan_breach_count) == (1, 1)


def test_an_out_of_scope_plant_is_all_zeros() -> None:
    summary = summary_from_snapshot(_snapshot(), plans=[], plant="4000")

    assert _counts(summary) == (0,) * 8
