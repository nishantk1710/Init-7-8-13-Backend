"""The rolling 24-month demand window, ending at the latest verified complete month.

What is pinned here, in the order the window is built:

* which months count as complete -- not merely which months have rows (June to August 2026 of the real extract
  hold 339, 13 and 1 movements and every one of them has rows);
* the window's boundaries: 24 calendar months, both ends included, ending on the latest complete month;
* a genuine zero (a complete month with no movement) against a missing month (never zero);
* that the window moves what classification, forecasting and the stock figures read -- and **nothing OAR reads**.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.initiatives.i7.adapters.repository import consumption_series_for
from app.initiatives.i7.contracts import MaterialIdentity, MaterialPlantKey, PlantIdentity
from app.initiatives.i7.errors import ConfigurationError
from app.initiatives.i7.features.builder import FeatureBuildResult, _build_one, _densify
from app.initiatives.i7.features.consumption_window import (
    MonthStatus,
    WindowStatus,
    assess_months,
    latest_run_window,
    resolve_window,
)
from app.initiatives.i7.policy import ConsumptionWindowPolicy, PolicyDocument
from app.initiatives.i7.recommendations import builder as recommendation_builder
from app.initiatives.i7.recommendations import workflow
from app.initiatives.i7.recommendations.types import LifecycleStatus
from app.models.i7_features import FeatureBuildRun
from app.models.i7_staging import StagedConsumption

POLICY = ConsumptionWindowPolicy()


def month(index: int, base: date = date(2024, 1, 1)) -> date:
    """The ``index``-th calendar month from ``base`` (0 is ``base`` itself)."""
    total = base.year * 12 + (base.month - 1) + index
    return date(total // 12, total % 12 + 1, 1)


def volumes(*pairs: tuple[date, int]) -> dict[date, int]:
    return dict(pairs)


def steady(first: date, last: date, level: int = 1000) -> dict[date, int]:
    out, current = {}, first
    while current <= last:
        out[current] = level
        current = month(1, current)
    return out


# --- 1. Which months are complete ----------------------------------------------------------------------------


def test_a_month_with_rows_is_not_complete_because_it_has_rows():
    """The real extract's tail: June 339, July 13, August 1 against ~2,400 in May. All have rows; none is complete."""
    data = steady(date(2025, 1, 1), date(2026, 5, 1), 2400)
    data.update({date(2026, 6, 1): 339, date(2026, 7, 1): 13, date(2026, 8, 1): 1})

    window = resolve_window(data, POLICY)

    assert window.end == date(2026, 5, 1)
    assert window.trailing_incomplete_months == (date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1))
    by_month = {a.month: a for a in window.assessments}
    for flagged in window.trailing_incomplete_months:
        assert by_month[flagged].status is MonthStatus.MISSING_DATA
        assert by_month[flagged].volume > 0, "the month has rows -- and is still not complete"


def test_the_window_ends_on_the_latest_complete_month_and_the_incomplete_tail_is_not_in_it():
    data = steady(date(2024, 1, 1), date(2026, 5, 1))
    data.update({date(2026, 6, 1): 50, date(2026, 7, 1): 10})

    window = resolve_window(data, POLICY)

    assert window.status is WindowStatus.USABLE
    assert window.end == date(2026, 5, 1)
    assert date(2026, 6, 1) not in window.months and date(2026, 7, 1) not in window.months
    assert window.missing_months == (), "the tail is excluded, not a hole inside the window"


def test_a_seasonal_dip_above_the_ratio_is_still_complete():
    data = steady(date(2025, 1, 1), date(2025, 12, 1), 1000)
    data[date(2025, 12, 1)] = 400  # 40% of the usual: quiet, not missing

    window = resolve_window(data, POLICY)

    assert window.end == date(2025, 12, 1)
    assert window.status is WindowStatus.USABLE


