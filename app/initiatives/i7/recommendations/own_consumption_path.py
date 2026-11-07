"""The own-consumption fallback, wired into recommendation generation.

Order of preference for an eligible OAR material-plant:

1. the existing similarity estimate, when it succeeded -- unchanged and never replaced;
2. otherwise its own consumption (:mod:`app.initiatives.i7.inventory.own_consumption`), when the last
   complete months hold enough of it;
3. otherwise manual review.

This module decides which material-plants the fallback applies to, reads what it needs from the database in
one grouped pass, and hands the builder one :class:`OwnConsumptionOutcome` per eligible material-plant. The
builder stays pure. Nothing here touches eligibility: the Min-Max conversion rule is evaluated by the same
function the builder uses (``builder.conversion_for``), and only a verdict of ELIGIBLE gets an outcome.

It is entirely switched by ``I7_OAR_OWN_CONSUMPTION_ENABLED``. Callers pass nothing when it is off.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.initiatives.i7.contracts import Criticality
from app.initiatives.i7.features.consumption_window import WindowStatus, latest_run_window
from app.initiatives.i7.inventory import lead_time as lead_time_module
from app.initiatives.i7.inventory import own_consumption as own_consumption_module
from app.initiatives.i7.inventory import service_level as service_level_module
from app.initiatives.i7.inventory.own_consumption import OwnConsumptionOutcome
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import builder, repository
from app.initiatives.i7.recommendations.types import ConversionEligibility

Key = tuple[str, str]


def last_complete_month(session: Session) -> date | None:
    """The latest month the newest feature run verified complete, or ``None``.

    ``None`` -- an unusable or unrecorded window -- is never read as "no consumption": the months cannot be
    vouched for, so the fallback does not run and nothing is assumed about them.
    """
    window = latest_run_window(session)
    if window is None or window.status != WindowStatus.USABLE.value or window.end is None:
        return None
    return date(window.end.year, window.end.month, 1)


def _tier(raw: str | None) -> Criticality | None:
    return builder._criticality_tier(raw)


def evaluate_rows(
    session: Session, rows: Iterable[Any], policy: PolicyDocument
) -> dict[Key, OwnConsumptionOutcome]:
    """One outcome per eligible OAR material-plant among ``rows`` (the joined Phase 3/6 records).

    Not eligible -- or not OAR -- gets no entry. An eligible one whose similarity estimate succeeded gets an
    outcome with no result: the fallback was not needed, and is not run.
    """
    outcomes: dict[Key, OwnConsumptionOutcome] = {}
    needing: list[Any] = []
    for row in rows:
        decision = builder.conversion_for(row, policy)
        if decision is None or decision.eligibility is not ConversionEligibility.ELIGIBLE:
            continue
        key = (row.sap_material_number, row.sap_plant_code)
        if row.estimate_status == "SUCCESS":
            outcomes[key] = OwnConsumptionOutcome(result=None)
        else:
            needing.append(row)

    if not needing:
        return outcomes

    settings = policy.own_consumption
    last_month = last_complete_month(session)
    monthly_by_key: dict | None = None
    if last_month is not None:
        months = own_consumption_module.window_months_ending(last_month, settings.window_months)
        monthly_by_key = repository.load_monthly_consumption(
            session, *own_consumption_module.window_bounds(months)
        )

    for row in needing:
        key = (row.sap_material_number, row.sap_plant_code)
        lead = lead_time_module.analyse(
            [], getattr(row, "planned_delivery_time_days", None), policy.lead_time
        )
        service = service_level_module.resolve(policy.service_level, _tier(row.criticality), None)
        monthly = None if monthly_by_key is None else monthly_by_key.get(key, {})
        outcomes[key] = OwnConsumptionOutcome(
            result=own_consumption_module.evaluate(
                monthly=monthly,
                last_complete_month=last_month,
                criticality=row.criticality,
                lead=lead,
                service=service,
                policy=settings,
            )
        )
    return outcomes
