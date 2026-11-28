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
from typing import Literal

from sqlalchemy import Select, and_, case, false, func, literal, or_, select, true
from sqlalchemy.orm import Session

from app.initiatives.i7.kpi_formulas.currency import REPORTING_CURRENCY, TO_ZAR
from app.initiatives.i7.kpi_formulas.resolved import Formulas
from app.initiatives.i7.kpi_formulas.sql import working_capital_sql
from app.initiatives.i7.recommendations.types import LifecycleStatus
from app.models.i7_recommendation import Recommendation, SapAdoptionResult
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

__all__ = [
    "REPORTING_CURRENCY",
    "WorkingCapitalImpact",
    "counted_as_stock_to_oar",
    "counted_in_working_capital",
    "oar_rule",
    "why_not_counted",
    "why_not_counted_as_stock_to_oar",
    "working_capital_impact",
]


@dataclass(frozen=True)
class WorkingCapitalImpact:
    amount: Decimal | None
    """Positive releases cash, negative ties more up, in ``currency``. ``None`` (never 0) when no row is included."""
    currency: str
    included_count: int
    excluded_count: int
    """Material-plants of the population left out: another or missing currency, no valid price, no reorder point,
    or an OAR material that is not eligible (and has no live conversion request) or was rejected."""
    overlap_count: int = 0
    """Only for ``stock_to_oar="add"``: material-plants counted twice in ``amount`` -- once for their Stock item
    release and once for their release if converted to OAR. 0 everywhere else."""

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


def counted_in_working_capital(formulas: Formulas):
    """The rule that decides whether a recommendation counts in the Working Capital card, as SQL over
    ``Recommendation``: priced in ZAR/NAD, a price above the placeholder floor, a formula that can be worked out, and
    the OAR rule. The card and the Excel export's "Counted in Working Capital" column both use this one expression."""
    rate = case(*[(_price_currency() == code, factor) for code, factor in TO_ZAR.items()], else_=None)
    return and_(
        rate.is_not(None),
        Recommendation.unit_price > DEFAULT_TOKEN_MAX,
        working_capital_sql(formulas).is_not(None),
        oar_rule(),
    )


def _price_problem(unit_price: Decimal | None, currency: str | None) -> str | None:
    """Why a price cannot be used, in words; ``None`` when it can."""
    if unit_price is None:
        return "no price"
    if unit_price <= DEFAULT_TOKEN_MAX:
        return f"placeholder price ({unit_price}, at or below {DEFAULT_TOKEN_MAX})"
    if currency not in TO_ZAR:
        return "price not in ZAR or NAD"
    return None


def counted_as_stock_to_oar(formulas: Formulas):
    """Whether the Overview's Stock item to OAR views count a recommendation, as SQL over ``Recommendation``.

    The same rule as the Working Capital card in ``stock_to_oar="replace"`` mode, for a row: eligible to convert to OAR
    (the newest Min-Max -> OAR run says Convert to OAR and its newest request was not rejected), priced in ZAR/NAD above
    the placeholder floor, the conversion's release (the saved formula with the recommended reorder point as 0) can be
    worked out, and the OAR rule holds."""
    from app.initiatives.i7.kpi_formulas.stock_to_oar import eligible_for_oar, oar_release_value

    rate = case(*[(_price_currency() == code, factor) for code, factor in TO_ZAR.items()], else_=None)
    return and_(
        eligible_for_oar(),
        rate.is_not(None),
        Recommendation.unit_price > DEFAULT_TOKEN_MAX,
        oar_release_value(formulas).is_not(None),
        oar_rule(),
    )


def why_not_counted_as_stock_to_oar(
    *,
    unit_price: Decimal | None,
    currency: str | None,
    current_rop: Decimal | None,
    eligible: bool,
) -> str:
    """The reason :func:`counted_as_stock_to_oar` leaves a recommendation out, in words for a reader of the export.

    ``eligible`` is whether the material-plant is still eligible to convert (False: its conversion was rejected)."""
    if not eligible:
        return "conversion request rejected"
    problem = _price_problem(unit_price, currency)
    if problem is not None:
        return problem
    if current_rop is None:
        return "no current ROP"
    return "the formula could not be worked out"


def why_not_counted(
    *,
    unit_price: Decimal | None,
    currency: str | None,
    current_rop: Decimal | None,
    recommended_rop: Decimal | None,
    conversion_eligibility: str | None,
    status: str | None,
    manually_converted: bool | None,
) -> str:
    """The reason a recommendation that :func:`counted_in_working_capital` leaves out is left out, in words a reader
    of the Excel export understands. The first rule that applies is named; when none does (a saved formula that cannot
    be worked out for the row) it says so."""
    problem = _price_problem(unit_price, currency)
    if problem is not None:
        return problem
    if current_rop is None or recommended_rop is None:
        return "no current or recommended ROP"
    if conversion_eligibility == ELIGIBLE and status == LifecycleStatus.REJECTED.value:
        return "conversion request rejected"
    if conversion_eligibility == NOT_ELIGIBLE and not (manually_converted and status in REQUEST_COUNTED_STATUSES):
        return "not eligible to convert and no conversion request"
    return "the formula could not be worked out"


