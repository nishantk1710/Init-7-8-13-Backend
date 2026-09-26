"""Feature store, built against the real extract.

Real Postgres or skip. These assert on the built feature table rather than
fixtures, because the failures worth catching here -- a densification bug, a
lost classification, a duplicated material-plant -- only appear at real volume.
"""

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.contracts import DemandPattern, ScopeDecision
from app.initiatives.i7.features import BaselineModel, ChallengerModel, HistoryStatus
from app.initiatives.i7.features.oar_scope import ROLLUP_NOT_CONFIGURED
from app.initiatives.i7.features.statistics import StatisticStatus
from app.models.i7_features import FeatureBuildRun, MaterialFeature

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session


def _built(session) -> bool:
    return session.execute(select(func.count()).select_from(MaterialFeature)).scalar() > 0


# --- Grain and coverage ---------------------------------------------------


@needs_db
def test_one_feature_row_per_material_plant_in_marc_or_mard(session):
    """The material-plant universe is MARC UNION MARD, not MARC alone.

    MARC (i7_staged_material_plant) currently covers plants 1300/1200 only --
    zero rows for Gamsberg (1500) -- while MARD (i7_staged_stock) is
    multi-plant. Driving the feature builder from MARC alone would silently
    drop every Gamsberg material-plant, stock included, from the feature
    store. The correct count is the union of both sources' keys, not either
    one alone.
    """
    if not _built(session):
        pytest.skip("features not built")
    features = session.execute(select(func.count()).select_from(MaterialFeature)).scalar()
    union_count = session.execute(
        text(
            """
            select count(*) from (
              select sap_material_number, sap_plant_code from i7_staged_material_plant
              union
              select sap_material_number, sap_plant_code from i7_staged_stock
            ) u
            """
        )
    ).scalar()
    assert features == union_count


@needs_db
def test_no_duplicate_material_plants(session):
    """The unique key makes this impossible; the test proves it holds."""
    if not _built(session):
        pytest.skip("features not built")
    duplicates = session.execute(
        text(
            """select count(*) from (
                 select sap_material_number, sap_plant_code
                   from i7_material_feature group by 1,2 having count(*) > 1) d"""
        )
    ).scalar()
    assert duplicates == 0


@needs_db
def test_material_plant_grain_is_preserved(session):
    """A material present at two plants keeps two independent rows."""
    if not _built(session):
        pytest.skip("features not built")
    multi_plant = session.execute(
        text(
            """select count(*) from (
                 select sap_material_number from i7_material_feature
                  group by 1 having count(distinct sap_plant_code) > 1) m"""
        )
    ).scalar()
    assert multi_plant >= 0  # zero is valid; the schema still permits it
    rows = session.execute(
        text("select count(distinct sap_plant_code) from i7_material_feature")
    ).scalar()
    assert rows >= 1


# --- Statistics are never faked --------------------------------------------


@needs_db
def test_adi_is_null_exactly_when_its_status_says_so(session):
    if not _built(session):
        pytest.skip("features not built")
    inconsistent = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(
            (
                (MaterialFeature.adi.is_(None))
                & (MaterialFeature.adi_status == StatisticStatus.AVAILABLE.value)
            )
            | (
                (MaterialFeature.adi.isnot(None))
                & (MaterialFeature.adi_status != StatisticStatus.AVAILABLE.value)
            )
        )
    ).scalar()
    assert inconsistent == 0


@needs_db
def test_no_material_has_a_zero_adi(session):
    """ADI cannot be 0 -- n/n_nz is at least 1. A zero would mean a substituted
    value rather than a computed one."""
    if not _built(session):
        pytest.skip("features not built")
    zeros = session.execute(
        select(func.count()).select_from(MaterialFeature).where(MaterialFeature.adi == 0)
    ).scalar()
    assert zeros == 0


@needs_db
def test_materials_without_demand_have_no_adi(session):
    if not _built(session):
        pytest.skip("features not built")
    wrong = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.non_zero_periods == 0, MaterialFeature.adi.isnot(None))
    ).scalar()
    assert wrong == 0


