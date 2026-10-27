"""Open repair PRs: repairs SAP has been asked for that have no PO yet.

Why this exists
---------------
The register is built from repair PO lines, so a repair is invisible to I08
until purchasing turns its PR into a PO. On the 15-Oct-2026 extract (a copy of
SAP QA) that is 108 repairs nobody here could see -- 68 at 1300 and 40 at 1500
-- and it takes the 80-series parts with an open repair from 46 to 94. This
module finds them; the register (behind ``I8_REPAIR_REQUESTS_ENABLED``) and the
assistant serve them.

Where a repair PR comes from
----------------------------
A repair starts as a repair ORDER: a reservation whose line for the faulty unit
carries movement type 543 (``I8_REPAIR_RESERVATION_MOVEMENT_TYPE``). That line
names the repair PR raised from it (RESB.BANFN/BNFPO), and the PR is what
purchasing converts. So the PRs here are exactly the ones a repair order's 543
line names -- not every PR with an item category, and not every PR on an
80-series part: new-buy PRs are raised by MRP and the inventory controllers and
carry no reservation at all (3 of 6,259 on 26-Oct-2026).

The open rule, all three at once (ruled 26-Oct-2026)
----------------------------------------------------
1. The PR item is not deleted (EBAN.LOEKZ, ``I8_REQUISITION_DELETED_INDICATOR``
   -- 'X' on EBAN, NOT EKPO's 'L').
2. No live PO points back to it: no EKPO line naming this BANFN/BNFPO that is
   not deleted and is not on a request for quotation. A BLOCKED PO still counts
   as a PO -- the repair has been ordered, SAP is holding it -- and a PO with no
   EKKO header counts as live, as it does in the register.
3. At least one of its 543 lines is neither deleted (RESB.XLOEK) nor
   final-issued (RESB.KZEAR): the repair order still wants the unit repaired.

On the 15-Oct extract that is 108. Each looser reading over-counts, and the
numbers say by how much: ignoring the PR deletion gives 156; "no live PO" alone
gives 122; treating a blocked PO as no PO gives 117.

Every value read off SAP is a setting, compared in a small named predicate --
the same discipline as ``register.py``, and ``tests/test_i8_rules.py`` holds this
module to it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.aging import aging_bucket, elapsed_days
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series
from app.initiatives.i8.register import _HEADER_SQL, timed_step
from app.initiatives.i8.staged import Sources, reading
from app.shared.sql_lists import fetch_in_chunks

logger = get_logger(__name__)

ZERO = Decimal(0)

# The repair status of a repair with no PO yet. The first member of the
# frontend's RepairStatus union; the register's PO rows are PO_ISSUED or
# AT_VENDOR (register.py).
PR_RAISED = "PR Raised"

# The overdue status of a repair with no PO yet. Nothing has been ordered, so
# there is no promised date to be late against -- not NO_DUE_DATE, which says
# a PO exists without one. PO rows never carry it (aging.OVERDUE_STATES).
NOT_ORDERED = "NOT_ORDERED"


@dataclass(frozen=True)
class RepairRequest:
    """One open repair PR item with no PO yet. Built only when all three parts
    of the open rule hold -- see the module docstring."""

    purchase_requisition: str
    """EBAN.BANFN."""
    item: str
    """EBAN.BNFPO, leading zeros stripped as every key is ('10', not '00010')."""
    material_id: str
    plant: str | None
    description: str | None
    """EBAN.TXZ01, the PR's own short text."""
    quantity: Decimal
    """EBAN.MENGE. Zero when SAP sent none, never guessed."""
    unit: str | None
    requested_on: date | None
    """EBAN.BADAT, the PR date. What days open and the aging band count from."""
    need_date: date | None
    """EBAN.LFDAT, the delivery date the PR asks for."""
    department: str | None
    """EBAN.AFNAM: the 5-digit department code (all 2,418 undeleted repair PRs
    carry one, 26-Oct-2026). A code; the name is looked up where it is served."""
    requisition_status: str | None
    """EBAN.STATU, as SAP sent it."""
    document_type: str | None
    """EBAN.BSART, as SAP sent it. Not a gate."""
    repair_order: str | None
    """RESB.AUFNR on the repair order's open 543 line, when it names one."""
    reservation: str | None
    """RESB.RSNUM of that line: the repair order's reservation."""
    days_open: int | None
    """BADAT to the reference date. None when the PR has no date."""
    aging_bucket: str | None
    """Banded on ``days_open`` with the register's own bands."""

    @property
    def key(self) -> tuple[str, str]:
        return (self.purchase_requisition, self.item)

    @property
    def row_id(self) -> str:
        """The register row id: ``PR-{BANFN}-{BNFPO}``. A PO row's id is
        ``{EBELN}-{EBELP}``; the prefix keeps the two apart, and the frontend
        splits at the LAST hyphen, giving document ``PR-{BANFN}``."""
        return f"PR-{self.purchase_requisition}-{self.item}"


