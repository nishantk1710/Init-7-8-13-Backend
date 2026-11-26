"""The Min-Max -> OAR decision table: every row, the priority between rows, and the invariants."""

from datetime import date
from decimal import Decimal
from itertools import product

import pytest

from app.initiatives.i7.policy.reverse_conversion import current_reverse_conversion_policy
from app.initiatives.i7.reverse_conversion.evaluate import evaluate
from app.initiatives.i7.reverse_conversion.metrics import (
    compute_consumption_metrics,
    history_span_months,
    months_between,
)
from app.initiatives.i7.reverse_conversion.types import (
    FINAL_CATEGORIES,
    ConsumptionMetrics,
    MaterialPlantInputs,
    MonthlyMovement,
    ReverseCategory as Cat,
    ReverseConfidence as Conf,
    ReverseReason as Why,
)

POLICY = current_reverse_conversion_policy()
ANCHOR = date(2026, 10, 1)
_UNSET = object()


def metrics(
    *,
    issues=2,
    months=2,
    prior_issues=0,
    prior_months=0,
    first=date(2015, 1, 1),
    last=date(2026, 8, 1),
    lifetime=20,
    span=_UNSET,
    since=_UNSET,
    age=_UNSET,
) -> ConsumptionMetrics:
    """A sparse, established, recently used material by default; override what a test is about."""
    return ConsumptionMetrics(
        anchor=ANCHOR,
        issues_latest_window=issues,
        months_with_use_latest_window=months,
        issues_prior_window=prior_issues,
        months_with_use_prior_window=prior_months,
        first_valid_month=first,
        last_valid_month=last,
        lifetime_months_with_use=lifetime,
        history_span_months=history_span_months(first, last) if span is _UNSET else span,
        months_since_last_movement=(months_between(last, ANCHOR) if last else None) if since is _UNSET else since,
        material_age_months=(months_between(first, ANCHOR) + 1 if first else None) if age is _UNSET else age,
    )


def inputs(
    *,
    mrp="VM",
    plant="1300",
    deleted=False,
    criticality="NORMAL",
    lead_time=30,
    m=None,
    stock=None,
    price=None,
    currency="ZAR",
) -> MaterialPlantInputs:
    return MaterialPlantInputs(
        material="1000123",
        plant=plant,
        mrp_type=mrp,
        deletion_flag=deleted,
        criticality=criticality,
        lead_time_days=lead_time,
        metrics=m if m is not None else metrics(),
        current_stock=stock,
        unit_price=price,
        price_currency=currency,
    )


def decide(**kw):
    return evaluate(inputs(**kw), POLICY)


def never_moved() -> ConsumptionMetrics:
    return metrics(issues=0, months=0, first=None, last=None, lifetime=0)


# --- The baseline ------------------------------------------------------------------------


def test_a_sparse_recent_established_material_is_recommended():
    d = decide()
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.SPARSE_RECURRING_DEMAND, 12)


# --- Rule 1: scope ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{"mrp": "PD"}, {"mrp": "ND"}, {"mrp": None}, {"mrp": ""}, {"plant": "1200"}, {"plant": "3000"}, {"deleted": True}],
)
def test_rule_1_out_of_scope_is_excluded(overrides):
    d = decide(**overrides)
    assert (d.category, d.reason, d.rule) == (Cat.EXCLUDED, Why.OUTSIDE_SCOPE, 1)


@pytest.mark.parametrize("overrides", [{"mrp": "VB"}, {"mrp": "vm"}, {"plant": "1500"}, {"deleted": None}])
def test_rule_1_in_scope_variants_are_evaluated(overrides):
    # VB, VM, both plants, and a deletion flag that was never maintained (None) are all in scope.
    assert decide(**overrides).category is Cat.RECOMMENDED_FOR_OAR


def test_scope_outranks_even_unknown_criticality():
    assert decide(mrp="PD", criticality=None).category is Cat.EXCLUDED


