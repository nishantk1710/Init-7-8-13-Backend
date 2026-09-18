"""W5.2 -- the repair status register, at material + repair-PO-line grain.

One row per material + purchase-order line, keyed on ``(EBELN, EBELP)``.

Not one row per material, and not one per purchase order. A single repair PO
carries several lines -- different materials, or the same material in separate
batches sent at different times -- and each line has its own schedule-line
delivery date, its own goods receipt and its own quantity. They go out and come
back independently. Aggregate to the material and you can no longer say "this
one is 60 days late while that one came back last week", which is the entire
point of the line.

The two rulings this module exists to honour
--------------------------------------------

**Pstyp is applied in Python, never in the query** (:func:`is_repair_line`).
SAP's OData service accepts a ``$filter`` on ``Pstyp``, answers HTTP 200, and
ignores it -- returning rows that do not match. There is no error to catch; the
call looks like it worked. It is verdict IGNORED in
``data-generator/discovery/filter_support.csv`` and failure item F1 in failure
report v3. Reading Postgres today this is moot, because our own database honours
every filter. The predicate stays in Python anyway so it is still correct at
cutover with no rewrite, and so it lives in one place that can be tested.

**ZREP corroborates, it never gates.** Item category 3 appears on ZREP documents
and nowhere else -- zero false positives across 62,000+ non-ZREP lines -- but
only 770 of the 1,225 repair lines have an EKKO header in this extract at all.
``raw_ekko`` starts at 07-Jan-2025 while ``raw_ekpo`` reaches further back. An
inner join to pick up the document type silently drops 455 genuine repair lines,
37% of the register. So the header is LEFT JOINed and displayed, and the gate is
item category alone.

What the data will and will not support
---------------------------------------
Stages 3 to 6 of the lifecycle (PO raised, dispatched, at vendor, received) can
be assembled per PO line. Stages 1 and 2 -- removal from the machine (MSEG 261 /
201) and the condition attestation -- cannot: those movements carry no PO
reference whatsoever, and W5.3 owns the attestation. They are deliberately left
out of v1 rather than linked by a guess.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.aging import (
    aging_bucket,
    days_between,
    days_remaining,
    overdue_state,
)
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series

logger = get_logger(__name__)

ZERO = Decimal(0)

# Repair status, matching the frontend's RepairStatus union exactly.
#
# Two members of that union are never emitted, and both for the same reason --
# the data does not support them, so inventing them would be a lie the UI
# renders confidently:
#
#   "PR Raised"        the register is built from PO lines, so a PO always
#                      exists by construction. A requisition with no PO yet is
#                      W5.3/W5.5 territory, not this read model.
#   "In Transit Return" nothing in MSEG or EKBE distinguishes "the vendor has
#                      shipped it back" from "still at the vendor". There is no
#                      goods-in-transit movement on these lines.
PO_ISSUED = "PO Issued"
AT_VENDOR = "At Vendor"
RECEIVED_STATUS = "Received"
CLOSED = "Closed"

# Receipt status, matching the frontend's ReceiptStatus union exactly.
NOT_YET_SHIPPED = "Not Yet Shipped"
AWAITING_RECEIPT = "Awaiting Receipt"
PARTIALLY_RECEIVED = "Partially Received"
FULLY_RECEIVED = "Received"


@dataclass(frozen=True)
class RepairLine:
    """One repair purchase-order line, with its assembled lifecycle."""

    # --- identity -------------------------------------------------------
    purchasing_document: str
    item: str
    material_id: str
    plant: str | None
    description: str | None

    # --- the line itself ------------------------------------------------
    ordered_qty: Decimal
    unit: str | None
    net_price: Decimal | None
    item_category: str | None
    delivery_completed: bool
    """EKPO.ELIKZ. Reported, never trusted as closure -- see :func:`repair_status`."""

    pr_number: str | None
    pr_item: str | None

    requisitioner: str | None
    """EKPO.AFNAM -- who asked for it. A code, not a name: no person directory
    was delivered, so it is served as a code the same way an unnamed vendor is.
    Populated on all 1,225 repair lines, 27 distinct values. This is the only
    person-shaped field on a repair line, and W5.3's declaration queue needs it
    for ``requester``."""

    # --- corroboration, LEFT JOINed -------------------------------------
    doc_type: str | None
    corroborated_by_doc_type: bool
    has_po_header: bool
    vendor: str | None
    vendor_name: str | None

    # --- lifecycle timestamps -------------------------------------------
    raised_at: date | None
    po_date: date | None
    due_date: date | None
    schedule_lines: int
    dispatched_at: date | None
    dispatched_qty: Decimal
    received_at: date | None
    received_qty: Decimal
    """Net of reversals. A 101 followed by its 102 nets to zero, not to a return."""

    reversals: int

    # --- derived --------------------------------------------------------
    repair_status: str
    receipt_status: str
    overdue_status: str
    qty_under_repair: Decimal
    days_open: int | None
    days_at_vendor: int | None
    days_in_current_stage: int | None
    days_remaining: int | None
    aging_bucket: str | None

    @property
    def is_open(self) -> bool:
        """No repaired unit back yet. The 781-line figure the FRS cares about."""
        return self.received_at is None

    @property
    def is_overdue(self) -> bool:
        return self.overdue_status == "OVERDUE"

    @property
    def key(self) -> tuple[str, str]:
        return (self.purchasing_document, self.item)


# --- Layer 1: identify the repair PO lines --------------------------------

# The candidate pull. Note what is NOT here: any predicate on pstyp.
#
# The optional narrowing below is by plant and material only -- the two
# properties the discovery probe records as HONOURED on PurchaseOrderItemSet
# (Ebeln, Ebelp, Loekz, Matnr, Werks and Banfn are honoured; Pstyp, Menge,
# Netpr and 56 others are not). Pulling with only filters SAP actually applies,
# then filtering the rest client-side, is the same shape the CPI client already
# enforces in app/integrations/sap/filters.py.
_CANDIDATES_SQL = """
select
    p.ebeln, p.ebelp, p.matnr, p.werks, p.pstyp, p.txz01,
    p.menge, p.meins, p.netpr, p.elikz, p.banfn, p.bnfpo, p.erdat, p.loekz,
    p.afnam
