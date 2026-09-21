"""I08 FR-6 -- does a repairable unit already exist for this part?

The question the assistant asks on behalf of the business, at the one moment it
can still change the answer: somebody is about to reserve a new spare, and there
may already be one on the shelf or one coming back from repair. Buying a new
unit while a repaired one is inbound is the specific waste Initiative 08 exists
to find, and this is the check that finds it *before* the money is committed
rather than in a report afterwards.

A pure rule, and why that matters
----------------------------------
Nothing here touches a database. It takes rows that have already been loaded --
universe rows from W5.1 and repair lines from W5.2 -- and returns a verdict.

That is deliberate and has a concrete payoff: the same rule serves the assistant
(which holds a cached snapshot), the standalone ``/api/i8/repairable-unit``
endpoint, and its own unit tests, and none of them can disagree. A rule that
opened its own session would have to be re-tested through every caller.

Two units, and they are not the same claim
-------------------------------------------
"A repairable unit exists" is true in two quite different ways, and collapsing
them would be dishonest:

* **In stock** -- a repairable sitting in a bin right now. Available today.
* **On a repair order** -- one that has been sent away, or is about to be, and
  is due back on a date. Available *later*, and only if the repair succeeds.

Both are reported, separately, with their dates. A requester deciding whether to
wait needs to know which kind they are being offered.

What this rule may NOT say, given today's data
-----------------------------------------------
**It never says "the vendor has it."** Measured on the July extract: of 788 open
repair lines, **zero** carry a dispatch movement, so no open line is ever in the
``At Vendor`` state -- every one of them reads ``PO Issued``. The repair has been
ordered; nothing records the unit physically leaving. FR-5(a)'s headline
sentence, *"the vendor has it"*, therefore has no data behind it on this extract.

That is the extract, not the logic -- the ``At Vendor`` branch is implemented
because a live client will populate 541 movements and finding out at cutover
that the assistant cannot describe them would be worse. But on today's data the
assistant says *"a repair is on order and due back on the 3rd"*, which is true,
rather than *"the vendor has it"*, which is not.

Never invent, never default
----------------------------
``stock_on_hand`` is ``None`` when MARD has no row for the material at that
plant, and ``None`` is not zero. "There is no repairable in stock" and "no source
told us how much stock there is" are different answers, and only one of them
should stop somebody buying a part. The verdict keeps them apart and says which
it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Sequence

from app.initiatives.i8.material_number import normalise
from app.initiatives.i8.register import RepairLine
from app.initiatives.i8.universe import UniverseRow
from app.shared.numbers import plain


class UnitSource(str, Enum):
    """Where an existing repairable unit is, if there is one."""

    STOCK = "STOCK"
    """On the shelf now."""

    ON_REPAIR_ORDER = "ON_REPAIR_ORDER"
    """On an open repair purchase order -- coming back, on a date."""


@dataclass(frozen=True)
class RepairEvidence:
    """One open repair line, reduced to what the assistant is allowed to state.

    A projection rather than the whole ``RepairLine``: this is stored verbatim
    as "the advice as served", and storing the full line would put forty fields
    of register internals into an append-only audit record that nobody reads
    back.
    """

    purchasing_document: str
    item: str
    quantity: Decimal
    raised_at: date | None
    due_date: date | None
    days_overdue: int | None
    """Positive when past the promised date. ``None`` when there is no due date
    to be late against -- 61 of 788 open lines, where the answer is "nobody
    promised a date", not "on time"."""

    vendor: str | None
    vendor_name: str | None
    """``None`` for a vendor LFA1 does not cover -- 106 vendors against 454
    suppliers. Served as a code rather than invented as a name."""

    status: str
    """The register's own ``repair_status``. On today's extract this is always
    ``PO Issued`` for an open line; see the module docstring."""

    dispatched: bool
    """Whether a dispatch movement exists. False on every open line today, and
    the reason the assistant does not claim the vendor holds the unit."""


