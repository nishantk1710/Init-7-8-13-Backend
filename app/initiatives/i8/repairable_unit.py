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

Since 27-Oct-2026 there are two more things to keep apart:

* **Requested, not yet ordered** -- an open repair PR with no PO
  (``requisitions.RepairRequest``). The repair order has asked for the unit to
  be repaired, but purchasing has not ordered it, so there is no vendor and no
  return date. 108 of them on the 15-Oct extract, invisible to the PO-only
  register. Counted only when the caller passes them, which it does only while
  ``I8_REPAIR_REQUESTS_ENABLED`` is on.
* **Usable or damaged** -- the stock on hand is the MARD total, which counts a
  damaged unit the same as a new one. Where the part has condition rows (MCHB
  batches by default: NEW 1,667 / REFURBISHD 975 / DAMAGED 790 on the 15-Oct
  extract) only the usable conditions count, so a shelf holding nothing but
  damaged units no longer reads as "a unit exists". Where it has none, the
  MARD total stands and the answer says the condition is unknown.

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
from typing import TYPE_CHECKING, Callable, Sequence

from app.initiatives.i8.material_number import normalise
from app.initiatives.i8.register import RepairLine
from app.initiatives.i8.universe import UniverseRow
from app.shared.numbers import plain

if TYPE_CHECKING:
    from app.initiatives.i8.requisitions import RepairRequest


class UnitSource(str, Enum):
    """Where an existing repairable unit is, if there is one."""

    STOCK = "STOCK"
    """On the shelf now (usable units only, where the condition is known)."""

    ON_REPAIR_ORDER = "ON_REPAIR_ORDER"
    """On an open repair purchase order -- coming back, on a date."""

    REPAIR_REQUESTED = "REPAIR_REQUESTED"
    """On an open repair PR with no PO yet -- asked for, not ordered, no date."""


#: How a condition code reads in a sentence. SAP's batch names are cut to ten
#: characters ('REFURBISHD'); any code not listed here is shown lower-cased as
#: SAP sent it rather than guessed at.
_CONDITION_WORDS = {"NEW": "new", "REFURBISHD": "refurbished", "DAMAGED": "damaged"}


def _condition_word(condition: str) -> str:
    return _CONDITION_WORDS.get(condition, condition.lower())


def request_age_phrase(days: int | None) -> str:
    """``requested N days ago``, as the frontend's ``requestAgePhrase`` words it.

    A PR dated after the reference date cannot happen in SAP order; if the data
    ever says so it reads as today rather than a negative age.
    """
    if days is None:
        return "request date not known"
    if days <= 0:
        return "requested today"
    if days == 1:
        return "requested 1 day ago"
    return f"requested {days} days ago"