from v_ekpo p
where p.ebeln is not null
  and p.ebelp is not null
  -- Cast is required, not decorative: Postgres cannot infer a type for a bare
  -- NULL parameter and refuses the statement with AmbiguousParameter.
  and (cast(:plant as text) is null or p.werks = cast(:plant as text))
  and (cast(:material as text) is null or p.matnr = cast(:material as text))
"""


def fetch_candidate_lines(
    db: Session, *, plant: str | None = None, material: str | None = None
) -> list[Mapping]:
    """Pull PO lines, unfiltered by item category. Deliberately.

    This is the "EKPO pull" the task plan names. It applies only filters that
    survive the round trip to SAP; the repair-PO test happens in Python, in
    :func:`is_repair_line`.
    """
    return list(
        db.execute(text(_CANDIDATES_SQL), {"plant": plant, "material": material})
        .mappings()
        .all()
    )


def is_repair_line(row: Mapping, cfg: I8Settings) -> bool:
    """Whether this PO line is a repair line. THE Pstyp ruling, in one place.

    A single equality test, and it is deliberately a function rather than a
    ``WHERE`` clause: it is the thing that must not migrate into an OData
    ``$filter`` at cutover, and a function is something a test can point at.
    """
    return row["pstyp"] == cfg.repair_item_category


# --- Layer 2: assemble the lifecycle per line -----------------------------

_SCHEDULE_SQL = """
-- Earliest promised date per line. Measured: every repair item that has
-- schedule lines has exactly one, so min() is unambiguous today. It is still
-- min() so that a second schedule line in a later extract resolves to the
-- earliest promise rather than an arbitrary row.
select ebeln, ebelp, min(eindt) as due_date, count(*) as schedule_lines
from v_eket
where ebeln = any(:documents)
group by 1, 2
"""

_RECEIPTS_SQL = """
-- Goods receipts NET OF REVERSALS.
--
-- SHKZG carries the sign: every 101 on these lines is 'S' (debit, a receipt)
-- and every 102 is 'H' (credit, its reversal). Summing quantity without the
-- sign reports a reversed receipt as a returned unit -- which is exactly the
-- mistake that puts a unit back in stock that is not there.
--
-- 19 of the 1,225 repair lines carry a reversal, and 7 of those net to zero:
-- counting raw 101s gives 444 received lines, netting gives 437.
select
    ebeln,
    ebelp,
    sum(case when shkzg = :credit then -menge else menge end) as net_qty,
    min(budat) filter (where bwart = :receipt)  as first_receipt,
    max(budat) filter (where bwart = :receipt)  as last_receipt,
    count(*)   filter (where bwart = :reversal) as reversals
from v_ekbe
where ebeln = any(:documents)
  and bewtp = :category
  and bwart in (:receipt, :reversal)