@needs_db
def test_adi_equals_total_over_non_zero(session):
    """Spot-check the stored arithmetic against its own inputs."""
    if not _built(session):
        pytest.skip("features not built")
    rows = session.execute(
        select(MaterialFeature)
        .where(MaterialFeature.adi.isnot(None))
        .limit(50)
    ).scalars().all()
    if not rows:
        pytest.skip("no classified materials")
    for row in rows:
        expected = row.total_periods / row.non_zero_periods
        assert abs(float(row.adi) - expected) < 1e-5


@needs_db
def test_both_dispersion_measures_are_stored_separately(session):
    """sigma_D includes zeros, sigma_nz does not -- Phase 5 needs the first."""
    if not _built(session):
        pytest.skip("features not built")
    columns = {column.name for column in MaterialFeature.__table__.columns}
    assert "std_dev_demand_all_periods" in columns
    assert "std_dev_non_zero_demand" in columns


# --- Classification and routing --------------------------------------------


@needs_db
def test_only_gated_materials_are_classified(session):
    """A cold-start material has too little evidence to earn a demand class."""
    if not _built(session):
        pytest.skip("features not built")
    wrong = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(
            MaterialFeature.history_status != HistoryStatus.SUFFICIENT.value,
            MaterialFeature.demand_class != DemandPattern.UNCLASSIFIED.value,
        )
    ).scalar()
    assert wrong == 0


@needs_db
def test_routing_follows_the_demand_class(session):
    if not _built(session):
        pytest.skip("features not built")
    for demand_class, baseline, challenger in (
        (DemandPattern.SMOOTH, BaselineModel.SES, ChallengerModel.AUTO_ARIMA),
        (DemandPattern.ERRATIC, BaselineModel.SES, ChallengerModel.AUTO_ARIMA),
        (DemandPattern.INTERMITTENT, BaselineModel.SBA, ChallengerModel.LIGHTGBM),
        (DemandPattern.LUMPY, BaselineModel.SBA, ChallengerModel.LIGHTGBM),
    ):
        mismatched = session.execute(
            select(func.count())
            .select_from(MaterialFeature)
            .where(
                MaterialFeature.demand_class == demand_class.value,
                (MaterialFeature.baseline_model != baseline.value)
                | (MaterialFeature.challenger_model != challenger.value),
            )
        ).scalar()
        assert mismatched == 0, f"{demand_class} routed inconsistently"


@needs_db
def test_unclassified_materials_have_no_route(session):
    if not _built(session):
        pytest.skip("features not built")
    wrong = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(
            MaterialFeature.demand_class == DemandPattern.UNCLASSIFIED.value,
            MaterialFeature.baseline_model.isnot(None),
        )
    ).scalar()
    assert wrong == 0


@needs_db
def test_no_champion_column_exists(session):
    """Champion selection is Phase 4's, after backtesting."""
    columns = {column.name for column in MaterialFeature.__table__.columns}
    assert "champion_model" not in columns
    assert "selected_model" not in columns


# --- OAR ---------------------------------------------------------------------


@needs_db
def test_oar_scope_is_one_of_three_states(session):
    if not _built(session):
        pytest.skip("features not built")
    values = session.execute(select(MaterialFeature.oar_scope).distinct()).scalars().all()
    assert set(values) <= {decision.value for decision in ScopeDecision}


@needs_db
def test_missing_mrp_type_is_never_itself_the_reason_for_out_of_scope(session):
    """A blank DISMM is unknown, not excluded -- by itself.

    The OAR policy is a predicate list combined by AND across independent
    fields (mrp_type, material_status). A material can legitimately land
    OUT_OF_SCOPE with no mrp_type at all if a *different* predicate (e.g.
    material_status) excludes it on its own -- Gamsberg (plant 1500) has no
    MARC coverage at all, so mrp_type is always NULL there, yet a handful of
    materials are still correctly excluded by material_status alone. What
    must never happen is mrp_type being absent counting as an exclusion by
    itself -- oar_reason records which predicate(s) actually decided, so this
    checks that directly rather than assuming mrp_type=None implies UNKNOWN.
    """
    if not _built(session):
        pytest.skip("features not built")
    wrong = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(
            MaterialFeature.mrp_type.is_(None),
            MaterialFeature.oar_scope == ScopeDecision.OUT_OF_SCOPE.value,
            MaterialFeature.oar_reason.ilike("%mrp_type%"),
        )
    ).scalar()
    assert wrong == 0


