"""Lead-time analysis.

**Deviation from the Formula Reference, recorded here on purpose.** Stage 3
documents lead time as computed from PO-to-GR history:

    LT_i (days) = Goods Receipt Date - PO Creation Date
    LT_avg = sum(LT_i) / m
    sigma_LT = sqrt( sum((LT_i - LT_avg)^2) / (m - 1) )

with a 5/2/0-1 PO tiering and a 0.3 x planned fallback only when fewer than 2
POs exist. That is what this module implemented until this change.

The business decision has since been made to use SAP's planned delivery time
(MARC-PLIFZ) as the lead time unconditionally, regardless of how much PO
history exists -- not only as the sub-2-PO fallback the Formula Reference
describes. This is a deliberate, requested override of the documented Stage 3
methodology, not a data-availability fallback and not an invented default:
record it as what it is rather than quietly relabelling MARC-PLIFZ as
"the fallback" when it is now always the source.

``sigma_LT`` is still the same conservative ``0.3 x planned`` the Formula
Reference specifies for its own fallback case, because no other variability
figure has been supplied for this path -- a single point (the planned days
figure) has no measurable spread of its own.

PO history is still counted and passed through for observability (the
recommendation's audit trail should be able to say how many POs existed for a
material-plant even though they no longer drive the calculation), but it no
longer selects the method or produces the averaged/measured statistics.
"""

from decimal import Decimal

from app.initiatives.i7.inventory.types import (
    DAYS_PER_MONTH,
    CalculationStatus,
    LeadTimeMethod,
    LeadTimeResult,
)
from app.initiatives.i7.policy import LeadTimePolicy


class PurchaseOrderInput:
    """One staged PO line, as the calculation sees it.

    Retained for observability only: ``po_count`` still reports on this input,
    but PO durations no longer drive the lead-time figure -- see the module
    docstring.
    """

    __slots__ = ("lead_time_days", "is_cancelled")

    def __init__(self, lead_time_days: int | None, is_cancelled: bool = False) -> None:
        self.lead_time_days = lead_time_days
        self.is_cancelled = is_cancelled


def analyse(
    orders: list[PurchaseOrderInput],
    planned_delivery_days: int | None,
    policy: LeadTimePolicy,
) -> LeadTimeResult:
    """Lead time from MARC-PLIFZ, unconditionally. See module docstring."""
    total = len(orders)
    cancelled = sum(1 for order in orders if order.is_cancelled)
    valid = sum(
        1
        for order in orders
        if not order.is_cancelled and order.lead_time_days is not None
    )

    if planned_delivery_days is None or planned_delivery_days <= 0:
        return LeadTimeResult(
            status=CalculationStatus.NOT_EVALUABLE_LEAD_TIME,
            po_count=total,
            valid_po_count=valid,
            excluded_cancelled_count=cancelled,
            detail=(
                "no MARC planned delivery time (PLIFZ) is available; "
                "no lead time is substituted"
            ),
        )

    planned_days = Decimal(planned_delivery_days)
    sigma_days = planned_days * Decimal(str(policy.planned_delivery_variability_factor))

    return LeadTimeResult(
        status=CalculationStatus.LIMITED,
        method=LeadTimeMethod.PLANNED_FALLBACK,
        po_count=total,
        valid_po_count=valid,
        excluded_cancelled_count=cancelled,
        lt_avg_days=planned_days,
        lt_avg_months=planned_days / DAYS_PER_MONTH,
        sigma_lt_days=sigma_days,
        sigma_lt_months=sigma_days / DAYS_PER_MONTH,
        planned_lt_days=planned_delivery_days,
        detail=(
            f"MARC planned delivery time (PLIFZ) used unconditionally per "
            f"business decision, not PO history ({valid} usable PO(s) available "
            f"but not used); sigma_LT = "
            f"{policy.planned_delivery_variability_factor} x planned"
        ),
    )
