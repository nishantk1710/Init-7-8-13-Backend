"""Recording the trend history: one row per generation per plant scope.

Called once, at the end of a **successful** ``generate_recommendations``. It computes the
headline KPIs with the same functions the Overview uses (``kpi_metrics``) for ``ALL``, ``1300``
and ``1500`` and saves them as ``captured`` rows.

Guarantees, because a history that is wrong is worse than none:

* **All or nothing.** The three scopes are computed before any is written and committed
  together, so a failure part-way leaves no snapshot rather than a half-set that makes one
  plant look different from the whole.
* **Idempotent.** ``(generation_key, plant_scope)`` is unique. A retry, a repeated call or two
  workers racing find the row already there and do nothing; a lost race surfaces as an
  IntegrityError, which is read as "someone else recorded it" and rolled back, not as a failure.
* **Never fabricates.** Excess and stockout figures come from the live calculation at the moment
  the generation finished. Nothing here estimates an earlier date.
* **Visible.** The caller logs a failure at ERROR and carries it on the run result.

It does not run a pipeline, and the dashboard API never calls it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.initiatives.i7.recommendations.kpi_metrics import (
    base_for_scope,
    compute_kpi_metrics,
    portfolio_currency,
)
from app.models.i7_trend import (
    SCOPE_ALL,
    SOURCE_CAPTURED,
    TREND_PLANT_SCOPES,
    RecommendationTrendSnapshot,
)

logger = logging.getLogger(__name__)


def generation_key(
    *,
    inventory_run_id: int | None,
    oar_run_id: int | None,
    forecast_run_id: int | None,
    formula_version: str,
    policy_id: str,
    policy_version: int,
) -> str:
    """Stable identity of one generation, from what it read and how it calculated.

    Two generations with the same key would produce the same recommendations (the generator
    itself skips rows whose inputs already exist), so they are one history point. A missing run
    is written ``none``, never ``0``.
    """

    def part(value: int | None) -> str:
        return "none" if value is None else str(value)

    return (
        f"inv={part(inventory_run_id)}|oar={part(oar_run_id)}|fc={part(forecast_run_id)}"
        f"|formula={formula_version}|policy={policy_id}:{policy_version}"
    )


@dataclass
class SnapshotOutcome:
    created: list[str] = field(default_factory=list)
    already_recorded: list[str] = field(default_factory=list)


def record_generation_snapshots(
    session: Session,
    *,
    key: str,
    inventory_run_id: int | None,
    oar_run_id: int | None,
    forecast_run_id: int | None,
    formula_version: str | None,
    captured_at: datetime | None = None,
) -> SnapshotOutcome:
    """Save the ALL / 1300 / 1500 snapshots for one finished generation, once."""
    outcome = SnapshotOutcome()
    existing = set(
        session.execute(
            select(RecommendationTrendSnapshot.plant_scope).where(RecommendationTrendSnapshot.generation_key == key)
        )
        .scalars()
        .all()
    )
    missing = [scope for scope in TREND_PLANT_SCOPES if scope not in existing]
    outcome.already_recorded = [scope for scope in TREND_PLANT_SCOPES if scope in existing]
    if not missing:
        return outcome

    when = captured_at or datetime.now(timezone.utc)
    currency = portfolio_currency(session)
    # Computed first, written together: a failure here leaves nothing behind.
    rows = []
    for scope in missing:
        plant = None if scope == SCOPE_ALL else scope
        metrics = compute_kpi_metrics(session, base_for_scope(plant))
        rows.append(
            RecommendationTrendSnapshot(
                generation_key=key,
                plant_scope=scope,
                source=SOURCE_CAPTURED,
                captured_at=when,
                inventory_run_id=inventory_run_id,
                oar_run_id=oar_run_id,
                forecast_run_id=forecast_run_id,
                formula_version=formula_version,
                total_count=metrics.total,
                stockout_risk_count=metrics.stockout_risk_count,
                excess_count=metrics.excess_count,
                excess_opportunity=metrics.excess_opportunity,
                net_safety_stock_value_impact=metrics.net_safety_stock_value_impact,
                currency=currency,
            )
        )

    session.add_all(rows)
    try:
        session.commit()
    except IntegrityError:
        # Another writer recorded this generation between our check and our insert. The
        # unique constraint did its job; there is nothing to repair.
        session.rollback()
        logger.info("trend snapshots for %s were recorded concurrently; nothing to add", key)
        outcome.already_recorded = list(TREND_PLANT_SCOPES)
        return outcome

    outcome.created = missing
    logger.info("trend snapshots recorded for %s: %s", key, ", ".join(missing))
    return outcome