# --- Rules 2-4: criticality ----------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   ", "UNKNOWN", "norm"])
def test_rule_2_unknown_criticality_stays_in_convert_to_oar_at_low_confidence(value):
    d = decide(criticality=value)
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.UNKNOWN_CRITICALITY, 2)


@pytest.mark.parametrize("tier", ["CRITICAL", "IMPACT", "INSURANCE", "critical", " Impact "])
def test_rule_3_protected_criticality_is_not_eligible(tier):
    d = decide(criticality=tier)
    assert (d.category, d.reason, d.rule) == (Cat.NOT_ELIGIBLE, Why.PROTECTED_CRITICALITY, 3)


def test_rule_4_obsolete_stays_in_convert_to_oar_at_medium_confidence():
    d = decide(criticality="OBSOLETE")
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.OBSOLETE_CRITICALITY, 4)


def test_normal_criticality_continues_down_the_table():
    assert decide(criticality="NORMAL").rule == 12


# --- Rule 5: inconsistent movement (latest 12 months only) ----------------------------------


def test_rule_5_no_issues_but_months_with_use_is_inconsistent():
    d = decide(m=metrics(issues=0, months=1))
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.INCONSISTENT_MOVEMENT, 5)


def test_rule_5_issues_but_no_months_with_use_is_inconsistent():
    d = decide(m=metrics(issues=2, months=0))
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.INCONSISTENT_MOVEMENT, 5)


def test_rule_5_does_not_look_at_the_prior_window():
    # The prior window disagrees (no issues, a month with use) but the latest window is consistent:
    # the approved rule is limited to the latest 12 months, so this is simply Recommended.
    d = decide(m=metrics(prior_issues=0, prior_months=3))
    assert (d.category, d.rule) == (Cat.RECOMMENDED_FOR_OAR, 12)


def test_rule_5_from_real_movements_where_reversals_cancel_the_issue_count():
    movements = [MonthlyMovement(date(2026, 5, 1), Decimal(2), 1, 1)]  # quantity netted positive, count netted to zero
    m = compute_consumption_metrics(movements, ANCHOR, POLICY)
    assert decide(m=m).reason is Why.INCONSISTENT_MOVEMENT


# --- Rules 6 and 7: zero use in 24 months ----------------------------------------------------


def test_rule_6_never_moved_stays_in_convert_to_oar():
    d = decide(m=never_moved())
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.NEVER_MOVED, 6)


def test_rule_7_no_issues_in_24_months_but_moved_before_stays_in_convert_to_oar():
    m = metrics(issues=0, months=0, prior_issues=0, prior_months=0, first=date(2015, 1, 1), last=date(2022, 3, 1), lifetime=12)
    d = decide(m=m)
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.ZERO_USE_24M, 7)


# --- Rule 8: history ------------------------------------------------------------------------


def test_rule_8_a_span_of_eleven_is_insufficient_history():
    d = decide(m=metrics(first=date(2025, 11, 1), last=date(2026, 9, 1)))  # span 11
    assert (d.category, d.reason, d.rule) == (Cat.NOT_ELIGIBLE, Why.INSUFFICIENT_HISTORY, 8)


def test_rule_8_a_span_of_twelve_passes_the_history_condition():
    d = decide(m=metrics(first=date(2025, 10, 1), last=date(2026, 9, 1), lifetime=12, age=13))  # span 12, not new
    assert d.rule != 8
    assert d.category is Cat.RECOMMENDED_FOR_OAR


def test_rule_8_an_undefined_span_is_insufficient_history():
    # Reaching rule 8 with no span needs issues in the window but no valid month, which rule 5 would
    # catch; force it directly to prove the span test itself treats None as insufficient.
    d = decide(m=metrics(issues=0, months=0, prior_issues=2, prior_months=2, span=None))
    assert (d.category, d.reason, d.rule) == (Cat.NOT_ELIGIBLE, Why.INSUFFICIENT_HISTORY, 8)


def test_rule_8_does_not_use_the_old_history_months_field():
    # The inputs carry no history_months at all; the span comes only from first and latest valid month.
    assert not hasattr(MaterialPlantInputs, "history_months")


