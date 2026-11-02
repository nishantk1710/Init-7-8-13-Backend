"""W5.2 -- the repair status register, at material + repair-PO-line grain.

One row per material + purchase-order line, keyed on ``(EBELN, EBELP)``.

Not one row per material, and not one per purchase order. A single repair PO
carries several lines -- different materials, or the same material in separate
batches sent at different times -- and each line has its own schedule-line
delivery date and its own quantity. They go out and come back independently.
Aggregate to the material and you can no longer say "this one is 60 days late
while that one is not due for a month", which is the entire point of the line.

The three rulings this module exists to honour
----------------------------------------------

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
item category alone. (By the 15-Oct data "nowhere else" no longer held: 49 ZDOM
lines carry item category 3 as well, all closed or deleted, and one RFQ -- see
the next ruling. Gating on ZREP would drop an open ZDOM line, so it still does
not gate.)

**A request for quotation is not an order** (:func:`is_rfq_line`). Ruled
21-Oct-2026. RFQ 6000016330/10 (document type AN) carries item category 3, so
it passed the gate. SAP never ticks "Delivery completed" on an RFQ, so it sat in
the register as open, with a quantity of 0: 105 open lines against 104 under
repair. It is the one test that needs the header, so a line with no header is
kept -- there is nothing to say it is an RFQ.

**Open is SAP's call: the "Delivery completed" flag** (:func:`is_delivery_completed`).
Ruled 19-Oct-2026. The register lists only the repair lines SAP still has
open -- EKPO.ELIKZ not set, and the line neither deleted nor blocked -- and
nothing else. Until then it decided from goods receipts, so it called 644 lines
open that SAP had closed (ticked "Delivery completed", nothing received against
640 of them) and showed ~732 open lines where SAP's own count was ~106; on the
15-Oct data the new rule gives 104. Goods receipts are not read at all now: a
line SAP has closed is not in the register whatever came back against it, and
a line SAP still has open is in it whatever did. The flag is tested in Python
beside the Pstyp test, not in the query: the OData probe never tested a filter
on Elikz (NOT_TESTED in ``filter_support.csv``), and a filter SAP has not been
shown to honour is not one to rely on.

What the data will and will not support
---------------------------------------
Stages 3 to 5 of the lifecycle (PO raised, dispatched, at vendor) can be
assembled per PO line. Stage 6, received, is not shown: a line in the register
has not been closed, and a closed one is not in it. Removal from the machine
(MSEG 261 / 201) is not a stage at all since 27-Oct-2026 (ruled 26-Oct): those
movements carry no PO reference whatsoever, so SAP has no record of removal
that could be tied to a line, and the stage was dropped rather than linked by a
guess. The condition declaration comes from this platform's own attestations,
not from SAP.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import MSSQL
from app.core.logging import get_logger
from app.initiatives.i8.aging import (
    BEYOND_LEAD_TIME,
    OVERDUE,
    aging_bucket,
    days_between,
    days_over_lead_time,
    days_remaining,
    elapsed_days,
    lead_time_state,
    overdue_state,
)
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series
from app.initiatives.i8.staged import Sources, reading
from app.shared.sql_lists import fetch_in_chunks

logger = get_logger(__name__)

ZERO = Decimal(0)

# Repair status, two members of the frontend's RepairStatus union.
#
# Every line in the register is open in SAP, so it is one of these two. The
# union's other members are never emitted:
#
#   "PR Raised"           the register is built from PO lines, so a PO always
#                         exists by construction. A repair PR with no PO yet
#                         is found by requisitions.py (27-Oct-2026), which
#                         owns that status (requisitions.PR_RAISED).
#   "Received", "Closed"  not since 19-Oct-2026: a line SAP has closed is not
#                         in the register, and goods receipts are not read.
#
# "In Transit Return" was removed from the union on 07-Oct-2026: it is not an
# FRS stage, and nothing in MSEG or EKBE distinguishes "the vendor has shipped
# it back" from "still at the vendor".
PO_ISSUED = "PO Issued"
AT_VENDOR = "At Vendor"


@dataclass(frozen=True)
class RepairLine:
    """One repair purchase-order line SAP still has open, with its assembled
    lifecycle. Lines SAP has closed, deleted or blocked, and lines on a request
    for quotation, are never built -- see :func:`load_repair_lines`."""

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

    # --- the planned standard, from MARC ---------------------------------
    lead_time_days: int | None
    """MARC.PLIFZ for this material at this plant -- planned delivery time in
    CALENDAR days, PO to received. None where MARC has no row or the value is
    unmaintained. The same field and meaning Initiative 07 uses."""

    # --- derived --------------------------------------------------------
    repair_status: str
    overdue_status: str
    lead_time_status: str
    """WITHIN_LEAD_TIME, BEYOND_LEAD_TIME or NO_LEAD_TIME. Independent of
    ``overdue_status``: one measures the date somebody promised, the other the
    time this material normally takes. A line can be ON_TIME and already
    BEYOND_LEAD_TIME, and that disagreement is a finding, not an error."""

    qty_under_repair: Decimal
    """EKPO.MENGE, the whole ordered quantity. The line is out until SAP
    closes it, and goods receipts are not read, so nothing is netted off."""

    days_open: int | None
    """The same clock as ``days_elapsed``: raised to today. The line is open in
    SAP, so it keeps ageing until SAP closes it and it leaves the register."""
    days_elapsed: int | None
    """Raised to today. What the lead-time check measures."""

    days_over_lead_time: int | None
    """Positive once past the planned time, negative while inside it, None when
    there is no lead time to measure against."""

    days_at_vendor: int | None
    """Dispatch (the 541) to today. None when no dispatch is attributable."""
    days_in_current_stage: int | None
    days_remaining: int | None
    aging_bucket: str | None

    # --- the repair PR this line was ordered from (27-Oct-2026) -----------
    pr_requested_on: date | None = None
    """EBAN.BADAT of ``pr_number``/``pr_item``: when the repair was asked for.
    Filled by the snapshot build from the PRs the requisition loader reads
    (:func:`with_requisition_details`); None when the PR was not read, never
    guessed from the PO date."""

    pr_department: str | None = None
    """EBAN.AFNAM of that PR: the 5-digit department code that raised the
    repair, the same code a PR row shows. The queue's requester. A code; the
    name is looked up where it is served."""

    # --- the price's currency (29-Oct-2026) ------------------------------
    currency: str | None = None
    """EKKO.WAERS: the currency :attr:`net_price` is in, ``ZAR`` on every open
    line today. None when the line has no PO header or the header has no
    currency -- then nobody knows, and the price must not be called rand."""

    @property
    def is_overdue(self) -> bool:
        return self.overdue_status == OVERDUE

    @property
    def is_beyond_lead_time(self) -> bool:
        return self.lead_time_status == BEYOND_LEAD_TIME

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
  -- NULL parameter and refuses the statement with AmbiguousParameter. varchar,
  -- not text: on SQL Server text is the legacy blob type and cannot be
  -- compared with =.
  and (cast(:plant as varchar(4000)) is null or p.werks = cast(:plant as varchar(4000)))
  and (cast(:material as varchar(4000)) is null or p.matnr = cast(:material as varchar(4000)))
"""