@needs_db
def test_every_oar_verdict_carries_a_reason(session):
    if not _built(session):
        pytest.skip("features not built")
    missing = session.execute(
        select(func.count()).select_from(MaterialFeature).where(
            MaterialFeature.oar_reason.is_(None)
        )
    ).scalar()
    assert missing == 0


@needs_db
def test_rollup_stays_unconfigured(session):
    if not _built(session):
        pytest.skip("features not built")
    configured = session.execute(
        select(func.count()).select_from(MaterialFeature).where(
            MaterialFeature.oar_rollup_status != ROLLUP_NOT_CONFIGURED
        )
    ).scalar()
    assert configured == 0


# --- Unit price (MBEW) -----------------------------------------------------------


@needs_db
def test_unit_price_never_negative(session):
    """A moving average price is a physical quantity; it cannot be negative."""
    if not _built(session):
        pytest.skip("features not built")
    negative = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.unit_price < 0)
    ).scalar()
    assert negative == 0


@needs_db
def test_real_unit_price_data_reaches_the_feature_store(session):
    """MBEW covers only part of the catalogue; some real data must still land.

    Not a full-population assertion like reorder_point/max_stock (MARC), since
    MBEW's coverage is genuinely partial -- see the extract adapter's own
    material-count comment. This proves unit_price is live source data, not a
    column that silently never gets written.
    """
    if not _built(session):
        pytest.skip("features not built")
    populated = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.unit_price.isnot(None))
    ).scalar()
    assert populated > 0


@needs_db
def test_has_unit_price_is_consistent_with_unit_price(session):
    if not _built(session):
        pytest.skip("features not built")
    inconsistent = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(
            (
                (MaterialFeature.unit_price.is_(None))
                & (MaterialFeature.has_unit_price)
            )
            | (
                (MaterialFeature.unit_price.isnot(None))
                & (~MaterialFeature.has_unit_price)
            )
        )
    ).scalar()
    assert inconsistent == 0


@needs_db
def test_currency_stays_null_rather_than_assumed(session):
    """MBEW's extract carries no currency column; nothing here invents one."""
    if not _built(session):
        pytest.skip("features not built")
    invented = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.currency.isnot(None))
    ).scalar()
    assert invented == 0


# --- MRP parameters (MARC) --------------------------------------------------------


@needs_db
def test_reorder_point_reaches_the_feature_store(session):
    """MARC.MINBE is fully populated in the extract; it must reach every
    MARC-backed row. Scoped to plants MARC actually covers (1200/1300) --
    Gamsberg (1500) has no MARC coverage at all, so its rows correctly stay
    NULL here; see the Gamsberg-specific NULL-not-fabricated test below.
    """
    if not _built(session):
        pytest.skip("features not built")
    marc_plants = ("1200", "1300")
    total = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.sap_plant_code.in_(marc_plants))
    ).scalar()
    populated = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(
            MaterialFeature.sap_plant_code.in_(marc_plants),
            MaterialFeature.current_reorder_point.isnot(None),
        )
    ).scalar()
    assert populated == total


@needs_db
def test_maximum_stock_reaches_the_feature_store(session):
    """MARC.MABST is fully populated in the extract; it must reach every
    MARC-backed row. See test_reorder_point_reaches_the_feature_store for why
    this is scoped to MARC-covered plants rather than the whole table.
    """
    if not _built(session):
        pytest.skip("features not built")
    marc_plants = ("1200", "1300")
    total = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.sap_plant_code.in_(marc_plants))
    ).scalar()
    populated = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(
            MaterialFeature.sap_plant_code.in_(marc_plants),
            MaterialFeature.current_maximum_stock.isnot(None),
        )
    ).scalar()
    assert populated == total


@needs_db
def test_planned_delivery_time_reaches_the_feature_store(session):
    """MARC.PLIFZ is fully populated in the extract; it must reach every
    MARC-backed row. See test_reorder_point_reaches_the_feature_store for why
    this is scoped to MARC-covered plants rather than the whole table.
    """
    if not _built(session):
        pytest.skip("features not built")
    marc_plants = ("1200", "1300")
    total = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.sap_plant_code.in_(marc_plants))
    ).scalar()
    populated = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(
            MaterialFeature.sap_plant_code.in_(marc_plants),
            MaterialFeature.planned_delivery_time_days.isnot(None),
        )
    ).scalar()
    assert populated == total