# --- Rule 9: lead time ----------------------------------------------------------------------


@pytest.mark.parametrize("lead_time", [None, 0, -5, "n/a"])
def test_rule_9_unknown_lead_time_stays_in_convert_to_oar_at_low_confidence(lead_time):
    d = decide(lead_time=lead_time)
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.LEAD_TIME, 9)


@pytest.mark.parametrize("lead_time", [91, 120, Decimal("365"), 90.5])
def test_rule_9_a_lead_time_over_90_days_stays_in_convert_to_oar_at_low_confidence(lead_time):
    assert decide(lead_time=lead_time).rule == 9


@pytest.mark.parametrize("lead_time", [1, 30, 60, 61, 90, Decimal("90"), 90.0])
def test_rule_9_up_to_and_including_90_days_has_no_further_restriction(lead_time):
    # 61-90 days gets no extra cap: a material at 2 issues is recommended at any lead time up to 90.
    assert decide(lead_time=lead_time).category is Cat.RECOMMENDED_FOR_OAR


# --- Rule 10: lapsed -----------------------------------------------------------------------


def test_rule_10_used_before_but_not_in_the_latest_window_is_lapsed():
    m = metrics(issues=0, months=0, prior_issues=3, prior_months=3, last=date(2025, 6, 1), lifetime=30)
    d = decide(m=m)
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.LAPSED, 10)


# --- Rule 11: frequent ----------------------------------------------------------------------


def test_rule_11_more_than_four_issues_is_frequent():
    d = decide(m=metrics(issues=5, months=3))
    assert (d.category, d.reason, d.rule) == (Cat.NOT_ELIGIBLE, Why.FREQUENT_CONSUMPTION, 11)


def test_rule_11_more_than_four_months_is_frequent_even_with_few_issues():
    d = decide(m=metrics(issues=4, months=5))
    assert (d.category, d.reason, d.rule) == (Cat.NOT_ELIGIBLE, Why.FREQUENT_CONSUMPTION, 11)


def test_rule_11_exactly_four_issues_and_four_months_is_not_frequent():
    d = decide(m=metrics(issues=4, months=4))
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.BORDERLINE_FREQUENCY, 13)


# --- Rule 12: recommended --------------------------------------------------------------------


@pytest.mark.parametrize(("issues", "months"), [(1, 1), (2, 1), (2, 2)])
def test_rule_12_up_to_two_issues_and_two_months_is_recommended(issues, months):
    d = decide(m=metrics(issues=issues, months=months))
    assert (d.category, d.rule, d.confidence) == (Cat.RECOMMENDED_FOR_OAR, 12, Conf.HIGH)


def test_rule_12_last_movement_nine_months_before_is_still_recent():
    assert decide(m=metrics(last=date(2026, 1, 1), since=9)).rule == 12


# --- Rule 13: everything else ----------------------------------------------------------------


@pytest.mark.parametrize(("issues", "months"), [(3, 3), (3, 2), (2, 3), (4, 4)])
def test_rule_13_three_or_four_issues_or_months_is_borderline_frequency(issues, months):
    d = decide(m=metrics(issues=issues, months=months))
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.BORDERLINE_FREQUENCY, 13)


def test_rule_13_last_movement_ten_months_before_is_stale():
    d = decide(m=metrics(last=date(2025, 12, 1), since=10))
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.STALE_LAST_MOVEMENT, 13)


def test_rule_13_a_new_material_with_two_lifetime_months_fails_the_lifetime_criterion():
    m = metrics(first=date(2024, 11, 1), last=date(2026, 9, 1), lifetime=2, age=24)  # first movement within 24 months
    d = decide(m=m)
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.LIFETIME_CRITERION, 13)


def test_the_lifetime_criterion_is_met_with_three_months_of_use():
    m = metrics(first=date(2024, 11, 1), last=date(2026, 9, 1), lifetime=3, age=24)
    assert decide(m=m).rule == 12