def test_a_month_with_no_rows_between_complete_months_is_missing_data_not_zero():
    data = steady(date(2025, 1, 1), date(2025, 12, 1))
    del data[date(2025, 7, 1)]

    window = resolve_window(data, POLICY)

    assert window.status is WindowStatus.MISSING_DATA
    assert window.missing_months == (date(2025, 7, 1),)
    assert window.end == date(2025, 12, 1)
    assert "no consumption rows" in window.reason_for(date(2025, 7, 1))


def test_an_interior_month_with_a_fraction_of_its_usual_volume_is_missing_data():
    data = steady(date(2025, 1, 1), date(2025, 12, 1))
    data[date(2025, 6, 1)] = 120

    window = resolve_window(data, POLICY)

    assert window.status is WindowStatus.MISSING_DATA
    assert window.missing_months == (date(2025, 6, 1),)


def test_a_flagged_month_does_not_lower_the_bar_for_the_next():
    """Reference volumes come from COMPLETE months only, so a run of bad months cannot make a later partial one pass."""
    data = steady(date(2025, 1, 1), date(2025, 6, 1), 1000)
    data.update({date(2025, 7, 1): 10, date(2025, 8, 1): 10, date(2025, 9, 1): 10, date(2025, 10, 1): 10})

    window = resolve_window(data, POLICY)

    assert window.end == date(2025, 6, 1)
    assert len(window.trailing_incomplete_months) == 4


def test_a_validated_month_is_complete_and_can_end_the_window():
    data = steady(date(2025, 1, 1), date(2025, 5, 1), 1000)
    data[date(2025, 6, 1)] = 30  # a genuinely quiet month, checked against SAP

    unvalidated = resolve_window(data, POLICY)
    validated = resolve_window(data, POLICY, validated=frozenset({date(2025, 6, 1)}))

    assert unvalidated.end == date(2025, 5, 1)
    assert validated.end == date(2025, 6, 1)
    assert validated.status is WindowStatus.USABLE
    assert "validated" in validated.reason_for(date(2025, 6, 1))


def test_validating_an_interior_month_clears_the_missing_data_flag():
    data = steady(date(2025, 1, 1), date(2025, 12, 1))
    data[date(2025, 6, 1)] = 5

    assert resolve_window(data, POLICY).status is WindowStatus.MISSING_DATA
    assert resolve_window(data, POLICY, validated=frozenset({date(2025, 6, 1)})).status is WindowStatus.USABLE


def test_no_staged_consumption_is_no_source_data_and_all_zero_volumes_have_no_complete_month():
    assert resolve_window({}, POLICY).status is WindowStatus.NO_SOURCE_DATA
    nothing = resolve_window({date(2025, 1, 1): 0, date(2025, 2, 1): 0}, POLICY)
    assert nothing.status is WindowStatus.NO_COMPLETE_MONTH
    assert nothing.range is None and nothing.months == ()


def test_the_opening_months_of_a_delivery_are_accepted_on_holding_movements_and_say_so():
    assessments = assess_months(steady(date(2025, 1, 1), date(2025, 5, 1)), POLICY)

    assert all(a.status is MonthStatus.COMPLETE for a in assessments)
    assert "opening month" in assessments[0].reason
    assert assessments[0].reference is None and assessments[3].reference == 1000.0


# --- 2. Window boundaries -------------------------------------------------------------------------------------


def test_the_window_is_exactly_24_calendar_months_both_ends_included():
    data = steady(date(2023, 1, 1), date(2026, 5, 1))

    window = resolve_window(data, POLICY)

    assert window.end == date(2026, 5, 1)
    assert window.start == date(2024, 6, 1)
    assert len(window.months) == 24
    assert window.months[0] == window.start and window.months[-1] == window.end
    assert month(-1, window.start) not in window.months, "the month before the start is outside"
    assert month(1, window.end) not in window.months, "the month after the end is outside"


def test_a_delivery_shorter_than_the_window_starts_where_the_delivery_does():
    """Months before the delivery began were not observed: neither zeros nor missing."""
    window = resolve_window(steady(date(2025, 9, 1), date(2026, 5, 1)), POLICY)

    assert window.start == date(2025, 9, 1)
    assert len(window.months) == 9
    assert window.status is WindowStatus.USABLE


