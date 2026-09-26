"""Reusable Current/I11 baseline vs I07 recommendation comparison.

One small function, called once per metric (Safety Stock, ROP, Max Stock,
Lead Time) from ``service.py``, rather than four near-duplicate aggregation
blocks inline in the service or the schema layer.

By the 2026-09-21 product decision (see ``app/schemas/i7/reports.py``
Section 12 docstring), the "I11 baseline" for this report is I07's own
already-persisted current-state columns on ``i7_recommendation``
(``current_safety_stock`` / ``current_rop`` / ``current_max_stock``) and the
MARC-PLIFZ lead time -- never a literal I11 system read, and never
``I11LeadTimeProvider``. This module does not know or care which columns it
is called with; it is pure comparison arithmetic over two already-persisted
numeric columns on the same row.

Nothing here recomputes anything Phase 5/7 already calculated -- it only
aggregates persisted values and counts population/coverage.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.i7_recommendation import Recommendation
from app.schemas.i7.reports import AvailabilityStatus, BaselineComparisonRow

NOT_EVALUABLE_STATUS = "NOT_EVALUABLE"
"""The ``Recommendation.status`` value meaning the record's own lifecycle
prevented any value from being computed at all -- distinct from "computed and
null". See ``BaselineComparisonRow.not_evaluable_count``."""


def compare_metric(
    session: Session,
    base: Select,
    *,
    metric: str,
    baseline_column,
    recommendation_column,
    self_referential: bool = False,
) -> BaselineComparisonRow:
    """Compute one metric's baseline-vs-I07 comparison row.

    ``base`` is a ``select(Recommendation)`` statement already scoped to the
    report's period/filters -- this function only adds ``WHERE``/aggregate
    clauses on top of it, never re-derives the row set itself.

    ``baseline_column`` / ``recommendation_column`` are ORM columns on
    ``Recommendation`` (e.g. ``Recommendation.current_safety_stock`` /
    ``Recommendation.recommended_safety_stock``). Passing the same column for
    both is valid and expected for Lead Time, where I07 uses the MARC-PLIFZ
    value directly as its own input (see the Lead Time call site in
    ``service.py``) -- the two sides then always agree on every populated row
    by construction, which the caller is expected to note in
    ``i07_lead_time_source`` rather than this function fabricating a
    difference that does not exist.

    Counts are broken out per the reporting requirement, never collapsed:
    - ``both_available_count``: both columns populated on the same row.
    - ``baseline_missing_count``: baseline column NULL (I07 side may or may
      not be populated).
    - ``recommendation_missing_count``: I07 column NULL (baseline side may or
      may not be populated).
    - ``not_evaluable_count``: rows whose ``status`` is ``NOT_EVALUABLE`` --
      counted once, independently of the population counts above (a
      NOT_EVALUABLE row can still have either/both columns populated; its
      lifecycle status is a separate fact from column population).
    """
    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()

    baseline_missing = session.execute(
        select(func.count()).select_from(
            base.where(baseline_column.is_(None)).subquery()
        )
    ).scalar_one()
    recommendation_missing = session.execute(
        select(func.count()).select_from(
            base.where(recommendation_column.is_(None)).subquery()
        )
    ).scalar_one()
    not_evaluable = session.execute(
        select(func.count()).select_from(
            base.where(Recommendation.status == NOT_EVALUABLE_STATUS).subquery()
        )
    ).scalar_one()

    both_base = base.where(baseline_column.isnot(None), recommendation_column.isnot(None))
    both_available = session.execute(
        select(func.count()).select_from(both_base.subquery())
    ).scalar_one()

    baseline_populated_base = base.where(baseline_column.isnot(None))
    baseline_populated = total - baseline_missing
    baseline_value = None
    if baseline_populated > 0:
        baseline_value = session.execute(
            baseline_populated_base.with_only_columns(func.avg(baseline_column))
        ).scalar_one()

    recommendation_populated_base = base.where(recommendation_column.isnot(None))
    recommendation_populated = total - recommendation_missing
    recommendation_value = None
    if recommendation_populated > 0:
        recommendation_value = session.execute(
            recommendation_populated_base.with_only_columns(func.avg(recommendation_column))
        ).scalar_one()

    delta: Decimal | None = None
    delta_percentage: Decimal | None = None
    if both_available > 0:
        delta = session.execute(
            both_base.with_only_columns(func.avg(recommendation_column - baseline_column))
        ).scalar_one()

        # Percentage delta is computed row-by-row (delta_row / baseline_row),
        # not from the two aggregates above, and only over rows where the
        # baseline value is non-zero -- guarding divide-by-zero explicitly
        # rather than letting a SQL division error surface or silently
        # skipping the whole metric.
        nonzero_base = both_base.where(baseline_column != 0)
        nonzero_count = session.execute(
            select(func.count()).select_from(nonzero_base.subquery())
        ).scalar_one()
        if nonzero_count > 0:
            delta_percentage = session.execute(
                nonzero_base.with_only_columns(
                    func.avg(
                        (recommendation_column - baseline_column) / baseline_column * 100
                    )
                )
            ).scalar_one()

    availability_status = (
        AvailabilityStatus.AVAILABLE if both_available > 0 else AvailabilityStatus.NOT_AVAILABLE
    )

    return BaselineComparisonRow(
        metric=metric,
        baseline_value=baseline_value,
        recommendation_value=recommendation_value,
        delta=delta,
        delta_percentage=delta_percentage,
        both_available_count=both_available,
        baseline_missing_count=baseline_missing,
        recommendation_missing_count=recommendation_missing,
        not_evaluable_count=not_evaluable,
        availability_status=availability_status,
        self_referential=self_referential,
    )
