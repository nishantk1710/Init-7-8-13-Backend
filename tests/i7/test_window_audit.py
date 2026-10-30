"""The window audit's comparison logic (``scripts/i7_window_audit.py``). Pure rows in, numbers out."""

from __future__ import annotations

from decimal import Decimal

from scripts.i7_window_audit import diff_inventory, summarise_eligibility


def feature(*, oar="OUT_OF_SCOPE", gate="SUFFICIENT", window_gate=None, cls="INTERMITTENT", baseline="SBA",
            mean="10", sd="4", price="100"):
    return {
        "oar_scope": oar, "history_status": gate, "window_history_status": window_gate,
        "demand_class": cls, "baseline_model": baseline,
        "mean_demand_all_periods": Decimal(mean), "std_dev_demand_all_periods": Decimal(sd),
        "unit_price": Decimal(price) if price is not None else None,
    }


def test_oar_rows_must_come_out_identical_and_a_change_is_counted():
    same = feature(oar="IN_SCOPE")
    before = {("A", "1"): same, ("B", "1"): feature(oar="IN_SCOPE")}
    after = {("A", "1"): dict(same), ("B", "1"): feature(oar="IN_SCOPE", mean="99")}

    summary = summarise_eligibility(before, after)

    assert summary["oar_material_plants"] == 2
    assert summary["oar_rows_changed"] == 1


def test_a_stock_item_that_loses_its_window_evidence_is_a_cold_start_transition():
    before = {("A", "1"): feature(), ("B", "1"): feature()}
    after = {
        ("A", "1"): feature(window_gate="COLD_START", cls="UNCLASSIFIED", baseline=None),
        ("B", "1"): feature(window_gate="SUFFICIENT", cls="LUMPY", baseline="TSB"),
    }

    summary = summarise_eligibility(before, after)

    assert summary["gate_transitions"] == {"SUFFICIENT -> COLD_START": 1}
    assert summary["gate_before"] == {"SUFFICIENT": 2}
    assert summary["gate_after"] == {"COLD_START": 1, "SUFFICIENT": 1}
    assert summary["class_changes"] == {"INTERMITTENT -> LUMPY": 1}
    assert summary["forecastable_before"] == 2 and summary["forecastable_after"] == 1


def test_a_missing_data_window_is_reported_as_missing_data_not_as_a_class_change():
    before = {("A", "1"): feature()}
    after = {("A", "1"): feature(window_gate="MISSING_DATA", cls="UNCLASSIFIED", baseline=None)}

    summary = summarise_eligibility(before, after)

    assert summary["gate_transitions"] == {"SUFFICIENT -> MISSING_DATA": 1}
    assert summary["class_changes"] == {}
    assert summary["forecastable_after"] == 0


def test_the_whole_history_gate_that_oar_reads_is_reported_unchanged():
    before = {("A", "1"): feature(), ("B", "1"): feature(gate="COLD_START")}
    after = {
        ("A", "1"): feature(window_gate="COLD_START", cls="UNCLASSIFIED", baseline=None),
        ("B", "1"): feature(gate="COLD_START", window_gate="COLD_START"),
    }

    summary = summarise_eligibility(before, after)

    assert summary["oar_donors_before"] == summary["oar_donors_after"] == 1


def test_the_drivers_are_compared_only_where_both_sides_are_classified_and_priced():
    before = {
        ("A", "1"): feature(mean="10", sd="4", price="100"),
        ("B", "1"): feature(mean="50", sd="9", price=None),  # no price: left out
        ("C", "1"): feature(mean="7", sd="2", price="100"),  # not classified after: left out
    }
    after = {
        ("A", "1"): feature(window_gate="SUFFICIENT", mean="15", sd="3", price="100"),
        ("B", "1"): feature(window_gate="SUFFICIENT", mean="5", sd="1", price=None),
        ("C", "1"): feature(window_gate="COLD_START", cls="UNCLASSIFIED", baseline=None, mean="1", sd="1"),
    }

    summary = summarise_eligibility(before, after)

    assert summary["driver_compared"] == 1
    assert summary["driver_demand_value"] == (Decimal(1000), Decimal(1500))
    assert summary["driver_spread_value"] == (Decimal(400), Decimal(300))


def inventory(ss, rop, mx, price="10", status="SUCCESS"):
    return {
        "safety_stock": ss, "rop": rop, "max_stock": mx, "price": Decimal(price),
        "safety_stock_status": status, "rop_status": status, "max_stock_status": status,
    }


def test_working_capital_is_quantity_times_price_over_material_plants_evaluable_in_both_runs():
    before = {("A", "1"): inventory(10, 20, 40), ("B", "1"): inventory(5, 8, 12), ("C", "1"): inventory(3, 6, 9)}
    after = {
        ("A", "1"): inventory(6, 20, 50),
        ("B", "1"): inventory(None, None, None, status="NOT_EVALUABLE_NO_HISTORY"),
        ("C", "1"): inventory(3, 6, 9),
        ("D", "1"): inventory(4, 7, 11),
    }

    diff = diff_inventory(before, after)

    ss = diff["safety_stock"]
    assert (ss["evaluable_before"], ss["evaluable_after"]) == (3, 3)
    assert (ss["newly_evaluable"], ss["no_longer_evaluable"]) == (1, 1)
    assert ss["compared"] == 2 and (ss["increased"], ss["decreased"], ss["unchanged"]) == (0, 1, 1)
    assert ss["value_before"] == Decimal(130) and ss["value_after"] == Decimal(90)
    assert diff["max_stock"]["increased"] == 1
