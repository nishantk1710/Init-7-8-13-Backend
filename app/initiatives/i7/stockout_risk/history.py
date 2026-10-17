"""Builds ``i7_stockout_risk_input``: each material-plant's last issue date and
its 90-day consumption, read as of the anchor date.

The movements come from the same ``n_mseg`` read the I07 staging uses, with the
same issue / reversal movement-type policy and the same debit/credit sign rule,
so "consumption" means the same thing here as in ``i7_staged_consumption``. The
difference is grain: staging totals by month, which cannot give an exact 90 days
or the day of the last issue, so this keeps the day.

The table is derived and rebuilt whole (delete, then insert), so a rerun is
always safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import bindparam, delete, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.initiatives.i7.adapters.extract import (
    _consumption_statement,
    _out_of_scope,
    _plant_scope_clause,
    _scoped,
    _stream,
)
from app.initiatives.i7.adapters.field_map import CREDIT_INDICATOR
from app.initiatives.i7.adapters.ingestion_policy import ExtractIngestionPolicy
from app.initiatives.i7.adapters.validation import clean, parse_date, parse_decimal
from app.initiatives.i7.stockout_risk import CONSUMPTION_WINDOW_DAYS, window_start
from app.models.i7_stockout_risk import StockoutRiskInput

logger = logging.getLogger(__name__)

#: Rows inserted per statement. SQL Server allows 2,100 bind parameters per
#: statement, and each row binds 6 of them.
INSERT_BATCH = 300


@dataclass
class UsageFacts:
    """What the movements say about one material-plant, as of the anchor."""

    last_issue_date: date
    consumption: Decimal
    """Net issued quantity in the window, never below zero."""


@dataclass
class HistoryResult:
    facts: dict[tuple[str, str], UsageFacts]
    skipped: dict[str, int] = field(default_factory=dict)
    """Rows left out, by reason. Counted, never silently dropped."""


def aggregate_issue_history(
    rows: Iterable[Any],
    issue_types: frozenset[str],
    reversal_types: frozenset[str],
    *,
    as_of: date,
    plant_scope: frozenset[str] = frozenset(),
) -> HistoryResult:
    """Per material-plant: the newest issue date and the net quantity issued in
    the window, from movement rows, looking only at movements on or before ``as_of``.

    * The sign rule is staging's: a credit (``H``) adds and a debit subtracts, so a
      reversal nets against the issue it cancels. Issues and reversals both count
      toward the window total; only issue lines set the last issue date, and
      reversals are not netted against it.
    * A material-plant that has reversals but no issue on or before ``as_of`` is not
      returned: it has not been issued, as far as these movements say.
    * The window total is floored at zero: reversals staged without their issue
      (the issue predates the extract) must not give a negative consumption.
    * A row with no material or plant, an unparseable date or quantity, or a date
      after ``as_of`` is skipped and counted.
    """
    start = window_start(as_of)
    last_issue: dict[tuple[str, str], date] = {}
    net: dict[tuple[str, str], Decimal] = {}
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for row in rows:
        material, plant = clean(row.material), clean(row.plant)
        if material is None or plant is None:
            skip("missing_material_or_plant")
            continue
        if _out_of_scope(plant, plant_scope):
            continue
        posted = parse_date(row.posting_date)
        if posted is None:
            skip("invalid_date")
            continue
        if posted > as_of:
            skip("after_anchor")
            continue
        quantity = parse_decimal(row.quantity)
        if quantity is None:
            skip("invalid_quantity")
            continue

        key = (material, plant)
        movement = clean(row.movement_type)
        if movement in issue_types:
            if key not in last_issue or posted > last_issue[key]:
                last_issue[key] = posted
        if posted >= start and (movement in issue_types or movement in reversal_types):
            signed = quantity if clean(row.debit_credit_ind) == CREDIT_INDICATOR else -quantity
            net[key] = net.get(key, Decimal(0)) + signed

    facts = {
        key: UsageFacts(last_issue_date=issued, consumption=max(net.get(key, Decimal(0)), Decimal(0)))
        for key, issued in last_issue.items()
    }
    return HistoryResult(facts=facts, skipped=skipped)


def resolve_anchor(session: Session, policy: ExtractIngestionPolicy, configured: date | None) -> date | None:
    """The date to read the history as of: the setting, or -- when it is blank --
    the newest issue date in the movement data. ``None`` when there is none."""
    if configured is not None:
        return configured
    scope = policy.plant_scope
    statement = text(
        "SELECT MAX(posting_date) AS newest FROM n_mseg "
        "WHERE movement_type IN :issue_types AND posting_date <> ''"
        + _plant_scope_clause("plant", scope, "material")
    ).bindparams(bindparam("issue_types", expanding=True))
    params: dict[str, Any] = {"issue_types": list(policy.consumption.issue_movement_types)}
    if scope:
        params["plant_scope"] = sorted(scope)
    newest = session.execute(_scoped(statement, scope), params).scalar()
    return parse_date(newest)


def build_stockout_risk_input(
    session: Session, *, as_of: date | None = None, policy: ExtractIngestionPolicy | None = None
) -> tuple[date | None, int]:
    """Rebuild ``i7_stockout_risk_input`` as of ``as_of`` (the setting when omitted).

    Returns ``(anchor used, rows written)``. With no anchor and no issue movements at
    all the table is left empty and the anchor is ``None``.

    The caller owns the transaction: nothing is committed here.
    """
    policy = policy or ExtractIngestionPolicy(plants=get_settings().i7_plant_list)
    configured = as_of if as_of is not None else get_settings().i7_stockout_risk_as_of
    anchor = resolve_anchor(session, policy, configured)
    if anchor is None:
        session.execute(delete(StockoutRiskInput))
        return None, 0

    params: dict[str, Any] = {"movement_types": list(policy.consumption.all_movement_types)}
    if policy.plant_scope:
        params["plant_scope"] = sorted(policy.plant_scope)
    # Consumed to the end before anything is written: SQL Server will not run a
    # write on a connection with a result still open (see extract._stream).
    result = aggregate_issue_history(
        _stream(session, _consumption_statement(policy.plant_scope), params),
        frozenset(policy.consumption.issue_movement_types),
        frozenset(policy.consumption.reversal_movement_types),
        as_of=anchor,
        plant_scope=policy.plant_scope,
    )

    session.execute(delete(StockoutRiskInput))
    rows = [
        {
            "sap_material_number": material,
            "sap_plant_code": plant,
            "as_of_date": anchor,
            "last_issue_date": facts.last_issue_date,
            "consumption_90d": facts.consumption,
            "window_days": CONSUMPTION_WINDOW_DAYS,
        }
        for (material, plant), facts in sorted(result.facts.items())
    ]
    for start in range(0, len(rows), INSERT_BATCH):
        session.execute(StockoutRiskInput.__table__.insert(), rows[start : start + INSERT_BATCH])
    logger.info(
        "stockout risk input: %d material-plants as of %s (skipped rows: %s)",
        len(rows),
        anchor,
        result.skipped or "none",
    )
    return anchor, len(rows)