def test_the_window_length_is_policy_not_a_literal():
    policy = ConsumptionWindowPolicy(window_months=12)
    window = resolve_window(steady(date(2024, 1, 1), date(2026, 5, 1)), policy)

    assert len(window.months) == 12 and window.start == date(2025, 6, 1)


# --- 3. Genuine zero versus missing -------------------------------------------------------------------------


def _key() -> MaterialPlantKey:
    return MaterialPlantKey(
        material=MaterialIdentity(sap_material_number="1000000001"), plant=PlantIdentity(sap_plant_code="1300")
    )


def test_a_complete_month_with_no_movement_is_an_explicit_zero_observation():
    window = resolve_window(steady(date(2025, 1, 1), date(2025, 6, 1)), POLICY)
    rows = [(date(2025, 3, 1), Decimal(7), "EA")]

    series = _densify(rows, _key(), window.range)

    assert [o.period for o in series.observations] == list(window.months)
    assert series.total_periods == 6 and series.non_zero_periods == 1
    assert series.observations[0].is_zero_demand and series.observations[2].quantity == 7


def test_rows_outside_the_window_are_not_part_of_the_series():
    window = resolve_window(steady(date(2024, 1, 1), date(2026, 5, 1)), POLICY)
    rows = [
        (month(-1, window.start), Decimal(99), "EA"),
        (window.start, Decimal(1), "EA"),
        (window.end, Decimal(2), "EA"),
        (month(1, window.end), Decimal(77), "EA"),
    ]

    series = _densify(rows, _key(), window.range)

    assert series.total_periods == 24
    assert sum(o.quantity for o in series.observations) == 3


# --- 4. The window moves the non-OAR statistics and nothing OAR reads ---------------------------------------


LEGACY_WINDOW = (date(2024, 1, 1), date(2026, 8, 1))  # the extract's own first-to-last months: 32
DEMAND_POLICY = PolicyDocument()


def _extract_volumes() -> dict[date, int]:
    data = steady(date(2024, 1, 1), date(2026, 5, 1))
    data.update({date(2026, 6, 1): 100, date(2026, 7, 1): 8, date(2026, 8, 1): 1})
    return data


def _attribute_row(material: str, mrp_type: str, mrp_controller: str) -> SimpleNamespace:
    return SimpleNamespace(
        sap_material_number=material, sap_plant_code="1300", criticality="NORMAL", mrp_type=mrp_type,
        mrp_controller=mrp_controller, material_status=None, unit_price=Decimal("10"), currency="ZAR",
        current_safety_stock=Decimal(3), current_reorder_point=Decimal(6), current_maximum_stock=Decimal(12),
        planned_delivery_time_days=30,
    )


def _consumption(months: list[date], quantity: int = 10) -> dict:
    rows = [(m, Decimal(quantity + i), "EA") for i, m in enumerate(months)]
    events = [(m, 1, 0) for m in months]
    return rows, events


def build(row, months, window, *, quantity=10):
    rows, events = _consumption(months, quantity)
    key = (row.sap_material_number, row.sap_plant_code)
    result = FeatureBuildResult()
    out = _build_one(
        row, {key: rows}, {key: events}, {}, {}, LEGACY_WINDOW, DEMAND_POLICY, 1, result,
        consumption_window=window,
    )
    return out, result


@pytest.fixture
def window():
    resolved = resolve_window(_extract_volumes(), POLICY)
    assert resolved.is_usable and resolved.end == date(2026, 5, 1) and resolved.start == date(2024, 6, 1)
    return resolved


