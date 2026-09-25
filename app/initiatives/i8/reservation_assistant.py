"""I08 FR-5 -- what the assistant tells somebody reserving a repairable spare.

The moment this exists for
---------------------------
A planner is creating a reservation for an 80-series part. Somewhere in the
system there may already be one of those parts on a repair order, due back next
week. Nobody checks, because checking means leaving the transaction and reading
a report that does not exist. So a new unit gets bought, and the repaired one
arrives into a store that no longer needs it.

This assembles the answer to *"is that happening right now?"* out of read models
that were already built and tested, and adds one number of its own: how long
buying new actually takes, so "wait for the repair" can be compared against
something rather than asserted.

Assembly, not computation
--------------------------
Everything here already exists. The repairable-unit verdict is FR-6
(``repairable_unit.py``); the repair lifecycle, vendor and due dates are W5.2's
register; criticality is the shared W3.4 source; the lead time for a new unit is
MARC's PLIFZ, already carried on every universe row. This module's job is to put
them in one place in the order a person would ask for them, and to be careful
about what it does not know.

The comparison the requester is actually making
------------------------------------------------
Two dates:

* the **soonest a repaired unit is due back**, from the open repair order;
* the **planned delivery time for a new one**, from MARC.

:attr:`I08Assessment.waiting_beats_buying` puts those together, and is ``None``
rather than a guess whenever either side is missing. MARC covers roughly two
thirds of the universe and holds no Gamsberg rows at all, so "we cannot compare
these" is a common and correct answer -- and far better than comparing a real
date against a defaulted zero, which would make waiting always look faster.

Overdue dates are the honest part
----------------------------------
695 of 788 open repair lines in this extract are already past their promised
return date. Serving that date as if it were a commitment would make the
assistant's central claim -- "one is coming back on the 3rd" -- the least
reliable sentence on the screen. So the overdue count and the caveat travel with
the advice, and :attr:`I08Assessment.repair_due_date_is_reliable` says plainly
when the date should not be leaned on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

from app.initiatives.i8.material_number import normalise
from app.initiatives.i8.register import RepairLine
from app.initiatives.i8.repairable_unit import RepairableUnitVerdict
from app.initiatives.i8.repairable_unit import assess as assess_repairable_unit
from app.initiatives.i8.universe import UniverseRow


@dataclass(frozen=True)
class I08Assessment:
    """Everything the I08 flow states, before anybody is asked anything.

    Stored verbatim as "the advice as served" -- both FRSs require keeping it,
    because benefit attribution reads it months later and asks what the platform
    actually said. Recomputing it then would answer a different question: the
    register moves, and an answer regenerated in March is not the one somebody
    acted on in September.
    """

    material_id: str
    plant: str
    description: str | None
    criticality: str | None
    """One of the five ZMM065 tiers, or ``None``. **Never defaulted** -- an
    unknown criticality displayed as NORMAL is worse than one displayed as
    unknown."""

    verdict: RepairableUnitVerdict
    """FR-6's answer, in full."""

    new_unit_lead_time_days: int | None
    """MARC.PLIFZ -- calendar days to buy a new one. ``None`` where MARC has no
    row or the value is unmaintained, which is the majority of the universe."""

    @property
    def repair_due_date(self) -> date | None:
        return self.verdict.soonest_due_date

    @property
    def repair_due_date_is_reliable(self) -> bool | None:
        """Whether the due date should be leaned on.

        ``None`` when there is no date at all. ``False`` when every open repair
        for this part is already past its promised date -- at which point the
        date is a plan that has already been missed, not a forecast.
        """
        if self.repair_due_date is None:
            return None
        return self.verdict.overdue_lines < self.verdict.open_repair_lines

    def waiting_beats_buying(self, today: date) -> bool | None:
        """Whether waiting for the repair lands sooner than buying new.

        ``None`` when the question cannot be answered honestly, which is three
        distinct cases and all of them common on this extract:

        * no due date on any open repair (61 of 788 lines);
        * no planned delivery time in MARC, which is most of the universe;
        * **the due date has already passed.** This one is the trap. An overdue
          repair has a due date in the past, so a naive comparison finds it
          "sooner" than any positive lead time and recommends waiting -- for a
          unit that is already late and has no new forecast. 695 of the 788 open
          repair lines here are in exactly that state, so the naive answer would
          have been wrong on 88% of them, and wrong in the direction that costs
          money and keeps a machine down.

        Never resolved to a default. Comparing a real date against a defaulted
        zero lead time would make waiting look faster every time.
        """
        if self.repair_due_date is None or self.new_unit_lead_time_days is None:
            return None

        days_until_repair = (self.repair_due_date - today).days
        if days_until_repair < 0:
            # The promised date has passed and nothing arrived. There is no
            # forecast left to compare against -- only a date that was missed.
            return None

        return days_until_repair <= self.new_unit_lead_time_days

    def headline(self, today: date) -> str:
        """The sentence of record, written to be read by the person reserving."""
        opening = self.verdict.headline

        if not self.verdict.exists:
            if self.new_unit_lead_time_days is not None:
                return (
                    f"{opening} Buying a new one takes about "
                    f"{self.new_unit_lead_time_days} days."
                )
            return opening

        buying = (
            f"about {self.new_unit_lead_time_days} days"
            if self.new_unit_lead_time_days is not None
            else None
        )
        overdue_by = (
            (today - self.repair_due_date).days
            if self.repair_due_date is not None and today > self.repair_due_date
            else None
        )

        # The overdue case first, because it is the common one here and because
        # it is the one a date comparison gets backwards.
        if overdue_by is not None:
            tail = (
                f" Buying a new one takes {buying}."
                if buying
                else " No planned delivery time is held for this part."
            )
            return (
                f"{opening} That repair was due {overdue_by} days ago and still "
                f"has not arrived, so its date is no longer a forecast -- chase "
                f"it before relying on it.{tail}"
            )

        comparison = self.waiting_beats_buying(today)

        if comparison is None:
            if buying is None:
                return (
                    f"{opening} No planned delivery time is held for this part, "
                    "so we cannot say how a new order would compare."
                )
            return f"{opening} Buying a new one takes {buying}."

        days_until_repair = (self.repair_due_date - today).days  # type: ignore[union-attr]
        if comparison:
            return (
                f"{opening} That repair is due in {days_until_repair} days, "
                f"against {buying} to buy a new one -- waiting looks faster."
            )
        return (
            f"{opening} That repair is not due for {days_until_repair} days, "
            f"which is longer than the {buying} a new one takes -- buying may "
            "genuinely be the faster route here."
        )

    def as_record(self, today: date) -> dict[str, Any]:
        """The assessment as a JSON-serialisable dictionary, for storage.

        Explicit rather than ``dataclasses.asdict``. This is written into an
        append-only column and read back months later, so the shape has to be a
        deliberate choice that changes only when somebody means it to -- not
        something that silently gains a field because a dataclass did.
        """
        return {
            "flow": "i08",
            "materialId": self.material_id,
            "plant": self.plant,
            "description": self.description,
            "criticality": self.criticality,
            "headline": self.headline(today),
            "caveats": list(self.verdict.caveats),
            "repairableUnitExists": self.verdict.exists,
            "sources": [source.value for source in self.verdict.sources],
            "stockOnHand": _number(self.verdict.stock_on_hand),
            "stockIsUnknown": self.verdict.stock_is_unknown,
            "openRepairLines": self.verdict.open_repair_lines,
            "quantityUnderRepair": _number(self.verdict.quantity_under_repair),
            "soonestDueDate": _iso(self.verdict.soonest_due_date),
            "overdueLines": self.verdict.overdue_lines,
            "repairDueDateIsReliable": self.repair_due_date_is_reliable,
            "newUnitLeadTimeDays": self.new_unit_lead_time_days,
            "waitingBeatsBuying": self.waiting_beats_buying(today),
            "referenceDate": today.isoformat(),
            "openRepairs": [
                {
                    "purchasingDocument": item.purchasing_document,
                    "item": item.item,
                    "quantity": _number(item.quantity),
                    "raisedAt": _iso(item.raised_at),
                    "dueDate": _iso(item.due_date),
                    "daysOverdue": item.days_overdue,
                    "vendor": item.vendor,
                    "vendorName": item.vendor_name,
                    "status": item.status,
                    "dispatched": item.dispatched,
                }
                for item in self.verdict.evidence
            ],
        }


