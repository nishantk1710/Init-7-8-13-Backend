"""Builds the feature store from Phase 2 staging.

    i7_staged_*  ->  builder  ->  i7_material_feature

Reads the staging layer only -- never ``raw_*``. Everything it does per
material-plant is delegated: statistics to :mod:`statistics`, gating and
classification to :mod:`classification`, scope to :mod:`oar_scope`. This module
is orchestration and persistence.

**Consumption is loaded once, grouped in SQL.** 45,409 material-plants each
needing their own series query would be 45,409 round trips. One ordered scan of
the ~13.7k staged consumption rows, grouped in Python as it streams, is a single
pass -- and the staged consumption table is small precisely because Phase 2
aggregated the 233k movements to months already.

**Densification happens here, not in storage.** Staging holds only months that
had movements; ADI counts total periods, so a gap must become an explicit zero
before any statistic is computed. The series is densified over the *extract's*
observation window -- see :func:`_densify` for why the window is the extract's
and not each material's own first-to-last movement.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterator

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.core.upsert import safe_batch_size, upsert
from app.initiatives.i7.contracts import (
    ConsumptionObservation,
    ConsumptionSeries,
    MaterialIdentity,
    MaterialPlantKey,
    PlantIdentity,
)
from app.initiatives.i7.features.classification import (
    DataSufficiency,
    HistoryStatus,
    assess_history,
    classify_demand,
    route_models,
)
from app.initiatives.i7.features.lead_time_provider import resolve_lead_time
from app.initiatives.i7.features.oar_scope import assess_oar_scope
from app.initiatives.i7.features.statistics import demand_statistics
from app.initiatives.i7.policy import PolicyDocument
from app.models.i7_features import FeatureBuildRun, MaterialFeature
from app.models.i7_staging import StagedConsumption, StagingRun

logger = logging.getLogger(__name__)

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

@dataclass
class FeatureBuildResult:
    run_id: int | None = None
    status: str = STATUS_SUCCEEDED
    features: int = 0
    history_status_counts: dict[str, int] = field(default_factory=dict)
    demand_class_counts: dict[str, int] = field(default_factory=dict)
    oar_scope_counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def _next_month(period: date) -> date:
    if period.month == 12:
        return date(period.year + 1, 1, 1)
    return date(period.year, period.month + 1, 1)


def _densify(
    rows: list[tuple[date, Decimal, str | None]],
    key: MaterialPlantKey,
    window: tuple[date, date],
) -> ConsumptionSeries:
    """Fill the observation window with explicit zeros where no demand occurred.

    The series spans the **extract's** window, not the material's own first-to-last
    movement. The Formula Reference defines ``n`` as "total periods (months)" and
    gates on ``total_history_months`` -- both properties of the observation
    window, not of where a particular material happens to have movements.

    Using a per-material window understates ADI, because a material whose first
    movement is late loses its leading zero-demand months from ``n`` while
    keeping every one of its ``n_nz``. Measured on this extract: 3,495 of 4,269
    material-plants started late, one example moving ADI from 1.500 to 1.625.
    Those leading months are real evidence of *no demand*, which is exactly what
    an intermittency measure needs to see.

    The window is the extract's, so a month outside it is still history nobody
    recorded and is never invented.
    """
    by_period = {period: (quantity, unit) for period, quantity, unit in rows}
    unit = next((unit for _, _, unit in rows if unit), None)

    observations: list[ConsumptionObservation] = []
    period, last = window
    while period <= last:
        quantity, row_unit = by_period.get(period, (Decimal(0), unit))
        observations.append(
            ConsumptionObservation(
                period=period,
                # Netting can drive a month negative when a reversal posts after
                # the issue it cancels. Demand is not negative; floor at zero.
                quantity=max(quantity, Decimal(0)),
                unit_of_measure=row_unit or unit,
            )
        )
        period = _next_month(period)

    return ConsumptionSeries(key=key, observations=tuple(observations))


def _load_consumption(session: Session) -> dict[tuple[str, str], list[tuple[date, Decimal, str | None]]]:
    """Every staged consumption row, grouped by material-plant.

    One ordered scan rather than a query per material. The staged table holds
    ~13.7k rows -- Phase 2 already collapsed 233k movements into it -- so this
    is small enough to group in memory, and doing so avoids tens of thousands of
    round trips.
    """
    grouped: dict[tuple[str, str], list[tuple[date, Decimal, str | None]]] = {}
    statement = (
        select(
            StagedConsumption.sap_material_number,
            StagedConsumption.sap_plant_code,
            StagedConsumption.period,
            StagedConsumption.quantity,
            StagedConsumption.unit_of_measure,
        ).order_by(
            StagedConsumption.sap_material_number,
            StagedConsumption.sap_plant_code,
            StagedConsumption.period,
        )
    )
    for material, plant, period, quantity, unit in session.execute(statement).yield_per(5000):
        grouped.setdefault((material, plant), []).append((period, quantity, unit))
    return grouped


def _load_consumption_event_counts(
    session: Session,
) -> dict[tuple[str, str], list[tuple[date, int, int]]]:
    """Every staged consumption row's issue/reversal transaction counts, grouped
    by material-plant.

    Separate from :func:`_load_consumption`, which loads *quantity* for the
    demand statistics (ADI/CV-squared/non_zero_periods). This loads the
    transaction-level counts the SOP 3.1.1 consumption trigger needs -- a
    materially different metric (see ``consumption_count_12m`` on
    ``MaterialFeature``), so it is not derived from ``non_zero_periods``.
    """
    grouped: dict[tuple[str, str], list[tuple[date, int, int]]] = {}
    statement = select(
        StagedConsumption.sap_material_number,
        StagedConsumption.sap_plant_code,
        StagedConsumption.period,
        StagedConsumption.issue_count,
        StagedConsumption.reversal_count,
    ).order_by(
        StagedConsumption.sap_material_number,
        StagedConsumption.sap_plant_code,
        StagedConsumption.period,
    )
    for material, plant, period, issue_count, reversal_count in session.execute(
        statement
    ).yield_per(5000):
        grouped.setdefault((material, plant), []).append(
            (period, issue_count, reversal_count)
        )
    return grouped


def _months_back(reference: date, months: int) -> date:
    """The first day of the month ``months`` months before ``reference``."""
    total = reference.year * 12 + (reference.month - 1) - months
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)


def consumption_count_12m(
    rows: list[tuple[date, int, int]] | None, reference: date | None
) -> int:
    """MSEG issue transactions minus reversal transactions, trailing 12 months
    ending at ``reference`` (the extract's own last staged month).

    Strictly a transaction-level count -- never converted into a count of
    non-zero months, and never derived from ``non_zero_periods``. Floored at
    zero: a month where reversals were staged without their matching issue
    (e.g. the issue predates the extract) must not make the trailing count
    negative.
    """
    if not rows or reference is None:
        return 0
    window_start = _months_back(reference, 11)
    total = sum(
        issue_count - reversal_count
        for period, issue_count, reversal_count in rows
        if window_start <= period <= reference
    )
    return max(total, 0)


def observation_window(session: Session) -> tuple[date, date] | None:
    """The extract's demand observation window: first to last staged month.

    Derived from the data rather than configured, because it is a property of
    what was delivered. Returns ``None`` when nothing is staged, which is the
    only case where no window exists.
    """
    low, high = session.execute(
        select(func.min(StagedConsumption.period), func.max(StagedConsumption.period))
    ).one()
    if low is None or high is None:
        return None
    return low, high


@dataclass(frozen=True)
class _StockPosition:
    current_stock: Decimal | None
    quality_inspection_stock: Decimal | None
    blocked_stock: Decimal | None
    stock_in_transfer: Decimal | None
    restricted_use_stock: Decimal | None
    returns_stock: Decimal | None
    storage_location_count: int


_STOCK_SQL = """
    SELECT sap_material_number, sap_plant_code,
           SUM(unrestricted_use_stock) AS current_stock,
           SUM(quality_inspection_stock) AS quality_inspection_stock,
           SUM(blocked_stock) AS blocked_stock,
           SUM(stock_in_transfer) AS stock_in_transfer,
           SUM(restricted_use_stock) AS restricted_use_stock,
           SUM(returns_stock) AS returns_stock,
           COUNT(*) AS storage_location_count
      FROM i7_staged_stock
     GROUP BY 1, 2
"""


def _load_stock(session: Session) -> dict[tuple[str, str], _StockPosition]:
    """Stock position per material-plant, summed across storage locations.

    Aggregated in SQL rather than per material-plant queries, for the same
    reason as :func:`_load_purchase_order_counts`. ``SUM`` over an all-NULL
    group returns NULL, not 0 -- a material-plant whose MARD rows never
    populated a given column stays unknown rather than reading as empty stock.
    """
    positions: dict[tuple[str, str], _StockPosition] = {}
    for row in session.execute(text(_STOCK_SQL)):
        positions[(row.sap_material_number, row.sap_plant_code)] = _StockPosition(
            current_stock=row.current_stock,
            quality_inspection_stock=row.quality_inspection_stock,
            blocked_stock=row.blocked_stock,
            stock_in_transfer=row.stock_in_transfer,
            restricted_use_stock=row.restricted_use_stock,
            returns_stock=row.returns_stock,
            storage_location_count=row.storage_location_count,
        )
    return positions


def _load_purchase_order_counts(session: Session) -> dict[tuple[str, str], int]:
    """PO lines per material-plant that have a usable duration.

    Counted in SQL. Availability only -- the 1-730 day window and the tiering
    are Phase 5 policy, so nothing is filtered on plausibility here.
    """
    statement = text(
        """
        SELECT sap_material_number, sap_plant_code, COUNT(*) AS n
          FROM i7_staged_purchase_order
         WHERE lead_time_days IS NOT NULL
           AND is_cancelled = false
         GROUP BY 1, 2
        """
    )
    return {(row[0], row[1]): row[2] for row in session.execute(statement)}


_ATTRIBUTE_SQL = """
    WITH universe AS (
        -- The material-plant universe is every key MARC (i7_staged_material_plant)
        -- OR MARD (i7_staged_stock) has ever seen for this material-plant --
        -- not MARC alone. MARC currently holds plants 1300/1200 only (zero rows
        -- for Gamsberg/1500), while MARD is multi-plant; driving this query from
        -- MARC alone silently drops every Gamsberg material-plant, including its
        -- stock, from the feature store entirely. See README's "Plant coverage
        -- gap" section: MARC-dependent fields (MRP type, ROP, lead time, OAR
        -- classification) are correctly unavailable for Gamsberg until MARC is
        -- re-extracted -- but that is a reason for those specific columns to be
        -- NULL, not a reason for the material-plant row itself to not exist.
        SELECT sap_material_number, sap_plant_code FROM i7_staged_material_plant
        UNION
        SELECT sap_material_number, sap_plant_code FROM i7_staged_stock
    )
    SELECT u.sap_material_number,
           u.sap_plant_code,
           p.mrp_type,
           p.current_safety_stock,
           p.current_reorder_point,
           p.current_maximum_stock,
           p.planned_delivery_time_days,
           m.material_status,
           m.criticality,
           m.unit_price,
           m.currency
      FROM universe u
      LEFT JOIN i7_staged_material_plant p
             ON p.sap_material_number = u.sap_material_number
            AND p.sap_plant_code = u.sap_plant_code
      LEFT JOIN i7_staged_material m
             ON m.sap_material_number = u.sap_material_number
     ORDER BY u.sap_material_number, u.sap_plant_code