@dataclass(frozen=True)
class RepairableUnitVerdict:
    """FR-6's answer, with everything it was decided from."""

    material_id: str
    plant: str | None

    is_repairable_material: bool
    """Whether the part is in the I08 repairable universe at all. When False
    every other field is empty and the answer is simply "this is not a
    repairable part" -- which is a valid, useful answer, not a failure."""

    exists: bool
    """Whether a repairable unit exists by either route."""

    sources: tuple[UnitSource, ...]

    stock_on_hand: Decimal | None
    """``None`` means no MARD row -- unknown, NOT zero."""

    stock_locations: int

    open_repair_lines: int
    quantity_under_repair: Decimal

    soonest_due_date: date | None
    """The earliest date any open repair is due back. What a requester decides
    against when choosing whether to wait."""

    overdue_lines: int

    evidence: tuple[RepairEvidence, ...]

    @property
    def has_stock(self) -> bool:
        """True only when stock is *known* and positive."""
        return self.stock_on_hand is not None and self.stock_on_hand > 0

    @property
    def stock_is_unknown(self) -> bool:
        return self.stock_on_hand is None

    @property
    def headline(self) -> str:
        """One sentence, written to be read by the person about to reserve.

        Deterministic. The language model may be asked to rewrite this more
        naturally (see ``app.assistant.narrative``), but this is the sentence of
        record and it is the one stored.
        """
        if not self.is_repairable_material:
            return (
                f"{self.material_id} is not an 80-series repairable part, so "
                "there is no repairable unit to look for."
            )

        parts: list[str] = []

        if self.has_stock:
            where = (
                f" across {self.stock_locations} storage locations"
                if self.stock_locations > 1
                else ""
            )
            parts.append(
                f"There {'is' if self.stock_on_hand == 1 else 'are'} "
                f"{plain(self.stock_on_hand)} in stock at plant {self.plant}{where}"
            )

        if self.open_repair_lines:
            line_word = "repair" if self.open_repair_lines == 1 else "repairs"
            clause = (
                f"{self.open_repair_lines} open {line_word} for "
                f"{plain(self.quantity_under_repair)} unit"
                f"{'' if self.quantity_under_repair == 1 else 's'}"
            )
            if self.soonest_due_date is not None:
                clause += f", the earliest due back {self.soonest_due_date.isoformat()}"
                if self.overdue_lines:
                    clause += (
                        f" ({self.overdue_lines} of them already past the "
                        "promised date)"
                    )
            else:
                clause += ", with no promised return date recorded"
            parts.append(("and there " if parts else "There ") + f"{'is' if self.open_repair_lines == 1 else 'are'} {clause}")

        if not parts:
            if self.stock_is_unknown:
                return (
                    f"No repair is open for {self.material_id} at plant "
                    f"{self.plant}, and no stock record exists for it there -- "
                    "so we cannot tell whether a repairable unit is on the "
                    "shelf. Worth a physical check before buying new."
                )
            return (
                f"No repairable unit exists for {self.material_id} at plant "
                f"{self.plant}: none in stock and no repair on order."
            )

        return ". ".join(parts) + "."

    @property
    def caveats(self) -> tuple[str, ...]:
        """What the requester should not read into the answer.

        Served alongside the headline rather than folded into it. A sentence
        that hedges every clause is unreadable; a short list of limits under it
        is not.
        """
        notes: list[str] = []

        if self.stock_is_unknown and self.is_repairable_material:
            notes.append(
                f"No stock record exists for {self.material_id} at plant "
                f"{self.plant}. That means no source told us, not that stock is zero."
            )

        if self.open_repair_lines and not any(e.dispatched for e in self.evidence):
            notes.append(
                "A repair being open means the purchase order exists, not that "
                "the unit has physically reached the vendor -- no dispatch "
                "movement is recorded against it."
            )

        if self.overdue_lines:
            notes.append(
                f"{self.overdue_lines} of these repairs {'is' if self.overdue_lines == 1 else 'are'} "
                "already past the promised return date, so the due date is a "
                "plan rather than a commitment."
            )

        missing_vendor = sum(1 for e in self.evidence if e.vendor and not e.vendor_name)
        if missing_vendor:
            notes.append(
                f"{missing_vendor} repair vendor"
                f"{'' if missing_vendor == 1 else 's'} shown by code only -- the "
                "supplier master does not cover them."
            )

        return tuple(notes)




