"""The Min-Max -> OAR report: every route, the reconciliation, the ranking, the filters, the anchor."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from reverse_conversion_support import ANCHOR, FakeSource, attr, month, population, sparse

from app.initiatives.i7.policy.reverse_conversion import current_reverse_conversion_policy
from app.initiatives.i7.reverse_conversion.errors import AnchorError
from app.initiatives.i7.reverse_conversion.metrics import resolve_anchor
from app.initiatives.i7.reverse_conversion.service import (
    ListFilters,
    apply_filters,
    build_report,
    clear_report_cache,
    get_report,
    lead_time_bucket,
    paginate,
    parse_anchor_month,
    resolve_report_anchor,
    stock_value,
    value_currency,
)
from app.initiatives.i7.reverse_conversion.types import FINAL_CATEGORIES, ReverseCategory as Cat, ReverseConfidence as Conf, ReverseReason as Why

POLICY = current_reverse_conversion_policy()


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_report_cache()
    yield
    clear_report_cache()


def report_for(source=None, anchor=ANCHOR):
    source = source or FakeSource(*population())
    return build_report(source, POLICY, resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=None, configured_anchor=anchor))


def by_material(report):
    return {row.material: row for row in report.rows}


# --- Every route of the decision table ----------------------------------------------------------


@pytest.mark.parametrize(
    ("material", "category", "reason"),
    [
        ("REC1", Cat.RECOMMENDED_FOR_OAR, Why.SPARSE_RECURRING_DEMAND),
        ("REC5", Cat.RECOMMENDED_FOR_OAR, Why.SPARSE_RECURRING_DEMAND),
        ("UNKCRIT", Cat.RECOMMENDED_FOR_OAR, Why.UNKNOWN_CRITICALITY),
        ("PROT", Cat.NOT_ELIGIBLE, Why.PROTECTED_CRITICALITY),
        ("OBS", Cat.RECOMMENDED_FOR_OAR, Why.OBSOLETE_CRITICALITY),
        ("INCONS", Cat.RECOMMENDED_FOR_OAR, Why.INCONSISTENT_MOVEMENT),
        ("NEVER", Cat.RECOMMENDED_FOR_OAR, Why.NEVER_MOVED),
        ("ZERO24", Cat.RECOMMENDED_FOR_OAR, Why.ZERO_USE_24M),
        ("NOHIST", Cat.NOT_ELIGIBLE, Why.INSUFFICIENT_HISTORY),
        ("LONGLT", Cat.RECOMMENDED_FOR_OAR, Why.LEAD_TIME),
        ("NOLT", Cat.RECOMMENDED_FOR_OAR, Why.LEAD_TIME),
        ("LAPSED", Cat.RECOMMENDED_FOR_OAR, Why.LAPSED),
        ("FREQ", Cat.NOT_ELIGIBLE, Why.FREQUENT_CONSUMPTION),
        ("BORDER", Cat.RECOMMENDED_FOR_OAR, Why.BORDERLINE_FREQUENCY),
        ("STALE", Cat.RECOMMENDED_FOR_OAR, Why.STALE_LAST_MOVEMENT),
    ],
)
def test_each_material_lands_on_its_route(material, category, reason):
    row = by_material(report_for())[material]
    assert (row.category, row.reason) == (category, reason)


def test_a_deletion_flagged_material_is_excluded_not_listed():
    report = report_for()
    assert "DELETED" not in by_material(report)
    assert report.reconciliation.excluded == 1


# --- Exactly one category, and the totals add up -------------------------------------------------


def test_every_material_plant_appears_once_in_exactly_one_of_the_four_categories():
    report = report_for()
    keys = [(row.material, row.plant) for row in report.rows]
    assert len(keys) == len(set(keys))
    assert all(row.category in FINAL_CATEGORIES for row in report.rows)


def test_the_category_totals_reconcile_with_the_population_read():
    report = report_for()
    r = report.reconciliation
    assert (r.source_population, r.excluded, r.categorised, r.duplicate_keys) == (19, 1, 18, 0)
    assert r.reconciled
    assert sum(t.count for t in report.totals_by_category.values()) == r.categorised
    assert {c: t.count for c, t in report.totals_by_category.items()} == {
        Cat.RECOMMENDED_FOR_OAR: 15,
        Cat.NOT_ELIGIBLE: 3,
    }


def test_reconciliation_is_reported_per_plant():
    report = report_for()
    assert report.reconciliation.by_plant == {"1300": (18, 17), "1500": (1, 1)}
    assert sum(p for p, _ in report.reconciliation.by_plant.values()) == report.reconciliation.source_population


def test_a_material_plant_read_twice_is_reported_not_hidden():
    attributes, movements = population()
    attributes.append(attr("REC1", stock=1, price=1))  # a duplicate key
    report = report_for(FakeSource(attributes, movements))
    assert report.reconciliation.duplicate_keys == 1
    assert not report.reconciliation.reconciled


def test_count_by_plant_and_reason_add_up_to_the_categorised_rows():
    report = report_for()
    assert sum(report.count_by_plant_category.values()) == report.reconciliation.categorised
    assert sum(report.count_by_category_reason.values()) == report.reconciliation.categorised
    assert report.count_by_plant_category[("1500", Cat.RECOMMENDED_FOR_OAR)] == 1


# --- Zero stock and missing price stay in, and only rank --------------------------------------


def test_zero_stock_and_missing_price_are_kept_in_the_recommended_group():
    rows = by_material(report_for())
    assert rows["REC4"].category is Cat.RECOMMENDED_FOR_OAR and rows["REC4"].current_stock == 0
    assert rows["REC3"].category is Cat.RECOMMENDED_FOR_OAR and rows["REC3"].stock_value is None
    assert rows["REC5"].category is Cat.RECOMMENDED_FOR_OAR and rows["REC5"].current_stock is None


def test_stock_value_is_stock_times_price_or_none():
    assert stock_value(Decimal(100), Decimal(10), "ZAR") == Decimal(1000)
    assert stock_value(Decimal(0), Decimal(10), "ZAR") == Decimal(0)
    assert stock_value(Decimal(5), None, "ZAR") is None
    assert stock_value(None, Decimal(10), "ZAR") is None


def test_category_totals_state_stock_figures_without_a_currency():
    totals = report_for().totals_by_category[Cat.RECOMMENDED_FOR_OAR]
    assert totals.stock_value == Decimal(6000)  # 1000 + 5000; the priceless and the empty add nothing
    assert totals.with_stock == 3  # REC1, REC2, REC3
    assert totals.with_stock_missing_price == 1  # REC3


# --- Ranking inside a category ---------------------------------------------------------------


def test_recommended_rows_rank_by_stock_value_then_unpriced_stock_then_no_stock():
    recommended = [row.material for row in report_for().rows if row.confidence is Conf.HIGH]
    # REC2 (5000), REC1 (1000), REC3 (stock, no price), then no stock: REC4 (0) and REC5 (none) by material.
    assert recommended == ["REC2", "REC1", "REC3", "REC4", "REC5"]


def test_rows_are_ordered_by_category_first():
    categories = [row.category for row in report_for().rows]
    assert categories == sorted(categories, key=FINAL_CATEGORIES.index)


def test_stock_value_never_changes_a_category():
    attributes, movements = population()
    plain = by_material(report_for(FakeSource(attributes, movements)))
    rich = [a if a.material != "FREQ" else attr("FREQ", stock=10**6, price=10**4) for a in attributes]
    boosted = by_material(report_for(FakeSource(rich, movements)))
    assert boosted["FREQ"].category is plain["FREQ"].category is Cat.NOT_ELIGIBLE


# --- Partial month -----------------------------------------------------------------------------


def test_a_partial_last_month_is_excluded_from_every_figure():
    attributes, movements = population()
    movements[("REC1", "1300")] = movements[("REC1", "1300")] + [month(2026, 11, quantity=9, issues=9)]
    row = by_material(report_for(FakeSource(attributes, movements)))["REC1"]
    assert row.category is Cat.RECOMMENDED_FOR_OAR  # 9 November issues would have made it frequent
    assert row.metrics.issues_latest_window == 2


def test_the_same_data_re_anchored_one_month_later_counts_that_month():
    attributes, movements = population()
    movements[("REC1", "1300")] = movements[("REC1", "1300")] + [month(2026, 11, quantity=9, issues=9)]
    later = report_for(FakeSource(attributes, movements), anchor=date(2026, 11, 1))
    assert by_material(later)["REC1"].category is Cat.NOT_ELIGIBLE


# --- The inconsistency investigation (approved: latest 12 months only) -----------------------------


@pytest.mark.parametrize(
    "months",
    [
        [month(2026, 5, quantity=3, issues=1, reversals=1)],  # issue count nets to 0, quantity positive
        [month(2026, 5, quantity=0, issues=2, reversals=0)],  # issues counted, quantity nets to nothing
    ],
)
def test_the_two_kinds_of_latest_window_disagreement_stay_in_convert_to_oar(months):
    report = report_for(FakeSource([attr("X")], {("X", "1300"): sparse(*months)}))
    row = by_material(report)["X"]
    assert (row.category, row.reason) == (Cat.RECOMMENDED_FOR_OAR, Why.INCONSISTENT_MOVEMENT)


def test_a_disagreement_only_in_the_prior_window_does_not_trigger_the_rule():
    # Prior window: a valid month but the issue count nets to zero. Latest window is clean and sparse.
    prior = month(2025, 4, quantity=3, issues=1, reversals=1)
    row = by_material(report_for(FakeSource([attr("X")], {("X", "1300"): sparse(prior, month(2026, 5))})))["X"]
    assert (row.category, row.reason) == (Cat.RECOMMENDED_FOR_OAR, Why.SPARSE_RECURRING_DEMAND)


def test_inconsistent_data_is_never_silently_zero_use_lapsed_or_active():
    row = by_material(report_for())["INCONS"]
    assert row.reason not in {Why.ZERO_USE_24M, Why.NEVER_MOVED, Why.LAPSED, Why.SPARSE_RECURRING_DEMAND}


def test_unknown_criticality_outranks_inconsistent_movement():
    row = by_material(report_for(FakeSource([attr("X", criticality=None)], {("X", "1300"): sparse(month(2026, 5, quantity=3, issues=1, reversals=1))})))["X"]
    assert row.reason is Why.UNKNOWN_CRITICALITY


# --- Lead time buckets -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "bucket"),
    [(None, "unknown"), (0, "unknown"), (-3, "unknown"), (1, "0-30d"), (30, "0-30d"), (31, "31-60d"), (60, "31-60d"), (61, "61-90d"), (90, "61-90d"), (91, ">90d"), (Decimal("200"), ">90d")],
)
def test_lead_time_buckets(value, bucket):
    assert lead_time_bucket(value) == bucket


# --- Filters and paging -----------------------------------------------------------------------


def test_filters_narrow_by_category_plant_reason_criticality_bucket_and_search():
    rows = report_for().rows
    assert {r.material for r in apply_filters(rows, ListFilters(category=Cat.NOT_ELIGIBLE))} == {"PROT", "NOHIST", "FREQ"}
    assert {r.material for r in apply_filters(rows, ListFilters(plant="1500"))} == {"REC5"}
    assert {r.material for r in apply_filters(rows, ListFilters(reason=Why.LEAD_TIME))} == {"LONGLT", "NOLT"}
    assert {r.material for r in apply_filters(rows, ListFilters(criticality="unknown"))} == {"UNKCRIT"}
    assert {r.material for r in apply_filters(rows, ListFilters(criticality="critical"))} == {"PROT"}
    assert {r.material for r in apply_filters(rows, ListFilters(lead_time_bucket=">90d"))} == {"LONGLT"}
    assert {r.material for r in apply_filters(rows, ListFilters(lead_time_bucket="unknown"))} == {"NOLT"}
    assert {r.material for r in apply_filters(rows, ListFilters(search="rec"))} == {"REC1", "REC2", "REC3", "REC4", "REC5"}


def test_filters_combine_and_keep_the_ranked_order():
    rows = report_for().rows
    chosen = apply_filters(rows, ListFilters(category=Cat.RECOMMENDED_FOR_OAR, confidence=Conf.HIGH, plant="1300"))
    assert [r.material for r in chosen] == ["REC2", "REC1", "REC3", "REC4"]


def test_search_matches_the_description_too():
    source = FakeSource([attr("A1", description="SULPHUR PRILLS"), attr("A2", description="HOSE")], {("A1", "1300"): sparse(month(2026, 5)), ("A2", "1300"): sparse(month(2026, 5))})
    rows = report_for(source).rows
    assert [r.material for r in apply_filters(rows, ListFilters(search="sulphur"))] == ["A1"]


def test_no_filter_returns_everything():
    rows = report_for().rows
    assert len(apply_filters(rows, ListFilters())) == len(rows)


def test_paging_slices_the_filtered_list():
    rows = list(report_for().rows)
    assert [r.material for r in paginate(rows, 1, 5)] == [r.material for r in rows[:5]]
    assert [r.material for r in paginate(rows, 2, 5)] == [r.material for r in rows[5:10]]
    assert paginate(rows, 99, 5) == []


# --- The anchor -------------------------------------------------------------------------------


def test_parse_anchor_month():
    assert parse_anchor_month("2026-10") == date(2026, 10, 1)
    assert parse_anchor_month(" 2026-01 ") == date(2026, 1, 1)
    assert parse_anchor_month(None) is None and parse_anchor_month("") is None and parse_anchor_month("  ") is None


@pytest.mark.parametrize("text", ["2026-13", "2026-00", "26-10", "2026-1", "October", "2026-10-01", "2026/10"])
def test_parse_anchor_month_refuses_anything_but_yyyy_mm(text):
    with pytest.raises(AnchorError):
        parse_anchor_month(text)


def test_with_the_default_source_and_no_pinned_anchor_it_refuses_to_guess():
    source = FakeSource(*population(), data_loaded_at=datetime(2026, 11, 23, tzinfo=timezone.utc))
    with pytest.raises(AnchorError, match="Configure the anchor explicitly"):
        resolve_report_anchor(source, anchor_source="configured")
    assert source.calls["data_loaded_at"] == 0  # the load time is not even consulted


def test_a_pinned_anchor_is_used_and_a_request_override_beats_the_setting():
    source = FakeSource(*population(), data_loaded_at=datetime(2026, 12, 5, tzinfo=timezone.utc))
    assert resolve_report_anchor(source, configured_month="2026-09", anchor_source="csv_extract").month == date(2026, 9, 1)
    assert resolve_report_anchor(source, override_month="2026-10", configured_month="2026-09", anchor_source="csv_extract").month == date(2026, 10, 1)


def test_the_csv_extract_source_derives_the_latest_complete_month_from_the_load_time():
    loaded = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    source = FakeSource(*population(), data_loaded_at=loaded)
    resolution = resolve_report_anchor(source, anchor_source="csv_extract")
    assert resolution.month == date(2026, 10, 1) and resolution.source == "data_load_timestamp"
    assert source.calls["data_loaded_at"] == 1


def test_the_csv_extract_source_without_a_load_time_still_refuses_to_guess():
    with pytest.raises(AnchorError):
        resolve_report_anchor(FakeSource(*population(), data_loaded_at=None), anchor_source="csv_extract")


def test_an_override_for_an_incomplete_month_is_refused_when_the_load_time_is_known():
    source = FakeSource(*population(), data_loaded_at=datetime(2026, 11, 23, tzinfo=timezone.utc))
    with pytest.raises(AnchorError, match="not a complete month"):
        resolve_report_anchor(source, override_month="2026-11", anchor_source="csv_extract")


# --- The cache ---------------------------------------------------------------------------------


def test_a_second_request_for_the_same_anchor_reuses_the_report():
    source = FakeSource(*population())
    first = get_report(source, POLICY, configured_month="2026-10")
    second = get_report(source, POLICY, configured_month="2026-10")
    assert first is second
    assert source.calls["consumption"] == 1


def test_a_different_anchor_is_a_different_report():
    source = FakeSource(*population())
    one = get_report(source, POLICY, configured_month="2026-10")
    other = get_report(source, POLICY, configured_month="2026-09")
    assert one is not other
    assert source.calls["consumption"] == 2


def test_clearing_the_cache_forces_a_fresh_calculation():
    source = FakeSource(*population())
    get_report(source, POLICY, configured_month="2026-10")
    clear_report_cache()
    get_report(source, POLICY, configured_month="2026-10")
    assert source.calls["consumption"] == 2


# --- Confidence --------------------------------------------------------------------------------------


def test_every_convert_to_oar_row_has_a_confidence_and_no_other_row_has_one():
    for row in report_for().rows:
        assert (row.confidence is not None) == (row.category is Cat.RECOMMENDED_FOR_OAR), row.material


def test_confidence_levels_are_counted_and_add_up_to_convert_to_oar():
    report = report_for()
    assert set(report.count_by_confidence) == set(Conf)
    assert sum(report.count_by_confidence.values()) == report.totals_by_category[Cat.RECOMMENDED_FOR_OAR].count


def test_convert_to_oar_is_listed_high_then_medium_then_low():
    order = [row.confidence for row in report_for().rows if row.category is Cat.RECOMMENDED_FOR_OAR]
    assert order == sorted(order, key=list(Conf).index)


# --- Money is counted in ZAR by the rule the OAR and Stock item figures use ----------------------------


def test_a_zar_price_is_counted_as_it_is_and_a_nad_price_at_one_to_one():
    assert stock_value(Decimal(10), Decimal("5.50"), "ZAR") == Decimal("55.00")
    assert stock_value(Decimal(10), Decimal("5.50"), "NAD") == Decimal("55.00")  # NAD is pegged to the rand
    assert value_currency(Decimal("5.50"), "ZAR") == "ZAR"
    assert value_currency(Decimal("5.50"), "NAD") == "ZAR"  # stated in rand terms


@pytest.mark.parametrize("currency", ["USD", "EUR", "", None])
def test_any_other_currency_or_none_is_left_out_never_converted(currency):
    assert stock_value(Decimal(10), Decimal("5.50"), currency) is None
    assert value_currency(Decimal("5.50"), currency) is None


@pytest.mark.parametrize("price", [Decimal("1.00"), Decimal("0.50"), Decimal(0)])
def test_a_placeholder_price_is_not_a_price(price):
    # At or below the placeholder ceiling (1.00), as the OAR and Stock item figures read it.
    assert stock_value(Decimal(10), price, "ZAR") is None


def test_a_row_priced_in_usd_is_left_out_of_the_report_and_counted_as_excluded():
    source = FakeSource(
        [attr("USDROW", stock=10, price=5, currency="USD"), attr("NADROW", stock=10, price=5, currency="NAD"), attr("ZARROW", stock=10, price=5)],
        {("USDROW", "1300"): sparse(month(2026, 5)), ("NADROW", "1300"): sparse(month(2026, 5)), ("ZARROW", "1300"): sparse(month(2026, 5))},
    )
    report = report_for(source)
    rows = by_material(report)
    assert "USDROW" not in rows  # a price in USD is not considered
    assert report.reconciliation.excluded == 1 and report.reconciliation.reconciled
    assert (rows["NADROW"].stock_value, rows["NADROW"].value_currency, rows["NADROW"].price_currency) == (Decimal(50), "ZAR", "NAD")
    assert (rows["ZARROW"].stock_value, rows["ZARROW"].value_currency) == (Decimal(50), "ZAR")