StockToOar = Literal["replace", "add"]
"""How the Overview's Stock item to OAR views value a population (see :func:`working_capital_impact`)."""


def working_capital_impact(
    session: Session,
    base: Select,
    formulas: Formulas,
    segment: str | None = None,
    *,
    adopted_only: bool = False,
    stock_to_oar: StockToOar | None = None,
) -> WorkingCapitalImpact:
    """The Working Capital Impact over ``base`` (a ``select(Recommendation)`` population), in ZAR.

    ``segment`` narrows the population: ``"oar"`` for OAR material-plants, ``"stock"`` for stock items (everything not OAR);
    ``None`` is both. The two segments add up to the whole: a row is in exactly one of them.

    ``stock_to_oar`` is for the Overview's two Stock item to OAR release views, over a population already narrowed to
    them. A stock-item material that is eligible to convert to OAR has a second release: the capital held at its
    current reorder point, which goes to zero (the saved formula with the recommended reorder point as 0, reorder point
    only). ``"replace"`` counts each material once: an eligible one at that OAR release instead of its Stock item
    release, every other at its Stock item release. ``"add"`` counts a material's Stock item release AND, if it is
    eligible, its OAR release, so a material in both is counted twice (``overlap_count`` says how many). ``None`` is the
    existing calculation, unchanged."""
    if adopted_only:
        # Only what SAP now holds: the reconciliation's ADOPTED result (the same evidence as "converted to Min-Max"). Approval alone
        # is not adoption. With nothing adopted the figure is 0 (measured: nothing has taken effect), not "not available".
        base = base.where(
            Recommendation.recommendation_id.in_(select(SapAdoptionResult.recommendation_id).where(SapAdoptionResult.status == "ADOPTED"))
        )
    if segment == "oar":
        # ``== true()``, not ``IS``: SQL Server rejects ``IS 1`` / ``IS 0`` on a bit column.
        base = base.where(Recommendation.is_oar == true())
    elif segment == "stock":
        base = base.where(or_(Recommendation.is_oar.is_(None), Recommendation.is_oar == false()))
    currency = _price_currency()
    rate = case(*[(currency == code, factor) for code, factor in TO_ZAR.items()], else_=None)
    value = working_capital_sql(formulas)
    included = counted_in_working_capital(formulas)
    overlap = literal(0)

    if stock_to_oar is not None:
        from app.initiatives.i7.kpi_formulas.stock_to_oar import eligible_for_oar, oar_release_value

        eligible, oar_value = eligible_for_oar(), oar_release_value(formulas)
        priced = and_(rate.is_not(None), Recommendation.unit_price > DEFAULT_TOKEN_MAX, oar_rule())
        if stock_to_oar == "replace":
            used = case((eligible, oar_value), else_=value)
            included = and_(priced, used.is_not(None))
            amount_expression = used * rate
        else:
            # The Stock item release counts where it releases (the population's own rule), and the OAR release is
            # added on top for an eligible one: an overlapping material appears in both.
            stock_part = case((value > 0, value), else_=0)
            oar_part = case((and_(eligible, oar_value > 0), oar_value), else_=0)
            included = and_(priced, or_(value.is_not(None), oar_value.is_not(None)))
            amount_expression = (stock_part + oar_part) * rate
            overlap = case((and_(included, value > 0, eligible, oar_value > 0), 1), else_=0)
    else:
        amount_expression = value * rate

    # Each row's own figures first, then the sums over them. SQL Server refuses an aggregate over an expression
    # that holds a subquery (the currency lookup), so the subquery stays in the per-row select and the outer
    # query only sums columns.
    per_row = base.with_only_columns(
        case((included, 1), else_=0).label("is_included"),
        case((included, amount_expression), else_=None).label("amount_zar"),
        overlap.label("is_overlap"),
    ).subquery()
    total, included_count, amount, overlap_count = session.execute(
        select(
            func.count(),
            func.coalesce(func.sum(per_row.c.is_included), 0),
            func.sum(per_row.c.amount_zar),
            func.coalesce(func.sum(per_row.c.is_overlap), 0),
        ).select_from(per_row)
    ).one()
    return WorkingCapitalImpact(
        amount=Decimal(str(amount)) if amount is not None else (Decimal(0) if adopted_only else None),
        currency=REPORTING_CURRENCY,
        included_count=int(included_count),
        excluded_count=int(total) - int(included_count),
        overlap_count=int(overlap_count),
    )
