"""The credibility blend wired into recommendation building (``recommendations.blend_path`` + the builder).

No database: the window, the monthly consumption, the stored neighbours, the lead times and the removed donors are
supplied by monkeypatching the reads ``blend_path`` makes. What is checked is who gets a blend, which similar
materials count, and that a material the blend cannot size keeps its similarity numbers untouched.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.initiatives.i7.features.consumption_window import RunWindow, WindowStatus
from app.initiatives.i7.oar.blend import BlendStatus
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import load_mock_max_stock_policy, load_mock_service_level_policy
from app.initiatives.i7.recommendations import blend_path, builder, rationale
from app.initiatives.i7.recommendations.types import LifecycleStatus

OCT = date(2026, 10, 1)
POLICY = PolicyDocument(service_level=load_mock_service_level_policy(), max_stock=load_mock_max_stock_policy())
KEY = ("5000034258", "1300")

# the wedge belts: 30 units in the year, issued 6 times
BELT_MONTHS = {date(2025, 12, 1): 6, date(2026, 1, 1): 4, date(2026, 3, 1): 4, date(2026, 4, 1): 6, date(2026, 8, 1): 10}


def oar_row(**overrides) -> SimpleNamespace:
    defaults = dict(
        sap_material_number=KEY[0], sap_plant_code=KEY[1], history_status="COLD_START", criticality="NORMAL",
        non_zero_periods=5, consumption_count_12m=6, demand_class="LUMPY", oar_scope="IN_SCOPE",
        feature_run_id=3, oar_run_id=3, status="SUCCESS", confidence="HIGH", candidates_considered=1628,
        eligible_candidates=1214, neighbour_count=5, best_similarity=Decimal("1.0"), estimate_status="SUCCESS",
        oar_safety_stock=Decimal(1), oar_rop=Decimal(36), oar_max_stock=Decimal(48),
        current_safety_stock=Decimal(0), current_reorder_point=Decimal(0), current_maximum_stock=Decimal(0),
        unit_price=Decimal("183"), planned_delivery_time_days=14, lead_time_days=Decimal(14),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def neighbour(material, plant, rank, sim, rop, ss, mx, eligible=True):
    return SimpleNamespace(
        sap_material_number=KEY[0], sap_plant_code=KEY[1], neighbour_material=material, neighbour_plant=plant,
        rank=rank, combined_similarity=Decimal(str(sim)), safety_stock=ss, rop=rop, max_stock=mx, inventory_eligible=eligible,
    )


NEIGHBOURS = [
    neighbour("5000034258", "1500", 1, 1.00, 10, 1, 20),
    neighbour("5000074749", "1500", 2, 0.99, 36, 6, 60),
    neighbour("5000074750", "1500", 3, 0.98, 36, 1, 60),
    neighbour("5000074752", "1500", 4, 0.98, 22, 1, 42),
    neighbour("5000074751", "1500", 5, 0.98, 36, 2, 48),
]
PEER_USE = {("5000034258", "1500"): 53, ("5000074749", "1500"): 96, ("5000074750", "1500"): 22,
            ("5000074752", "1500"): 10, ("5000074751", "1500"): 13}


@pytest.fixture
def world(monkeypatch):
    """Supply the verified window, the consumption, the stored neighbours, lead times and removals the path reads."""

    def apply(*, own=BELT_MONTHS, neighbours=NEIGHBOURS, removed=None, window=True, lead_times=None):
        monkeypatch.setattr(
            blend_path, "latest_run_window",
            lambda session: RunWindow(WindowStatus.USABLE.value, date(2024, 11, 1), OCT) if window else None,
        )
        monthly = {KEY: own}
        for donor, annual in PEER_USE.items():
            monthly[donor] = {date(2026, 5, 1): annual}
        monkeypatch.setattr(blend_path.repository, "load_monthly_consumption", lambda s, a, b: monthly)
        monkeypatch.setattr(blend_path.repository, "load_oar_neighbours", lambda s, run: {KEY: list(neighbours)})
        monkeypatch.setattr(blend_path.repository, "load_feature_lead_times", lambda s, run: lead_times or {})
        monkeypatch.setattr(blend_path.exclusions_module, "load_active", lambda s: {KEY: frozenset(removed or ())})

    return apply


def run(rows, policy=POLICY):
    return blend_path.evaluate_rows(object(), rows, policy)


def build(row, blend=None):
    return builder.build_oar_recommendation(row, POLICY, blend)


# --- who gets a blend ------------------------------------------------------------------------------------


def test_an_eligible_material_with_a_similarity_estimate_gets_a_blend(world) -> None:
    world()
    result = run([oar_row()])[KEY]
    assert result.status is BlendStatus.BLENDED and result.used_peer_count == 5
    assert result.rop < 36 and result.max_stock < 48  # the similarity-only answer was 36 / 48


def test_a_material_that_does_not_qualify_for_min_max_keeps_its_similarity_estimate(world) -> None:
    world()
    row = oar_row(consumption_count_12m=2)  # NORMAL tier, two issues: not eligible
    assert run([row]) == {}
    built = build(row, None)
    assert (built.recommended_safety_stock, built.recommended_rop, built.recommended_max_stock) == (1, 36, 48)
    assert built.safety_stock_method == "similarity_weighted"


def test_a_failed_similarity_estimate_gets_no_blend(world) -> None:
    world()
    assert run([oar_row(estimate_status="NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS", neighbour_count=1)]) == {}


def test_the_conversion_decision_is_untouched_by_the_blend(world) -> None:
    world()
    row = oar_row()
    blend = run([row])[KEY]
    assert build(row, blend).conversion == build(row, None).conversion


# --- which similar materials count -----------------------------------------------------------------------


def test_a_donor_a_reviewer_removed_is_set_aside(world) -> None:
    world(removed={("5000034258", "1500")})  # the 100% match
    result = run([oar_row()])[KEY]
    assert result.used_peer_count == 4
    assert "5000034258" not in {p.material for p in result.peers}


def test_a_neighbour_below_the_minimum_similarity_or_without_inventory_is_ignored(world) -> None:
    world(neighbours=NEIGHBOURS[:2] + [neighbour("x", "1500", 3, 0.55, 5, 1, 6), neighbour("y", "1500", 4, 0.9, 5, 1, 6, eligible=False)])
    assert {p.material for p in run([oar_row()])[KEY].peers} == {"5000034258", "5000074749"}


# --- what is written into the recommendation -------------------------------------------------------------


def test_the_recommendation_carries_the_blend_and_says_so(world) -> None:
    world()
    row = oar_row()
    blend = run([row])[KEY]
    built = build(row, blend)

    assert built.status is LifecycleStatus.READY_FOR_REVIEW
    assert (built.recommended_safety_stock, built.recommended_rop, built.recommended_max_stock) == (
        blend.ss, blend.rop, blend.max_stock,
    )
    assert built.safety_stock_method == "credibility_blend"
    assert built.max_stock_strategy == "rop_x_peer_max_ratio"
    assert built.lead_time_days == Decimal("14.0") and built.lead_time_method == "PLANNED_FALLBACK"
    trace = dict(built.calculation_trace)
    for name in ("estimation_method", "rop_own", "rop_peer", "w_own", "w_peer", "peer_quality_q", "lead_time_source",
                 "guardrails_applied", "guardrails_not_applied", "similarity_rop", "blend_summary"):
        assert name in trace, name
    assert trace["estimation_method"] == "CREDIBILITY_BLEND" and trace["similarity_rop"] == "36"


def test_the_card_text_states_the_split_and_is_not_written_by_a_model(world) -> None:
    world()
    row = oar_row()
    built = build(row, run([row])[KEY])
    result = rationale.generate_rationale(built)
    assert result.source == rationale.RATIONALE_SOURCE_FALLBACK
    assert "% from this material's own 6 consumption events" in result.text
    assert "% from 5 similar materials" in result.text and "Lead time 14 days (from the material master)" in result.text


def test_the_impact_is_worked_from_the_blended_numbers(world) -> None:
    world()
    row = oar_row()
    blend = run([row])[KEY]
    delta_blend = build(row, blend).impact.reorder_point.delta
    delta_similarity = build(row, None).impact.reorder_point.delta
    assert delta_blend == blend.rop and delta_similarity == 36


# --- what the blend cannot size: the similarity numbers stand --------------------------------------------


@pytest.mark.parametrize(
    "setup, status",
    [
        (dict(own={}), BlendStatus.NO_CONSUMPTION),
        (dict(window=False), BlendStatus.NO_BUCKETS),
    ],
)
def test_a_material_the_blend_cannot_size_keeps_the_similarity_numbers(world, setup, status) -> None:
    world(**setup)
    row = oar_row()
    blend = run([row])[KEY]
    assert blend.status is status and not blend.sized
    built = build(row, blend)
    assert (built.recommended_safety_stock, built.recommended_rop, built.recommended_max_stock) == (1, 36, 48)
    assert built.safety_stock_method == "similarity_weighted"
    assert "estimation_method" not in dict(built.calculation_trace) or dict(built.calculation_trace)["estimation_method"] != "CREDIBILITY_BLEND"


def test_with_no_lead_time_anywhere_the_similarity_numbers_stand(world) -> None:
    world()
    row = oar_row(lead_time_days=None, planned_delivery_time_days=None)
    blend = run([row])[KEY]
    assert blend.status is BlendStatus.NO_LEAD_TIME
    assert build(row, blend).recommended_rop == 36


def test_a_missing_lead_time_falls_back_to_the_similar_materials(world) -> None:
    world(lead_times={("5000074749", "1500"): Decimal(14), ("5000074750", "1500"): Decimal(14)})
    row = oar_row(lead_time_days=None, planned_delivery_time_days=None)
    blend = run([row])[KEY]
    assert blend.sized and blend.lead_time_source == "PEER_WEIGHTED_MEAN" and blend.lead_time_days == pytest.approx(14)
    built = build(row, blend)
    assert built.lead_time_method == "PEER_WEIGHTED_MEAN"
    assert "average of its similar materials is assumed" in " ".join(f.detail for f in built.factors)
    assert "assumed, average of the similar materials" in rationale.generate_rationale(built).text


def test_the_blend_is_reproducible(world) -> None:
    world()
    assert run([oar_row()]) == run([oar_row()])