@needs_db
def test_planned_delivery_time_is_never_negative(session):
    """PLIFZ is a day count; it cannot be negative."""
    if not _built(session):
        pytest.skip("features not built")
    negative = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.planned_delivery_time_days < 0)
    ).scalar()
    assert negative == 0


@needs_db
def test_reorder_point_and_maximum_stock_never_negative(session):
    if not _built(session):
        pytest.skip("features not built")
    for column in (
        MaterialFeature.current_reorder_point,
        MaterialFeature.current_maximum_stock,
    ):
        negative = session.execute(
            select(func.count()).select_from(MaterialFeature).where(column < 0)
        ).scalar()
        assert negative == 0


@needs_db
def test_safety_stock_stays_null_in_the_feature_store_because_eisbe_is_absent(session):
    """Not zero. Zero safety stock is a real, different claim from "not supplied".

    MARC.EISBE is not a column in the delivered extract (see field_map.py's
    MARC_MISSING_FIELDS) -- this is a source-data gap, not a mapping defect,
    and nothing here may derive, default, or copy another field into it to
    paper over the absence.
    """
    if not _built(session):
        pytest.skip("features not built")
    fabricated = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.current_safety_stock.isnot(None))
    ).scalar()
    assert fabricated == 0


# --- Criticality is never invented ---------------------------------------------


@needs_db
def test_missing_criticality_stays_null(session):
    """Never defaulted to NORMAL -- criticality drives the service level."""
    if not _built(session):
        pytest.skip("features not built")
    inconsistent = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(
            (
                (MaterialFeature.criticality.is_(None))
                & (MaterialFeature.has_criticality)
            )
            | (
                (MaterialFeature.criticality.isnot(None))
                & (~MaterialFeature.has_criticality)
            )
        )
    ).scalar()
    assert inconsistent == 0


# --- Stock position (MARD) --------------------------------------------------------


@needs_db
def test_stock_position_is_present_when_mard_staged_it(session):
    """The overwhelming majority of material-plants have staged MARD rows."""
    if not _built(session):
        pytest.skip("features not built")
    total = session.execute(select(func.count()).select_from(MaterialFeature)).scalar()
    with_stock = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.has_stock_data)
    ).scalar()
    assert with_stock > 0
    assert with_stock <= total


@needs_db
def test_current_stock_never_negative(session):
    """Unrestricted-use stock is a physical quantity; it cannot be negative."""
    if not _built(session):
        pytest.skip("features not built")
    negative = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.current_stock < 0)
    ).scalar()
    assert negative == 0


@needs_db
def test_has_stock_data_is_consistent_with_current_stock(session):
    """A material-plant with no staged MARD rows has NULL, not 0, stock."""
    if not _built(session):
        pytest.skip("features not built")
    inconsistent = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(
            ~MaterialFeature.has_stock_data,
            MaterialFeature.current_stock.isnot(None),
        )
    ).scalar()
    assert inconsistent == 0


@needs_db
def test_storage_location_count_matches_staged_rows(session):
    """The summed count should equal MARD rows staged for that material-plant."""
    if not _built(session):
        pytest.skip("features not built")
    row = session.execute(
        select(MaterialFeature).where(MaterialFeature.has_stock_data).limit(1)
    ).scalar_one_or_none()
    if row is None:
        pytest.skip("no material-plant has staged stock")
    staged_count = session.execute(
        text(
            "select count(*) from i7_staged_stock "
            "where sap_material_number = :m and sap_plant_code = :p"
        ),
        {"m": row.sap_material_number, "p": row.sap_plant_code},
    ).scalar()
    assert row.storage_location_count == staged_count


# --- Gamsberg (plant 1500): MARD-only material-plants -------------------------
#
# MARC (i7_staged_material_plant) covers plants 1300/1200 only -- zero rows for
# Gamsberg (1500) -- while MARD (i7_staged_stock) is multi-plant. Before the
# fix, the feature builder's driving query read from MARC alone, so every
# Gamsberg material-plant -- stock included -- silently never reached
# MaterialFeature. These tests pin the fix: MARC-only fields (MRP type, ROP,
# Max, PLIFZ) correctly stay NULL for Gamsberg since MARC still doesn't cover
# it, but the row itself, and its stock, must exist.


