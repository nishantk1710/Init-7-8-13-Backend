"""FR-8 -- a new unit bought while a repair of the same part was still out.

The duplicate spend Initiative 08 exists to stop, measured after the fact:
somebody raised a purchase order for a brand-new repairable part at a plant
where the same part was already away being repaired. FR-5 asks for a
justification at the moment of the reservation; FR-8 flags the purchases that
went ahead without one. This module finds the purchases and the repair each one
overlapped. :func:`app.initiatives.i8.exceptions.unjustified_acquisitions`
decides which of them lack a justification.

Why this is not blocked on RESB.BEDNR
--------------------------------------
``MISSING_SESSION_ID`` genuinely is: it needs the session id read back off the
reservation, and that field is not exposed. This check needs nothing SAP does
not already give us. The purchase line is in EKPO (item category 0 on an
80-series material, FRS s4.1), the repair it overlapped is in the register, and
a justification carries material, plant and date -- the same material + plant +
window key the attestation check uses, for the same reason. When BEDNR lands,
an exact session match can replace the window; the detector does not change
shape.

What "a repairable unit existed" means here
--------------------------------------------
FR-6 has two limbs. Only one can be judged for a purchase in the past:

* **An open repair** is reconstructable -- the repair line's raised date and its
  receipt say whether it was out on the day the purchase was raised.
* **Stock on hand** is not. MARD is a snapshot of today, so it cannot say what
  was on the shelf the day a 2024 purchase was raised, and reading today's
  stock into that date would accuse purchases on evidence that did not exist
  when they were made.

So a purchase is flagged only when a repair of the same material was open at
the same plant when it was raised. That is narrower than FR-6, deliberately,
and it is the case that is unambiguously duplicate spend.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series, normalise
from app.initiatives.i8.register import RepairLine, is_deleted_line

#: The justification kind that answers a new purchase. The same value the
#: assistant writes when a requester proceeds past the I08 challenge
#: (``app.assistant.turns``).
NEW_ACQUISITION_KIND = "NEW_ACQUISITION"

ZERO = Decimal(0)


@dataclass(frozen=True)
class NewAcquisition:
    """One purchase line that bought a new 80-series unit."""

    purchasing_document: str
    item: str
    material_id: str
    plant: str | None
    description: str | None
    ordered_qty: Decimal
    raised_at: date | None
    """EKPO.ERDAT, the line's own creation date. Populated on every new-purchase
    line in the July extract."""

    @property
    def key(self) -> tuple[str, str]:
        return (self.purchasing_document, self.item)


@dataclass(frozen=True)
class JustificationRecord:
    """A NEW_ACQUISITION justification, reduced to what matching needs."""

    material_id: str
    plant: str
    recorded_on: date
    exception_id: str | None = None


def is_new_purchase_line(row: Mapping, cfg: I8Settings) -> bool:
    """A standard purchase of an 80-series part that SAP has not deleted.

    The item-category test lives here, in Python, for the same reason the
    repair test does in :func:`app.initiatives.i8.register.is_repair_line`.
    """
    return (
        row["pstyp"] == cfg.new_purchase_item_category
        and is_eighty_series(row["matnr"], cfg)
        and not is_deleted_line(row, cfg)
    )


def find_new_acquisitions(
    candidates: Iterable[Mapping], cfg: I8Settings | None = None
) -> list[NewAcquisition]:
    """Every new-purchase line in an EKPO pull the register already fetched."""
    cfg = cfg or get_i8_settings()
    return [
        NewAcquisition(
            purchasing_document=row["ebeln"],
            item=row["ebelp"],
            material_id=row["matnr"],
            plant=row["werks"],
            description=row["txz01"],
            ordered_qty=row["menge"] if row["menge"] is not None else ZERO,
            raised_at=row["erdat"],
        )
        for row in candidates
        if is_new_purchase_line(row, cfg)
    ]


def repairs_by_material_plant(
    lines: Iterable[RepairLine],
) -> dict[tuple[str, str | None], list[RepairLine]]:
    """(material, plant) -> repair lines. Built once; 2,331 purchases are
    matched against it rather than each scanning the whole register."""
    index: dict[tuple[str, str | None], list[RepairLine]] = {}
    for line in lines:
        index.setdefault((line.material_id, line.plant), []).append(line)
    return index


def open_repair_at(
    acquisition: NewAcquisition,
    repairs: Mapping[tuple[str, str | None], Sequence[RepairLine]],
) -> RepairLine | None:
    """The repair of the same part at the same plant that was out when this
    purchase was raised, or None.

    "Out" means raised on or before the purchase and not yet received back by
    then. Where several overlap, the one due back soonest is returned -- the
    unit the buyer could most plausibly have waited for. A repair with no due
    date sorts last, and the register key breaks ties so the answer is stable.
    """
    if acquisition.raised_at is None or acquisition.plant is None:
        return None
    bought = acquisition.raised_at
    overlapping = [
        line
        for line in repairs.get((acquisition.material_id, acquisition.plant), ())
        if line.raised_at is not None
        and line.raised_at <= bought
        and (line.received_at is None or line.received_at > bought)
    ]
    if not overlapping:
        return None
    return min(
        overlapping,
        key=lambda line: (line.due_date is None, line.due_date or date.max, line.key),
    )


def load_justifications(db: Session) -> list[JustificationRecord]:
    """Every NEW_ACQUISITION justification, from the shared WS7 table.

    One query for the whole table, read once per view build -- the same shape
    as the attestation coverage check.
    """
    from app.assistant.models import Justification

    rows = db.execute(
        select(
            Justification.material_id,
            Justification.plant,
            Justification.recorded_at,
            Justification.exception_id,
        ).where(Justification.kind == NEW_ACQUISITION_KIND)
    ).all()
    return [
        JustificationRecord(
            # Normalised on both sides, as the attestation check does: a
            # justification typed against a padded number is about the same
            # part as a purchase read unpadded.
            material_id=normalise(material_id) or material_id,
            plant=(plant or "").strip(),
            recorded_on=recorded_at.date(),
            exception_id=exception_id,
        )
        for material_id, plant, recorded_at, exception_id in rows
    ]