def fetch_candidate_lines(
    db: Session,
    *,
    plant: str | None = None,
    material: str | None = None,
    sources: Sources | None = None,
) -> list[Mapping]:
    """Pull PO lines, unfiltered by item category. Deliberately.

    This is the "EKPO pull" the task plan names. It applies only filters that
    survive the round trip to SAP; the repair-PO test happens in Python, in
    :func:`is_repair_line`.

    ``sources`` is a snapshot build's staged copies (``staged.py``): there the
    pull reads a copy already narrowed to repair and 80-series lines -- still
    without a pstyp predicate in this query, and still decided by
    :func:`is_repair_line`.
    """
    return list(
        db.execute(text(reading(_CANDIDATES_SQL, sources)), {"plant": plant, "material": material})
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


def is_deleted_line(row: Mapping, cfg: I8Settings) -> bool:
    """Whether SAP has deleted this PO line (EKPO.LOEKZ).

    A deleted line is not a repair anybody is waiting on. Left in, all 44 in
    the July extract read as open, most of them as overdue, and each raised a
    missing-attestation exception for a repair that was cancelled.
    """
    return row["loekz"] == cfg.po_deleted_indicator


def is_delivery_completed(row: Mapping, cfg: I8Settings) -> bool:
    """Whether SAP has closed this PO line: "Delivery completed" (EKPO.ELIKZ).

    THE open/closed ruling of 19-Oct-2026, in one place. A ticked line is
    closed whatever was received against it, as SAP's own open-PO count reads
    it -- see the module docstring. In Python rather than the query for the
    same reason :func:`is_repair_line` is.
    """
    return row["elikz"] == cfg.delivery_completed_indicator


def is_blocked_line(row: Mapping, cfg: I8Settings) -> bool:
    """Whether SAP has blocked this PO line (EKPO.LOEKZ, the blocked value).

    Left out of the register since 19-Oct-2026, as SAP's open-PO count leaves
    it out. Blocked is not cancelled: a line released again comes back in on
    the next snapshot.
    """
    return row["loekz"] == cfg.po_blocked_indicator


def is_rfq_line(header: Mapping | None, cfg: I8Settings) -> bool:
    """Whether this line is on a request for quotation (EKKO.BSART), not an order.

    Takes the header, not the line: the document type lives on EKKO. A line
    with no header is not called an RFQ -- see the module docstring.
    """
    return header is not None and header["bsart"] == cfg.rfq_doc_type


def is_placeholder_price(net_price: Decimal | None, cfg: I8Settings) -> bool:
    """Whether the line's net price is a placeholder rather than a cost: above
    zero and at or below ``I8_REPAIR_COST_TOKEN_MAX`` (R1.00).

    The same token values MBEW holds (see ``I8_NEW_ITEM_PRICE_TOKEN_MAX``), on
    the PO: 16 of the 104 open repair lines carried a NETPR of 1.00 on
    29-Oct-2026, a pump and a man-winder cage among them. Served as a cost,
    "R 1" would read as a repair that costs nothing.
    """
    return net_price is not None and 0 < net_price <= cfg.repair_cost_token_max


def repair_cost(net_price: Decimal | None, cfg: I8Settings) -> Decimal | None:
    """The line's net price as a repair cost, or None when it is not one: no
    price, a zero, or a placeholder (:func:`is_placeholder_price`). In the
    line's own currency, :attr:`RepairLine.currency` -- never converted."""
    if net_price is None or net_price <= 0 or is_placeholder_price(net_price, cfg):
        return None
    return net_price


# --- Layer 2: assemble the lifecycle per line -----------------------------

_SCHEDULE_SQL = """
-- Earliest promised date per line. Measured: every repair item that has
-- schedule lines has exactly one, so min() is unambiguous today. It is still
-- min() so that a second schedule line in a later extract resolves to the
-- earliest promise rather than an arbitrary row.
select ebeln, ebelp, min(eindt) as due_date, count(*) as schedule_lines
from v_eket
where ebeln in :documents
group by ebeln, ebelp
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
where ebeln in :documents
  and ebelp is not null
  and bwart = :dispatch
  and sobkz = :vendor_stock
group by ebeln, ebelp
"""

# On SQL Server the dispatch query reads a staged copy of MSEG, not v_mseg.
#
# Straight from the view it ran past the 120 s statement limit on all 12 build
# attempts on the nonprod Azure SQL on 28-Sep, one of them with nothing else
# running, so the Repairable Spares pages never loaded. Filtered on ebeln, the
# query makes SQL Server compute sap_key(purchase_order) for every one of
# MSEG's ~233k rows before it can discard any, and -- the likely reason it is
# so slow -- SQL Server does not inline a scalar function whose result the
# query groups by, so that is ~233k separate function calls. The staged copy
# holds only the 541 vendor-side rows with a PO item, fetched with the cheap
# predicates and no GROUP BY (the shape PR #40's staged universe query runs
# in). The query above then runs over that table unchanged, so its rules stay
# in one place. Postgres reads v_mseg directly; it has none of this cost.
_DISPATCH_STAGE_COLUMNS = "ebeln, ebelp, budat, menge, bwart, sobkz"
_DISPATCH_STAGE_FILTER = "ebelp is not null and bwart = :dispatch and sobkz = :vendor_stock"
_DISPATCH_STAGE_TABLE = "#i8_mseg_dispatch"


def fetch_dispatches(
    db: Session,
    documents: Sequence[str],
    params: Mapping[str, str],
    *,
    source: str = "v_mseg",
) -> list[Mapping]:
    """The dispatch rows for ``documents``: staged through a temp table on SQL Server."""
    if db.get_bind().dialect.name != MSSQL:
        return fetch_in_chunks(db, _DISPATCH_SQL.replace("v_mseg", source), "documents", documents, params)

    table, columns = _DISPATCH_STAGE_TABLE, _DISPATCH_STAGE_COLUMNS
    db.execute(text(f"drop table if exists {table}"))
    # Created without bind parameters, as in universe.fetch_universe_candidates:
    # a parameterised statement runs in sp_executesql, which drops any temp
    # table it creates when it returns.
    db.execute(text(f"select {columns} into {table} from {source} where 1 = 0"))
    db.execute(
        text(f"insert into {table} select {columns} from {source} where {_DISPATCH_STAGE_FILTER}"),
        params,
    )
    rows = fetch_in_chunks(db, _DISPATCH_SQL.replace("v_mseg", table), "documents", documents, params)
    db.execute(text(f"drop table {table}"))
    return rows


_HEADER_SQL = """
-- LEFT JOINed, never an inner join. 455 of 1,225 repair lines have no row here.
--
-- AEDAT (created on), NOT BEDAT (document date). Both are in this extract, and
-- the register used BEDAT until 21-Sep. **BEDAT is not exposed by CPI** -- it is
-- absent from the OData projection, and Initiative 07 already anchors on AEDAT
-- for exactly that reason (see its model guide: "Release date: EKKO-AEDAT
-- (interim proxy -- EKKO-BEDAT is not exposed)"). Reading a column that
-- disappears at cutover is a bug with a date on it, and two initiatives
-- anchoring on different columns would report different ages for one PO line.
--
-- WAERS: the currency EKPO.NETPR is in. A PO is priced in the vendor's
-- currency, not always the rand: plants 1300 and 1500 carry USD, EUR, INR and
-- NAD lines (15-Oct-2026 export), and one repair line was priced in USD.
select ebeln, bsart, lifnr, aedat, waers
from v_ekko
where ebeln in :documents
"""

_VENDOR_SQL = "select lifnr, name1 from v_lfa1 where lifnr is not null"

_LEAD_TIME_SQL = """
-- Planned delivery time per material + plant: MARC.PLIFZ, in calendar days,
-- measured PO to received.
--
-- Keyed on BOTH material and plant, not material alone. MARC is a plant-level
-- master and the same part can carry a different planned time at each site, so
-- collapsing to the material would quietly apply Black Mountain's number to
-- Gamsberg.
--
-- max() because MARC is one row per material+plant -- the same aggregate the
-- universe query uses for MINBE and DISMM, and for the same reason: it picks
-- that single value without the query having to assert uniqueness.
--
-- KNOWN COVERAGE GAP, and it is in the delivery, not here: the July MARC
-- extract has ZERO rows for Gamsberg (1500) -- see app/seed/manifest.py.
-- Both in-scope plants carry repair lines, so every Gamsberg repair line
-- resolves to NO_LEAD_TIME until a MARC
-- extract covering 1500 arrives. The register reports that as a count rather
-- than absorbing it.
select matnr, werks, max(plifz) as plifz
from v_marc
where matnr in :materials
  and werks is not null
group by matnr, werks
"""


def _evidence(
    db: Session,
    documents: Sequence[str],
    materials: Sequence[str],
    cfg: I8Settings,
    sources: Sources | None = None,
) -> tuple[dict, dict, dict, dict, dict]:
    """Fetch every lifecycle source for the identified repair documents.

    Keyed lookups built once, rather than a query per line: a query per line
    per source is thousands of round trips, and the register is served on
    request.

    No goods receipts (EKBE) since 19-Oct-2026: whether a line is open is
    SAP's "Delivery completed" flag, read off the EKPO row itself.

    ``materials`` is separate from ``documents`` because the lead time belongs
    to the material + plant, not to the purchase order -- MARC knows nothing
    about EBELN.
    """
    documents = list(documents)
    materials = list(materials)

    # Every query below groups by the column it filters on, so each chunk's
    # rows are complete on their own and concatenating them is exact.
    #
    # Each is timed in the log: on Azure SQL one of them outran the statement
    # limit on every build, and the log is the only place that is visible.
    with timed_step("schedule lines (EKET)"):
        schedules = {
            (r["ebeln"], r["ebelp"]): r
            for r in fetch_in_chunks(db, reading(_SCHEDULE_SQL, sources), "documents", documents)
        }
    with timed_step("dispatches (MSEG 541)"):
        dispatches = {
            (r["ebeln"], r["ebelp"]): r
            for r in fetch_dispatches(
                db,
                documents,
                {
                    "dispatch": cfg.dispatch_movement_type,
                    "vendor_stock": cfg.vendor_special_stock,
                },
                source=reading("v_mseg", sources),
            )
        }
    with timed_step("PO headers (EKKO)"):
        headers = {r["ebeln"]: r for r in fetch_in_chunks(db, reading(_HEADER_SQL, sources), "documents", documents)}
    with timed_step("vendor names (LFA1)"):
        vendors = {r["lifnr"]: r["name1"] for r in db.execute(text(reading(_VENDOR_SQL, sources))).mappings()}
    with timed_step("lead times (MARC)"):
        lead_times = {
            (r["matnr"], r["werks"]): r["plifz"]
            for r in fetch_in_chunks(db, reading(_LEAD_TIME_SQL, sources), "materials", materials)
        }
    return schedules, dispatches, headers, vendors, lead_times


@contextmanager
def timed_step(step: str) -> Iterator[None]:
    """Log how long one step of the snapshot build took."""
    started = time.monotonic()
    yield
    logger.info("I08 snapshot: %s in %.1fs", step, time.monotonic() - started)


def repair_status(*, dispatched_at: date | None) -> str:
    """Where this open line sits in its lifecycle: At Vendor once a dispatch
    (the 541) is attributable to it, PO Issued until then.

    Nothing later. The line is open in SAP or it would not be here -- see
    :func:`is_delivery_completed`, which replaced reading goods receipts on
    19-Oct-2026.
    """
    return AT_VENDOR if dispatched_at is not None else PO_ISSUED


def _lead_time_days(plifz) -> int | None:
    """MARC.PLIFZ as whole days, or None when it is not maintained.

    ``sap_num`` gives a Decimal, and PLIFZ arrives as 0 far more often than it
    arrives as a real number -- blank on every non-stock and service-type
    material. Zero is folded into None here, at the boundary, so no rule
    downstream has to remember that "no days" means "nobody told us".
    """
    if plifz is None:
        return None
    days = int(plifz)
    return days if days > 0 else None


def _build_line(
    row: Mapping,
    *,
    schedule: Mapping | None,
    dispatch: Mapping | None,
    header: Mapping | None,
    vendor_names: Mapping[str, str],
    lead_time: int | None,
    today: date,
    cfg: I8Settings,
) -> RepairLine:
    ordered_qty = row["menge"] if row["menge"] is not None else ZERO

    dispatched_at = dispatch["first_dispatch"] if dispatch else None
    dispatched_qty = dispatch["qty"] if dispatch and dispatch["qty"] else ZERO

    due_date = schedule["due_date"] if schedule else None
    schedule_lines = int(schedule["schedule_lines"]) if schedule else 0

    doc_type = header["bsart"] if header else None
    vendor = header["lifnr"] if header else None
    po_date = header["aedat"] if header else None
    currency = header["waers"] if header else None

    # ERDAT is the line's own creation date and is populated on every repair
    # line; the header date is a fallback for a shape where it is not.
    raised_at = row["erdat"] or po_date

    # The stage clock: time since whatever most recently happened to this line.
    stage_started = max(
        (d for d in (raised_at, dispatched_at) if d is not None),
        default=None,
    )

    # The repair clock, behind days open, the aging band and the lead-time
    # check alike. Raised to today: the line is open in SAP until it leaves
    # the register.
    elapsed = elapsed_days(raised_at=raised_at, today=today)

    return RepairLine(
        purchasing_document=row["ebeln"],
        item=row["ebelp"],
        material_id=row["matnr"],
        plant=row["werks"],
        description=row["txz01"],
        ordered_qty=ordered_qty,
        unit=row["meins"],
        net_price=row["netpr"],
        currency=currency,
        item_category=row["pstyp"],
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
        repair_status=repair_status(dispatched_at=dispatched_at),
        overdue_status=overdue_state(
            due_date=due_date,
            today=today,
            grace_days=cfg.overdue_grace_days,
        ),
        lead_time_days=lead_time,
        # No grace_days argument, and that is the ruling rather than an
        # oversight -- see the note in aging.py.
        lead_time_status=lead_time_state(elapsed=elapsed, lead_time_days=lead_time),
        days_elapsed=elapsed,
        days_over_lead_time=days_over_lead_time(
            elapsed=elapsed, lead_time_days=lead_time
        ),
        # The whole line: it is out until SAP closes it.
        qty_under_repair=ordered_qty,
        days_open=elapsed,
        days_at_vendor=days_between(dispatched_at, today),
        days_in_current_stage=days_between(stage_started, today),
        days_remaining=days_remaining(due_date, today),
        # Banded on days open, so the register's aging filter agrees with the
        # column beside it.
        aging_bucket=aging_bucket(elapsed, cfg.aging_band_boundaries_list),
    )


# --- The register ---------------------------------------------------------


@dataclass(frozen=True)
class RegisterStats:
    """Counts worth reporting with the register, and the data-quality gaps.

    Every field defaults to zero so the empty register is a valid value rather
    than a special case with a positional argument list to keep in step.

    Every line counted here is open in SAP. The four ``excluded_*`` figures
    say how many repair lines SAP has deleted, closed or blocked, or holds on a
    request for quotation, so the total reconciles with a raw EKPO count and
    with SAP's own open-PO list.
    """

    total_lines: int = 0
    open_lines: int = 0
    """The same number as ``total_lines``: every line in the register is open in
    SAP. Kept under its own name because it is the figure the screens quote."""

    overdue_lines: int = 0

    no_due_date_lines: int = 0
    """Lines with no promised date -- the NO_DUE_DATE queue."""

    lines_with_lead_time: int = 0
    """Lines whose material+plant has a usable MARC.PLIFZ.

    Reported rather than assumed, because the July MARC extract has no
    Gamsberg (1500) rows and every repair line there therefore has
    no planned delivery time at all. A lead-time breach count means nothing
    without the population it was measured over.
    """

    lines_beyond_lead_time: int = 0
    lines_on_eighty_series: int = 0
    lines_with_po_header: int = 0
    lines_corroborated_by_doc_type: int = 0

    lines_with_dispatch: int = 0
    """Lines a dispatch (the 541) is attributable to -- the At Vendor ones. Few:
    2 of the 104 open lines on the 15-Oct data. Stores post most 541s on the
    day of the goods receipt, so a dispatch usually appears only as SAP is
    about to close the line."""

    distinct_materials: int = 0
    distinct_vendors: int = 0
    vendors_resolved_to_a_name: int = 0
    candidates_scanned: int = 0

    excluded_deleted_lines: int = 0
    """Repair lines SAP has deleted (EKPO.LOEKZ), left out of every figure above."""

    excluded_delivery_completed_lines: int = 0
    """Repair lines SAP has closed -- "Delivery completed", EKPO.ELIKZ -- left
    out of every figure above, whatever was received against them. Every repair
    ever finished is here: 4,965 on the 15-Oct data, 640 of them closed with
    nothing received. A line both deleted and closed counts as deleted."""

    excluded_blocked_lines: int = 0
    """Repair lines SAP has blocked (EKPO.LOEKZ), left out of every figure
    above. Counted only when not already deleted or closed: 68 on the 15-Oct
    data."""

    excluded_rfq_lines: int = 0
    """Item-category-3 lines on a request for quotation (EKKO.BSART), left out
    of every figure above: an RFQ orders nothing. Counted only when not already
    deleted, closed or blocked: 1 on the 15-Oct data."""

    # --- open repair PRs with no PO yet (requisitions.py, 27-Oct-2026) ---
    #
    # Not in any figure above: those count PO lines only. Filled by the
    # snapshot build from requisitions.RequestStats, whether or not the PRs are
    # served (I8_REPAIR_REQUESTS_ENABLED). Each excluded PR is counted once,
    # under the first of these it fails, in this order.

    open_repair_requests: int = 0
    """Repair PRs still open with no PO yet: 108 on the 15-Oct data."""

    requisitions_excluded_deleted: int = 0
    """Repair PRs SAP has deleted (EBAN.LOEKZ)."""

    requisitions_excluded_live_po: int = 0
    """Repair PRs a live PO line points back to -- these are in the register
    as PO lines once open, or were closed there."""

    requisitions_excluded_reservation_closed: int = 0
    """Repair PRs whose repair-order lines are all deleted or final-issued."""


def load_repair_lines(
    db: Session,
    cfg: I8Settings | None = None,
    *,
    today: date | None = None,
    plant: str | None = None,
    material: str | None = None,
    candidates: Sequence[Mapping] | None = None,
    sources: Sources | None = None,
) -> tuple[list[RepairLine], RegisterStats]:
    """Build the register. Layers 1 and 2, then the Layer 3 arithmetic.

    Returns the repair lines SAP still has open, and only those: a line SAP has
    deleted, closed ("Delivery completed") or blocked, or one on a request for
    quotation, is counted in the stats and never built. Every screen that reads
    the register -- declarations, exceptions, the assistant -- therefore sees
    open order lines only.

    ``candidates`` lets a caller that already holds the EKPO pull pass it in,
    so the snapshot reads the 82,718 rows once for both the register and the
    new-acquisition check rather than twice.

    ``sources`` is a snapshot build's staged copies (``staged.py``). There the
    candidates are already narrowed, so ``candidates_scanned`` is the count the
    staging took over the unfiltered EKPO, not ``len(candidates)``.
    """
    cfg = cfg or get_i8_settings()
    today = today or cfg.reference_date_value or date.today()

    if candidates is None:
        candidates = fetch_candidate_lines(db, plant=plant, material=material, sources=sources)
    scanned = (
        sources.candidates_scanned
        if sources is not None and sources.candidates_scanned is not None
        else len(candidates)
    )

    # Layer 1. The Pstyp ruling: in Python, over rows already fetched. Then
    # what SAP still has open, in the same place and for the same reason:
    # not deleted, not "Delivery completed", not blocked. In that order, so a
    # line is counted under the first of the three that applies to it.
    repair_candidates = [row for row in candidates if is_repair_line(row, cfg)]
    live = [row for row in repair_candidates if not is_deleted_line(row, cfg)]
    undelivered = [row for row in live if not is_delivery_completed(row, cfg)]
    repair_rows = [row for row in undelivered if not is_blocked_line(row, cfg)]
    excluded = {
        "candidates_scanned": scanned,
        "excluded_deleted_lines": len(repair_candidates) - len(live),
        "excluded_delivery_completed_lines": len(live) - len(undelivered),
        "excluded_blocked_lines": len(undelivered) - len(repair_rows),
    }

    if not repair_rows:
        return [], RegisterStats(**excluded)

    documents = sorted({row["ebeln"] for row in repair_rows})
    materials = sorted({row["matnr"] for row in repair_rows if row["matnr"]})
    schedules, dispatches, headers, vendor_names, lead_times = _evidence(
        db, documents, materials, cfg, sources
    )

    # Requests for quotation. The one test that needs the header, so it runs
    # once the header is here rather than with the three above -- and so it is
    # counted last: an RFQ SAP has deleted, closed or blocked is counted as that.
    order_rows = [row for row in repair_rows if not is_rfq_line(headers.get(row["ebeln"]), cfg)]
    excluded["excluded_rfq_lines"] = len(repair_rows) - len(order_rows)

    lines = [
        _build_line(
            row,
            schedule=schedules.get((row["ebeln"], row["ebelp"])),
            dispatch=dispatches.get((row["ebeln"], row["ebelp"])),
            header=headers.get(row["ebeln"]),
            vendor_names=vendor_names,
            lead_time=_lead_time_days(lead_times.get((row["matnr"], row["werks"]))),
            today=today,
            cfg=cfg,
        )
        for row in order_rows
    ]

    vendors = {line.vendor for line in lines if line.vendor}
    stats = RegisterStats(
        total_lines=len(lines),
        open_lines=len(lines),
        overdue_lines=sum(1 for line in lines if line.is_overdue),
        no_due_date_lines=sum(
            1 for line in lines if line.overdue_status == "NO_DUE_DATE"
        ),
        lines_with_lead_time=sum(1 for line in lines if line.lead_time_days),
        lines_beyond_lead_time=sum(1 for line in lines if line.is_beyond_lead_time),
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
        distinct_materials=len({line.material_id for line in lines}),
        distinct_vendors=len(vendors),
        vendors_resolved_to_a_name=len(
            {line.vendor for line in lines if line.vendor and line.vendor_name}
        ),
        **excluded,
    )
    logger.info(
        "I08 register: %d open repair lines of %d candidates (%d overdue; "
        "left out: %d delivery completed, %d blocked, %d deleted in SAP, %d on an RFQ)",
        stats.total_lines,
        stats.candidates_scanned,
        stats.overdue_lines,
        stats.excluded_delivery_completed_lines,
        stats.excluded_blocked_lines,
        stats.excluded_deleted_lines,
        stats.excluded_rfq_lines,
    )
    return lines, stats


def open_repair_index(
    lines: Sequence[RepairLine],
) -> dict[tuple[str, str | None], tuple[int, Decimal]]:
    """(material, plant) -> (open line count, quantity out for repair).

    The join point between W5.2 and W5.1: this is what gives every universe row
    its ``hasOpenRepair`` flag without the universe model needing to know
    anything about item categories or purchase orders. Every register line is
    open in SAP, so every one counts.
    """
    index: dict[tuple[str, str | None], tuple[int, Decimal]] = {}
    for line in lines:
        key = (line.material_id, line.plant)
        count, qty = index.get(key, (0, ZERO))
        index[key] = (count + 1, qty + line.qty_under_repair)
    return index

def with_requisition_details(
    lines: Sequence[RepairLine],
    requisitions: Mapping[tuple[str, str], Mapping],
) -> list[RepairLine]:
    """Each line with its repair PR's date and department filled in.

    ``requisitions`` maps (BANFN, BNFPO) to a PR row with ``badat`` and
    ``afnam`` -- the rows the requisition loader reads anyway (27-Oct-2026), so
    EBAN is not read twice. A line whose PR is not among them keeps None: the
    PR was not read, which is not the same as "no department".
    """
    out: list[RepairLine] = []
    for line in lines:
        pr = (
            requisitions.get((line.pr_number, line.pr_item))
            if line.pr_number and line.pr_item
            else None
        )
        out.append(
            replace(line, pr_requested_on=pr["badat"], pr_department=pr["afnam"])
            if pr is not None
            else line
        )
    return out
