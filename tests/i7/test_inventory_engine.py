"""End-to-end inventory calculation, and the persisted result.

The seven required scenarios are driven through :func:`calculate_one` with
synthetic input rows, so each gate can be exercised in isolation. The
database-backed tests then check the same properties on the real run.

Service levels here are **test fixtures** used to open the gate and prove the
chain works. The signed matrix remains unset in configuration.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.contracts import Criticality
from app.initiatives.i7.inventory import calculate_one
from app.initiatives.i7.inventory.lead_time import PurchaseOrderInput
from app.initiatives.i7.inventory.types import CalculationStatus
from app.initiatives.i7.policy import (
    MaxStockStrategy as MaxStockPolicy,
    PolicyDocument,
    ServiceLevelKey,
    ServiceLevelPolicy,
)
from app.models.i7_inventory import InventoryCalculation, InventoryRun

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


def signed_policy(level: float = 0.95, **overrides) -> PolicyDocument:
    """A policy with a signed matrix -- a TEST FIXTURE, not configuration."""
    return PolicyDocument(
        service_level=ServiceLevelPolicy(
            matrix=tuple(
                (ServiceLevelKey(criticality=tier), level) for tier in Criticality
            )
        ),
        **overrides,
    )


def row(
    demand_class: str = "SMOOTH",
    criticality: str | None = "NORMAL",
    forecast_rate: Decimal | None = Decimal("5.83"),
    parameters: str | None = None,
    planned_days: int | None = 120,
    mu_nz: Decimal | None = Decimal("5.57"),
    sigma_nz: Decimal | None = Decimal("1.56"),
    total_periods: int = 12,
    non_zero_periods: int = 8,
) -> SimpleNamespace:
    return SimpleNamespace(
        sap_material_number="000000000010000000",
        sap_plant_code="1300",
        demand_class=demand_class,
        history_status="SUFFICIENT",
        criticality=criticality,
        total_periods=total_periods,
        non_zero_periods=non_zero_periods,
        mean_demand_all_periods=Decimal("5.83"),
        std_dev_demand_all_periods=Decimal("1.37"),
        mean_non_zero_demand=mu_nz,
        std_dev_non_zero_demand=sigma_nz,
        planned_delivery_time_days=planned_days,
        unit_price=Decimal(12500),
        model_name="SES",
        forecast_rate=forecast_rate,
        forecast_unit="EA",
        parameters=parameters,
    )


DEMAND = [Decimal(x) for x in (6, 0, 5, 7, 0, 6, 8, 0, 5, 7, 4, 6)]
FIVE_ORDERS = [PurchaseOrderInput(d) for d in (111, 125, 130, 115, 120)]


# --- Case 1: Smooth, end to end ------------------------------------------


def test_case_1_smooth_produces_safety_stock_and_rop():
    result = calculate_one(row("SMOOTH"), FIVE_ORDERS, signed_policy(), DEMAND)
    assert result["safety_stock_status"] == CalculationStatus.SUCCESS.value
    assert result["safety_stock_method"] == "normal"
    assert result["safety_stock"] > 0
    assert result["rop_status"] == CalculationStatus.SUCCESS.value
    assert result["rop"] >= result["safety_stock"]
    # Lead time comes from MARC-PLIFZ unconditionally by business decision --
    # see app/initiatives/i7/inventory/lead_time.py's module docstring. Five
    # usable POs no longer produce ACTUAL_STATISTICAL; PLIFZ still decides.
    assert result["lead_time_method"] == "PLANNED_FALLBACK"


def test_case_1_rop_uses_the_forecast_not_the_historical_average():
    """Substituting D_avg would discard the whole forecasting layer."""
    high = calculate_one(
        row("SMOOTH", forecast_rate=Decimal(20)), FIVE_ORDERS, signed_policy(), DEMAND
    )
    low = calculate_one(
        row("SMOOTH", forecast_rate=Decimal(1)), FIVE_ORDERS, signed_policy(), DEMAND
    )
    # Same history, so the same safety stock -- only E[LTD] moves.
    assert high["safety_stock"] == low["safety_stock"]
    assert high["expected_lead_time_demand"] > low["expected_lead_time_demand"]


# --- Case 2: Intermittent -------------------------------------------------


def test_case_2_intermittent_uses_compound_poisson():
    result = calculate_one(
        row("INTERMITTENT", parameters="alpha=0.10;p_final=1.68;z_final=5.62"),
        FIVE_ORDERS,
        signed_policy(),
        DEMAND,
    )
    assert result["safety_stock_status"] == CalculationStatus.SUCCESS.value
    assert result["safety_stock_method"] == "compound_poisson"
    assert "lambda" in result["safety_stock_trace"]


def test_case_2_without_sba_parameters_is_blocked():
    result = calculate_one(
        row("INTERMITTENT", parameters=None), FIVE_ORDERS, signed_policy(), DEMAND
    )
    assert result["safety_stock_status"] == (
        CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND.value
    )


# --- Case 3: Lumpy --------------------------------------------------------


def test_case_3_lumpy_uses_monte_carlo():
    result = calculate_one(
        row("LUMPY", parameters="alpha=0.10;p_final=2.50;z_final=40"),
        FIVE_ORDERS,
        signed_policy(),
        [Decimal(x) for x in (5, 0, 40, 0, 0, 8, 0, 120, 0, 15, 0, 60)],
    )
    assert result["safety_stock_status"] == CalculationStatus.SUCCESS.value
    assert result["safety_stock_method"] == "monte_carlo"
    assert "random_seed" in result["safety_stock_trace"]


def test_case_3_lumpy_is_reproducible():
    arguments = (
        row("LUMPY", parameters="alpha=0.10;p_final=2.50;z_final=40"),
        FIVE_ORDERS,
        signed_policy(),
        [Decimal(x) for x in (5, 0, 40, 0, 0, 8, 0, 120, 0, 15, 0, 60)],
    )
    assert calculate_one(*arguments)["safety_stock"] == calculate_one(*arguments)["safety_stock"]


# --- Case 4: service level missing ------------------------------------------


def test_case_4_unsigned_service_level_blocks_everything_downstream():
    """The designed gate. Lead time and variability still compute."""
    result = calculate_one(row("SMOOTH"), FIVE_ORDERS, PolicyDocument(), DEMAND)

    # LIMITED, not SUCCESS: lead time now comes from MARC-PLIFZ unconditionally
    # (business decision -- see inventory/lead_time.py's module docstring),
    # and PLANNED_FALLBACK's status is LIMITED regardless of PO history.
    assert result["lead_time_status"] == CalculationStatus.LIMITED.value
    assert result["lt_avg_months"] is not None
    assert result["variability_status"] == CalculationStatus.SUCCESS.value
    assert result["sigma_d"] is not None

    blocked = CalculationStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET.value
    assert result["service_level_status"] == blocked
    assert result["z_factor"] is None
    assert result["safety_stock_status"] == blocked
    assert result["safety_stock"] is None
    assert result["rop_status"] == blocked
    assert result["rop"] is None


# --- Case 5: lead time missing ------------------------------------------------


def test_case_5_missing_lead_time_blocks_safety_stock():
    result = calculate_one(row("SMOOTH", planned_days=None), [], signed_policy(), DEMAND)
    assert result["lead_time_status"] == CalculationStatus.NOT_EVALUABLE_LEAD_TIME.value
    assert result["safety_stock_status"] == CalculationStatus.NOT_EVALUABLE_LEAD_TIME.value
    assert result["safety_stock"] is None
    assert result["rop"] is None


def test_case_5_planned_delivery_time_rescues_it():
    result = calculate_one(row("SMOOTH", planned_days=120), [], signed_policy(), DEMAND)
    assert result["lead_time_method"] == "PLANNED_FALLBACK"
    assert result["safety_stock_status"] == CalculationStatus.SUCCESS.value


# --- Case 6: no history ---------------------------------------------------------


def test_case_6_unclassified_defers_to_the_oar_engine():
    """Phase 6's problem. No neighbour is invented and no average substituted."""
    result = calculate_one(row("UNCLASSIFIED"), FIVE_ORDERS, signed_policy(), [])
    deferred = CalculationStatus.DEFERRED_TO_OAR.value
    assert result["safety_stock_status"] == deferred
    assert result["rop_status"] == deferred
    assert result["max_stock_status"] == deferred
    assert result["safety_stock"] is None