def test_a_stock_item_is_classified_on_the_window_not_the_whole_extract(window):
    row = _attribute_row("1000000010", "VB", "ABC")
    in_window = [month(i, window.start) for i in range(0, 24, 2)]  # 12 months with demand, inside the window
    before_window = [date(2024, 1, 1), date(2024, 2, 1)]  # demand the window leaves out
    after_end = [date(2026, 7, 1)]  # a partial month the window does not read

    out, result = build(row, before_window + in_window + after_end, window)

    assert out["total_periods"] == 24 and out["history_months"] == 24
    assert out["non_zero_periods"] == 12
    assert out["first_period"] == window.start and out["last_period"] == window.end
    assert out["window_history_status"] == "SUFFICIENT"
    assert out["demand_class"] != "UNCLASSIFIED" and out["baseline_model"] is not None
    assert result.window_history_status_counts == {"SUFFICIENT": 1}


def test_the_whole_history_gate_is_kept_because_oar_reads_it(window):
    """A stock item with plenty of old demand but little in the window: classification follows the window,
    ``history_status`` -- which decides who may lend a value to OAR -- stays the whole-extract verdict."""
    row = _attribute_row("1000000011", "VB", "ABC")
    old = [month(i, date(2024, 1, 1)) for i in range(0, 5)]  # 5 months before the window
    recent = [date(2026, 4, 1), date(2026, 5, 1)]  # only 2 inside it

    out, _ = build(row, old + recent, window)

    assert out["history_status"] == "SUFFICIENT", "unchanged: the OAR donor gate"
    assert out["window_history_status"] == "COLD_START"
    assert out["demand_class"] == "UNCLASSIFIED" and out["baseline_model"] is None
    assert out["routing_reason"].startswith("WINDOW:")


def test_an_oar_material_plant_is_built_exactly_as_it_was(window):
    """No window, with a usable window, and with a MISSING_DATA window: the same row, byte for byte."""
    row = _attribute_row("4000000020", "ND", "OAR")
    months = [month(i, date(2024, 1, 1)) for i in range(0, 30, 2)]
    broken = resolve_window({**_extract_volumes(), date(2025, 3, 1): 4}, POLICY)
    assert broken.status is WindowStatus.MISSING_DATA

    without, _ = build(row, months, None)
    with_window, result = build(row, months, window)
    with_broken, _ = build(row, months, broken)

    assert without["oar_scope"] == "IN_SCOPE"
    assert with_window == without
    assert with_broken == without
    assert without["window_history_status"] is None
    assert result.window_history_status_counts == {}


def test_the_conversion_count_keeps_its_own_reference_month(window):
    """``consumption_count_12m`` feeds OAR's conversion rule: it still ends at the extract's last staged month,
    not at the window's end."""
    row = _attribute_row("1000000012", "VB", "ABC")
    months = [date(2026, 3, 1), date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)]

    with_window, _ = build(row, months, window)
    without, _ = build(row, months, None)

    assert with_window["consumption_count_12m"] == without["consumption_count_12m"] == 4


def test_a_missing_data_window_leaves_the_stock_item_unclassified_and_flagged():
    broken = resolve_window({**_extract_volumes(), date(2025, 3, 1): 4}, POLICY)
    row = _attribute_row("1000000013", "VB", "ABC")
    months = [month(i, date(2024, 6, 1)) for i in range(0, 24, 2)]

    out, result = build(row, months, broken)
    legacy, _ = build(row, months, None)

    assert out["window_history_status"] == "MISSING_DATA"
    assert out["demand_class"] == "UNCLASSIFIED" and out["baseline_model"] is None
    assert out["routing_reason"].startswith("MISSING_DATA:") and "2025-03" in out["routing_reason"]
    assert out["history_status"] == legacy["history_status"]
    assert out["total_periods"] == legacy["total_periods"], "OAR's inputs are left as they were"
    assert result.window_history_status_counts == {"MISSING_DATA": 1}


def test_a_stock_item_that_was_never_consumed_has_no_history_not_zero_demand_in_the_window(window):
    row = _attribute_row("1000000014", "VB", "ABC")
    result = FeatureBuildResult()

    out = _build_one(row, {}, {}, {}, {}, LEGACY_WINDOW, DEMAND_POLICY, 1, result, consumption_window=window)

    assert out["window_history_status"] == "NO_HISTORY"
    assert out["total_periods"] == 0 and out["demand_class"] == "UNCLASSIFIED"


