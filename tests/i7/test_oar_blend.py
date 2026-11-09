"""The credibility-weighted ROP / SS / Max for an OAR material (``oar.blend``): pure, so no database.

The fixtures are the eight materials checked on production -- their real monthly use, issue count, lead time and
similar materials (similarity, SAP ROP / SS / Max, annual use) -- so the numbers asserted here are the ones the
business looked at. Bands, not single values, where the bootstrap's own spread would make a point value brittle.
"""

from __future__ import annotations

import math

import pytest

from datetime import date

from app.initiatives.i7.oar.blend import (
    BlendInput,
    IssueLine,
    net_issue_sizes,
    BlendStatus,
    LeadTimeSource,
    PeerInput as P,
    estimate,
    explain,
    explain_split,
    own_estimate,
    resolve_lead_time,
)
from app.initiatives.i7.policy import OarBlendPolicy, PriceBandCap

CFG = OarBlendPolicy()
SEED = 42

FX = {
    "angle50": dict(
        m=[40, 0, 0, 0, 0, 0, 36, 44, 0, 0, 0, 0], n=6, lt=21,
        peers=[P("5000074253", "1500", .72, 10, 2, 11, 11), P("5000074250", "1500", .71, 25, 5, 29, 48),
               P("5000074254", "1500", .70, 29, 6, 34, 35), P("5000074248", "1500", .70, 8, 1, 9, 9),
               P("5000074252", "1500", .69, 1, 1, 1, 1)]),
    "angle60": dict(
        m=[0, 10, 0, 0, 0, 0, 9, 12, 5, 0, 0, 0], n=6, lt=21,
        peers=[P("5000074254", "1500", .71, 29, 6, 34, 35), P("5000074288", "1500", .70, 9, 1, 11, 5),
               P("5000074253", "1500", .69, 10, 2, 11, 11), P("5000074252", "1500", .68, 1, 1, 1, 1),
               P("5000074250", "1500", .68, 25, 5, 29, 48)]),
    "plate": dict(
        m=[16, 0, 0, 0, 0, 0, 18, 0, 20, 0, 0, 0], n=7, lt=21,
        peers=[P("5000018106", "1300", .62, 14, 2, 16, 14), P("5000016392", "1300", .61, 4, 1, 4, 0),
               P("5000018107", "1300", .60, 9, 2, 10, 0), P("5000016394", "1300", .60, 2, 1, 2, 2)]),
    "belts": dict(
        m=[0, 6, 4, 0, 4, 6, 0, 0, 0, 10, 0, 0], n=6, lt=14,
        peers=[P("5000034258", "1500", 1.0, 10, 1, 20, 53), P("5000074749", "1500", .99, 36, 6, 60, 96),
               P("5000074750", "1500", .98, 36, 1, 60, 22), P("5000074752", "1500", .98, 22, 1, 42, 10),
               P("5000074751", "1500", .98, 36, 2, 48, 13)]),
    "light": dict(
        m=[30, 0, 40, 30, 0, 0, 0, 0, 0, 0, 100, 10], n=6, lt=21,
        peers=[P("5000079444", "1500", .62, 20, 3, 50, 50), P("5000079443", "1500", .61, 7, 1, 8, 49)]),
    "element_a": dict(
        m=[4, 0, 2, 3, 1, 0, 0, 0, 0, 0, 0, 0], n=10, lt=21,
        peers=[P("5000076234", "1500", .84, 7, 1, 9, 9), P("5000017527", "1300", .84, 17, 3, 19, 19)]),
    "element_b": dict(
        m=[1, 0, 2, 0, 0, 0, 5, 0, 0, 0, 0, 0], n=6, lt=21,
        peers=[P("5000076234", "1500", .85, 7, 1, 9, 9), P("5000017527", "1300", .85, 17, 3, 19, 19)]),
    "gasket": dict(
        m=[0, 0, 0, 0, 6, 0, 0, 0, 3, 0, 0, 0], n=5, lt=30,
        peers=[P("5000074422", "1500", .62, 1, 1, 1, 0), P("5000074418", "1500", .61, 2, 1, 2, 2)]),
}


def run(name: str, config: OarBlendPolicy = CFG, **overrides):
    f = FX[name]
    kwargs = dict(monthly=f["m"], n_events=f["n"], lead_time_days=f["lt"], peers=f["peers"])
    kwargs.update(overrides)
    return estimate(BlendInput(**kwargs), config, SEED)


# --- deterministic ---------------------------------------------------------------------------------------