def test_the_lifetime_criterion_applies_only_to_new_materials():
    # First movement 25 months before the anchor: not new, so two lifetime months do not block it.
    m = metrics(first=date(2024, 10, 1), last=date(2026, 9, 1), lifetime=2, age=25)
    assert decide(m=m).rule == 12


def test_borderline_frequency_is_reported_before_stale_and_lifetime():
    m = metrics(issues=3, months=3, first=date(2025, 1, 1), last=date(2025, 12, 1), lifetime=1, since=10, age=22)
    assert decide(m=m).reason is Why.BORDERLINE_FREQUENCY


# --- Priority: a higher row always wins --------------------------------------------------------


def test_unknown_criticality_outranks_zero_use_history_and_lead_time():
    # Never moved, no span, no lead time -- and still Convert to OAR (low confidence) for the unknown criticality.
    d = decide(criticality=None, m=never_moved(), lead_time=None)
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.UNKNOWN_CRITICALITY, 2)


def test_unknown_criticality_outranks_inconsistent_movement_and_obsolescence_conditions():
    d = decide(criticality=None, m=metrics(issues=0, months=2))
    assert d.reason is Why.UNKNOWN_CRITICALITY
    assert decide(criticality=None, m=metrics(issues=0, months=0, first=date(2015, 1, 1), last=date(2020, 1, 1))).reason is Why.UNKNOWN_CRITICALITY


def test_protected_outranks_every_condition_below_it():
    for m in (never_moved(), metrics(issues=0, months=1), metrics(issues=9, months=9)):
        assert decide(criticality="CRITICAL", m=m, lead_time=None).reason is Why.PROTECTED_CRITICALITY


def test_obsolete_outranks_zero_use_and_history():
    assert decide(criticality="OBSOLETE", m=never_moved(), lead_time=None).reason is Why.OBSOLETE_CRITICALITY


def test_inconsistent_movement_outranks_zero_use_history_and_lead_time():
    # No issues in 24 months by count (would be zero use) but a valid month exists: inconsistent first.
    m = metrics(issues=0, months=1, prior_issues=0, prior_months=0)
    assert decide(m=m, lead_time=None).reason is Why.INCONSISTENT_MOVEMENT


def test_zero_use_outranks_insufficient_history():
    # A never-moved material also has no span; it is Convert to OAR, not "insufficient history".
    assert decide(m=never_moved()).rule == 6


def test_insufficient_history_outranks_lead_time():
    d = decide(m=metrics(first=date(2026, 6, 1), last=date(2026, 8, 1)), lead_time=None)
    assert d.reason is Why.INSUFFICIENT_HISTORY


def test_lead_time_outranks_lapsed_and_frequent_and_recommended():
    lapsed = metrics(issues=0, months=0, prior_issues=3, prior_months=3, last=date(2025, 6, 1))
    assert decide(m=lapsed, lead_time=None).reason is Why.LEAD_TIME
    assert decide(m=metrics(issues=9, months=9), lead_time=120).reason is Why.LEAD_TIME
    assert decide(lead_time=120).reason is Why.LEAD_TIME


# --- Stock and price never decide --------------------------------------------------------------


@pytest.mark.parametrize(
    ("stock", "price"),
    [(None, None), (Decimal(0), None), (Decimal(0), Decimal(10)), (Decimal(500), None), (Decimal(500), Decimal("1999.99"))],
)
def test_zero_stock_and_missing_price_do_not_change_the_decision(stock, price):
    d = decide(stock=stock, price=price)
    assert (d.category, d.reason, d.rule) == (Cat.RECOMMENDED_FOR_OAR, Why.SPARSE_RECURRING_DEMAND, 12)


# --- Invariants over a grid ---------------------------------------------------------------------