"""


def _upsert(session: Session, rows: list[dict[str, Any]]) -> None:
    """Insert a batch, updating on the material-plant natural key.

    An atomic upsert for the same reason as Phase 2: idempotency must be a
    database property, since an application-level existence check races with a
    concurrent build. See :mod:`app.core.upsert`.
    """
    upsert(session, MaterialFeature, rows, ["sap_material_number", "sap_plant_code"])


def build_features(policy: PolicyDocument | None = None) -> FeatureBuildResult:
    """Compute features for every staged material-plant.

    Idempotent: a second build converges on the same rows.
    """
    policy = policy or PolicyDocument()
    session_factory = get_sessionmaker()

    with session_factory() as session:
        movement_types = session.execute(
            select(StagingRun.consumption_movement_types)
            .where(StagingRun.status == "succeeded")
            .order_by(StagingRun.id.desc())
            .limit(1)
        ).scalar()

        run = FeatureBuildRun(
            status="running",
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            consumption_movement_types=movement_types,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    result = FeatureBuildResult(run_id=run_id)

    try:
        with session_factory() as session:
            logger.info("feature run %d: loading consumption", run_id)
            consumption = _load_consumption(session)
            consumption_events = _load_consumption_event_counts(session)
            purchase_orders = _load_purchase_order_counts(session)
            stock = _load_stock(session)
            window = observation_window(session)

            logger.info("feature run %d: computing features", run_id)
            batch_size = safe_batch_size(MaterialFeature, 2000, session.get_bind().dialect.name)
            batch: list[dict[str, Any]] = []
            built = 0

            for row in session.execute(text(_ATTRIBUTE_SQL)).yield_per(5000):
                feature = _build_one(
                    row,
                    consumption,
                    consumption_events,
                    purchase_orders,
                    stock,
                    window,
                    policy,
                    run_id,
                    result,
                )
                batch.append(feature)
                if len(batch) >= batch_size:
                    _upsert(session, batch)
                    built += len(batch)
                    batch = []

            if batch:
                _upsert(session, batch)
                built += len(batch)

            result.features = built
            session.commit()

        with session_factory() as session:
            stored = session.get(FeatureBuildRun, run_id)
            stored.status = STATUS_SUCCEEDED
            stored.features_built = result.features
            stored.finished_at = datetime.now(timezone.utc)
            session.commit()

        logger.info(
            "feature run %d: %d features -- classes %s, OAR %s",
            run_id,
            result.features,
            result.demand_class_counts,
            result.oar_scope_counts,
        )
        return result

    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("feature run %d failed: %s", run_id, detail)
        result.status = STATUS_FAILED
        result.error = detail
        try:
            with session_factory() as session:
                stored = session.get(FeatureBuildRun, run_id)
                stored.status = STATUS_FAILED
                stored.error = detail[:4000]
                stored.finished_at = datetime.now(timezone.utc)
                session.commit()
        except Exception:
            logger.exception("feature run %d: could not record the failure either", run_id)
        return result


def _build_one(
    row: Any,
    consumption: dict[tuple[str, str], list[tuple[date, Decimal, str | None]]],
    consumption_events: dict[tuple[str, str], list[tuple[date, int, int]]],
    purchase_orders: dict[tuple[str, str], int],
    stock: dict[tuple[str, str], _StockPosition],
    window: tuple[date, date] | None,
    policy: PolicyDocument,
    run_id: int,
    result: FeatureBuildResult,
) -> dict[str, Any]:
    """One material-plant's features."""
    from app.initiatives.i7.contracts import MaterialAttributes

    material = row.sap_material_number
    plant = row.sap_plant_code
    key = MaterialPlantKey(
        material=MaterialIdentity(sap_material_number=material),
        plant=PlantIdentity(sap_plant_code=plant),
    )

    rows = consumption.get((material, plant))
    if rows and window is not None:
        series = _densify(rows, key, window)
    else:
        # No demand movements at all. An empty series rather than a window of
        # zeros: "never consumed" is NO_HISTORY, a different state from "in the
        # window and did not move", and conflating them would send 41,140
        # material-plants through the gate as though they had been observed.
        series = ConsumptionSeries(key=key, observations=())

    statistics = demand_statistics(series)
    event_rows = consumption_events.get((material, plant))
    reference_date = window[1] if window is not None else None
    count_12m = consumption_count_12m(event_rows, reference_date)
    history = assess_history(statistics, policy.history_gate, policy.confidence)

    # Classification only runs when the gate passed. A cold-start material has
    # too little evidence for ADI and CV-squared to mean anything, and labelling
    # it anyway would give the OAR path a demand class it never earned.
    if history.status is HistoryStatus.SUFFICIENT:
        demand_class = classify_demand(
            statistics.adi.value, statistics.cv_squared.value, policy.classification
        )
    else:
        from app.initiatives.i7.contracts import DemandPattern

        demand_class = DemandPattern.UNCLASSIFIED

    routing = route_models(demand_class)

    attributes = MaterialAttributes(
        key=key,
        mrp_type=row.mrp_type,
        material_status=row.material_status,
    )
    oar = assess_oar_scope(attributes, policy.oar)

    order_count = purchase_orders.get((material, plant), 0)
    stock_position = stock.get((material, plant))

    lead_time = resolve_lead_time(key, row.planned_delivery_time_days)

    result.history_status_counts[history.status.value] = (
        result.history_status_counts.get(history.status.value, 0) + 1
    )
    result.demand_class_counts[demand_class.value] = (
        result.demand_class_counts.get(demand_class.value, 0) + 1
    )
    result.oar_scope_counts[oar.scope.value] = (
        result.oar_scope_counts.get(oar.scope.value, 0) + 1
    )

    return {
        "sap_material_number": material,
        "sap_plant_code": plant,
        "total_periods": statistics.total_periods,
        "non_zero_periods": statistics.non_zero_periods,
        "consumption_count_12m": count_12m,
        "first_period": series.observations[0].period if series.observations else None,
        "last_period": series.observations[-1].period if series.observations else None,
        "history_months": statistics.total_periods,
        "history_status": history.status.value,
        "data_sufficiency": history.sufficiency.value,
        "required_history_months": history.required_months,
        "total_demand": statistics.total_demand if statistics.total_periods else None,
        "mean_demand_all_periods": statistics.mean_all_periods,
        "std_dev_demand_all_periods": statistics.std_dev_all_periods,
        "mean_non_zero_demand": statistics.mean_non_zero,
        "std_dev_non_zero_demand": statistics.std_dev_non_zero,
        "adi": statistics.adi.value,
        "adi_status": statistics.adi.status.value,
        "cv_squared": statistics.cv_squared.value,
        "cv_squared_status": statistics.cv_squared.status.value,
        "demand_class": demand_class.value,
        "baseline_model": routing.baseline.value if routing.baseline else None,
        "challenger_model": routing.challenger.value if routing.challenger else None,
        "routing_reason": routing.reason[:255],
        "oar_scope": oar.scope.value,
        "oar_reason": oar.reason[:255],
        "oar_rollup_status": oar.rollup_status,
        # Absent criticality stays absent -- never defaulted to NORMAL.
        "criticality": row.criticality,
        "mrp_type": row.mrp_type,
        "material_status": row.material_status,
        "unit_price": row.unit_price,
        "currency": row.currency,
        "has_unit_price": row.unit_price is not None,
        "has_criticality": row.criticality is not None,
        "has_mrp_type": row.mrp_type is not None,
        "has_material_status": row.material_status is not None,
        "has_consumption": bool(rows),
        "has_lead_time": order_count > 0,
        "purchase_order_count": order_count,
        "current_safety_stock": row.current_safety_stock,
        "current_reorder_point": row.current_reorder_point,
        "current_maximum_stock": row.current_maximum_stock,
        "planned_delivery_time_days": row.planned_delivery_time_days,
        "lead_time_source": lead_time.source.value,
        "lead_time_days": lead_time.lead_time_days,
        "current_stock": stock_position.current_stock if stock_position else None,
        "quality_inspection_stock": (
            stock_position.quality_inspection_stock if stock_position else None
        ),
        "blocked_stock": stock_position.blocked_stock if stock_position else None,
        "stock_in_transfer": stock_position.stock_in_transfer if stock_position else None,
        "restricted_use_stock": (
            stock_position.restricted_use_stock if stock_position else None
        ),
        "returns_stock": stock_position.returns_stock if stock_position else None,
        "has_stock_data": stock_position is not None,
        "storage_location_count": (
            stock_position.storage_location_count if stock_position else 0
        ),
        "feature_run_id": run_id,
    }