def test_the_same_input_and_seed_give_the_same_result() -> None:
    assert run("belts") == run("belts")
    assert own_estimate(FX["belts"]["m"], 14, CFG, 7) == own_estimate(FX["belts"]["m"], 14, CFG, 7)


# --- the credibility weight and the scale-free peers -----------------------------------------------------


def test_the_own_weight_is_n_over_n_plus_k_q() -> None:
    result = run("belts")
    expected = result.n_events / (result.n_events + CFG.credibility_k * result.peer_quality_q)
    assert result.w_own == pytest.approx(expected)
    assert result.w_peer == pytest.approx(1 - expected)


def test_weak_peers_hand_weight_back_to_the_own_history() -> None:
    strong = run("element_a")  # similarity 0.84
    weak = run("element_a", peers=[p._replace(similarity=0.60) for p in FX["element_a"]["peers"]])
    assert weak.peer_quality_q < strong.peer_quality_q
    assert weak.w_own > strong.w_own


def test_more_own_issues_move_weight_to_the_own_estimate() -> None:
    assert run("belts", n_events=20).w_own > run("belts", n_events=5).w_own


def test_a_peers_demand_scale_does_not_leak_into_the_answer() -> None:
    """Ten times the use and ten times the ROP/SS/Max is the same policy: the result must not move."""
    scaled = [p._replace(rop=p.rop * 10, safety_stock=p.safety_stock * 10, max_stock=p.max_stock * 10,
                         annual_consumption=p.annual_consumption * 10) for p in FX["angle50"]["peers"]]
    base, big = run("angle50"), run("angle50", peers=scaled)
    assert (big.rop, big.ss, big.max_stock) == (base.rop, base.ss, base.max_stock)


def test_a_closer_match_weighs_much_more_than_a_looser_one() -> None:
    result = run("angle50")
    weights = {p.material: p.weight for p in result.peers}
    assert weights["5000074253"] > weights["5000074252"]  # 0.72 against 0.69
    # steep weighting: weight is (sim - 0.5) ** 3, so 1.00 against 0.60 is (0.5 / 0.1) ** 3 = 125 times
    steep = estimate(BlendInput(FX["belts"]["m"], 6, 14, [P("a", "1", 1.0, 10, 1, 20, 53), P("b", "1", 0.6, 36, 6, 60, 96)]), CFG, SEED)
    by = {p.material: p.weight for p in steep.peers}
    assert by["a"] / by["b"] == pytest.approx(125.0)


def test_a_peer_with_no_use_or_no_rop_is_left_out_of_the_cover_ratios() -> None:
    result = run("plate")  # two of its four similar materials have no use in the window
    used = {p.material: p.used for p in result.peers}
    assert used == {"5000018106": True, "5000016392": False, "5000018107": False, "5000016394": True}
    assert result.used_peer_count == 2


def test_no_usable_peer_leaves_the_own_estimate_alone() -> None:
    result = run("gasket", peers=[P("x", "1", .62, 1, 1, 1, 0)])
    assert result.status is BlendStatus.OWN_ONLY
    assert result.w_own == 1.0 and result.rop_peer is None
    assert "no_usable_similar_material: own estimate only" in result.guardrails_applied


# --- the own estimate ------------------------------------------------------------------------------------


def test_the_reorder_point_is_the_empirical_percentile_of_lead_time_demand() -> None:
    # 12 buckets: seven zeros, 4, 4, 6, 6, 10 -> the 85th percentile of one month is 6 (the belts)
    rop, ss, mean = own_estimate(FX["belts"]["m"], 14, CFG, SEED)
    assert rop == pytest.approx(6.0, abs=0.01)
    assert mean == pytest.approx(2.5, abs=0.1)
    assert ss == pytest.approx(rop - mean)


def test_prorating_a_short_lead_time_lowers_the_own_estimate() -> None:
    whole = own_estimate(FX["belts"]["m"], 14, CFG, SEED)[0]
    prorated = own_estimate(FX["belts"]["m"], 14, OarBlendPolicy(lead_time_bucket_mode="prorated"), SEED)[0]
    assert prorated < whole


def test_a_longer_lead_time_needs_more_stock() -> None:
    assert own_estimate(FX["light"]["m"], 60, CFG, SEED)[0] > own_estimate(FX["light"]["m"], 21, CFG, SEED)[0]


def test_croston_sba_is_an_alternative_estimator_behind_the_flag() -> None:
    steady = [5.0] * 12  # every month 5: Croston's rate is 5 x (1 - alpha / 2) a month
    cfg = OarBlendPolicy(own_estimator="croston_sba")
    rop, ss, mean = own_estimate(steady, 30.44, cfg, SEED)
    assert mean == pytest.approx(5.0 * (1 - cfg.croston_alpha / 2))
    assert ss == 0.0
    result = run("belts", config=cfg)
    assert result.sized and result.estimator == "croston_sba"