# --- Case 7: obsolete -------------------------------------------------------------


def test_case_7_obsolete_gets_no_safety_stock():
    """Represented explicitly, never as a zero -- a zero reads as a real target."""
    result = calculate_one(
        row("SMOOTH", criticality="OBSOLETE"), FIVE_ORDERS, signed_policy(), DEMAND
    )
    assert result["safety_stock_status"] == CalculationStatus.NOT_APPLICABLE_OBSOLETE.value
    assert result["safety_stock"] is None
    assert result["rop"] is None


def test_obsolete_still_records_lead_time_and_variability():
    """The data is useful even where no recommendation follows."""
    result = calculate_one(
        row("SMOOTH", criticality="OBSOLETE"), FIVE_ORDERS, signed_policy(), DEMAND
    )
    assert result["lt_avg_months"] is not None
    assert result["sigma_d"] is not None


# --- Max stock gate ------------------------------------------------------------------


def test_max_stock_is_not_configured_even_when_everything_else_succeeds():
    result = calculate_one(row("SMOOTH"), FIVE_ORDERS, signed_policy(), DEMAND)
    assert result["safety_stock_status"] == CalculationStatus.SUCCESS.value
    assert result["max_stock_status"] == CalculationStatus.NOT_CONFIGURED.value
    assert result["max_stock"] is None