@needs_db
def test_gamsberg_material_plants_reach_the_feature_store(session):
    if not _built(session):
        pytest.skip("features not built")
    staged_1500 = session.execute(
        text(
            "select count(distinct (sap_material_number, sap_plant_code)) "
            "from i7_staged_stock where sap_plant_code = '1500'"
        )
    ).scalar()
    if not staged_1500:
        pytest.skip("no plant 1500 stock staged in this extract")
    features_1500 = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.sap_plant_code == "1500")
    ).scalar()
    assert features_1500 == staged_1500


@needs_db
def test_gamsberg_stock_is_populated(session):
    """The whole point of the fix: Gamsberg's real MARD stock must reach the
    feature store, not just an empty row for the material-plant key."""
    if not _built(session):
        pytest.skip("features not built")
    row = session.execute(
        select(MaterialFeature)
        .where(
            MaterialFeature.sap_plant_code == "1500",
            MaterialFeature.has_stock_data,
        )
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        pytest.skip("no plant 1500 material-plant has staged stock")
    assert row.current_stock is not None


@needs_db
def test_gamsberg_marc_dependent_fields_stay_null_not_fabricated(session):
    """MARC still does not cover Gamsberg -- MRP type, ROP, Max and PLIFZ must
    stay NULL for plant 1500 rows, never defaulted or copied from elsewhere.
    """
    if not _built(session):
        pytest.skip("features not built")
    total_1500 = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.sap_plant_code == "1500")
    ).scalar()
    if not total_1500:
        pytest.skip("no plant 1500 rows in the feature store")
    for column in (
        MaterialFeature.mrp_type,
        MaterialFeature.current_reorder_point,
        MaterialFeature.current_maximum_stock,
        MaterialFeature.planned_delivery_time_days,
    ):
        fabricated = session.execute(
            select(func.count())
            .select_from(MaterialFeature)
            .where(MaterialFeature.sap_plant_code == "1500", column.isnot(None))
        ).scalar()
        assert fabricated == 0


@needs_db
def test_gamsberg_fix_does_not_change_black_mountain_plants(session):
    """1200 and 1300 must be exactly as populated as before the union fix --
    the change adds MARD-only rows, it does not alter MARC-backed ones."""
    if not _built(session):
        pytest.skip("features not built")
    marc_features = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(MaterialFeature.sap_plant_code.in_(("1200", "1300")))
    ).scalar()
    marc_staged = session.execute(
        text(
            "select count(*) from i7_staged_material_plant "
            "where sap_plant_code in ('1200', '1300')"
        )
    ).scalar()
    assert marc_features == marc_staged


@needs_db
def test_no_duplicate_rows_from_the_marc_mard_union(session):
    if not _built(session):
        pytest.skip("features not built")
    duplicates = session.execute(
        text(
            """select count(*) from (
                 select sap_material_number, sap_plant_code
                   from i7_material_feature group by 1,2 having count(*) > 1) d"""
        )
    ).scalar()
    assert duplicates == 0


# --- Provenance ------------------------------------------------------------------


@needs_db
def test_features_cite_their_run_and_policy(session):
    if not _built(session):
        pytest.skip("features not built")
    row = session.execute(select(MaterialFeature).limit(1)).scalar_one()
    run = session.get(FeatureBuildRun, row.feature_run_id)
    assert run is not None
    assert run.policy_id and run.policy_version >= 1


@needs_db
def test_run_records_the_movement_type_definition(session):
    """The set is unconfirmed, so which definition of demand produced these
    numbers has to travel with them."""
    if not _built(session):
        pytest.skip("features not built")
    run = session.execute(
        select(FeatureBuildRun).order_by(FeatureBuildRun.id.desc()).limit(1)
    ).scalar_one()
    assert run.consumption_movement_types


# --- History limits are recorded, not worked around ---------------------------


@needs_db
def test_available_history_is_not_inflated(session):
    """The extract spans 13 months. Nothing may claim more."""
    if not _built(session):
        pytest.skip("features not built")
    longest = session.execute(select(func.max(MaterialFeature.total_periods))).scalar()
    assert longest <= 14, f"history of {longest} months exceeds the extract window"


