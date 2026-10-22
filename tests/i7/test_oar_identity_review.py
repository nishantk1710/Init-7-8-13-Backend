"""The expert-review tooling (scripts/oar_identity_review.py): sampling, blind sheet, measurement.

Everything here is synthetic. These tests prove the MECHANICS -- the sample is spread and reproducible, the sheet
hides what the engines chose, a blank answer is unjudged rather than "no", the measurements are the right
arithmetic. They say nothing about whether the new rules find the right materials on real data; that is what
running the tool with the real model and a reviewer is for.
"""

from __future__ import annotations

import csv
from decimal import Decimal

import pytest

from app.initiatives.i7.oar.types import CandidateAttributes
from scripts import oar_identity_review as review
from scripts.oar_identity_review import Judged, PairRecord, TargetInfo

D = Decimal


def info(material, *, crit="NORMAL", group="G1", description=True, typed=False, pn=False, eligible=50):
    return TargetInfo((material, "1300"), crit, group, description, typed, pn, eligible)


def population():
    pool = []
    for i in range(300):
        pool.append(info(f"A{i}", crit="NORMAL", group="G1", typed=True))
    for i in range(100):
        pool.append(info(f"B{i}", crit="CRITICAL", group="G2", pn=True))
    for i in range(40):
        pool.append(info(f"C{i}", crit="IMPACT", group="308175", typed=bool(i % 2), pn=bool(i % 3)))
    for i in range(10):
        pool.append(info(f"D{i}", crit="NORMAL", group="G9"))
    pool.append(info("NODESC", description=False))
    pool.append(info("FEWDONORS", eligible=2))
    return pool


# --- sampling ----------------------------------------------------------------------------------------


def test_the_sample_is_the_requested_size_and_deterministic() -> None:
    first = review.choose_targets(population(), 50, seed=3)
    again = review.choose_targets(population(), 50, seed=3)
    other = review.choose_targets(population(), 50, seed=4)

    assert len(first) == 50
    assert [t.key for t in first] == [t.key for t in again]
    assert [t.key for t in first] != [t.key for t in other]


def test_the_sample_is_spread_across_strata_not_filled_by_the_biggest() -> None:
    chosen = review.choose_targets(population(), 50, seed=1)
    strata = {t.stratum for t in chosen}
    groups = {t.group for t in chosen}

    assert len(strata) >= 6
    assert {"G1", "G2", "308175", "G9"} <= groups
    big = sum(1 for t in chosen if t.group == "G1")
    assert big < 30  # 300 of 450 targets are G1; they must not take 2/3 of the sample


def test_targets_that_cannot_be_reviewed_are_never_chosen() -> None:
    keys = {t.key[0] for t in review.choose_targets(population(), 450, seed=1)}

    assert "NODESC" not in keys and "FEWDONORS" not in keys


def test_the_reported_cases_are_always_included() -> None:
    chosen = review.choose_targets(population(), 10, seed=1, include=[("D3", "1300"), ("MISSING", "1300")])

    assert ("D3", "1300") in {t.key for t in chosen}  # a missing one is skipped, not an error


def test_the_focus_group_is_over_sampled_with_a_spread_of_description_kinds() -> None:
    chosen = review.choose_targets(population(), 20, seed=1, focus_group="308175", focus_count=10)
    focus = [t for t in chosen if t.group == "308175"]

    assert len(focus) >= 10
    assert {(t.has_component_type, t.has_part_number) for t in focus} >= {(True, True), (False, False)}


# --- merging the arms -----------------------------------------------------------------------------------

T = ("T", "1300")


def k(name):
    return (name, "1300")


def test_a_candidate_chosen_by_both_rules_is_one_pair_with_both_ranks() -> None:
    records = review.merge_pairs(
        T,
        old_neighbours=[(k("A"), D("0.9"), D("0.95"), True), (k("B"), D("0.5"), D("0.8"), True)],
        new_neighbours=[(k("A"), D("0.9"), D("0.95"), True)],
        refused=[(k("B"), D("0.5"), D("0.8"), True, "REJECTED_COMPONENT_CONFLICT", "nozzle")],
        random_donors=[(k("R"), D("0.1"), D("0.6"), False)],
    )
    by = {r.candidate: r for r in records}

    assert len(records) == 3
    assert by[k("A")].arms == {"old", "new"} and (by[k("A")].old_rank, by[k("A")].new_rank) == (1, 1)
    assert by[k("B")].arms == {"old", "refused"} and by[k("B")].new_rank is None
    assert by[k("R")].arms == {"random"}