@dataclass(frozen=True)
class RequestStats:
    """How the open repair PRs were arrived at, so the count reconciles.

    Each excluded PR is counted once, under the first part of the rule it
    fails, in this order: deleted PR, live PO, reservation line(s) closed.
    """

    open_repair_requests: int = 0
    requisitions_excluded_deleted: int = 0
    requisitions_excluded_live_po: int = 0
    requisitions_excluded_reservation_closed: int = 0
    repair_reservation_lines: int = 0
    """The 543 lines naming a PR, on an 80-series part, that were read."""
    requisitions_not_found: int = 0
    """PRs a 543 line names that the in-scope 80-series PR lines do not hold.
    Reported rather than absorbed: such a repair cannot be shown at all."""


# --- the rule, one predicate per part ----------------------------------------


def is_repair_reservation_line(resb_row: Mapping, cfg: I8Settings) -> bool:
    """Whether this reservation line is a repair order's line for the faulty
    unit (RESB.BWART) and names the PR raised from it (RESB.BANFN)."""
    return resb_row["bwart"] == cfg.repair_reservation_movement_type and bool(resb_row["banfn"])


def is_reservation_line_closed(resb_row: Mapping, cfg: I8Settings) -> bool:
    """Whether the repair order no longer wants this unit: the line is deleted
    (RESB.XLOEK) or final-issued (RESB.KZEAR)."""
    return (
        resb_row["xloek"] == cfg.reservation_deleted_indicator
        or resb_row["kzear"] == cfg.reservation_final_issue_indicator
    )


def is_requisition_deleted(eban_row: Mapping, cfg: I8Settings) -> bool:
    """Whether SAP has deleted this PR item (EBAN.LOEKZ). EBAN marks it 'X',
    not EKPO's 'L' -- hence its own setting."""
    return eban_row["loekz"] == cfg.requisition_deleted_indicator


def has_live_purchase_order(
    pr_key: tuple[str, str],
    back_pointers: Mapping[tuple[str, str], Sequence[Mapping]],
    headers: Mapping[str, Mapping],
    cfg: I8Settings,
) -> bool:
    """Whether some PO line names this PR and is a live order.

    Live: not deleted (EKPO.LOEKZ) and not on a request for quotation
    (EKKO.BSART). Blocked (the other LOEKZ value) is still live -- the repair
    has been ordered and SAP is holding it; treating it as no PO would add 9
    PRs on the 15-Oct extract. A line with no header is live: nothing says it
    is an RFQ, the register's own reading.
    """
    for po in back_pointers.get(pr_key, ()):
        if po["loekz"] == cfg.po_deleted_indicator:
            continue
        header = headers.get(po["ebeln"])
        if header is not None and header["bsart"] == cfg.rfq_doc_type:
            continue
        return True
    return False


def index_back_pointers(po_rows: Iterable[Mapping]) -> dict[tuple[str, str], list[Mapping]]:
    """(BANFN, BNFPO) -> the PO lines that name it."""
    index: dict[tuple[str, str], list[Mapping]] = defaultdict(list)
    for row in po_rows:
        if row["banfn"] and row["bnfpo"]:
            index[(row["banfn"], row["bnfpo"])].append(row)
    return dict(index)


# --- the classification, pure -------------------------------------------------