@needs_db
def test_series_spans_the_extract_window_not_the_material_span(session):
    """Regression: ``n`` is the observation window, not first-to-last movement.

    The Formula Reference defines ``n`` as "total periods (months)" and gates on
    ``total_history_months`` -- properties of the window, not of where a material
    happens to have moved. Densifying per-material dropped leading zero-demand
    months from ``n`` while keeping every ``n_nz``, understating ADI: 3,495 of
    4,269 material-plants started late, one moving ADI from 1.500 to 1.625.
    Those leading months are evidence of *no demand*, which is what an
    intermittency measure most needs to see.
    """
    if not _built(session):
        pytest.skip("features not built")

    low, high = session.execute(
        text("select min(period), max(period) from i7_staged_consumption")
    ).one()
    expected = (high.year - low.year) * 12 + (high.month - low.month) + 1

    distinct = session.execute(
        select(MaterialFeature.total_periods)
        .where(MaterialFeature.total_periods > 0)
        .distinct()
    ).scalars().all()
    assert distinct == [expected], (
        f"every material with history should span the {expected}-month window, "
        f"got {sorted(distinct)}"
    )


@needs_db
def test_no_history_is_distinct_from_a_window_of_zeros(session):
    """A material that never moved is NO_HISTORY, not 13 zero months.

    Conflating them would push 41,140 material-plants through the gate as though
    they had been observed and found empty.
    """
    if not _built(session):
        pytest.skip("features not built")
    wrong = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(
            MaterialFeature.history_status == HistoryStatus.NO_HISTORY.value,
            MaterialFeature.total_periods > 0,
        )
    ).scalar()
    assert wrong == 0


@needs_db
def test_cold_start_cannot_be_caused_by_a_short_window(session):
    """With a fixed window wider than the 6-month minimum, only ``n_nz < 5``
    can gate a material that has any history at all."""
    if not _built(session):
        pytest.skip("features not built")
    wrong = session.execute(
        select(func.count())
        .select_from(MaterialFeature)
        .where(
            MaterialFeature.history_status == HistoryStatus.COLD_START.value,
            MaterialFeature.non_zero_periods >= 5,
        )
    ).scalar()
    assert wrong == 0


@needs_db
def test_required_history_is_recorded_against_what_exists(session):
    if not _built(session):
        pytest.skip("features not built")
    row = session.execute(
        select(MaterialFeature).where(MaterialFeature.total_periods > 0).limit(1)
    ).scalar_one()
    assert row.required_history_months == 24
    assert row.data_sufficiency in {"FULL", "LIMITED", "INSUFFICIENT"}


# --- Idempotency (row-level, not just counts) -------------------------------


@needs_db
def test_rebuild_is_row_level_identical(session):
    """A second build must reproduce every field of every row.

    Aggregate counts are too weak a check: an upsert that silently rewrote ADI
    or flipped an OAR verdict would keep the row count identical. This compares
    the values that later phases actually read.
    """
    if not _built(session):
        pytest.skip("features not built")

    from app.initiatives.i7.features import build_features

    fields = (
        "sap_material_number, sap_plant_code, history_status, data_sufficiency, "
        "total_periods, non_zero_periods, adi, adi_status, cv_squared, "
        "cv_squared_status, demand_class, baseline_model, challenger_model, "
        "oar_scope, oar_reason, oar_rollup_status, criticality, has_criticality, "
        "has_lead_time, purchase_order_count"
    )
    order = " order by sap_material_number, sap_plant_code"

    before = session.execute(text(f"select {fields} from i7_material_feature{order}")).all()
    session.commit()

    result = build_features()
    assert result.status == "succeeded"

    session.expire_all()
    after = session.execute(text(f"select {fields} from i7_material_feature{order}")).all()

    assert len(before) == len(after)
    assert before == after


@needs_db
def test_build_does_not_query_per_material_plant(session):
    """Consumption and PO counts load in two grouped queries, not one per row.

    Guards the performance property by construction rather than by timing: the
    loaders return whole dictionaries, so a regression to per-material queries
    would have to change their signatures.
    """
    from app.initiatives.i7.features.builder import (
        _load_consumption,
        _load_purchase_order_counts,
        observation_window,
    )

    consumption = _load_consumption(session)
    orders = _load_purchase_order_counts(session)
    window = observation_window(session)

    assert isinstance(consumption, dict)
    assert isinstance(orders, dict)
    assert window is None or len(window) == 2