# --- 5. Recommendations built on a missing window cannot be finalised ----------------------------------------


def normal_row(**overrides) -> SimpleNamespace:
    defaults = dict(
        sap_material_number="1000000001", sap_plant_code="1300", demand_class="INTERMITTENT",
        history_status="SUFFICIENT", window_history_status="SUFFICIENT", criticality=None,
        non_zero_periods=10, consumption_count_12m=None, oar_scope="OUT_OF_SCOPE", feature_run_id=1,
        baseline_model="SBA", forecast_rate=Decimal("5.83"), current_safety_stock=Decimal(2),
        current_reorder_point=Decimal(4), current_maximum_stock=Decimal(6), unit_price=Decimal("10"),
        lead_time_method="ACTUAL_STATISTICAL", valid_po_count=6, lt_avg_months=Decimal("3.99"),
        lt_avg_days=Decimal("121.4"), sigma_lt_days=Decimal("12.0"), circuit=None,
        service_level=Decimal("0.95"), z_factor=Decimal("1.645"), safety_stock_status="SUCCESS",
        safety_stock_method="TSB", safety_stock=Decimal(5), detail=None, rop_status="SUCCESS", rop=Decimal(9),
        max_stock_status="SUCCESS", max_stock_strategy="X", max_stock=Decimal(14), inventory_run_id=1,
        forecast_run_id=1,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_a_recommendation_on_a_complete_window_is_reviewable():
    built = recommendation_builder.build_normal_recommendation(normal_row(), DEMAND_POLICY)

    assert built.status is LifecycleStatus.READY_FOR_REVIEW
    assert built.recommended_rop == Decimal(9) and built.blocking_reason is None


def test_a_recommendation_on_a_missing_data_window_is_blocked_and_carries_no_numbers():
    row = normal_row(window_history_status="MISSING_DATA")

    built = recommendation_builder.build_normal_recommendation(row, DEMAND_POLICY)

    assert built.status is LifecycleStatus.NOT_EVALUABLE
    assert built.blocking_reason.startswith("MISSING_DATA")
    assert built.recommended_safety_stock is None and built.recommended_rop is None
    assert built.recommended_max_stock is None
    with pytest.raises(workflow.WorkflowError):
        workflow.submit(workflow.start(state=built.status))


def test_a_row_built_before_the_window_existed_is_judged_as_it_always_was():
    row = normal_row()
    del row.window_history_status

    assert (
        recommendation_builder.build_normal_recommendation(row, DEMAND_POLICY).status
        is LifecycleStatus.READY_FOR_REVIEW
    )


# --- 6. Forecasting reads the window the feature run recorded -------------------------------------------------


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (FeatureBuildRun, StagedConsumption):
        model.__table__.to_metadata(metadata).indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def staged(period: date, quantity: str = "5") -> StagedConsumption:
    return StagedConsumption(
        sap_material_number="M1", sap_plant_code="1300", period=period, quantity=Decimal(quantity),
        unit_of_measure="EA", staging_run_id=1, source_table="n_mseg",
    )


def run(**window) -> FeatureBuildRun:
    return FeatureBuildRun(status="succeeded", policy_id="p", policy_version=1, **window)


def seed(db, run_row=None):
    db.add_all([staged(date(2025, m, 1)) for m in range(1, 13)] + [staged(date(2026, 1, 1), "1")])
    if run_row is not None:
        db.add(run_row)
    db.commit()


def test_forecasting_reads_the_window_the_run_recorded(db):
    seed(db, run(window_status="USABLE", window_start=date(2025, 3, 1), window_end=date(2025, 10, 1)))

    series = consumption_series_for(db, "M1", "1300")

    assert [o.period for o in series.observations] == [date(2025, m, 1) for m in range(3, 11)]


def test_a_missing_data_window_gives_forecasting_no_series_to_fill_with_zeros(db):
    seed(db, run(window_status="MISSING_DATA", window_start=date(2025, 3, 1), window_end=date(2025, 10, 1),
                 window_missing_months="2025-06"))

    assert consumption_series_for(db, "M1", "1300") is None


def test_a_run_without_a_window_falls_back_to_the_extracts_own(db):
    seed(db, run())

    series = consumption_series_for(db, "M1", "1300")

    assert series.observations[0].period == date(2025, 1, 1) and series.observations[-1].period == date(2026, 1, 1)


def test_with_no_run_at_all_the_extracts_own_window_is_used(db):
    seed(db)

    assert consumption_series_for(db, "M1", "1300").total_periods == 13


def test_the_newest_succeeded_run_decides(db):
    seed(db, run(window_status="USABLE", window_start=date(2025, 1, 1), window_end=date(2025, 12, 1)))
    db.add(run(window_status="USABLE", window_start=date(2025, 6, 1), window_end=date(2025, 8, 1)))
    db.add(FeatureBuildRun(status="failed", policy_id="p", policy_version=1, window_status="MISSING_DATA"))
    db.commit()

    recorded = latest_run_window(db)

    assert recorded.start == date(2025, 6, 1) and recorded.end == date(2025, 8, 1)


# --- 7. Policy and configuration -----------------------------------------------------------------------------


def test_the_default_policy_is_24_months():
    assert PolicyDocument().consumption_window.window_months == 24


def test_the_reference_must_be_at_least_the_minimum_reference():
    with pytest.raises(ValidationError):
        ConsumptionWindowPolicy(completeness_reference_months=2, completeness_min_reference_months=3)


def test_a_window_shorter_than_the_history_gate_is_refused():
    policy = PolicyDocument(consumption_window=ConsumptionWindowPolicy(window_months=5))

    with pytest.raises(ConfigurationError):
        policy.validate_document()


def test_validated_months_are_read_as_first_of_month_dates():
    settings = Settings(i7_demand_validated_months="2026-06, 2026-07")

    assert settings.i7_demand_validated_month_set == frozenset({date(2026, 6, 1), date(2026, 7, 1)})
    assert Settings(i7_demand_validated_months="").i7_demand_validated_month_set == frozenset()


def test_a_malformed_validated_month_is_refused_not_skipped():
    with pytest.raises(ValueError, match="YYYY-MM"):
        Settings(i7_demand_validated_months="June 2026").i7_demand_validated_month_set


# --- 8. The run records the window it read ---------------------------------------------------------------------


def test_the_run_records_the_window_status_bounds_and_the_months_that_were_not_read():
    from app.initiatives.i7.features.builder import _record_window

    data = steady(date(2024, 1, 1), date(2026, 5, 1))
    data.update({date(2026, 6, 1): 50, date(2026, 7, 1): 10})
    stored = FeatureBuildRun(status="running", policy_id="p", policy_version=1)

    _record_window(stored, resolve_window(data, POLICY))

    assert stored.window_status == "USABLE"
    assert (stored.window_start, stored.window_end) == (date(2024, 6, 1), date(2026, 5, 1))
    assert stored.window_missing_months is None
    assert stored.window_trailing_months == "2026-06, 2026-07"


def test_the_run_records_which_months_inside_the_window_are_missing():
    from app.initiatives.i7.features.builder import _record_window

    data = steady(date(2025, 1, 1), date(2025, 12, 1))
    del data[date(2025, 4, 1)]
    stored = FeatureBuildRun(status="running", policy_id="p", policy_version=1)

    _record_window(stored, resolve_window(data, POLICY))

    assert stored.window_status == "MISSING_DATA" and stored.window_missing_months == "2025-04"


def test_a_build_without_a_window_leaves_the_run_unrecorded():
    from app.initiatives.i7.features.builder import _record_window

    stored = FeatureBuildRun(status="running", policy_id="p", policy_version=1)

    _record_window(stored, None)

    assert stored.window_status is None and stored.window_start is None