def assess(
    *,
    material_id: str,
    plant: str | None,
    universe_rows: Sequence[UniverseRow],
    repair_lines: Sequence[RepairLine],
    today: date,
) -> RepairableUnitVerdict:
    """Decide FR-6 for one material at one plant.

    ``universe_rows`` and ``repair_lines`` are the *whole* sets -- the snapshot's
    -- and are narrowed here. The caller does not pre-filter, because the
    matching rule (normalised material, and a plant that may be ``None`` on a
    universe row) is exactly the thing that should live in one place.
    """
    material_key = normalise(material_id) or ""
    plant_key = (plant or "").strip() or None

    rows = [
        row
        for row in universe_rows
        if row.material_id == material_key
        and (plant_key is None or row.plant == plant_key)
    ]

    if not rows:
        # Not in the repairable universe. Every other field stays empty rather
        # than being filled with zeros that would read as "we checked and found
        # nothing" -- we did not check, because there was nothing to check.
        return RepairableUnitVerdict(
            material_id=material_key,
            plant=plant_key,
            is_repairable_material=False,
            exists=False,
            sources=(),
            stock_on_hand=None,
            stock_locations=0,
            open_repair_lines=0,
            quantity_under_repair=Decimal(0),
            soonest_due_date=None,
            overdue_lines=0,
            evidence=(),
        )

    # Stock: summed across the matched rows, but None when NO row knows. A row
    # with no MARD entry contributes nothing rather than a zero, so "one plant
    # has 2 and another has no record" reports 2, not 2-and-a-guess.
    known_stock = [row.stock_on_hand for row in rows if row.stock_on_hand is not None]
    stock_on_hand = sum(known_stock, Decimal(0)) if known_stock else None
    stock_locations = sum(row.storage_locations for row in rows)

    open_lines = [
        line
        for line in repair_lines
        if line.material_id == material_key
        and line.is_open
        and (plant_key is None or line.plant == plant_key)
    ]

    evidence = tuple(
        RepairEvidence(
            purchasing_document=line.purchasing_document,
            item=line.item,
            quantity=line.qty_under_repair,
            raised_at=line.raised_at,
            due_date=line.due_date,
            # Computed against the caller's date, not the snapshot's. The
            # assistant states this to somebody at the moment they are
            # deciding, and a process-lifetime cache would freeze "today" at
            # start-up -- see get_snapshot's own note about that.
            days_overdue=(
                (today - line.due_date).days
                if line.due_date is not None and today > line.due_date
                else None
            ),
            vendor=line.vendor,
            vendor_name=line.vendor_name,
            status=line.repair_status,
            dispatched=line.dispatched_at is not None,
        )
        for line in open_lines
    )

    due_dates = [line.due_date for line in open_lines if line.due_date is not None]
    quantity_under_repair = sum(
        (line.qty_under_repair for line in open_lines), Decimal(0)
    )

    sources: list[UnitSource] = []
    if stock_on_hand is not None and stock_on_hand > 0:
        sources.append(UnitSource.STOCK)
    if open_lines:
        sources.append(UnitSource.ON_REPAIR_ORDER)

    return RepairableUnitVerdict(
        material_id=material_key,
        plant=plant_key,
        is_repairable_material=True,
        exists=bool(sources),
        sources=tuple(sources),
        stock_on_hand=stock_on_hand,
        stock_locations=stock_locations,
        open_repair_lines=len(open_lines),
        quantity_under_repair=quantity_under_repair,
        soonest_due_date=min(due_dates) if due_dates else None,
        overdue_lines=sum(1 for item in evidence if item.days_overdue is not None),
        evidence=evidence,
    )
