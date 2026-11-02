"""Saving, applying and previewing KPI formula versions.

**Save & Apply** is one action with two outcomes:

* the usage window is unchanged -- nothing needs rebuilding, so the new version is ``active`` at once;
* the usage window changed -- the version is saved as ``applying`` and :func:`run_rebuild` rebuilds
  ``i7_stockout_risk_input`` for it in the background; only when that finishes does it become
  ``active``. Until then the Overview keeps using the previous version against the old usage table, so
  the two are never mismatched. A rebuild that fails leaves the previous version active and the old
  usage table untouched (delete + insert is one transaction).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.initiatives.i7.kpi_formulas.resolved import (
    ACTIVE,
    Formulas,
    FormulaValidationError,
    from_document,
    load_active,
    newest_active,
    validate,
)
from app.initiatives.i7.kpi_formulas.sql import excess_condition_sql, excess_value_sql
from app.initiatives.i7.kpi_formulas.working_capital import working_capital_impact
from app.models.i7_kpi_formula import KpiFormulaVersion
from app.models.i7_recommendation import Recommendation
from app.models.i7_stockout_risk import StockoutRiskInput

logger = logging.getLogger(__name__)

APPLYING = "applying"
SUPERSEDED = "superseded"
FAILED = "failed"

#: A rebuild that has not reported back after this long died with its process.
STALE_AFTER = timedelta(minutes=30)


class ApplyInProgress(RuntimeError):
    """Another version is still being rebuilt."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class NotSetUp(RuntimeError):
    """The ``i7_kpi_formula_version`` table does not exist: the migration has not been applied to this
    database (a deploy does not run migrations unless told to)."""


def schema_ready(session: Session) -> bool:
    """Whether the versions table exists. Rolls back on a miss, so the session stays usable."""
    try:
        session.execute(select(KpiFormulaVersion.id).limit(1))
        return True
    except SQLAlchemyError:
        session.rollback()
        return False


def _expire_stale(session: Session) -> None:
    try:
        rows = session.execute(select(KpiFormulaVersion).where(KpiFormulaVersion.status == APPLYING)).scalars().all()
    except SQLAlchemyError:
        session.rollback()  # no table yet: nothing can be applying
        return
    for row in rows:
        if _now() - _aware(row.created_at) > STALE_AFTER:
            row.status = FAILED
            row.error = "The rebuild did not report back and was abandoned. Apply again."
    session.flush()


