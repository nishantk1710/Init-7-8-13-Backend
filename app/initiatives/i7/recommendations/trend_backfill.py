"""Rebuild the *excess-inventory* history from the recommendation rows already stored.

    python -m app.initiatives.i7.recommendations.trend_backfill            # dry run: prints, writes nothing
    python -m app.initiatives.i7.recommendations.trend_backfill --apply    # writes the reconstructed rows

Not part of the migration and never run automatically: it is a one-off an operator runs after
reading the dry-run.

**What can be rebuilt, and what cannot**

* Excess count and opportunity: yes. Every recommendation row keeps its ``current_max_stock``,
  ``recommended_max_stock`` and ``unit_price``, and older generations' rows are never deleted.
  The state "as of" a generation is the newest row of each material-plant at or below that
  generation's last row id -- exactly what the Overview showed right after that run.
* Stockout count: **no**. It needs the on-hand stock of that day. ``i7_staged_stock`` is
  overwritten every staging run and a recommendation does not store on-hand, so the count is
  stored as NULL on these rows, never as a number taken from today's stock.

**What a reconstructed point is allowed to claim**

A generation is rebuilt only if at least one of its material-plants has both a current and a
recommended max stock; otherwise "0 excess" would mean "nothing was calculated" and be read as
"nothing was excessive". Money is summed only over rows with a price, as the Overview does. The
currency is left NULL -- which currency applied on that day is not recorded.

**Known limits, reported by the dry run**

* An ADJUST edits ``recommended_*`` in place; a row adjusted after its generation reconstructs
  with the adjusted value (the count is printed as ``adjusted``).
* A generation is identified by ``(feature_run_id, formula_version, policy_version)``. Two
  recommendation runs on the same feature run and formula would merge into one point, the state
  after the later one.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.initiatives.i7.recommendations.kpi_metrics import (
    base_for_scope,
    count_rows,
    excess_base,
    excess_opportunity,
    net_safety_stock_value_impact,
)
from app.models.i7_recommendation import Recommendation
from app.models.i7_trend import (
    SCOPE_ALL,
    SOURCE_RECONSTRUCTED,
    TREND_PLANT_SCOPES,
    RecommendationTrendSnapshot,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Generation:
    feature_run_id: int | None
    formula_version: str
    policy_version: int
    first_id: int
    last_id: int
    generated_at: datetime
    row_count: int

    @property
    def key(self) -> str:
        feature = "none" if self.feature_run_id is None else str(self.feature_run_id)
        return f"hist|feature={feature}|formula={self.formula_version}|policy={self.policy_version}"


@dataclass(frozen=True)
class Plan:
    generation: Generation
    reconstructable: bool
    reason: str
    rows: tuple[RecommendationTrendSnapshot, ...] = ()
    excess_without_price: int = 0
    adjusted: int = 0


def find_generations(session: Session) -> list[Generation]:
    """Every distinct generation in the table, oldest first."""
    result = session.execute(
        select(
            Recommendation.feature_run_id,
            Recommendation.formula_version,
            Recommendation.policy_version,
            func.min(Recommendation.id),
            func.max(Recommendation.id),
            func.max(Recommendation.generated_at),
            func.count(),
        )
        .group_by(Recommendation.feature_run_id, Recommendation.formula_version, Recommendation.policy_version)
        .order_by(func.min(Recommendation.id))
    ).all()
    return [Generation(*row) for row in result]


def _computable(session: Session, base: Select) -> int:
    """Material-plants in the population with both max stocks present."""
    return count_rows(
        session,
        base.where(
            Recommendation.current_max_stock.isnot(None),
            Recommendation.recommended_max_stock.isnot(None),
        ),
    )


def plan_generation(session: Session, generation: Generation) -> Plan:
    """What would be recorded for one generation, or why it cannot be."""
    whole = base_for_scope(None, up_to_id=generation.last_id)
    if _computable(session, whole) == 0:
        return Plan(
            generation,
            False,
            "no material-plant had both a current and a recommended max stock -- "
            "0 excess would mean 'not calculated', so nothing is recorded",
        )

    rows = []
    for scope in TREND_PLANT_SCOPES:
        base = base_for_scope(None if scope == SCOPE_ALL else scope, up_to_id=generation.last_id)
        rows.append(
            RecommendationTrendSnapshot(
                generation_key=generation.key,
                plant_scope=scope,
                source=SOURCE_RECONSTRUCTED,
                captured_at=generation.generated_at,
                feature_run_id=generation.feature_run_id,
                formula_version=generation.formula_version,
                total_count=count_rows(session, base),
                stockout_risk_count=None,  # not reconstructable: see the module docstring
                excess_count=count_rows(session, excess_base(base)),
                excess_opportunity=excess_opportunity(session, base),
                net_safety_stock_value_impact=net_safety_stock_value_impact(session, base),
                currency=None,
            )
        )

    excess_rows = excess_base(whole)
    return Plan(
        generation,
        True,
        "ok",
        tuple(rows),
        excess_without_price=count_rows(session, excess_rows.where(Recommendation.unit_price.is_(None))),
        adjusted=count_rows(session, whole.where(Recommendation.adjustment_count > 0)),
    )


def build_plan(session: Session) -> list[Plan]:
    existing = set(session.execute(select(RecommendationTrendSnapshot.generation_key)).scalars().all())
    plans = []
    for generation in find_generations(session):
        if generation.key in existing:
            plans.append(Plan(generation, False, "already recorded"))
        else:
            plans.append(plan_generation(session, generation))
    return plans


def apply_plan(session: Session, plans: list[Plan]) -> int:
    """Write every reconstructable generation, once; returns how many rows were added."""
    rows = [row for plan in plans if plan.reconstructable for row in plan.rows]
    session.add_all(rows)
    session.commit()
    return len(rows)


def render(plans: list[Plan]) -> str:
    lines = ["generation (feature run | formula | policy)        at                 rows  result"]
    for plan in plans:
        g = plan.generation
        head = f"{g.feature_run_id!s:>6} | {g.formula_version} | v{g.policy_version}"
        if not plan.reconstructable:
            lines.append(f"{head:<50} {g.generated_at:%Y-%m-%d %H:%M} {g.row_count:>6}  SKIP: {plan.reason}")
            continue
        whole = next(row for row in plan.rows if row.plant_scope == SCOPE_ALL)
        lines.append(
            f"{head:<50} {g.generated_at:%Y-%m-%d %H:%M} {g.row_count:>6}  "
            f"excess={whole.excess_count} of {whole.total_count}, opportunity={whole.excess_opportunity}, "
            f"excess_without_price={plan.excess_without_price}, adjusted={plan.adjusted}"
        )
    ok = sum(1 for plan in plans if plan.reconstructable)
    lines.append(f"\n{ok} of {len(plans)} generations can be reconstructed (x3 plant scopes each).")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the rows (default: print only)")
    args = parser.parse_args(argv)
    with get_sessionmaker()() as session:
        plans = build_plan(session)
        print(render(plans))
        if not args.apply:
            print("\nDry run: nothing written. Re-run with --apply to write the reconstructable rows.")
            return 0
        added = apply_plan(session, plans)
        print(f"\nWrote {added} reconstructed snapshot rows.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