def test_max_stock_computes_once_a_strategy_is_signed():
    policy = signed_policy(
        max_stock=MaxStockPolicy(
            strategy="review_period",
            review_period_months=tuple((tier, 3.0) for tier in Criticality),
        )
    )
    result = calculate_one(row("SMOOTH"), FIVE_ORDERS, policy, DEMAND)
    assert result["max_stock_status"] == CalculationStatus.SUCCESS.value
    assert result["max_stock"] > result["rop"]


# --- Persistence ---------------------------------------------------------------------


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session


def _ran(session) -> bool:
    return session.execute(select(func.count()).select_from(InventoryCalculation)).scalar() > 0


@needs_db
def test_every_material_plant_has_a_calculation(session):
    """Scoped to one run: several runs may coexist, each a complete set."""
    if not _ran(session):
        pytest.skip("no inventory run")
    from app.models.i7_features import MaterialFeature

    latest = session.execute(
        select(InventoryRun.id)
        .where(InventoryRun.status == "succeeded")
        .order_by(InventoryRun.id.desc())
        .limit(1)
    ).scalar()

    calculations = session.execute(
        select(func.count())
        .select_from(InventoryCalculation)
        .where(InventoryCalculation.inventory_run_id == latest)
    ).scalar()
    features = session.execute(select(func.count()).select_from(MaterialFeature)).scalar()
    assert calculations == features


@needs_db
def test_no_duplicate_calculations(session):
    if not _ran(session):
        pytest.skip("no inventory run")
    from sqlalchemy import text

    duplicates = session.execute(
        text(
            """select count(*) from (
                 select inventory_run_id, sap_material_number, sap_plant_code
                   from i7_inventory_calculation group by 1,2,3 having count(*) > 1) d"""
        )
    ).scalar()
    assert duplicates == 0


# --- Feature store is the source, not staging --------------------------------
#
# These assert on the actual SQL text and on real divergence between staging
# and the feature store, so a regression that reintroduces a staging join
# would fail here even if it produced a plausible-looking number.


def test_input_sql_reads_lead_time_and_price_from_the_feature_store_only():
    """No i7_staged_material_plant / i7_staged_material join in the input query.

    A future edit that re-adds ``LEFT JOIN i7_staged_material_plant`` or
    ``LEFT JOIN i7_staged_material`` here would silently start reading
    planned_delivery_time_days / unit_price from staging again -- this would
    fail immediately rather than waiting on a live-data divergence to surface.
    """
    from app.initiatives.i7.inventory import service as inventory_service

    assert "i7_staged_material_plant" not in inventory_service._INPUT_SQL
    assert "i7_staged_material" not in inventory_service._INPUT_SQL
    assert "f.planned_delivery_time_days" in inventory_service._INPUT_SQL
    assert "f.unit_price" in inventory_service._INPUT_SQL


def test_calculate_one_uses_feature_store_lead_time_even_if_staging_disagrees():
    """The feature-store row is authoritative even when staging has drifted.

    Simulates the real bug class this fix closes: if the two sources ever
    disagree (a stale staging row, a re-run feature build), a consumer reading
    staging directly would silently use the wrong number. row() here plays the
    role of the feature-store row _INPUT_SQL now produces -- its
    planned_delivery_time_days is deliberately different from what a
    staging-sourced value would have been, and the calculation must follow it.
    """
    feature_says_60_days = row(planned_days=60)
    result = calculate_one(feature_says_60_days, [], signed_policy(), DEMAND)
    assert result["lt_avg_days"] == Decimal(60)
    assert result["lead_time_method"] == "PLANNED_FALLBACK"