def test_only_the_best_refused_are_added() -> None:
    refused = [(k(f"X{i}"), D("0.5"), D("0.7"), True, "S", "r") for i in range(9)]
    records = review.merge_pairs(T, old_neighbours=[], new_neighbours=[], refused=refused,
                                 random_donors=[], refused_limit=5)

    assert len(records) == 5


# --- the blind sheet ------------------------------------------------------------------------------------


def attrs(material, description, group="G1", price="10"):
    return CandidateAttributes(material, "1300", "NORMAL", True, 24, group, None, "EA", None, None,
                               D(price), description)


def written(tmp_path):
    target = attrs("T", "FILTER PN:5112305242")
    candidates = {k(f"C{i}"): attrs(f"C{i}", f"DESC {i}") for i in range(12)}
    records = [
        PairRecord(T, k(f"C{i}"), {"old"} if i < 6 else {"new"}, old_rank=i + 1 if i < 6 else None,
                   new_rank=None if i < 6 else i - 5, text_similarity=D("0.6"), same_group=True,
                   combined=D("0.8"), identity_status="CONFIRMED_TEXT", identity_reason="r")
        for i in range(12)
    ]
    review.write_files(tmp_path, {T: target}, candidates, {T: records}, {T: "NORMAL|G1|type|pn"}, seed=1)
    return records


