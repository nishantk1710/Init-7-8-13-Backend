"""Record today's trend point from the recommendations that already exist -- generating nothing.

    python -m app.initiatives.i7.recommendations.trend_snapshot_now            # dry run: prints, writes nothing
    python -m app.initiatives.i7.recommendations.trend_snapshot_now --apply    # records the snapshot

For giving the trend cards their first real point without running a pipeline or the recommendation
generator. It reads the recommendations as they stand, computes the same KPIs as the Overview, and
saves them under the same generation key the generator would use for these upstream runs. So when
the next real run happens on unchanged inputs it finds the snapshot already there, and when the
inputs have moved on it records a new one.

**It writes only to ``i7_recommendation_trend_snapshot``.** It never writes, updates or deletes a
recommendation, and never starts a pipeline stage.

**It refuses when it would mislabel.** The key names the latest inventory and OAR runs. If the
current recommendations were built from older runs (a newer run exists that has not been turned into
recommendations yet), the snapshot would describe one generation under another's name, so it stops
and says why instead.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.initiatives.i7.recommendations import repository
from app.initiatives.i7.recommendations.kpi_metrics import base_for_scope, compute_kpi_metrics
from app.initiatives.i7.recommendations.trend_snapshots import generation_key, record_generation_snapshots
from app.models.i7_recommendation import Recommendation
from app.models.i7_trend import RecommendationTrendSnapshot, SCOPE_ALL, TREND_PLANT_SCOPES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurrentGeneration:
    key: str
    inventory_run_id: int | None
    oar_run_id: int | None
    forecast_run_id: int | None
    formula_version: str


class NotRecordable(Exception):
    """The current recommendations cannot be honestly labelled as one generation."""


def current_generation(session: Session) -> CurrentGeneration:
    """The generation the recommendations on file belong to, or ``NotRecordable`` with the reason."""
    newest = session.execute(select(Recommendation).order_by(Recommendation.id.desc()).limit(1)).scalar_one_or_none()
    if newest is None:
        raise NotRecordable("there are no recommendations yet")

    inventory_run_id = repository.latest_inventory_run(session)
    oar_run_id = repository.latest_oar_run(session)
    forecast_run_id = (
        repository.forecast_run_for_inventory_run(session, inventory_run_id) if inventory_run_id is not None else None
    )

    def built_from(column, run_id: int | None) -> bool:
        if run_id is None:
            return True
        return session.execute(select(func.count()).where(column == run_id)).scalar_one() > 0

    if not built_from(Recommendation.inventory_run_id, inventory_run_id) or not built_from(
        Recommendation.oar_run_id, oar_run_id
    ):
        raise NotRecordable(
            f"the current recommendations were not built from the latest runs (inventory run "
            f"{inventory_run_id}, OAR run {oar_run_id}); a newer run exists that has not been turned into "
            "recommendations yet. Recording now would put one generation's figures under another's name."
        )

    return CurrentGeneration(
        key=generation_key(
            inventory_run_id=inventory_run_id,
            oar_run_id=oar_run_id,
            forecast_run_id=forecast_run_id,
            formula_version=newest.formula_version,
            policy_id=newest.policy_id,
            policy_version=newest.policy_version,
        ),
        inventory_run_id=inventory_run_id,
        oar_run_id=oar_run_id,
        forecast_run_id=forecast_run_id,
        formula_version=newest.formula_version,
    )


def render(session: Session, generation: CurrentGeneration) -> str:
    recorded = set(
        session.execute(
            select(RecommendationTrendSnapshot.plant_scope).where(RecommendationTrendSnapshot.generation_key == generation.key)
        )
        .scalars()
        .all()
    )
    lines = [f"generation: {generation.key}", "scope   total  stockout_risk  excess  opportunity      status"]
    for scope in TREND_PLANT_SCOPES:
        metrics = compute_kpi_metrics(session, base_for_scope(None if scope == SCOPE_ALL else scope))
        lines.append(
            f"{scope:<6} {metrics.total:>6} {metrics.stockout_risk_count:>14} {metrics.excess_count:>7} "
            f"{metrics.excess_opportunity!s:>12}      {'already recorded' if scope in recorded else 'would be recorded'}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="record the snapshot (default: print only)")
    args = parser.parse_args(argv)

    with get_sessionmaker()() as session:
        try:
            generation = current_generation(session)
        except NotRecordable as exc:
            print(f"Nothing recorded: {exc}")
            return 1
        print(render(session, generation))
        if not args.apply:
            print("\nDry run: nothing written. Re-run with --apply to record these figures.")
            return 0
        outcome = record_generation_snapshots(
            session,
            key=generation.key,
            inventory_run_id=generation.inventory_run_id,
            oar_run_id=generation.oar_run_id,
            forecast_run_id=generation.forecast_run_id,
            formula_version=generation.formula_version,
        )
        print(f"\nRecorded: {', '.join(outcome.created) or 'nothing new'}; already there: {', '.join(outcome.already_recorded) or 'none'}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