# --- Max is derived, then guarded ------------------------------------------------------------------------


def test_max_follows_the_rop_and_never_falls_below_it() -> None:
    for name in FX:
        result = run(name)
        assert result.max_stock >= result.rop


def test_the_max_to_rop_ratio_has_a_floor_while_there_is_no_eoq() -> None:
    result = run("gasket")  # its one usable peer has Max / ROP of 1.0
    assert result.max_ratio == pytest.approx(CFG.max_ratio_floor)


def test_the_max_never_exceeds_a_years_use() -> None:
    result = run("belts")
    assert result.max_stock <= result.annual_consumption  # 30
    assert "max_capped_at_annual_consumption" in result.guardrails_applied


def test_a_max_above_six_months_of_cover_is_flagged() -> None:
    assert any(g.startswith("flag_max_above_6") for g in run("angle50").guardrails_applied)


def test_a_price_band_tightens_the_max_cap() -> None:
    cfg = OarBlendPolicy(max_cap_by_price=(PriceBandCap(min_unit_price=500, max_months_cover=6),))
    capped = run("angle50", config=cfg, unit_price=1000.0)
    plain = run("angle50", unit_price=1000.0)
    assert capped.max_stock < plain.max_stock
    assert capped.max_stock <= math.ceil(6 * 120 / 12)  # six months of its own use: 60
    assert capped.max_stock >= capped.rop
    assert any(g.startswith("max_capped_by_price_band") for g in capped.guardrails_applied)
    # a cheaper item is not touched
    assert run("angle50", config=cfg, unit_price=100.0).max_stock == plain.max_stock


def test_a_price_cap_below_the_rop_still_leaves_the_max_at_the_rop() -> None:
    cfg = OarBlendPolicy(max_cap_by_price=(PriceBandCap(min_unit_price=500, max_months_cover=2),))
    result = run("angle50", config=cfg, unit_price=1000.0)
    assert result.max_stock == result.rop
    assert "max_raised_to_rop" in result.guardrails_applied


def test_what_is_not_applied_is_stated_in_every_result() -> None:
    result = run("belts")
    text = " ".join(result.guardrails_not_applied)
    assert "issue_size_floor" in text and "project_or_order_exclusion" in text and "pack_rounding" in text


# --- the lead-time fallback chain ------------------------------------------------------------------------


def test_lead_time_is_taken_from_the_material_master_first() -> None:
    days, source = resolve_lead_time(21, 30, FX["belts"]["peers"], floor=.5, exponent=3)
    assert (days, source) == (21.0, LeadTimeSource.MATERIAL_MASTER.value)


def test_lead_time_falls_back_to_the_plant_default() -> None:
    assert resolve_lead_time(None, 30, [], floor=.5, exponent=3) == (30.0, LeadTimeSource.PLANT_DEFAULT.value)


def test_lead_time_falls_back_to_the_similar_materials_weighted_mean() -> None:
    peers = [P("a", "1", .9, 1, 1, 1, 1, lead_time_days=10), P("b", "1", .6, 1, 1, 1, 1, lead_time_days=50)]
    days, source = resolve_lead_time(None, None, peers, floor=.5, exponent=3)
    assert source == LeadTimeSource.PEER_WEIGHTED_MEAN.value
    assert 10 < days < 50 and days < 30  # the closer match counts for more


def test_with_no_lead_time_anywhere_nothing_is_invented() -> None:
    result = run("belts", lead_time_days=None)
    assert result.status is BlendStatus.NO_LEAD_TIME and not result.sized and result.rop is None


def test_the_lead_time_source_is_recorded() -> None:
    assert run("belts").lead_time_source == LeadTimeSource.MATERIAL_MASTER.value
    assert run("belts", lead_time_days=None, config=OarBlendPolicy(plant_default_lead_time_days=28)).lead_time_source == "PLANT_DEFAULT"


# --- what cannot be sized --------------------------------------------------------------------------------


def test_no_consumption_in_the_window_is_not_read_as_zero_need() -> None:
    result = run("belts", monthly=[0.0] * 12)
    assert result.status is BlendStatus.NO_CONSUMPTION and result.rop is None


def test_an_unusable_window_is_not_read_as_no_consumption() -> None:
    assert run("belts", monthly=None).status is BlendStatus.NO_BUCKETS


# --- the eight materials checked on production -----------------------------------------------------------