def rows(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def test_the_reviewer_sheet_carries_no_engine_information(tmp_path) -> None:
    written(tmp_path)
    sheet = rows(tmp_path / "review_sheet.csv")

    assert set(sheet[0]) == set(review.REVIEW_COLUMNS)
    forbidden = {"old_rank", "new_rank", "arms", "combined", "text_similarity", "identity_status", "identity_reason"}
    assert not forbidden & set(sheet[0])
    assert all(row["relevant"] == "" for row in sheet)
    assert (tmp_path / "PROTOCOL.md").read_text(encoding="utf-8").startswith("# OAR similarity review")


def test_the_rows_are_shuffled_so_position_hints_nothing(tmp_path) -> None:
    written(tmp_path)
    key = rows(tmp_path / "key.csv")
    order = [row["candidate_material"] for row in key]

    assert sorted(order) == sorted(f"C{i}" for i in range(12))
    assert order != [f"C{i}" for i in range(12)]  # not in engine order


def test_the_key_joins_back_to_the_sheet_by_pair_id(tmp_path) -> None:
    written(tmp_path)
    sheet = {row["pair_id"]: row for row in rows(tmp_path / "review_sheet.csv")}
    key = {row["pair_id"]: row for row in rows(tmp_path / "key.csv")}

    assert sheet.keys() == key.keys() and len(sheet) == 12
    for pair_id, row in key.items():
        assert row["candidate_material"] == sheet[pair_id]["candidate_material"]
        assert row["stratum"] == "NORMAL|G1|type|pn"
    assert {r["arms"] for r in key.values()} == {"old", "new"}


# --- reading the completed sheet --------------------------------------------------------------------------


def fill(tmp_path, answers: dict[str, str]):
    """Complete the written sheet: ``answers`` maps candidate material -> relevant value."""
    sheet = rows(tmp_path / "review_sheet.csv")
    for row in sheet:
        row["relevant"] = answers.get(row["candidate_material"], "")
        row["reviewer"] = "R. Expert"
    path = tmp_path / "completed.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=review.REVIEW_COLUMNS)
        writer.writeheader()
        writer.writerows(sheet)
    return path


def test_blank_and_unsure_are_counted_and_never_read_as_no(tmp_path) -> None:
    written(tmp_path)
    path = fill(tmp_path, {"C0": "yes", "C1": "NO", "C2": "unsure", "C3": "y", "C4": "n"})

    load = review.load_review(path, tmp_path / "key.csv")

    assert len(load.judged) == 4 and load.unsure == 1 and load.unjudged == 7
    assert {j.candidate[0]: j.relevant for j in load.judged} == {"C0": True, "C1": False, "C3": True, "C4": False}


def test_an_unreadable_answer_is_an_error_not_a_skip(tmp_path) -> None:
    written(tmp_path)
    path = fill(tmp_path, {"C0": "maybe-ish"})

    with pytest.raises(ValueError, match="yes/no/unsure"):
        review.load_review(path, tmp_path / "key.csv")


def test_a_pair_id_missing_from_the_key_is_reported(tmp_path) -> None:
    written(tmp_path)
    sheet = rows(tmp_path / "review_sheet.csv")
    sheet[0]["pair_id"], sheet[0]["relevant"] = "P9999", "yes"
    path = tmp_path / "bad.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=review.REVIEW_COLUMNS)
        writer.writeheader()
        writer.writerows(sheet)

    load = review.load_review(path, tmp_path / "key.csv")

    assert load.unknown_pair_ids == ["P9999"] and load.judged == []


# --- the measurements, by hand --------------------------------------------------------------------------------

T1, T2 = ("T1", "1300"), ("T2", "1300")


def j(pair_id, target, cand, relevant, *, old=None, new=None, text, same, status, arms):
    return Judged(pair_id, target, k(cand), relevant, frozenset(arms), old, new, D(text), same, D("0.8"), status, None)


JUDGED = [
    j("1", T1, "A", True, old=1, new=1, text="0.90", same=True, status="CONFIRMED_PART_NUMBER", arms={"old", "new"}),
    j("2", T1, "B", False, old=2, text="0.55", same=True, status="REJECTED_COMPONENT_CONFLICT", arms={"old", "refused"}),
    j("3", T1, "C", True, old=3, new=2, text="0.70", same=True, status="CONFIRMED_GROUP_TEXT", arms={"old", "new"}),
    j("4", T1, "D", True, text="0.40", same=True, status="INSUFFICIENT_GROUP_ONLY", arms={"refused"}),
    j("5", T1, "R", False, text="0.10", same=False, status="INSUFFICIENT_TEXT", arms={"random"}),
    j("6", T2, "E", False, old=1, text="0.30", same=True, status="INSUFFICIENT_GROUP_ONLY", arms={"old"}),
    j("7", T2, "F", True, new=1, text="0.80", same=False, status="CONFIRMED_TEXT", arms={"new"}),
]


def test_ranking_quality_old_against_new() -> None:
    """Old top lists: T1 [A yes, B no, C yes], T2 [E no]. New: T1 [A, C], T2 [F yes].
    precision@5: old (2/3 + 0)/2 = 0.333, new (1 + 1)/2 = 1.0.
    recall@5 (genuine: T1 {A,C,D}, T2 {F}): old (2/3 + 0)/2 = 0.333, new (2/3 + 1)/2 = 0.833."""
    m = review.evaluate_review(JUDGED)
    old, new = m["arms"]["old"], m["arms"]["new"]

    assert m["pairs"] == 7 and m["targets"] == 2
    assert old["precision_at_k_judged"] == pytest.approx(1 / 3) and new["precision_at_k_judged"] == 1.0
    assert old["recall_at_k"] == pytest.approx(1 / 3) and new["recall_at_k"] == pytest.approx(5 / 6)
    assert (old["genuine_neighbours"], old["wrong_neighbours"]) == (2, 2)
    assert (new["genuine_neighbours"], new["wrong_neighbours"]) == (3, 0)
    assert (old["targets_with_minimum_neighbours"], new["targets_with_minimum_neighbours"]) == (1, 1)


def test_the_identity_stage_is_measured_as_a_classifier() -> None:
    """Genuine: A, C, D, F (4). Refused by the new rule: B, D, R, E. The one genuine refusal is D."""
    ident = review.evaluate_review(JUDGED)["identity"]

    assert ident["genuine_total"] == 4 and ident["genuine_refused"] == 1
    assert ident["false_rejection_rate"] == 0.25
    assert ident["kept_not_genuine"] == 0 and ident["refused_not_genuine"] == 3
    assert ident["genuine_refused_by_status"] == {"INSUFFICIENT_GROUP_ONLY": 1}
    assert ident["component_conflicts_judged_genuine"] == []


def test_a_genuine_match_lost_to_a_component_conflict_is_called_out() -> None:
    wrong = [j("9", T1, "Z", True, text="0.8", same=True, status="REJECTED_COMPONENT_CONFLICT", arms={"refused"})]
    ident = review.evaluate_review(JUDGED + wrong)["identity"]

    assert [p["pair_id"] for p in ident["component_conflicts_judged_genuine"]] == ["9"]


def test_the_what_if_applies_each_rule_to_the_same_pairs() -> None:
    """D is a genuine same-group match the built rule refused because nothing specific is shared; its reason
    says the component type agrees. Accepting such pairs admits D (genuine) and nothing wrong here."""
    d = j("4", T1, "D", True, text="0.40", same=True, status="INSUFFICIENT_GROUP_ONLY", arms={"refused"})
    d = Judged(**{**d.__dict__, "identity_reason": "no identity match: ... (component type agrees, but nothing specific is shared)"})
    others = [x for x in JUDGED if x.pair_id != "4"]

    built, alternative = review.what_if(others + [d])

    # built admits A, C, F (all genuine); the alternative also admits D
    assert (built["admitted"], built["genuine_admitted"], built["precision"]) == (3, 3, 1.0)
    assert built["recall"] == 0.75  # 3 of the 4 genuine pairs: D was refused
    assert (alternative["admitted"], alternative["genuine_admitted"], alternative["wrong_admitted"]) == (4, 4, 0)
    assert alternative["recall"] == 1.0
    assert alternative["targets_with_two_or_more_admitted"] == 1  # T1 now has A, C, D


def test_a_component_conflict_is_never_admitted_by_a_variant() -> None:
    conflict = Judged(**{**JUDGED[1].__dict__, "identity_reason": "component type conflict (component type agrees?)"})

    assert all(w["admitted"] == 0 for w in review.what_if([conflict]))


def test_the_what_if_marker_matches_the_identity_stage_wording() -> None:
    """The what-if reads the reason text; if oar.identity rewords it, this fails instead of silently
    admitting nothing."""
    from app.initiatives.i7.oar import identity
    from app.initiatives.i7.policy.thresholds import SimilarityPolicy

    config = identity.IdentityConfig.from_policy(SimilarityPolicy())
    verdict = identity.assess_descriptions(
        "FILTER PN:5112305242", "FILTER ELEMENT HYDRAULIC", same_material_group=True,
        text_similarity=D("0.44"), config=config,
    )

    assert verdict.status.value == "INSUFFICIENT_GROUP_ONLY"
    assert review.TYPE_AGREES_MARK in verdict.reason


def test_where_the_rules_disagree() -> None:
    m = review.evaluate_review(JUDGED)

    assert m["disagreement"] == {
        "dropped_by_new": {"pairs": 2, "genuine": 0},  # B and E: both correctly dropped
        "added_by_new": {"pairs": 1, "genuine": 1},  # F
    }
    assert m["random_controls"] == {"pairs": 1, "genuine": 0}


def test_text_floors_are_swept_per_route_and_conflicts_are_not_a_floor_question() -> None:
    """Same-group pairs (B is excluded -- a component conflict never reached the floor): A .90 yes,
    C .70 yes, D .40 yes, E .30 no. At 0.50: admitted A, C -> precision 1.0, recall 2/3. At 0.30: all four,
    precision 3/4."""
    floor = review.evaluate_review(JUDGED)["text_floor"]
    same = floor["same_group"]
    point = {p["threshold"]: p for p in same["points"]}

    assert same["pairs"] == 4 and same["relevant"] == 3
    assert (point["0.50"]["admitted"], point["0.50"]["precision"]) == (2, 1.0)
    assert point["0.50"]["recall"] == pytest.approx(2 / 3)
    assert (point["0.30"]["admitted"], point["0.30"]["precision"]) == (4, 0.75)
    assert same["recommended"] is None  # too few pairs to recommend anything: collect more reviews
    assert floor["other_group"]["pairs"] == 2


def test_the_gates_are_met_or_not_with_their_numbers() -> None:
    m = review.evaluate_review(JUDGED)

    results = review.gates(m, maximum_false_rejection=0.10, minimum_precision=0.80, minimum_target_share=0.70)
    met = [met for _, met, _ in results]

    assert met == [True, False, False, True]  # precision ok; 25% > 10% lost; 1 of 2 targets < 70%; no conflict lost
    assert "1 of 4 genuine candidates refused" in results[1][2]


def test_nothing_measurable_is_reported_as_such_not_as_a_pass() -> None:
    m = review.evaluate_review([j("1", T1, "A", True, new=1, text="0.9", same=True, status="CONFIRMED_TEXT", arms={"new"})])
    results = review.gates(m, maximum_false_rejection=0.1, minimum_precision=0.8, minimum_target_share=0.7)

    assert results[0][1] is None  # no old ranking to compare with


def test_the_command_line_evaluation_prints_the_report(tmp_path, capsys) -> None:
    written(tmp_path)
    sheet = rows(tmp_path / "review_sheet.csv")
    for i, row in enumerate(sheet):
        row["relevant"] = "yes" if i % 2 else "no"
    path = tmp_path / "completed.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=review.REVIEW_COLUMNS)
        writer.writeheader()
        writer.writerows(sheet)

    status = review.main(["evaluate", "--review", str(path), "--key", str(tmp_path / "key.csv"),
                          "--report", str(tmp_path / "report.md")])
    out = capsys.readouterr().out

    assert status == 0
    for heading in ("Neighbours (top 5)", "Where the rules disagree", "The identity stage", "Text floors",
                    "What-if", "Gates (PROPOSED"):
        assert heading in out
    assert "Gates (PROPOSED" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_an_empty_sheet_is_refused(tmp_path, capsys) -> None:
    written(tmp_path)
    path = fill(tmp_path, {})

    assert review.main(["evaluate", "--review", str(path), "--key", str(tmp_path / "key.csv")]) == 1


def test_sampling_stops_when_the_real_model_is_unavailable(monkeypatch, capsys) -> None:
    """A review built on a stand-in would measure the stand-in. Checked before the database is touched."""
    from app.initiatives.i7.oar import text_similarity

    monkeypatch.setattr(text_similarity, "_load_model", lambda name: None)

    status = review.main(["sample", "--out", "unused"])

    assert status == 2
    assert "real text model" in capsys.readouterr().err


# --- floors for a new text score ------------------------------------------------------------------------------


def test_the_equivalent_floor_keeps_the_same_count_of_unrelated_pairs_above_it() -> None:
    """Ten unrelated pairs. The old floor (0.50) on the old score admitted 2. On the new score those same ten
    pairs sit higher, so the floor that admits the same two is the 2nd highest new score."""
    old = [0.55, 0.52, 0.40, 0.35, 0.30, 0.28, 0.25, 0.20, 0.15, 0.10]
    new = [0.91, 0.88, 0.80, 0.74, 0.70, 0.66, 0.60, 0.55, 0.52, 0.51]

    result = review.equivalent_floor(old, new, 0.50)

    assert result["selectivity_at_old_floor"] == 0.2
    assert result["new_share_at_old_floor"] == 1.0  # the SAME 0.50 would admit every unrelated pair now
    assert result["proposed_floor"] == 0.88
    assert result["new_distribution"]["max"] == 0.91 and result["new_distribution"]["min"] == 0.51


def test_no_floor_is_invented_when_the_old_floor_admitted_nothing() -> None:
    result = review.equivalent_floor([0.1, 0.2, 0.3], [0.6, 0.7, 0.8], 0.50)

    assert result["proposed_floor"] is None and result["selectivity_at_old_floor"] == 0.0


def test_old_and_new_scores_must_be_for_the_same_pairs() -> None:
    with pytest.raises(ValueError, match="same pairs"):
        review.equivalent_floor([0.5], [0.6, 0.7], 0.50)


def test_only_pure_random_controls_are_the_unrelated_population(tmp_path) -> None:
    sheet = tmp_path / "review_sheet.csv"
    key = tmp_path / "key.csv"
    with open(sheet, "w", newline="", encoding="utf-8-sig") as h:
        w = csv.DictWriter(h, fieldnames=["pair_id", "target_description", "candidate_description"])
        w.writeheader()
        for i in range(1, 5):
            w.writerow({"pair_id": f"P{i}", "target_description": "T", "candidate_description": f"C{i}"})
    with open(key, "w", newline="", encoding="utf-8-sig") as h:
        w = csv.DictWriter(h, fieldnames=["pair_id", "arms", "text_similarity"])
        w.writeheader()
        w.writerow({"pair_id": "P1", "arms": "random", "text_similarity": "0.31"})
        w.writerow({"pair_id": "P2", "arms": "old+random", "text_similarity": "0.40"})  # an engine liked it too
        w.writerow({"pair_id": "P3", "arms": "random", "text_similarity": ""})  # no old score: left out
        w.writerow({"pair_id": "P4", "arms": "refused", "text_similarity": "0.45"})

    pairs = review.unrelated_pairs(sheet, key)

    assert pairs == [("T", "C1", D("0.31"))]


def test_the_wilson_interval_is_honest_at_zero_and_at_small_n() -> None:
    assert review.wilson_interval(0, 0) == (0.0, 1.0)
    low, high = review.wilson_interval(0, 150)
    assert low == 0.0 and high == pytest.approx(0.025, abs=0.001)  # zero seen is not zero possible
    low, high = review.wilson_interval(10, 150)
    assert (low, high) == (pytest.approx(0.0366, abs=0.002), pytest.approx(0.1184, abs=0.002))
    assert review.wilson_interval(2, 10)[1] > 0.5  # ten pairs say very little


def test_the_floor_comes_with_a_range_from_the_sampling_error_in_the_selectivity() -> None:
    """100 unrelated pairs, 10 admitted by the old floor (selectivity 10%, 95% interval 5.5% to 17.4%).
    New scores 0.99, 0.986, ... The proposed floor is the 10th highest (0.954); the interval's upper end admits
    18 pairs (floor 0.922) and its lower end 5 (floor 0.974)."""
    new = [round(0.99 - i * 0.004, 3) for i in range(100)]
    old = [0.6] * 10 + [0.3] * 90

    result = review.equivalent_floor(old, new, 0.50)

    assert result["admitted_by_old_floor"] == 10
    assert result["proposed_floor"] == pytest.approx(0.954)
    low, high = result["floor_range"]
    assert (low, high) == (pytest.approx(0.922), pytest.approx(0.974))
    assert low <= result["proposed_floor"] <= high


def test_confirmed_matches_below_the_floor_are_counted_against_the_old_floor_too() -> None:
    rows = [
        ("P1", "T1", "C1", 0.90, 0.80),
        ("P2", "T2", "C2", 0.70, 0.55),  # below a 0.80 floor on the new score, above 0.50 on the old
        ("P3", "T3", "C3", 0.60, 0.45),  # below both
        ("P4", "T4", "C4", 0.95, None),  # no old score recorded
    ]

    checked = review.confirmed_below_floor(rows, floor=0.80, old_floor=0.50)

    assert checked["count"] == 4
    assert [r[0] for r in checked["below"]] == ["P3", "P2"]  # lowest first
    assert (checked["old_count"], checked["old_below"]) == (3, 1)
    low, high = checked["below_interval"]
    assert low < 0.5 < high  # 2 of 4 tells us almost nothing


def report_for(confirmed_rows):
    result = review.equivalent_floor([0.55, 0.52, 0.20, 0.15], [0.95, 0.90, 0.60, 0.55], 0.50)
    confirmed = (review.confirmed_below_floor(confirmed_rows, result["proposed_floor"], 0.50)
                 if confirmed_rows is not None else None)
    return review.render_floors(result, 0.50, confirmed)


def test_the_floors_report_counts_expert_confirmed_matches_below_the_proposed_floor() -> None:
    text = report_for([("P1", "FILTER", "FILTER ELEMENT", 0.93, 0.62), ("P2", "BUSH", "BUSHING HALF", 0.80, 0.51),
                       ("P3", "COVER PL", "COVER PLATE", 0.97, 0.70)])

    assert "Equivalent floor: 0.900" in text
    assert "Expert-confirmed matching pairs: **3**" in text
    assert "Below the proposed floor (0.900): **1**" in text
    assert "P2  new 0.800" in text and "BUSH  ->  BUSHING HALF" in text  # the one it would refuse, listed
    assert "below the OLD floor (0.5) on the OLD score: **0** of 3" in text


def test_the_floors_report_says_the_sample_is_small_and_what_zero_below_does_not_prove() -> None:
    text = report_for([("P1", "A", "B", 0.95, 0.60), ("P2", "C", "D", 0.97, 0.70)])

    assert "How much to trust this" in text
    assert "The unrelated population is small" in text and "(4 pairs, 2 of them above the old floor)" in text
    assert "zero of 2 confirmed matches below the floor does NOT show none would be" in text
    assert "The confirmed matches are few" in text


def test_the_floors_report_gives_a_range_not_only_one_number() -> None:
    text = report_for(None)

    assert "the floor could be anywhere from" in text and "95% interval" in text


def test_text_floors_calibrated_stays_false_and_the_report_says_so() -> None:
    from app.initiatives.i7.policy.thresholds import SimilarityPolicy

    assert SimilarityPolicy().text_floors_calibrated is False
    assert "`text_floors_calibrated` stays False" in report_for([("P1", "A", "B", 0.95, 0.60)])
    assert "`text_floors_calibrated` stays False" in report_for(None)
    assert "`text_floors_calibrated` stays False" in review.render_floors(
        review.equivalent_floor([0.1], [0.7], 0.50), 0.50, None
    )


def test_without_expert_answers_the_report_says_so_and_how_to_get_them() -> None:
    text = report_for(None)

    assert "No expert answers in this file" in text and "COMPLETED review sheet" in text


def write_pair_files(tmp_path, answers: dict[str, str]):
    sheet, key = tmp_path / "sheet.csv", tmp_path / "key.csv"
    with open(sheet, "w", newline="", encoding="utf-8-sig") as h:
        w = csv.DictWriter(h, fieldnames=["pair_id", "target_description", "candidate_description", "relevant"])
        w.writeheader()
        for i in range(1, 6):
            w.writerow({"pair_id": f"P{i}", "target_description": "T", "candidate_description": f"C{i}",
                        "relevant": answers.get(f"P{i}", "")})
    with open(key, "w", newline="", encoding="utf-8-sig") as h:
        w = csv.DictWriter(h, fieldnames=["pair_id", "arms", "text_similarity"])
        w.writeheader()
        for i, arms in enumerate(["random", "random", "random", "old+new", "refused"], start=1):
            w.writerow({"pair_id": f"P{i}", "arms": arms, "text_similarity": f"0.{i}0"})
    return sheet, key


def test_a_random_pair_the_expert_confirmed_is_not_an_unrelated_pair(tmp_path) -> None:
    sheet, key = write_pair_files(tmp_path, {"P1": "yes", "P2": "no", "P3": "unsure"})

    pairs = review.unrelated_pairs(sheet, key)

    assert [c for _, c, _ in pairs] == ["C2", "C3"]  # P1 is a genuine match by chance: out; no/unsure/blank stay


def test_confirmed_matches_come_from_every_arm_with_their_old_score(tmp_path) -> None:
    sheet, key = write_pair_files(tmp_path, {"P1": "yes", "P4": "y", "P5": "no"})

    found = review.confirmed_matches(sheet, key)

    assert found == [("P1", "T", "C1", D("0.10")), ("P4", "T", "C4", D("0.40"))]
    assert review.confirmed_matches(*write_pair_files(tmp_path, {})) == []  # a blank sheet confirms nothing


def test_the_floors_command_stops_without_the_text_model(monkeypatch, capsys, tmp_path) -> None:
    from app.initiatives.i7.oar import text_similarity

    monkeypatch.setattr(text_similarity, "_load_model", lambda name: None)

    status = review.main(["floors", "--review", str(tmp_path / "a"), "--key", str(tmp_path / "b")])

    assert status == 2 and "not available" in capsys.readouterr().err