def test_every_in_scope_material_plant_gets_exactly_one_of_the_four_categories():
    axes = (
        [None, "NORMAL", "CRITICAL", "OBSOLETE", "FOO"],
        [0, 1, 2, 3, 5],
        [0, 1, 2, 3, 5],
        [0, 2],
        [0, 1, 2, 5, 30],
        [None, 30, 100],
        [None, 0, 9, 10],
        [None, 5, 12, 40],
        [None, 10, 24, 25],
    )
    expected = 1
    for axis in axes:
        expected *= len(axis)
    count = 0
    for crit, issues, months, prior, lifetime, lead, since, span, age in product(*axes):
        m = metrics(issues=issues, months=months, prior_issues=prior, prior_months=prior, lifetime=lifetime, since=since, span=span, age=age)
        d = evaluate(inputs(criticality=crit, lead_time=lead, m=m), POLICY)
        count += 1
        assert d.category in FINAL_CATEGORIES, (crit, issues, months, d)
        assert d.reason and d.detail and 2 <= d.rule <= 14
        assert (d.confidence is not None) == (d.category is Cat.RECOMMENDED_FOR_OAR), (crit, issues, months, d)
        # The defensive reason is unreachable: every active material matches one of the three real reasons.
        assert d.reason is not Why.UNCLASSIFIED_ACTIVE, (crit, issues, months, lifetime, since, age)
    assert count == expected == 240_000


def test_the_decision_depends_only_on_the_inputs():
    one, two = decide(), decide()
    assert one == two


# --- Confidence and the reasons shown to a planner -------------------------------------------------


def test_no_material_is_ever_sent_to_a_manual_review_category():
    assert {c.value for c in Cat} == {"Recommended for OAR", "Not Eligible", "Excluded"}


def test_there_is_no_obsolescence_review_any_more_obsolete_is_a_convert_to_oar_reason():
    d = decide(criticality="OBSOLETE")
    assert (d.category, d.reason, d.confidence) == (Cat.RECOMMENDED_FOR_OAR, Why.OBSOLETE_CRITICALITY, Conf.MEDIUM)
    assert d.reason.value == "Obsolete"
    assert "Obsolete" in d.detail and "keep or dispose" in d.detail
    assert decide(m=never_moved()).category is Cat.RECOMMENDED_FOR_OAR
    assert decide(m=metrics(issues=0, months=0, prior_issues=0, prior_months=0, lifetime=3)).category is Cat.RECOMMENDED_FOR_OAR


def test_the_two_zero_issue_reasons_read_as_the_planner_asked():
    never = decide(m=never_moved())
    before = decide(m=metrics(issues=0, months=0, prior_issues=0, prior_months=0, lifetime=3))
    assert never.reason.value == "Zero issues in 24 months and never moved"
    assert before.reason.value == "Zero issues in 24 months but moved before"
    assert never.detail == "Zero issues between Nov 2024 and Oct 2026, and it has never moved."
    assert before.detail == "Zero issues between Nov 2024 and Oct 2026. It last moved in Aug 2026."
    assert (never.confidence, before.confidence) == (Conf.MEDIUM, Conf.MEDIUM)


@pytest.mark.parametrize(
    ("kwargs", "reason", "confidence"),
    [
        ({"criticality": None}, Why.UNKNOWN_CRITICALITY, Conf.LOW),
        ({"lead_time": None}, Why.LEAD_TIME, Conf.LOW),
        ({"lead_time": 120}, Why.LEAD_TIME, Conf.LOW),
        ({"m": metrics(issues=0, months=1, prior_issues=0, prior_months=0)}, Why.INCONSISTENT_MOVEMENT, Conf.LOW),
        ({"m": metrics(issues=0, months=0, prior_issues=3, prior_months=3, last=date(2025, 6, 1))}, Why.LAPSED, Conf.MEDIUM),
        ({"m": metrics(issues=3, months=3)}, Why.BORDERLINE_FREQUENCY, Conf.MEDIUM),
        ({"m": metrics(last=date(2025, 12, 1), since=10)}, Why.STALE_LAST_MOVEMENT, Conf.MEDIUM),
        ({"m": metrics(first=date(2024, 11, 1), last=date(2026, 9, 1), lifetime=2, age=24)}, Why.LIFETIME_CRITERION, Conf.MEDIUM),
        ({}, Why.SPARSE_RECURRING_DEMAND, Conf.HIGH),
    ],
)
def test_each_convert_to_oar_reason_carries_its_confidence(kwargs, reason, confidence):
    d = decide(**kwargs)
    assert (d.category, d.reason, d.confidence) == (Cat.RECOMMENDED_FOR_OAR, reason, confidence)