group by 1, 2
"""

_DISPATCH_SQL = """
-- Dispatch to the vendor: MSEG 541, vendor-stock side only.
--
-- sobkz = 'O' picks one of the two rows every 541 posts, so quantities are not
-- doubled. ebelp is already NULL in the view where the raw column held '0',
-- and those movements are excluded here rather than attached to a guessed
-- line: 132 of the 710 dispatches on 80-series materials carry no PO item, and
-- inventing a link for them would corrupt per-line aging.
select
    ebeln,
    ebelp,
    min(budat) as first_dispatch,
    max(budat) as last_dispatch,
    sum(menge) as qty,
    count(*)   as postings
from v_mseg
where ebeln = any(:documents)
  and ebelp is not null
  and bwart = :dispatch
  and sobkz = :vendor_stock
group by 1, 2
"""

_HEADER_SQL = """
-- LEFT JOINed, never an inner join. 455 of 1,225 repair lines have no row here.
select ebeln, bsart, lifnr, bedat
from v_ekko
where ebeln = any(:documents)
"""

_VENDOR_SQL = "select lifnr, name1 from v_lfa1 where lifnr is not null"


def _evidence(
    db: Session, documents: Sequence[str], cfg: I8Settings
) -> tuple[dict, dict, dict, dict, dict]:
    """Fetch every lifecycle source for the identified repair documents.

    Keyed lookups built once, rather than a query per line: 1,225 lines times
    five sources is 6,125 round trips, and the register is served on request.
    """
    documents = list(documents)

    schedules = {
        (r["ebeln"], r["ebelp"]): r
        for r in db.execute(text(_SCHEDULE_SQL), {"documents": documents})
        .mappings()
        .all()
    }
    receipts = {
        (r["ebeln"], r["ebelp"]): r
        for r in db.execute(
            text(_RECEIPTS_SQL),
            {
                "documents": documents,
                "category": cfg.gr_history_category,
                "receipt": cfg.gr_movement_type,
                "reversal": cfg.gr_reversal_movement_type,
                "credit": "H",
            },
        )
        .mappings()
        .all()
    }
    dispatches = {
        (r["ebeln"], r["ebelp"]): r
        for r in db.execute(
            text(_DISPATCH_SQL),
            {
                "documents": documents,
                "dispatch": cfg.dispatch_movement_type,
                "vendor_stock": cfg.vendor_special_stock,
            },
        )
        .mappings()
        .all()
    }
    headers = {
        r["ebeln"]: r
        for r in db.execute(text(_HEADER_SQL), {"documents": documents})
        .mappings()
        .all()
    }
    vendors = {r["lifnr"]: r["name1"] for r in db.execute(text(_VENDOR_SQL)).mappings()}
    return schedules, receipts, dispatches, headers, vendors


def repair_status(
    *,
    received_qty: Decimal,
    ordered_qty: Decimal,
    dispatched_at: date | None,
    delivery_completed: bool,
) -> str:
    """Where this line sits in its lifecycle.

    ELIKZ is deliberately NOT the closure test. 1,025 of the 1,225 repair lines
    carry the delivery-complete flag but only 436 of them have any goods
    receipt at all: driving status from the flag would report 589 units as back
    in stock when nothing has come back. Receipts decide; the flag only
    separates "back" from "back and signed off", and is served as its own field
    so the UI can show the discrepancy rather than inherit it.
    """
    fully_received = received_qty > ZERO and received_qty >= ordered_qty
    if fully_received:
        return CLOSED if delivery_completed else RECEIVED_STATUS
    if dispatched_at is not None:
        return AT_VENDOR
    return PO_ISSUED


def receipt_status(
    *, received_qty: Decimal, ordered_qty: Decimal, dispatched_at: date | None
) -> str:
    """Physical receipt state of the repaired unit(s) back into stores."""
    if received_qty > ZERO and received_qty >= ordered_qty:
        return FULLY_RECEIVED
    if received_qty > ZERO:
        # No line in the July extract is in this state, and it is implemented
        # anyway: a partial receipt is ordinary in a live client, and finding
        # out at cutover that the register rounds it to "Received" would mean
        # reporting units back that are still at the vendor.
        return PARTIALLY_RECEIVED
    if dispatched_at is not None:
        return AWAITING_RECEIPT
    return NOT_YET_SHIPPED


def _build_line(
    row: Mapping,
    *,
    schedule: Mapping | None,
    receipt: Mapping | None,
    dispatch: Mapping | None,
    header: Mapping | None,
    vendor_names: Mapping[str, str],
    today: date,
    cfg: I8Settings,
) -> RepairLine:
    ordered_qty = row["menge"] if row["menge"] is not None else ZERO

    received_qty = receipt["net_qty"] if receipt and receipt["net_qty"] else ZERO
    reversals = int(receipt["reversals"]) if receipt else 0
    # A receipt that has been fully reversed is not a receipt. Dropping the
    # date too is what stops the line reading as RECEIVED with nothing back.
    received_at = receipt["first_receipt"] if receipt and received_qty > ZERO else None

    dispatched_at = dispatch["first_dispatch"] if dispatch else None
    dispatched_qty = dispatch["qty"] if dispatch and dispatch["qty"] else ZERO

    due_date = schedule["due_date"] if schedule else None
    schedule_lines = int(schedule["schedule_lines"]) if schedule else 0

    doc_type = header["bsart"] if header else None
    vendor = header["lifnr"] if header else None
    po_date = header["bedat"] if header else None

    # ERDAT is the line's own creation date and is populated on every repair
    # line; the header date is a fallback for a shape where it is not.
    raised_at = row["erdat"] or po_date

    status = repair_status(
        received_qty=received_qty,
        ordered_qty=ordered_qty,
        dispatched_at=dispatched_at,
        delivery_completed=row["elikz"] == "X",
    )

    # The stage clock: time since whatever most recently happened to this line.
    stage_started = max(
        (d for d in (raised_at, dispatched_at, received_at) if d is not None),
        default=None,
    )

    return RepairLine(
        purchasing_document=row["ebeln"],
        item=row["ebelp"],
        material_id=row["matnr"],
        plant=row["werks"],
        description=row["txz01"],
        ordered_qty=ordered_qty,
        unit=row["meins"],
        net_price=row["netpr"],
        item_category=row["pstyp"],
        delivery_completed=row["elikz"] == "X",
        pr_number=row["banfn"],
        pr_item=row["bnfpo"],
        requisitioner=row["afnam"],
        doc_type=doc_type,
        corroborated_by_doc_type=doc_type == cfg.repair_doc_type,
        has_po_header=header is not None,
        vendor=vendor,
        vendor_name=vendor_names.get(vendor) if vendor else None,
        raised_at=raised_at,
        po_date=po_date,
        due_date=due_date,
        schedule_lines=schedule_lines,
        dispatched_at=dispatched_at,
        dispatched_qty=dispatched_qty,
        received_at=received_at,
        received_qty=received_qty,
        reversals=reversals,
        repair_status=status,
        receipt_status=receipt_status(
            received_qty=received_qty,
            ordered_qty=ordered_qty,
            dispatched_at=dispatched_at,
        ),
        overdue_status=overdue_state(
            received_at=received_at,
            due_date=due_date,
            today=today,
            grace_days=cfg.overdue_grace_days,
        ),
        qty_under_repair=(
            ZERO if received_at is not None else max(ordered_qty - received_qty, ZERO)
        ),
        days_open=days_between(raised_at, today),
        # The number that matters: how long this unit has actually been away.
        # Measured to the receipt where there is one, to today where there is
        # not -- so an open repair keeps ageing instead of freezing.
        days_at_vendor=days_between(dispatched_at, received_at or today),
        days_in_current_stage=days_between(stage_started, today),
        days_remaining=days_remaining(due_date, today),
        aging_bucket=aging_bucket(
            days_between(raised_at, today), cfg.aging_band_boundaries_list
        ),
    )


# --- The register ---------------------------------------------------------


@dataclass(frozen=True)
class RegisterStats:
    """Counts worth reporting with the register, and the data-quality gaps.

    Every field defaults to zero so the empty register is a valid value rather
    than a special case with a positional argument list to keep in step.
    """

    total_lines: int = 0
    open_lines: int = 0
    received_lines: int = 0
    overdue_lines: int = 0

    no_due_date_lines: int = 0
    """Open lines with no promised date -- the NO_DUE_DATE queue."""

    lines_without_due_date: int = 0
    """All lines with no promised date, received ones included.

    Both figures are reported because they answer different questions and
    differ: 63 repair lines have no EKET schedule line at all, and 2 of them
    have already come back, so 61 are actually chaseable.
    """

    partially_received_lines: int = 0
    lines_with_reversals: int = 0
    lines_on_eighty_series: int = 0
    lines_with_po_header: int = 0
    lines_corroborated_by_doc_type: int = 0
    lines_with_dispatch: int = 0

    open_lines_with_dispatch: int = 0
    """Open lines that have a dispatch movement -- measured at ZERO.

    A finding, not a bug in this code. Every 541 in the extract that can be
    attached to a repair line belongs to a line that has already come back, so
    no currently-open repair has a recorded dispatch date. The consequence:
    "At Vendor" is never the status of an open line, and daysAtVendor can only
    be computed for completed repairs. The register still shows the open work
    -- which is what the FRS asks for -- but the per-stage vendor clock is
    unavailable for it until dispatch movements are extracted for open lines.
    """

    distinct_materials: int = 0
    distinct_vendors: int = 0
    vendors_resolved_to_a_name: int = 0
    candidates_scanned: int = 0


def load_repair_lines(
    db: Session,
    cfg: I8Settings | None = None,
    *,
    today: date | None = None,
    plant: str | None = None,
    material: str | None = None,
) -> tuple[list[RepairLine], RegisterStats]:
    """Build the register. Layers 1 and 2, then the Layer 3 arithmetic.

    Returns every repair line, open or closed -- filtering to the open ones is
    a presentation concern and the closed ones are what vendor turnaround is
    computed from.
    """
    cfg = cfg or get_i8_settings()
    today = today or cfg.reference_date_value or date.today()

    candidates = fetch_candidate_lines(db, plant=plant, material=material)

    # Layer 1. The Pstyp ruling: in Python, over rows already fetched.
    repair_rows = [row for row in candidates if is_repair_line(row, cfg)]

    if not repair_rows:
        return [], RegisterStats(candidates_scanned=len(candidates))

    documents = sorted({row["ebeln"] for row in repair_rows})
    schedules, receipts, dispatches, headers, vendor_names = _evidence(
        db, documents, cfg
    )

    lines = [
        _build_line(
            row,
            schedule=schedules.get((row["ebeln"], row["ebelp"])),
            receipt=receipts.get((row["ebeln"], row["ebelp"])),
            dispatch=dispatches.get((row["ebeln"], row["ebelp"])),
            header=headers.get(row["ebeln"]),
            vendor_names=vendor_names,
            today=today,
            cfg=cfg,
        )
        for row in repair_rows
    ]

    vendors = {line.vendor for line in lines if line.vendor}
    stats = RegisterStats(
        total_lines=len(lines),
        open_lines=sum(1 for line in lines if line.is_open),
        received_lines=sum(1 for line in lines if not line.is_open),
        overdue_lines=sum(1 for line in lines if line.is_overdue),
        no_due_date_lines=sum(
            1 for line in lines if line.overdue_status == "NO_DUE_DATE"
        ),
        lines_without_due_date=sum(1 for line in lines if line.due_date is None),
        partially_received_lines=sum(
            1 for line in lines if line.receipt_status == PARTIALLY_RECEIVED
        ),
        lines_with_reversals=sum(1 for line in lines if line.reversals),
        # Measured, not assumed: this is how the register proves the two
        # signals agree rather than taking it on trust.
        lines_on_eighty_series=sum(
            1 for line in lines if is_eighty_series(line.material_id, cfg)
        ),
        lines_with_po_header=sum(1 for line in lines if line.has_po_header),
        lines_corroborated_by_doc_type=sum(
            1 for line in lines if line.corroborated_by_doc_type
        ),
        lines_with_dispatch=sum(1 for line in lines if line.dispatched_at),
        open_lines_with_dispatch=sum(
            1 for line in lines if line.is_open and line.dispatched_at
        ),
        distinct_materials=len({line.material_id for line in lines}),
        distinct_vendors=len(vendors),
        vendors_resolved_to_a_name=len(
            {line.vendor for line in lines if line.vendor and line.vendor_name}
        ),
        candidates_scanned=len(candidates),
    )
    logger.info(
        "I08 register: %d repair lines of %d candidates (%d open, %d overdue)",
        stats.total_lines,
        stats.candidates_scanned,
        stats.open_lines,
        stats.overdue_lines,
    )
    return lines, stats


def open_repair_index(
    lines: Sequence[RepairLine],
) -> dict[tuple[str, str | None], tuple[int, Decimal]]:
    """(material, plant) -> (open line count, quantity out for repair).

    The join point between W5.2 and W5.1: this is what gives every universe row
    its ``hasOpenRepair`` flag without the universe model needing to know
    anything about item categories or purchase orders.
    """
    index: dict[tuple[str, str | None], tuple[int, Decimal]] = {}
    for line in lines:
        if not line.is_open:
            continue
        key = (line.material_id, line.plant)
        count, qty = index.get(key, (0, ZERO))
        index[key] = (count + 1, qty + line.qty_under_repair)
    return index