def _sort_key(request: RepairRequest) -> tuple:
    # BADAT first, an undated PR last; then the PR number and item as numbers
    # where they are numbers, so item 20 sorts before item 100.
    def number(value: str) -> tuple[int, int | str]:
        return (0, int(value)) if value.isdigit() else (1, value)

    return (
        request.requested_on is None,
        request.requested_on or date.min,
        number(request.purchase_requisition),
        number(request.item),
    )


def classify_repair_requests(
    resb_rows: Iterable[Mapping],
    eban_rows: Iterable[Mapping],
    back_pointers: Mapping[tuple[str, str], Sequence[Mapping]],
    headers: Mapping[str, Mapping],
    cfg: I8Settings,
    reference_date: date,
) -> tuple[tuple[RepairRequest, ...], RequestStats]:
    """The open repair PRs among ``resb_rows``, and how the rest fell out.

    No database: the loader and the extract check both call this. Rows are
    mappings with the ``v_resb`` / ``v_eban`` column names; ``back_pointers``
    comes from :func:`index_back_pointers` over EKPO lines (with ``ebeln``,
    ``loekz``), ``headers`` maps EBELN to an EKKO row (with ``bsart``).

    80-series parts only, in Python as everywhere else: the staged copies are
    narrowed with a superset LIKE, so the exact test has to happen here for a
    staged build and a direct one to agree.
    """
    reservation_lines: dict[tuple[str, str], list[Mapping]] = defaultdict(list)
    read = 0
    for row in resb_rows:
        if not is_repair_reservation_line(row, cfg) or not is_eighty_series(row["matnr"], cfg):
            continue
        read += 1
        reservation_lines[(row["banfn"], row["bnfpo"])].append(row)

    requisitions = {
        (row["banfn"], row["bnfpo"]): row
        for row in eban_rows
        if row["banfn"] and row["bnfpo"] and is_eighty_series(row["matnr"], cfg)
    }

    counts = {"deleted": 0, "live_po": 0, "closed": 0, "not_found": 0}
    found: list[RepairRequest] = []
    for key, lines in reservation_lines.items():
        pr = requisitions.get(key)
        if pr is None:
            counts["not_found"] += 1
            continue
        # The order the exclusions are counted in, one each.
        if is_requisition_deleted(pr, cfg):
            counts["deleted"] += 1
            continue
        if has_live_purchase_order(key, back_pointers, headers, cfg):
            counts["live_po"] += 1
            continue
        open_lines = [line for line in lines if not is_reservation_line_closed(line, cfg)]
        if not open_lines:
            counts["closed"] += 1
            continue
        found.append(_build_request(pr, open_lines, reference_date, cfg))

    requests = tuple(sorted(found, key=_sort_key))
    stats = RequestStats(
        open_repair_requests=len(requests),
        requisitions_excluded_deleted=counts["deleted"],
        requisitions_excluded_live_po=counts["live_po"],
        requisitions_excluded_reservation_closed=counts["closed"],
        repair_reservation_lines=read,
        requisitions_not_found=counts["not_found"],
    )
    return requests, stats


def _build_request(
    pr: Mapping, open_lines: Sequence[Mapping], reference_date: date, cfg: I8Settings
) -> RepairRequest:
    # Two open 543 lines for one PR would be one repair asked for twice; the
    # lowest reservation and item is taken so the choice is stable.
    line = min(open_lines, key=lambda r: (r["rsnum"] or "", r["rspos"] or ""))
    days = elapsed_days(raised_at=pr["badat"], today=reference_date)
    return RepairRequest(
        purchase_requisition=pr["banfn"],
        item=pr["bnfpo"],
        material_id=pr["matnr"],
        plant=pr["werks"],
        description=pr["txz01"],
        quantity=pr["menge"] if pr["menge"] is not None else ZERO,
        unit=pr["meins"],
        requested_on=pr["badat"],
        need_date=pr["lfdat"],
        department=pr["afnam"],
        requisition_status=pr["statu"],
        document_type=pr["bsart"],
        repair_order=line["aufnr"],
        reservation=line["rsnum"] or pr["rsnum"],
        days_open=days,
        aging_bucket=aging_bucket(days, cfg.aging_band_boundaries_list),
    )