def test_calculate_one_uses_feature_store_variability_even_if_staging_disagrees():
    """Same principle for demand variability: from_feature reads the row's own
    mean/std-dev, never a value derived from a separately-queried consumption
    series that could disagree with it.
    """
    from app.initiatives.i7.inventory import variability as variability_module

    feature_row = row(total_periods=12, non_zero_periods=8)
    # A consumption series that would produce DIFFERENT statistics than the
    # feature row claims, proving the result follows the row, not this series.
    disagreeing_series = [Decimal(x) for x in range(1, 13)]
    result = calculate_one(feature_row, [], signed_policy(), disagreeing_series)

    expected = variability_module.from_feature(
        feature_row.total_periods,
        feature_row.non_zero_periods,
        feature_row.mean_demand_all_periods,
        feature_row.std_dev_demand_all_periods,
    )
    assert Decimal(str(result["sigma_d"])) == expected.sigma_d
    assert Decimal(str(result["d_avg"])) == expected.d_avg
    # Confirm it does NOT match the disagreeing series' own statistics.
    from statistics import stdev

    wrong_mean = sum(disagreeing_series) / len(disagreeing_series)
    assert Decimal(str(result["d_avg"])) != wrong_mean


@needs_db
def test_no_quantity_exists_without_a_success_status(session):
    """Never a fabricated zero; an unavailable value is NULL."""
    if not _ran(session):
        pytest.skip("no inventory run")
    for status_column, value_column in (
        (InventoryCalculation.safety_stock_status, InventoryCalculation.safety_stock),
        (InventoryCalculation.rop_status, InventoryCalculation.rop),
        (InventoryCalculation.max_stock_status, InventoryCalculation.max_stock),
    ):
        wrong = session.execute(
            select(func.count())
            .select_from(InventoryCalculation)
            .where(status_column != "SUCCESS", value_column.isnot(None))
        ).scalar()
        assert wrong == 0


@needs_db
def test_no_negative_quantities(session):
    if not _ran(session):
        pytest.skip("no inventory run")
    for column in (
        InventoryCalculation.safety_stock,
        InventoryCalculation.rop,
        InventoryCalculation.max_stock,
    ):
        negative = session.execute(
            select(func.count()).select_from(InventoryCalculation).where(column < 0)
        ).scalar()
        assert negative == 0


@needs_db
def test_max_stock_is_never_configured_on_current_policy(session):
    if not _ran(session):
        pytest.skip("no inventory run")
    computed = session.execute(
        select(func.count())
        .select_from(InventoryCalculation)
        .where(InventoryCalculation.max_stock.isnot(None))
    ).scalar()
    assert computed == 0


@needs_db
def test_run_records_its_inputs_and_formula_version(session):
    if not _ran(session):
        pytest.skip("no inventory run")
    run = session.execute(
        select(InventoryRun).order_by(InventoryRun.id.desc()).limit(1)
    ).scalar_one()
    assert run.formula_version
    assert run.policy_id
    assert run.service_level_configured is False


@needs_db
def test_repeating_the_run_reuses_it(session):
    """Idempotency: the same feature, forecast, policy and formula versions
    identify the same run rather than writing a second copy.

    Run twice back to back rather than relying on whatever run happens to exist:
    the feature-store tests rebuild features during the suite, which legitimately
    changes ``feature_run_id`` and so legitimately warrants a new run. The
    property under test is that *unchanged inputs* reuse, not that a run is
    never created.
    """
    if not _ran(session):
        pytest.skip("no inventory run")
    from app.initiatives.i7.inventory import run_inventory_calculations

    session.commit()

    first = run_inventory_calculations()
    assert first.status == "succeeded"

    second = run_inventory_calculations()
    assert second.reused_existing is True
    assert second.run_id == first.run_id

    session.expire_all()
    rows = session.execute(
        select(func.count())
        .select_from(InventoryCalculation)
        .where(InventoryCalculation.inventory_run_id == first.run_id)
    ).scalar()
    # The reused run holds exactly one set of calculations, not two.
    assert rows == first.calculations