@dataclass(frozen=True)
class RepairRequestEvidence:
    """One open repair PR with no PO yet, reduced to what the assistant states.

    Stored verbatim with the advice, like :class:`RepairEvidence`; the age is
    computed against the caller's date so a replayed session shows the age it
    showed then.
    """

    purchase_requisition: str
    item: str
    quantity: Decimal
    requested_on: date | None
    """EBAN.BADAT, the PR date."""
    days_since_requested: int | None
    """``requested_on`` to the caller's today. None when the PR has no date."""
    department: str | None
    """EBAN.AFNAM, the 5-digit requesting department code."""
    department_name: str | None
    """The department list's name for the code; None when it has none."""
    repair_order: str | None
    """RESB.AUFNR on the repair order's open 543 line, when it names one."""


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
    """Whether a repairable unit exists by any route: usable stock (the MARD
    total when the condition is unknown), an open repair PO, or -- when the
    caller counted them -- an open repair PR."""

    sources: tuple[UnitSource, ...]

    stock_on_hand: Decimal | None
    """``None`` means no MARD row -- unknown, NOT zero. The MARD total, damaged
    units included; see :attr:`usable_stock`."""

    stock_locations: int

    open_repair_lines: int
    quantity_under_repair: Decimal

    soonest_due_date: date | None
    """The earliest date any open repair is due back. What a requester decides
    against when choosing whether to wait."""

    overdue_lines: int

    evidence: tuple[RepairEvidence, ...]

    # --- since 27-Oct-2026: open repair PRs and the usable/damaged split -----
    # Appended with defaults: older callers and tests construct this by keyword
    # without them, and an absent field must read as "not counted".

    request_evidence: tuple[RepairRequestEvidence, ...] = ()
    """Open repair PRs with no PO yet, oldest first. Empty when none, or when
    the caller did not count them (:attr:`repair_requests_counted`)."""

    repair_requests_counted: bool = False
    """Whether the caller passed the open repair PRs at all. Off, "no open
    repair" would claim more than was checked, so the wording stays "no open
    repair PO"."""

    usable_stock: Decimal | None = None
    """Units in a usable condition (new, refurbished). None when the condition
    is unknown -- then :attr:`stock_on_hand` is all there is."""

    damaged_stock: Decimal | None = None
    """Damaged units, never counted as a unit that exists. None when unknown."""

    usable_breakdown: tuple[tuple[str, Decimal], ...] = ()
    """The usable units by condition, in the configured order
    (``I8_USABLE_STOCK_CONDITIONS``), conditions holding none left out."""

    stock_condition_known: bool = False
    """True when the part has condition rows, so the split can be trusted."""

    stock_source: str | None = None
    """``MCHB`` / ``MBEW`` when the condition is known, ``MARD`` when only the
    total is (the fallback), None when there is no stock record at all."""

    @property
    def has_stock(self) -> bool:
        """True only when stock is *known* and positive. The MARD total."""
        return self.stock_on_hand is not None and self.stock_on_hand > 0

    @property
    def has_usable_stock(self) -> bool:
        """What the verdict counts: usable units where the condition is known,
        the MARD total where it is not."""
        if self.stock_condition_known:
            return self.usable_stock is not None and self.usable_stock > 0
        return self.has_stock

    @property
    def stock_is_unknown(self) -> bool:
        return self.stock_on_hand is None

    @property
    def open_repair_request_lines(self) -> int:
        return len(self.request_evidence)

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

        # Each clause is its own sentence. Until 27-Oct-2026 the second began
        # "and there", which the ". " join below turned into ". and there".
        parts: list[str] = []
        where = (
            f" across {self.stock_locations} storage locations"
            if self.stock_locations > 1
            else ""
        )

        if self.stock_condition_known:
            if self.has_usable_stock:
                usable = self.usable_stock
                clause = (
                    f"There {'is' if usable == 1 else 'are'} {plain(usable)} "
                    f"usable in stock at plant {self.plant}{where}"
                )
                # The breakdown only says something when there is more than
                # one condition to tell apart.
                if len(self.usable_breakdown) > 1:
                    clause += " (" + ", ".join(
                        f"{plain(units)} {_condition_word(condition)}"
                        for condition, units in self.usable_breakdown
                    ) + ")"
                if self.damaged_stock:
                    clause += f"; {self._damaged_clause()}"
                parts.append(clause)
        elif self.has_stock:
            parts.append(
                f"There {'is' if self.stock_on_hand == 1 else 'are'} "
                f"{plain(self.stock_on_hand)} in stock at plant {self.plant}{where} "
                "(condition unknown)"
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
            parts.append(f"There {'is' if self.open_repair_lines == 1 else 'are'} {clause}")

        if self.request_evidence:
            count = len(self.request_evidence)
            known_ages = [
                e.days_since_requested
                for e in self.request_evidence
                if e.days_since_requested is not None
            ]
            if count == 1:
                age = request_age_phrase(self.request_evidence[0].days_since_requested)
                parts.append(f"There is 1 repair requested, not yet ordered ({age})")
            else:
                clause = f"There are {count} repairs requested, not yet ordered"
                if known_ages:
                    clause += f" (the oldest {request_age_phrase(max(known_ages))})"
                parts.append(clause)

        if not parts:
            repair = "no open repair" if self.repair_requests_counted else "no open repair PO"
            if self.stock_is_unknown and not self.stock_condition_known:
                return (
                    f"No repair is open for {self.material_id} at plant "
                    f"{self.plant}, and no stock record exists for it there -- "
                    "so we cannot tell whether stock is available. Worth a "
                    "physical check before buying new."
                )
            if self.damaged_stock:
                return (
                    f"No repairable unit exists for {self.material_id} at plant "
                    f"{self.plant}: no usable stock ({self._damaged_clause()}) "
                    f"and {repair}."
                )
            return (
                f"No repairable unit exists for {self.material_id} at plant "
                f"{self.plant}: no stock available and {repair}."
            )

        return ". ".join(parts) + "."

    def _damaged_clause(self) -> str:
        damaged = self.damaged_stock or Decimal(0)
        return (
            f"{plain(damaged)} damaged unit{'' if damaged == 1 else 's'} "
            f"{'is' if damaged == 1 else 'are'} not counted"
        )

    @property
    def caveats(self) -> tuple[str, ...]:
        """What the requester should not read into the answer.

        Served alongside the headline rather than folded into it. A sentence
        that hedges every clause is unreadable; a short list of limits under it
        is not.
        """
        notes: list[str] = []

        if (
            self.stock_is_unknown
            and not self.stock_condition_known
            and self.is_repairable_material
        ):
            notes.append(
                f"No stock record exists for {self.material_id} at plant "
                f"{self.plant}. That means no source told us, not that stock is zero."
            )

        # Only where there is stock to split: a part whose MARD total is 0 has
        # no units whose condition matters (the 1,516 MARD rows with no MCHB
        # rows on the 15-Oct extract all hold 0).
        if (
            self.is_repairable_material
            and not self.stock_condition_known
            and self.has_stock
        ):
            notes.append(
                "The stock record does not say which units are usable and which "
                "are damaged."
            )

        if self.request_evidence and not self.open_repair_lines:
            notes.append(
                "A requested repair has no PO yet, so there is no date for its return."
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
    repair_requests: Sequence[RepairRequest] | None = None,
    department_name: Callable[[str | None], str | None] | None = None,
    usable_conditions: Sequence[str] | None = None,
) -> RepairableUnitVerdict:
    """Decide FR-6 for one material at one plant.

    ``universe_rows`` and ``repair_lines`` are the *whole* sets -- the snapshot's
    -- and are narrowed here. The caller does not pre-filter, because the
    matching rule (normalised material, and a plant that may be ``None`` on a
    universe row) is exactly the thing that should live in one place.

    ``repair_requests`` is the snapshot's whole set of open repair PRs, narrowed
    the same way. ``None`` means "not counted" (the caller's switch,
    ``I8_REPAIR_REQUESTS_ENABLED``, is off) and keeps the old wording; an empty
    sequence means counted and none found. ``department_name`` turns a
    department code into its name (``departments.name_for``); without it, or
    when it has no name, the code is shown alone. ``usable_conditions`` orders
    the usable breakdown; it defaults to ``I8_USABLE_STOCK_CONDITIONS``.
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

    # Every register line is open in SAP -- closed ones are never built.
    open_lines = [
        line
        for line in repair_lines
        if line.material_id == material_key
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

    split = _stock_split(rows, usable_conditions)

    open_requests = (
        [
            request
            for request in repair_requests
            if request.material_id == material_key
            and (plant_key is None or request.plant == plant_key)
        ]
        if repair_requests is not None
        else []
    )
    # Oldest first, as the register lists them; an undated PR goes last.
    open_requests.sort(
        key=lambda r: (r.requested_on is None, r.requested_on or date.min, r.key)
    )
    request_evidence = tuple(
        RepairRequestEvidence(
            purchase_requisition=request.purchase_requisition,
            item=request.item,
            quantity=request.quantity,
            requested_on=request.requested_on,
            # Against the caller's date, for the reason given on days_overdue.
            days_since_requested=(
                (today - request.requested_on).days
                if request.requested_on is not None
                else None
            ),
            department=request.department,
            department_name=(
                department_name(request.department)
                if department_name is not None and request.department
                else None
            ),
            repair_order=request.repair_order,
        )
        for request in open_requests
    )

    sources: list[UnitSource] = []
    if split.counts_as_stock(stock_on_hand):
        sources.append(UnitSource.STOCK)
    if open_lines:
        sources.append(UnitSource.ON_REPAIR_ORDER)
    if request_evidence:
        sources.append(UnitSource.REPAIR_REQUESTED)

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
        request_evidence=request_evidence,
        repair_requests_counted=repair_requests is not None,
        usable_stock=split.usable,
        damaged_stock=split.damaged,
        usable_breakdown=split.breakdown,
        stock_condition_known=split.known,
        stock_source=split.source if split.known else ("MARD" if stock_on_hand is not None else None),
    )


@dataclass(frozen=True)
class _StockSplit:
    known: bool
    source: str | None
    usable: Decimal | None
    damaged: Decimal | None
    breakdown: tuple[tuple[str, Decimal], ...]

    def counts_as_stock(self, stock_on_hand: Decimal | None) -> bool:
        if self.known:
            return self.usable is not None and self.usable > 0
        return stock_on_hand is not None and stock_on_hand > 0


_UNKNOWN_SPLIT = _StockSplit(known=False, source=None, usable=None, damaged=None, breakdown=())


def _stock_split(
    rows: Sequence[UniverseRow], usable_conditions: Sequence[str] | None
) -> _StockSplit:
    """Usable and damaged units across the matched rows, or unknown.

    Known only when every row that holds stock says what condition it is in. A
    row whose MARD total is 0 and has no condition rows says nothing either way
    and is passed over (1,516 such rows on the 15-Oct extract, all LABST 0); a
    row holding stock with no condition rows makes the whole answer unknown, so
    a multi-plant total never mixes split and unsplit units.
    """
    with_condition = [row for row in rows if row.stock_condition_source is not None]
    if not with_condition:
        return _UNKNOWN_SPLIT
    if any(
        row.stock_condition_source is None
        and row.stock_on_hand is not None
        and row.stock_on_hand > 0
        for row in rows
    ):
        return _UNKNOWN_SPLIT

    if usable_conditions is None:
        from app.initiatives.i8.config import get_i8_settings

        usable_conditions = get_i8_settings().usable_stock_condition_list

    by_condition: dict[str, Decimal] = {}
    for row in with_condition:
        for condition, units in row.stock_by_condition:
            by_condition[condition] = by_condition.get(condition, Decimal(0)) + units
    breakdown = tuple(
        (condition, by_condition[condition])
        for condition in dict.fromkeys(c.upper() for c in usable_conditions)
        if by_condition.get(condition, Decimal(0)) > 0
    )

    return _StockSplit(
        known=True,
        source=with_condition[0].stock_condition_source,
        usable=sum((row.usable_stock or Decimal(0) for row in with_condition), Decimal(0)),
        damaged=sum((row.damaged_stock or Decimal(0) for row in with_condition), Decimal(0)),
        breakdown=breakdown,
    )