def _number(value: Decimal | None) -> str | None:
    """Decimals as strings, never floats.

    A quantity that round-trips through a float comes back as 2.9999999999999996,
    and this record is evidence.
    """
    return None if value is None else str(value)


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def build(
    *,
    material_id: str,
    plant: str,
    universe_rows: Sequence[UniverseRow],
    repair_lines: Sequence[RepairLine],
    today: date,
) -> I08Assessment:
    """Assemble the I08 assessment for one material at one plant.

    Takes the whole snapshot's rows and narrows them here, for the same reason
    ``repairable_unit.assess`` does: the matching rule belongs in one place, and
    this function delegates to that one rather than repeating it.
    """
    material_key = normalise(material_id) or ""
    plant_key = (plant or "").strip()

    verdict = assess_repairable_unit(
        material_id=material_key,
        plant=plant_key,
        universe_rows=universe_rows,
        repair_lines=repair_lines,
        today=today,
    )

    rows = [
        row
        for row in universe_rows
        if row.material_id == material_key and row.plant == plant_key
    ]

    # Both are taken from the first row that HAS one rather than the first row.
    # A universe row exists per material+plant, but the sources behind it cover
    # different subsets, so the first row is quite often the one that knows
    # least -- and None here means "no source knows", which must not be produced
    # by picking badly.
    description = next((row.description for row in rows if row.description), None)
    criticality = next((row.criticality for row in rows if row.criticality), None)
    lead_time = next(
        (row.planned_delivery_days for row in rows if row.planned_delivery_days), None
    )

    return I08Assessment(
        material_id=material_key,
        plant=plant_key,
        description=description,
        criticality=criticality,
        verdict=verdict,
        new_unit_lead_time_days=lead_time,
    )