@pytest.mark.parametrize(
    "name, rop_band, max_band",
    [
        ("angle50", (45, 65), (55, 75)),   # single draws of 36-44: the old ROP of 10 was below one draw
        ("angle60", (14, 20), (17, 24)),
        ("plate", (20, 30), (25, 36)),
        ("belts", (17, 25), (26, 30)),     # was 36 / 48; capped at the year's use of 30
        ("light", (40, 50), (75, 95)),     # one 100-unit draw in the window (the guardrail for it needs MSEG lines)
        ("element_a", (4, 7), (5, 8)),
        ("element_b", (3, 6), (4, 7)),
        ("gasket", (4, 7), (5, 8)),
    ],
)
def test_the_eight_production_materials(name, rop_band, max_band) -> None:
    result = run(name)
    assert result.status is BlendStatus.BLENDED
    assert rop_band[0] <= result.rop <= rop_band[1]
    assert max_band[0] <= result.max_stock <= max_band[1]
    assert result.ss <= result.rop
    assert 0 < result.w_own < 1 and result.w_own + result.w_peer == pytest.approx(1)


def test_the_wedge_belt_no_longer_carries_the_peers_scale() -> None:
    result = run("belts")
    assert result.rop < 36 and result.max_stock < 48  # the similarity-only answer was 36 / 48
    assert result.rop_peer > result.rop_own  # the peers still pull up, but the own history now carries 61%


# --- the words on the card -------------------------------------------------------------------------------


def test_the_explanation_is_plain_words_with_the_split_in_numbers() -> None:
    lines = explain(run("belts")).split("\n")  # one short paragraph per line; the card shows each as a bullet
    assert lines[0] == (
        "The recommended reorder level is 21 units. It is mainly based on this material's own usage (61%), "
        "with some reference from 5 similar materials (39%)."
    )
    assert lines[-1] == "The calculation also uses a lead time of 14 days."  # a lead time kept for the material is not annotated
    text = " ".join(lines)
    assert "A safety stock of 4 units is kept to protect against unexpected demand or delays in supply." in text
    assert "The maximum stock is 30 units, so there is enough stock without overstocking." in text
    assert "thin" not in text and "kept for this material in SAP" not in text


def test_a_guardrail_is_said_in_a_sentence_never_as_its_code() -> None:
    text = explain(run("light", issue_sizes=LED_ISSUES))
    assert "The reorder level was raised to 50 so that it covers one normal single issue of this material (about 50 units)." in text
    for code in ("rop_raised", "flag_max", "max_capped", "issue_size_p90", "guardrail"):
        assert code not in text


def test_the_max_limits_are_each_said_plainly() -> None:
    text = explain(run("belts"))
    assert "It is limited to one year of this material's usage." in text
    assert "That is more than 6 months of this material's usage, so it is worth a check." in text


def test_a_blend_mainly_from_similar_materials_says_so() -> None:
    mostly_peers = run("element_a", n_events=1)  # one issue: the peers carry most of the weight
    assert mostly_peers.w_own < 0.5
    assert "It is mainly based on 2 similar materials" in explain(mostly_peers).split("\n")[0]


def test_with_no_usable_similar_material_it_is_based_on_the_material_alone() -> None:
    alone = run("gasket", peers=[P("x", "1", .62, 1, 1, 1, 0)])
    assert "It is based on this material's own usage; no similar material could be scaled to its demand." in explain(alone)


def test_an_assumed_lead_time_is_said_to_be_assumed() -> None:
    plant = run("belts", lead_time_days=None, config=OarBlendPolicy(plant_default_lead_time_days=28))
    assert explain(plant).endswith("a lead time of 28 days (assumed: plant default).")


def test_one_unit_is_singular() -> None:
    from dataclasses import replace

    text = explain(replace(run("belts"), rop=1, ss=1, max_stock=2))
    assert "reorder level is 1 unit." in text and "A safety stock of 1 unit is kept" in text
    assert "1 units" not in text and "maximum stock is 2 units" in text


def test_no_safety_stock_is_not_called_a_stock_of_zero() -> None:
    from dataclasses import replace

    text = explain(replace(run("belts"), ss=0))
    assert "No extra safety stock is kept." in text and "safety stock of 0" not in text


def test_the_one_line_split_for_the_list_of_drivers() -> None:
    assert explain_split(run("belts")) == (
        "Reorder level 21: 61% from this material's own usage, 39% from 5 similar materials."
    )


def test_an_unsized_result_has_no_explanation() -> None:
    assert explain(run("belts", lead_time_days=None)) == ""


# --- the issue-size floor: ROP is never below a normal single issue ------------------------------------

