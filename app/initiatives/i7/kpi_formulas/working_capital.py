"""Working Capital Impact, in ZAR: the one calculation behind the Overview card, the KPI-formula preview and the
Quarterly Report.

The price is MBEW's (``VERPR / PEINH``, staged on the recommendation as ``unit_price``) and its currency is MBEW's
``WAERS`` (staged on ``i7_staged_material_plant.currency``). A purchase order's price or currency is never used.

**Which material-plants count.** A material-plant is counted only when

* its price is in a currency that can be stated in ZAR (:data:`~.currency.TO_ZAR`: ZAR as it is, NAD at 1:1) and is
  not a placeholder (at or below :data:`~app.shared.valuation_price.DEFAULT_TOKEN_MAX`), and the formula can be
  worked out for it; and
* it passes the OAR rule below. A recommendation with no Min-Max conversion verdict (a stock item) is not
  affected by it.

**The OAR rule** (``conversion_eligibility`` is the rule's own verdict, and is never changed by a user request):

* ELIGIBLE: counts, unless it was rejected;
* NOT_ELIGIBLE: counts only when somebody asked to convert it (``manually_converted``) and the request is alive -- in
  the approval chain (pending, held, sent back, adjusted) or approved, including SAP-pending, executed and adopted. A
  rejected request, or no request at all, leaves it out. A pending request counts from the moment it is submitted.

The formula is the Overview's saved one, ``unit_price * (current_rop - recommended_rop)`` unless somebody edited it.
Left-out rows are counted, so the figure always says how much of the population it covers.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.initiatives.i7.kpi_formulas.currency import REPORTING_CURRENCY, TO_ZAR
from app.initiatives.i7.kpi_formulas.resolved import Formulas
from app.initiatives.i7.kpi_formulas.sql import working_capital_sql
from app.initiatives.i7.recommendations.types import LifecycleStatus
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterialPlant
from app.shared.valuation_price import DEFAULT_TOKEN_MAX

ELIGIBLE = "ELIGIBLE"
NOT_ELIGIBLE = "NOT_ELIGIBLE"

REQUEST_COUNTED_STATUSES: tuple[str, ...] = tuple(
    status.value
    for status in LifecycleStatus
    if status not in (LifecycleStatus.REJECTED, LifecycleStatus.READY_FOR_REVIEW, LifecycleStatus.NOT_EVALUABLE)
)
"""Where a manual conversion request counts: submitted and not rejected. READY_FOR_REVIEW and NOT_EVALUABLE are
before any request (and a NOT_ELIGIBLE row there has none); REJECTED is a request that was turned down."""

__all__ = ["REPORTING_CURRENCY", "WorkingCapitalImpact", "oar_rule", "working_capital_impact"]


@dataclass(frozen=True)
class WorkingCapitalImpact:
    amount: Decimal | None
    """Positive releases cash, negative ties more up, in ``currency``. ``None`` (never 0) when no row is included."""
    currency: str
    included_count: int
    excluded_count: int
    """Material-plants of the population left out: another or missing currency, no valid price, no reorder point,
    or an OAR material that is not eligible (and has no live conversion request) or was rejected."""

    @property
    def population(self) -> int:
        return self.included_count + self.excluded_count


def oar_rule():
    """The OAR condition above, as SQL over ``Recommendation``. True for every row it does not apply to."""
    verdict = Recommendation.conversion_eligibility
    return or_(
        verdict.is_(None),
        verdict.not_in((ELIGIBLE, NOT_ELIGIBLE)),
        and_(verdict == ELIGIBLE, Recommendation.status != LifecycleStatus.REJECTED.value),
        and_(
            verdict == NOT_ELIGIBLE,
            Recommendation.manually_converted,
            Recommendation.status.in_(REQUEST_COUNTED_STATUSES),
        ),
    )


def _price_currency():
    """The currency of this recommendation's material-plant price: a lookup on the unique (material, plant) key."""
    return (
        select(StagedMaterialPlant.currency)
        .where(
            StagedMaterialPlant.sap_material_number == Recommendation.sap_material_number,
            StagedMaterialPlant.sap_plant_code == Recommendation.sap_plant_code,
        )
        .scalar_subquery()
    )


def working_capital_impact(session: Session, base: Select, formulas: Formulas) -> WorkingCapitalImpact:
    """The Working Capital Impact over ``base`` (a ``select(Recommendation)`` population), in ZAR."""
    currency = _price_currency()
    rate = case(*[(currency == code, factor) for code, factor in TO_ZAR.items()], else_=None)
    value = working_capital_sql(formulas)
    included = and_(rate.is_not(None), Recommendation.unit_price > DEFAULT_TOKEN_MAX, value.is_not(None), oar_rule())

    # Each row's own figures first, then the sums over them. SQL Server refuses an aggregate over an expression
    # that holds a subquery (the currency lookup), so the subquery stays in the per-row select and the outer
    # query only sums columns.
    per_row = base.with_only_columns(
        case((included, 1), else_=0).label("is_included"),
        case((included, value * rate), else_=None).label("amount_zar"),
    ).subquery()
    total, included_count, amount = session.execute(
        select(func.count(), func.coalesce(func.sum(per_row.c.is_included), 0), func.sum(per_row.c.amount_zar)).select_from(
            per_row
        )
    ).one()
    return WorkingCapitalImpact(
        amount=Decimal(str(amount)) if amount is not None else None,
        currency=REPORTING_CURRENCY,
        included_count=int(included_count),
        excluded_count=int(total) - int(included_count),
    )