def applying_version(session: Session) -> KpiFormulaVersion | None:
    _expire_stale(session)
    try:
        return session.execute(
            select(KpiFormulaVersion)
            .where(KpiFormulaVersion.status == APPLYING)
            .order_by(KpiFormulaVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
    except SQLAlchemyError:
        session.rollback()
        return None


def _next_version(session: Session) -> int:
    return int(session.execute(select(func.coalesce(func.max(KpiFormulaVersion.version), 0))).scalar_one()) + 1


def _activate(session: Session, row: KpiFormulaVersion) -> None:
    session.execute(
        update(KpiFormulaVersion)
        .where(KpiFormulaVersion.status == ACTIVE, KpiFormulaVersion.id != row.id)
        .values(status=SUPERSEDED)
    )
    row.status = ACTIVE
    row.applied_at = _now()
    row.error = None


def _built_window(session: Session) -> int | None:
    """The window the usage table was built with, or ``None`` when it is empty or missing."""
    try:
        return session.execute(select(func.max(StockoutRiskInput.window_days))).scalar()
    except SQLAlchemyError:
        session.rollback()
        return None


def save_and_apply(session: Session, values: dict[str, Any], actor: str, note: str | None = None) -> KpiFormulaVersion:
    """Validate ``values`` and save them as the next version. Raises :class:`FormulaValidationError` or
    :class:`ApplyInProgress`. Commits."""
    formulas = validate(values)
    if not schema_ready(session):
        raise NotSetUp(
            "The database has not been set up for editable calculations yet: the migration that creates "
            "i7_kpi_formula_version has not been run. Ask whoever manages the database to run it."
        )
    if applying_version(session) is not None:
        session.rollback()
        raise ApplyInProgress("Another change is still being applied. Wait for it to finish.")

    built = _built_window(session)
    rebuild_needed = built is not None and built != formulas.window_days
    row = KpiFormulaVersion(
        version=_next_version(session),
        status=APPLYING if rebuild_needed else ACTIVE,
        document_json=json.dumps({"fields": formulas.values}),
        note=(note or "").strip()[:500] or None,
        created_by=actor,
        rebuild_needed=rebuild_needed,
    )
    session.add(row)
    session.flush()
    if not rebuild_needed:
        _activate(session, row)
    session.commit()
    return row


def run_rebuild(version_id: int) -> None:
    """Rebuild the usage table for an ``applying`` version, then make it active. Runs in the background
    with its own session; never raises."""
    from app.core.db import get_sessionmaker
    from app.initiatives.i7.stockout_risk.history import build_stockout_risk_input

    with get_sessionmaker()() as session:
        row = session.get(KpiFormulaVersion, version_id)
        if row is None or row.status != APPLYING:
            return
        try:
            formulas = from_document(row.document_json, row.version)
            _, rows = build_stockout_risk_input(session, window_days=formulas.window_days)
            row.rows_built = rows
            _activate(session, row)
            session.commit()
            logger.info("kpi formulas v%s applied: usage rebuilt for %d material-plants", row.version, rows)
        except Exception as exc:  # noqa: BLE001 -- recorded on the version, which is the only report this job has
            session.rollback()
            logger.exception("kpi formulas v%s: the rebuild failed", version_id)
            failed = session.get(KpiFormulaVersion, version_id)
            if failed is not None:
                failed.status = FAILED
                failed.error = f"{type(exc).__name__}: {exc}"[:1000]
                session.commit()


def history(session: Session, limit: int = 20) -> list[KpiFormulaVersion]:
    _expire_stale(session)
    session.commit()
    try:
        return list(
            session.execute(select(KpiFormulaVersion).order_by(KpiFormulaVersion.version.desc()).limit(limit)).scalars()
        )
    except SQLAlchemyError:
        session.rollback()
        return []


def current_base() -> Select:
    """The Overview's population with no filters: the newest recommendation of each material-plant, smooth
    demand left out."""
    from app.api.i7.recommendations import _not_smooth

    newest = select(func.max(Recommendation.id).label("id")).group_by(
        Recommendation.sap_material_number, Recommendation.sap_plant_code
    ).subquery()
    return select(Recommendation).join(newest, newest.c.id == Recommendation.id).where(_not_smooth())


@dataclass(frozen=True)
class Figures:
    stockout_at_risk: int
    stockout_evaluated: int
    stockout_insufficient: int
    excess_count: int
    excess_opportunity: Decimal | None
    working_capital: Decimal | None
    adoption_rate: Decimal | None
    """As a percentage (75 = 75%); ``None`` when the formula cannot be worked out."""
    counted_as_adopted: Decimal | None


def figures(session: Session, formulas: Formulas) -> Figures:
    """The three KPIs over the whole portfolio under ``formulas`` -- what a Preview shows."""
    from app.initiatives.i7.stockout_risk.counts import stockout_risk_counts

    base = current_base()
    stockout = stockout_risk_counts(session, base, formulas=formulas)
    excess_base = base.where(excess_condition_sql(formulas))
    excess_count = session.execute(select(func.count()).select_from(excess_base.subquery())).scalar_one()
    excess_value = excess_value_sql(formulas)
    excess_total = session.execute(
        excess_base.with_only_columns(func.sum(excess_value)).where(excess_value.is_not(None))
    ).scalar()
    capital_total = working_capital_impact(session, base, formulas).amount

    from app.api.i7.adoption import adoption_counts
    from app.initiatives.i7.kpi_formulas import expression

    counts = adoption_counts(session)
    adoption_values: dict[str, Decimal | None] = {
        "adopted": Decimal(counts.adopted),
        "partially_adopted": Decimal(counts.partially_adopted),
        "not_adopted": Decimal(counts.not_adopted),
        "pending": Decimal(counts.pending),
        "tracked": Decimal(counts.tracked),
    }
    rate = expression.evaluate(formulas.parsed["adoption_rate"], adoption_values)
    card = expression.evaluate(formulas.parsed["adopted_card"], adoption_values)
    return Figures(
        stockout_at_risk=stockout.at_risk,
        stockout_evaluated=stockout.evaluated,
        stockout_insufficient=stockout.insufficient_data,
        excess_count=int(excess_count),
        excess_opportunity=Decimal(str(excess_total)) if excess_total is not None else None,
        working_capital=Decimal(str(capital_total)) if capital_total is not None else None,
        adoption_rate=rate * 100 if isinstance(rate, Decimal) else None,
        counted_as_adopted=card if isinstance(card, Decimal) else None,
    )


def preview(session: Session, values: dict[str, Any]) -> tuple[Formulas, Formulas, Figures, Figures, bool]:
    """``(draft, active, draft figures, active figures, window_needs_rebuild)``. Saves nothing. Raises
    :class:`FormulaValidationError`."""
    draft = validate(values)
    active = load_active(session)
    built = _built_window(session)
    return draft, active, figures(session, draft), figures(session, active), (
        built is not None and built != draft.window_days
    )


__all__ = [
    "ApplyInProgress",
    "NotSetUp",
    "FormulaValidationError",
    "applying_version",
    "figures",
    "history",
    "newest_active",
    "preview",
    "run_rebuild",
    "save_and_apply",
]