# --- the loader ----------------------------------------------------------------

_RESERVATION_SQL = """
-- The repair orders' lines for the faulty unit. The movement type is a bound
-- setting, not a literal; the deletion and final-issue flags are read here and
-- judged in Python (requisitions.is_reservation_line_closed). Lines that name
-- no PR yet are read too (27-Oct-2026): the new-purchase check needs every
-- repair order's reservation number to tell a repair PR from a new-buy one
-- (acquisitions.repair_order_references); is_repair_reservation_line still
-- takes only the lines that name a PR here.
select rsnum, rspos, matnr, werks, bwart, banfn, bnfpo, aufnr, xloek, kzear
from v_resb
where bwart = :movement_type
"""

_REQUISITION_SQL = """
-- The PR items those lines name. Every item of each PR is read and the match on
-- (BANFN, BNFPO) is made in Python, as the register matches its PO lines.
select banfn, bnfpo, matnr, werks, txz01, menge, meins, badat, lfdat, loekz,
       statu, bsart, afnam, rsnum
from v_eban
where banfn in :requisitions
"""


def fetch_repair_order_lines(
    db: Session, cfg: I8Settings | None = None, *, sources: Sources | None = None
) -> list[Mapping]:
    """Every repair order's line for the faulty unit (``v_resb``), read once
    per build: the open repair PRs and the new-purchase check both use them."""
    cfg = cfg or get_i8_settings()
    with timed_step("repair-order reservation lines (RESB)"):
        return list(
            db.execute(
                text(reading(_RESERVATION_SQL, sources)),
                {"movement_type": cfg.repair_reservation_movement_type},
            ).mappings()
        )


def load_repair_requests(
    db: Session,
    cfg: I8Settings | None = None,
    *,
    reference_date: date,
    candidates: Sequence[Mapping],
    sources: Sources | None = None,
    reservation_lines: Sequence[Mapping] | None = None,
) -> tuple[tuple[RepairRequest, ...], RequestStats]:
    """The open repair PRs, read in three batches and classified in Python.

    ``candidates`` is the EKPO pull the snapshot build already holds (with
    ``banfn``, ``bnfpo``, ``loekz``): it carries every PO line that could point
    back to a repair PR -- staged, it keeps repair and 80-series lines, and a
    repair PR is on an 80-series part -- so EKPO is not read a second time.
    The headers are read only for the POs that do point back, through the
    register's own header query.

    ``reservation_lines`` are the repair-order lines when the caller has read
    them already (:func:`fetch_repair_order_lines`); None reads them here.
    """
    cfg = cfg or get_i8_settings()

    resb_rows = (
        list(reservation_lines)
        if reservation_lines is not None
        else fetch_repair_order_lines(db, cfg, sources=sources)
    )
    named = sorted({row["banfn"] for row in resb_rows if row["banfn"]})
    with timed_step("repair PRs (EBAN)"):
        eban_rows = (
            fetch_in_chunks(db, reading(_REQUISITION_SQL, sources), "requisitions", named) if named else []
        )

    wanted = {(row["banfn"], row["bnfpo"]) for row in resb_rows if row["banfn"]}
    back_pointers = index_back_pointers(
        row for row in candidates if (row["banfn"], row["bnfpo"]) in wanted
    )
    documents = sorted({po["ebeln"] for rows in back_pointers.values() for po in rows})
    with timed_step("repair PRs' PO headers (EKKO)"):
        headers = (
            {r["ebeln"]: r for r in fetch_in_chunks(db, reading(_HEADER_SQL, sources), "documents", documents)}
            if documents
            else {}
        )

    requests, stats = classify_repair_requests(
        resb_rows, eban_rows, back_pointers, headers, cfg, reference_date
    )
    logger.info(
        "I08 repair PRs: %d open with no PO yet (left out: %d deleted, %d with a live PO, "
        "%d whose repair-order line is closed; %d named but not found)",
        stats.open_repair_requests,
        stats.requisitions_excluded_deleted,
        stats.requisitions_excluded_live_po,
        stats.requisitions_excluded_reservation_closed,
        stats.requisitions_not_found,
    )
    return requests, stats