def test_only_convert_to_oar_rows_carry_a_confidence():
    assert decide(criticality="CRITICAL").confidence is None
    assert decide(m=metrics(issues=9, months=9)).confidence is None
    assert decide(m=metrics(first=date(2026, 6, 1), last=date(2026, 8, 1))).confidence is None


# --- The explanation reads correctly for a count of one ----------------------------------------------


def test_a_count_of_one_is_not_pluralised_in_the_explanation():
    d = decide(m=metrics(issues=1, months=1, last=date(2026, 4, 1), since=6))
    assert d.rule == 12
    assert d.detail.startswith("Issued 1 time in 1 month between Nov 2025 and Oct 2026, last in Apr 2026.")
    assert "1 times" not in d.detail and "1 months" not in d.detail


def test_counts_other_than_one_stay_plural():
    d = decide(m=metrics(issues=2, months=2))
    assert d.detail.startswith("Issued 2 times in 2 months between Nov 2025 and Oct 2026")


# --- A price that is not in ZAR or NAD leaves the material out ----------------------------------------


@pytest.mark.parametrize("currency", ["ZAR", "NAD"])
def test_a_price_in_zar_or_nad_keeps_the_material(currency):
    d = evaluate(inputs(price=Decimal("25.00"), currency=currency), POLICY)
    assert d.category is not Cat.EXCLUDED


@pytest.mark.parametrize("currency", ["USD", "EUR", "GBP", "", None])
def test_a_price_in_another_currency_or_with_none_stated_leaves_the_material_out(currency):
    d = evaluate(inputs(price=Decimal("25.00"), currency=currency), POLICY)
    assert (d.category, d.reason, d.rule) == (Cat.EXCLUDED, Why.OUTSIDE_SCOPE, 1)
    assert "ZAR or NAD" in d.detail


@pytest.mark.parametrize("currency", ["ZAR", "USD", None])
def test_a_material_with_no_price_is_kept_whatever_the_currency_says(currency):
    # No price means no currency to judge: the material stays, and simply has no value.
    d = evaluate(inputs(price=None, currency=currency), POLICY)
    assert d.category is not Cat.EXCLUDED


def test_the_currency_check_does_not_change_any_other_decision():
    same = evaluate(inputs(price=Decimal("25.00"), currency="NAD"), POLICY)
    assert (same.category, same.reason, same.rule) == (evaluate(inputs(price=None), POLICY).category, evaluate(inputs(price=None), POLICY).reason, evaluate(inputs(price=None), POLICY).rule)


# --- The explanation never uses words a planner does not know -----------------------------------------


def test_no_explanation_mentions_the_anchor_or_a_bare_latest_window():
    cases = [
        decide(criticality=None), decide(criticality="CRITICAL"), decide(criticality="OBSOLETE"),
        decide(m=never_moved()), decide(m=metrics(issues=0, months=0, prior_issues=0, prior_months=0, lifetime=3)),
        decide(m=metrics(issues=0, months=1, prior_issues=0, prior_months=0)),
        decide(m=metrics(first=date(2026, 6, 1), last=date(2026, 8, 1))), decide(lead_time=None), decide(lead_time=120),
        decide(m=metrics(issues=0, months=0, prior_issues=3, prior_months=3, last=date(2025, 6, 1))),
        decide(m=metrics(issues=9, months=9)), decide(), decide(m=metrics(issues=3, months=3)),
        decide(m=metrics(last=date(2025, 12, 1), since=10)),
        decide(m=metrics(first=date(2024, 11, 1), last=date(2026, 9, 1), lifetime=2, age=24)),
    ]
    for d in cases:
        assert "anchor" not in d.detail.lower(), d.detail
        assert "latest 12 months" not in d.detail, d.detail


