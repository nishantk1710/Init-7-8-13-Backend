"""Canonical purchase-order observations for lead-time analysis.

Structure only. No averaging, no sigma, no outlier policy -- Phase 3 owns the
arithmetic, and the 1-730 day validity window is a *policy* threshold that lives
in configuration, not a constant hidden in a contract.

``lead_time_days`` is therefore a derived convenience, not a judgement: it
subtracts two dates when both exist and returns ``None`` otherwise. Whether a
given value is usable is decided later, against configured bounds.

Who owns lead time is itself unresolved (Phase 0, R5): the Formula Reference has
I07 computing it from PO and GR dates, the FRS has I07 consuming an Initiative
11 Z-program and forbids computing its own. This contract supports both, and
:class:`~app.initiatives.i7.contracts.enums.LeadTimeSource` records which one a
figure came from -- provenance being a stated acceptance criterion either way.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, model_validator

from app.initiatives.i7.contracts.identity import MaterialPlantKey


class PurchaseOrderObservation(BaseModel):
    """One PO line and its goods receipt, as far as it has progressed."""

    model_config = ConfigDict(frozen=True)

    key: MaterialPlantKey

    purchasing_document: str
    item: str | None = None

    created_on: date | None = None
    """PO creation date (EKKO.AEDAT is the confirmed interim proxy; BEDAT is not
    exposed on the live service)."""

    goods_receipt_date: date | None = None
    """Earliest GR posting date for the line. ``None`` while the PO is open --
    an open PO is a real state, not a broken record."""

    quantity_ordered: float | None = None
    quantity_received: float | None = None

    supplier: str | None = None
    """For the supplier-weighting the I11 program applies."""

    is_cancelled: bool = False
    """Cancelled POs are excluded from lead-time statistics."""

    @model_validator(mode="after")
    def _receipt_not_before_creation(self) -> "PurchaseOrderObservation":
        if (
            self.created_on is not None
            and self.goods_receipt_date is not None
            and self.goods_receipt_date < self.created_on
        ):
            raise ValueError(
                "goods_receipt_date precedes created_on -- a receipt cannot "
                "predate the order that caused it"
            )
        return self

    @property
    def lead_time_days(self) -> int | None:
        """Elapsed days, or ``None`` when either date is missing.

        Plausibility is not judged here: the acceptable range is configuration.
        """
        if self.created_on is None or self.goods_receipt_date is None:
            return None
        return (self.goods_receipt_date - self.created_on).days