LED_ISSUES = [30, 40, 30, 50, 50, 10]  # the LED light's single issue lines, Nov 2025 - Oct 2026 (two of 50 on 14 Sep)
ISSUE_TYPES, REVERSAL_TYPES = frozenset({"201", "261"}), frozenset({"202", "262"})


def line(movement, qty, reservation="R1", day=1):
    return IssueLine(movement, float(qty), reservation, date(2026, 5, day))


def test_a_reversal_cancels_the_earlier_issue_of_the_same_size_and_reservation() -> None:
    lines = [line("201", 50, "R1", 1), line("201", 30, "R2", 2), line("202", 50, "R1", 3)]
    assert net_issue_sizes(lines, ISSUE_TYPES, REVERSAL_TYPES) == [30.0]


def test_a_reversal_with_nothing_to_cancel_is_ignored() -> None:
    lines = [line("201", 30, "R2", 2), line("202", 50, "R9", 3)]  # its issue predates the window
    assert net_issue_sizes(lines, ISSUE_TYPES, REVERSAL_TYPES) == [30.0]


def test_two_issues_of_the_same_size_are_two_issues_and_one_reversal_cancels_one() -> None:
    lines = [line("201", 50, "R1", 1), line("201", 50, "R1", 2), line("202", 50, "R1", 3)]
    assert net_issue_sizes(lines, ISSUE_TYPES, REVERSAL_TYPES) == [50.0]


def test_order_issues_261_count_like_cost_centre_issues_201() -> None:
    assert net_issue_sizes([line("261", 7), line("201", 3)], ISSUE_TYPES, REVERSAL_TYPES) == [7.0, 3.0] or sorted(
        net_issue_sizes([line("261", 7), line("201", 3)], ISSUE_TYPES, REVERSAL_TYPES)
    ) == [3.0, 7.0]


def test_the_rop_is_raised_to_the_90th_percentile_of_single_issues() -> None:
    plain = run("light")
    floored = run("light", issue_sizes=LED_ISSUES)
    assert floored.issue_size_floor == pytest.approx(50.0)  # 90th percentile of 10, 30, 30, 40, 50, 50
    assert plain.rop < 50 <= floored.rop
    assert "rop_raised_to_issue_size_p90" in floored.guardrails_applied
    assert floored.max_stock >= floored.rop  # Max follows the raised reorder point
    assert floored.issue_count == 6


def test_max_is_derived_from_the_raised_rop() -> None:
    plain, floored = run("light"), run("light", issue_sizes=LED_ISSUES)
    assert floored.max_stock > plain.max_stock or floored.max_stock == floored.rop


def test_a_rop_already_above_the_floor_is_left_alone() -> None:
    plain = run("belts")  # ROP 21; its issues are 4 to 10
    floored = run("belts", issue_sizes=[6, 4, 4, 6, 10])
    assert floored.rop == plain.rop and floored.issue_size_floor == pytest.approx(8.4)
    assert not any(g.startswith("rop_raised") for g in floored.guardrails_applied)


def test_without_single_issue_lines_the_floor_is_not_applied_and_says_so() -> None:
    result = run("light")
    assert result.issue_size_floor is None
    assert any(n.startswith("issue_size_floor") for n in result.guardrails_not_applied)


def test_once_the_floor_is_worked_out_it_leaves_the_not_applied_list() -> None:
    result = run("light", issue_sizes=LED_ISSUES)
    text = " ".join(result.guardrails_not_applied)
    assert "issue_size_floor" not in text
    assert "project_or_order_exclusion" in text and "pack_rounding" in text  # these are still not applied


def test_the_floor_can_be_switched_off() -> None:
    off = OarBlendPolicy(issue_size_floor_percentile=None)
    assert run("light", config=off, issue_sizes=LED_ISSUES).issue_size_floor is None


def test_a_percentile_other_than_90_is_honoured() -> None:
    assert run("light", config=OarBlendPolicy(issue_size_floor_percentile=50), issue_sizes=LED_ISSUES).issue_size_floor == pytest.approx(35.0)


def test_the_outlier_rule_is_off_by_default_and_sets_aside_a_huge_single_issue_when_on() -> None:
    sizes = [10, 10, 11, 12, 400]
    off = run("belts", issue_sizes=sizes)
    on = run("belts", config=OarBlendPolicy(issue_outlier_multiple=5), issue_sizes=sizes)
    assert off.issue_size_floor > 100  # one huge issue sets the floor
    assert on.issue_size_floor < 15
    assert any(g.startswith("issue_outliers_ignored") for g in on.guardrails_applied)
    assert not any(g.startswith("issue_outliers_ignored") for g in off.guardrails_applied)