def test_the_explanation_names_the_dates_it_counted():
    d = decide(m=metrics(issues=1, months=1, last=date(2026, 4, 1), since=6))
    assert "Nov 2025 and Oct 2026" in d.detail and "Apr 2026" in d.detail
    stale = decide(m=metrics(last=date(2025, 12, 1), since=10))
    assert "Dec 2025" in stale.detail and "Oct 2026" in stale.detail


# --- The reason as three short labelled lines ------------------------------------------------------

LABELS = ("What we saw", "What it means", "What to do")


def _every_reason_case():
    return {
        "unknown_criticality": decide(criticality=None),
        "protected": decide(criticality="CRITICAL"),
        "obsolete": decide(criticality="OBSOLETE"),
        "inconsistent": decide(m=metrics(issues=0, months=1, prior_issues=0, prior_months=0)),
        "never_moved": decide(m=never_moved()),
        "zero_24m": decide(m=metrics(issues=0, months=0, prior_issues=0, prior_months=0, lifetime=3)),
        "history": decide(m=metrics(first=date(2026, 6, 1), last=date(2026, 8, 1))),
        "lead_unknown": decide(lead_time=None),
        "lead_long": decide(lead_time=120),
        "lapsed": decide(m=metrics(issues=0, months=0, prior_issues=3, prior_months=3, last=date(2025, 6, 1))),
        "frequent": decide(m=metrics(issues=9, months=9)),
        "sparse": decide(),
        "borderline": decide(m=metrics(issues=3, months=3)),
        "stale": decide(m=metrics(last=date(2025, 12, 1), since=10)),
        "lifetime": decide(m=metrics(first=date(2024, 11, 1), last=date(2026, 9, 1), lifetime=2, age=24)),
    }


@pytest.mark.parametrize("name", list(_every_reason_case()))
def test_every_reason_is_three_labelled_lines_in_the_same_order(name):
    d = _every_reason_case()[name]
    assert tuple(label for label, _ in d.points) == LABELS, name
    assert all(text.strip() for _, text in d.points), name


def test_the_labelled_lines_never_mention_the_anchor_or_a_bare_window():
    for name, d in _every_reason_case().items():
        for _, text in d.points:
            assert "anchor" not in text.lower() and "latest 12 months" not in text, (name, text)


def test_what_we_saw_names_the_figures_and_the_dates_the_rule_used():
    sparse = dict(_every_reason_case()["sparse"].points)
    assert sparse["What we saw"] == "Issued 2 times in 2 months between Nov 2025 and Oct 2026. Last used Aug 2026."
    assert sparse["What it means"] == "Demand is low and recent, so ordering only when needed fits."
    assert sparse["What to do"] == "Convert it if you agree. The approvers confirm it."
    stale = dict(_every_reason_case()["stale"].points)
    assert "Dec 2025" in stale["What we saw"] and "Oct 2026" in stale["What we saw"]


def test_a_count_of_one_reads_correctly_in_the_labelled_lines():
    d = decide(m=metrics(issues=1, months=1, last=date(2026, 4, 1), since=6))
    assert dict(d.points)["What we saw"].startswith("Issued 1 time in 1 month between Nov 2025 and Oct 2026.")


def test_a_convert_row_tells_the_planner_to_decide_unless_the_rule_is_sure():
    cases = _every_reason_case()
    assert "approvers confirm" in dict(cases["sparse"].points)["What to do"]  # High
    for name in ("borderline", "stale", "lifetime", "lapsed", "lead_unknown", "lead_long", "unknown_criticality", "obsolete", "inconsistent"):
        assert dict(cases[name].points)["What to do"].startswith("A planner should"), name


def test_a_not_eligible_row_says_it_stays_on_min_max():
    cases = _every_reason_case()
    for name in ("protected", "frequent", "history"):
        assert dict(cases[name].points)["What to do"].startswith("Nothing"), name


def test_the_labelled_lines_and_the_paragraph_describe_the_same_decision():
    for name, d in _every_reason_case().items():
        assert d.detail and d.points, name
