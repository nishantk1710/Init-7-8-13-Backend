"""I13 FR-2 -- what the assistant tells somebody reserving an OAR spare.

Composition over built parts
-----------------------------
There is no new computation in this module, and that is the point. Initiative
13's WATCH mart already holds every number FR-2(a)'s cross-check asks for --
stock on hand, open PO quantity, average monthly consumption, months of cover,
days since last movement, aging band, GRNI -- all computed, all tested, all
persisted. ``PostgresCrossPlantStockProvider`` already answers the one field
WATCH does not carry, because the ACT exception queue needed it first.

So this assembles a reservation-time answer out of two existing sources and adds
nothing of its own. Anything it recomputed would be a second opinion about a
number the exception queue is already publishing, and the two would disagree in
front of a user within a release.

What the requester is being told, in the order they would ask
--------------------------------------------------------------
1. **What you have.** Stock here, and how much is already on order.
2. **How long that lasts.** Months of cover, against consumption.
3. **Whether this part actually moves.** Days since last movement, and the
   aging band. A part untouched for 400 days is the case FR-2 exists for.
4. **Whether somebody else has one.** Stock at other plants -- informational
   only. The platform never proposes a transfer, never creates a reservation
   elsewhere and never posts to SAP; it says the stock is there and leaves the
   decision with people who can make it.

Absences are answers
---------------------
``months_of_cover`` is ``None`` when nothing is consumed, and WATCH carries a
``months_of_cover_reason`` saying so. That is not zero cover -- it is *infinite*
cover, and rendering it as zero would tell somebody to buy more of a part
nothing ever consumes. The reason travels with the assessment so the UI can show
the explanation rather than a blank.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

from app.initiatives.i13.act.domain import CrossPlantStockInfo
from app.initiatives.i13.models import AgingBand, WatchMetric
from app.shared.numbers import plain


@dataclass(frozen=True)
class I13Assessment:
    """Everything the I13 flow states before the plan is captured.

    Stored verbatim as "the advice as served" -- see ``AssistantSession.assessment``.
    """

    material: str
    plant: str
    requested_quantity: Decimal | None

    metric: WatchMetric
    """W6.3's WATCH row for this material+plant. Reused whole rather than
    unpacked, so a new WATCH field is available here without a change."""

    cross_plant_stock: tuple[CrossPlantStockInfo, ...]
    """Informational only. Never a transfer instruction."""

    @property
    def total_elsewhere(self) -> Decimal:
        return sum((info.stock_on_hand for info in self.cross_plant_stock), Decimal("0"))

    @property
    def cover_is_unknown(self) -> bool:
        """True when there is no consumption rate to divide by.

        Infinite cover, not none. The distinction decides whether somebody buys
        more of a part nothing consumes.
        """
        return self.metric.months_of_cover is None

    @property
    def is_dormant(self) -> bool:
        """Whether this part has not moved for long enough to be worth saying.

        Reads W3.5's aging band rather than re-deriving a threshold from days,
        so the assistant and the aging report always agree about the same part.
        The bands are FAST / SLOW / NON_MOVING -- anything but FAST is worth
        saying out loud to somebody about to buy another one.
        """
        return self.metric.aging_band in (AgingBand.SLOW, AgingBand.NON_MOVING)

    @property
    def headline(self) -> str:
        """The sentence of record, in the order a requester would ask."""
        parts: list[str] = []

        stock = self.metric.stock_on_hand
        if stock is None:
            parts.append(
                f"No stock record exists for {self.material} at plant {self.plant}"
            )
        else:
            parts.append(
                f"You have {plain(stock)} at plant {self.plant}"
            )

        if self.metric.open_po_quantity > 0:
            parts.append(f"{plain(self.metric.open_po_quantity)} already on order")

        if self.metric.months_of_cover is not None:
            parts.append(
                f"about {plain(self.metric.months_of_cover)} months of cover"
            )
        elif self.metric.months_of_cover_reason:
            parts.append(f"cover cannot be calculated ({self.metric.months_of_cover_reason})")

        sentence = ", ".join(parts) + "."

        if self.metric.days_since_last_movement is not None and self.is_dormant:
            # NON_MOVING -> "non-moving". The band is a code in the data and a
            # phrase in a sentence, and the sentence is what a person reads.
            band = self.metric.aging_band.value.replace("_", "-").lower()
            sentence += (
                f" This part has not moved in "
                f"{self.metric.days_since_last_movement} days ({band})."
            )

        if self.cross_plant_stock:
            where = ", ".join(
                f"{plain(info.stock_on_hand)} at {info.plant}"
                for info in self.cross_plant_stock
            )
            sentence += f" Other plants hold {where}."

        return sentence

    @property
    def caveats(self) -> tuple[str, ...]:
        notes: list[str] = []

        if self.metric.stock_on_hand is None:
            notes.append(
                f"No stock record exists for {self.material} at plant {self.plant}. "
                "That means no source told us, not that stock is zero."
            )

        if self.cover_is_unknown:
            notes.append(
                "Months of cover cannot be calculated because nothing has been "
                "consumed in the window. That means cover is effectively "
                "unlimited, not that it is zero."
            )

        if self.cross_plant_stock:
            notes.append(
                "Stock at other plants is shown for information. The platform "
                "does not create transfers or reservations anywhere, and does "
                "not write to SAP at all."
            )

        if self.metric.gr_not_issued_flag:
            notes.append(
                f"Stock received {self.metric.gr_not_issued_days_since_gr} days "
                "ago for this part has still not been issued, which is already "
                "an open exception."
            )

        return tuple(notes)

    def as_record(self, today: date) -> dict[str, Any]:
        """The assessment as stored. Explicit, for the same reason as I08's."""
        metric = self.metric
        return {
            "flow": "i13",
            "material": self.material,
            "plant": self.plant,
            "requestedQuantity": _number(self.requested_quantity),
            "headline": self.headline,
            "caveats": list(self.caveats),
            "stockOnHand": _number(metric.stock_on_hand),
            "openPoQuantity": _number(metric.open_po_quantity),
            "averageMonthlyConsumption": _number(metric.average_monthly_consumption),
            "monthsOfCover": _number(metric.months_of_cover),
            "monthsOfCoverReason": metric.months_of_cover_reason,
            "projectedMonthsOfCover": _number(metric.projected_months_of_cover),
            "daysSinceLastMovement": metric.days_since_last_movement,
            "daysSinceLastIssue": metric.days_since_last_issue,
            "lastMovementDate": _iso(metric.last_movement_date),
            "consumptionCount12m": metric.consumption_count_12m,
            "consumedQty12m": _number(metric.consumed_qty_12m),
            "agingBand": metric.aging_band.value,
            "inventoryTurns": _number(metric.inventory_turns),
            "grNotIssuedFlag": metric.gr_not_issued_flag,
            "grNotIssuedDaysSinceGr": metric.gr_not_issued_days_since_gr,
            "acquiredVsPlanStatus": metric.acquired_vs_plan_status.value,
            "materialScope": metric.material_scope.value,
            "crossPlantStock": [
                {"plant": info.plant, "stockOnHand": _number(info.stock_on_hand)}
                for info in self.cross_plant_stock
            ],
            "referenceDate": today.isoformat(),
        }




def _number(value: Decimal | None) -> str | None:
    """Decimals as strings, never floats -- this record is evidence."""
    return None if value is None else str(value)


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def build(
    *,
    material: str,
    plant: str,
    metric: WatchMetric,
    cross_plant_stock: Sequence[CrossPlantStockInfo] = (),
    requested_quantity: Decimal | None = None,
) -> I13Assessment:
    """Assemble the I13 assessment from a WATCH row and cross-plant stock.

    Both are passed in rather than loaded here. The caller already holds a
    database session and the repositories, and a builder that opened its own
    would make this untestable without Postgres for no gain.

    **Plants holding nothing are dropped.** ``PostgresCrossPlantStockProvider``
    returns a row per plant the material is known at, including those with zero
    on hand, because the exception queue wants that completeness. In a sentence
    it reads as "other plants hold 0 at 1500" -- which says there is
    stock elsewhere and then says there is not. Filtering here rather than in
    the provider leaves ACT's use of it untouched.
    """
    with_stock = tuple(
        info for info in cross_plant_stock if info.stock_on_hand > Decimal("0")
    )
    return I13Assessment(
        material=material,
        plant=plant,
        requested_quantity=requested_quantity,
        metric=metric,
        cross_plant_stock=with_stock,
    )
