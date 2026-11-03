"""New purchases of 80-series units, and the justification each one follows.

FR-5 asks the assistant for a reason when a requester goes ahead with a new
unit although a repairable one exists. This module finds the purchases SAP
then raised, so the Justifications screen can show which purchase each reason
answered (:func:`purchases_answered`).

Until 3-Nov-2026 the same purchases also fed a bought-new exception (FR-8): a
purchase made while a repair of the part was open, with no reason recorded.
The exception queue was removed that day, and its overlap rules with it.

The purchase is the new-buy PR (since 27-Oct-2026)
--------------------------------------------------
A new unit is bought through a stock purchase requisition: MRP raises it, or
an inventory controller raises it by hand. So the PR is read, dated by its own
PR date (EBAN.BADAT). Until 27-Oct PO lines were read, dated by EKPO.ERDAT,
which the live data does not carry -- so no purchase was ever dated.

* **A new-buy PR** is an 80-series PR item that is not deleted and is not a
  repair PR: no repair order's line names it, and the reservation it carries
  (if any) is not a repair order's.
* **A direct purchase-order line** -- one raised with no PR behind it -- is a
  purchase too: a standard item (EKPO.PSTYP, ``I8_NEW_PURCHASE_ITEM_CATEGORY``)
  on an 80-series part, not deleted, for a quantity, on a document that is an
  order. Contracts and requests for quotation are not orders; which document
  types those are is I13's shared rule (``I13_NON_ORDER_DOC_TYPES``), read
  here, not copied. It is dated by its PO header's created-on date (EKKO.AEDAT).
* **A PO line raised from a PR is the same purchase as the PR**, so it is not
  counted again: only the PR is.

Matched by part, plant and date, permanently
---------------------------------------------
New-buy PRs carry no reservation -- 3 of 6,259 on 80-series parts name one
(measured 26-Oct-2026) -- because they are raised by the MRP and inventory
desks, never from the requester's reservation. So no key will ever tie a
purchase to the request it answers. A justification carries material, plant
and date, and a purchase is answered by one for the same part at the same plant
within the window: the same key the attestation check uses, and it is the rule,
not a stand-in for an exact match.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series, series_like_patterns
from app.initiatives.i8.register import _HEADER_SQL, is_deleted_line, timed_step
from app.initiatives.i8.requisitions import is_requisition_deleted
from app.initiatives.i8.staged import Sources, reading
from app.shared.sql_lists import fetch_in_chunks, like_any

logger = get_logger(__name__)

#: The justification kind that answers a new purchase. The same value the
#: assistant writes when a requester proceeds past the I08 challenge
#: (``app.assistant.turns``).
NEW_ACQUISITION_KIND = "NEW_ACQUISITION"

#: Which kind of document a purchase or a repair is: the wire's
#: ``SAPDocumentReference.type`` values. Labels of ours, not SAP values.
PURCHASE_ORDER = "PO"
PURCHASE_REQUISITION = "PR"

ZERO = Decimal(0)


@dataclass(frozen=True)
class NewAcquisition:
    """One purchase of a new 80-series unit: a new-buy PR item, or a direct
    purchase-order line with no PR behind it."""

    purchasing_document: str
    """EBAN.BANFN for a PR, EKPO.EBELN for a direct PO."""
    item: str
    material_id: str
    plant: str | None
    description: str | None
    ordered_qty: Decimal
    raised_at: date | None
    """When the purchase was asked for: the PR date (EBAN.BADAT) for a PR, the
    PO header's created-on date (EKKO.AEDAT) for a direct PO. Never EKPO.ERDAT:
    the live data does not carry it, and reading it left every purchase undated
    -- and so never checked -- until 27-Oct-2026."""

    document_type: str = PURCHASE_ORDER
    """``PR`` or ``PO``. Appended with a default: a stored snapshot and the
    tests construct this positionally."""
    requested_by: str | None = None
    """EBAN.AFNAM on a PR, as SAP sent it. None on a direct PO."""

    @property
    def key(self) -> tuple[str, str, str]:
        """Type first: a PR number and a PO number are different series and a
        key must never let one stand for the other."""
        return (self.document_type, self.purchasing_document, self.item)

    @property
    def reference(self) -> str:
        """``PR-{BANFN}-{BNFPO}`` for a PR, ``{EBELN}-{EBELP}`` for a PO -- the
        same forms as the register's row ids."""
        return document_reference(self.document_type, self.purchasing_document, self.item)


def document_reference(document_type: str, document: str, item: str) -> str:
    """How a register row and a detail link name a document line:
    ``PR-{BANFN}-{BNFPO}`` or ``{EBELN}-{EBELP}``."""
    if document_type == PURCHASE_REQUISITION:
        return f"PR-{document}-{item}"
    return f"{document}-{item}"


@dataclass(frozen=True)
class JustificationRecord:
    """A NEW_ACQUISITION justification: what matching it to a purchase needs,
    and what the Justifications screen shows beside it."""

    material_id: str
    plant: str
    recorded_on: date
    exception_id: str | None = None

    # Display only -- matching never reads these. Optional so a test can build
    # a record from the three fields the rules actually use.
    id: str | None = None
    reason_category: str | None = None
    free_text: str | None = None
    author: str | None = None
    recorded_at: datetime | None = None
    session_id: str | None = None


# --- which documents are purchases: one predicate per rule ---------------------


def is_new_purchase_line(row: Mapping, cfg: I8Settings) -> bool:
    """A standard purchase-order line on an 80-series part that SAP has not
    deleted (a blocked one is still a purchase).

    The item-category test lives here, in Python, for the same reason the
    repair test does in :func:`app.initiatives.i8.register.is_repair_line`.
    """
    return (
        row["pstyp"] == cfg.new_purchase_item_category
        and is_eighty_series(row["matnr"], cfg)
        and not is_deleted_line(row, cfg)
    )


def is_raised_from_requisition(row: Mapping) -> bool:
    """Whether this PO line names the PR it was raised from (EKPO.BANFN). Such
    a line is the PR's purchase, counted on the PR."""
    return bool(row["banfn"])


def has_quantity(row: Mapping) -> bool:
    """Whether the line orders anything. A line for nothing bought nothing."""
    return row["menge"] is not None and row["menge"] > ZERO


def is_order_document(header: Mapping | None, non_order_doc_types: frozenset[str]) -> bool:
    """Whether the PO is an order, not a contract or a request for quotation
    (EKKO.BSART against I13's shared list). A line with no header is an order:
    nothing says otherwise -- the register's reading of a missing header."""
    if header is None:
        return True
    return (header["bsart"] or "").strip().upper() not in non_order_doc_types


def is_direct_purchase_line(
    row: Mapping, header: Mapping | None, cfg: I8Settings, non_order_doc_types: frozenset[str]
) -> bool:
    """A purchase-order line that bought a new unit with no PR behind it."""
    return (
        is_new_purchase_line(row, cfg)
        and has_quantity(row)
        and not is_raised_from_requisition(row)
        and is_order_document(header, non_order_doc_types)
    )


def is_repair_order_line(resb_row: Mapping, cfg: I8Settings) -> bool:
    """Whether this reservation line is a repair order's line for the faulty
    unit (RESB.BWART), whether or not it names a PR yet."""
    return resb_row["bwart"] == cfg.repair_reservation_movement_type


def repair_order_references(
    reservation_lines: Iterable[Mapping], cfg: I8Settings
) -> tuple[frozenset[tuple[str, str]], frozenset[str]]:
    """The PR items repair orders name (BANFN, BNFPO), and the repair orders'
    reservation numbers (RSNUM): what tells a repair PR from a new-buy one.

    Every repair-order line counts, deleted or final-issued as well: a PR that
    was raised for a repair stays a repair PR when the order closes. 80-series
    parts only, in Python, so a staged build and a direct one agree.
    """
    named: set[tuple[str, str]] = set()
    reservations: set[str] = set()
    for row in reservation_lines:
        if not is_repair_order_line(row, cfg) or not is_eighty_series(row["matnr"], cfg):
            continue
        if row["rsnum"]:
            reservations.add(row["rsnum"])
        if row["banfn"] and row["bnfpo"]:
            named.add((row["banfn"], row["bnfpo"]))
    return frozenset(named), frozenset(reservations)


def is_new_buy_requisition(
    eban_row: Mapping,
    repair_requisitions: frozenset[tuple[str, str]],
    repair_reservations: frozenset[str],
    cfg: I8Settings,
) -> bool:
    """An 80-series PR item, not deleted, that is not a repair PR."""
    if not (eban_row["banfn"] and eban_row["bnfpo"]):
        return False
    return (
        is_eighty_series(eban_row["matnr"], cfg)
        and not is_requisition_deleted(eban_row, cfg)
        and (eban_row["banfn"], eban_row["bnfpo"]) not in repair_requisitions
        and not (eban_row["rsnum"] and eban_row["rsnum"] in repair_reservations)
    )


# --- the purchases, pure -------------------------------------------------------


def find_new_acquisitions(
    requisitions: Iterable[Mapping],
    candidates: Iterable[Mapping],
    headers: Mapping[str, Mapping],
    reservation_lines: Iterable[Mapping],
    cfg: I8Settings | None = None,
    *,
    non_order_doc_types: frozenset[str] = frozenset(),
) -> list[NewAcquisition]:
    """Every new purchase: the new-buy PR items in ``requisitions`` (``v_eban``
    rows), then the direct purchase-order lines in ``candidates`` (the EKPO
    pull the register already holds), in key order.

    No database. ``headers`` maps EBELN to an EKKO row (``bsart``, ``aedat``);
    ``reservation_lines`` are ``v_resb`` rows, read to tell repair PRs apart.
    """
    cfg = cfg or get_i8_settings()
    repair_requisitions, repair_reservations = repair_order_references(reservation_lines, cfg)

    found = [
        NewAcquisition(
            purchasing_document=row["banfn"],
            item=row["bnfpo"],
            material_id=row["matnr"],
            plant=row["werks"],
            description=row["txz01"],
            ordered_qty=row["menge"] if row["menge"] is not None else ZERO,
            raised_at=row["badat"],
            document_type=PURCHASE_REQUISITION,
            requested_by=row["afnam"],
        )
        for row in requisitions
        if is_new_buy_requisition(row, repair_requisitions, repair_reservations, cfg)
    ]
    for row in candidates:
        header = headers.get(row["ebeln"])
        if not is_direct_purchase_line(row, header, cfg, non_order_doc_types):
            continue
        found.append(
            NewAcquisition(
                purchasing_document=row["ebeln"],
                item=row["ebelp"],
                material_id=row["matnr"],
                plant=row["werks"],
                description=row["txz01"],
                ordered_qty=row["menge"],
                raised_at=header["aedat"] if header is not None else None,
                document_type=PURCHASE_ORDER,
            )
        )
    return sorted(found, key=lambda acquisition: acquisition.key)


# --- the loader ----------------------------------------------------------------

_REQUISITION_SQL = """
-- Every PR item on a candidate 80-series part. The LIKE is the usual loose
-- prefilter (material_number.series_like_patterns); which of these are new-buy
-- PRs is decided in Python (acquisitions.is_new_buy_requisition).
select banfn, bnfpo, matnr, werks, txz01, menge, badat, loekz, afnam, rsnum
from v_eban
where {matnr_like}
"""


def load_new_acquisitions(
    db: Session,
    cfg: I8Settings | None = None,
    *,
    candidates: Sequence[Mapping],
    reservation_lines: Sequence[Mapping],
    sources: Sources | None = None,
    non_order_doc_types: frozenset[str] | None = None,
) -> list[NewAcquisition]:
    """The new purchases, read in two batches and classified in Python.

    ``candidates`` is the EKPO pull the snapshot build already holds, and
    ``reservation_lines`` the repair-order lines it read for the open repair
    PRs (:func:`app.initiatives.i8.requisitions.fetch_repair_order_lines`), so
    neither is read twice. The PO headers are read only for the direct lines.
    """
    cfg = cfg or get_i8_settings()
    if non_order_doc_types is None:
        from app.core.config import get_settings

        non_order_doc_types = get_settings().i13_non_order_doc_type_set

    matnr_like, pattern_params = like_any("matnr", "pattern", series_like_patterns(cfg))
    with timed_step("new-buy PRs (EBAN)"):
        requisitions = list(
            db.execute(
                text(reading(_REQUISITION_SQL.format(matnr_like=matnr_like), sources)), pattern_params
            ).mappings()
        )

    direct = [
        row
        for row in candidates
        if is_new_purchase_line(row, cfg) and has_quantity(row) and not is_raised_from_requisition(row)
    ]
    documents = sorted({row["ebeln"] for row in direct})
    with timed_step("new purchases' PO headers (EKKO)"):
        headers = (
            {r["ebeln"]: r for r in fetch_in_chunks(db, reading(_HEADER_SQL, sources), "documents", documents)}
            if documents
            else {}
        )

    found = find_new_acquisitions(
        requisitions, direct, headers, reservation_lines, cfg, non_order_doc_types=non_order_doc_types
    )
    logger.info(
        "I08 new purchases: %d new-buy PRs and %d direct PO lines (of %d PR items and %d PO lines read)",
        sum(1 for a in found if a.document_type == PURCHASE_REQUISITION),
        sum(1 for a in found if a.document_type == PURCHASE_ORDER),
        len(requisitions),
        len(direct),
    )
    return found


# --- which purchase a justification answered ----------------------------------

#: The id a bought-new finding had in the exception queue, before it was removed
#: on 3-Nov-2026. A reason recorded against one still answers that purchase.
_BOUGHT_NEW_FINDING_PREFIX = "EX-UNJUSTIFIED_ACQUISITION-"


def within_justification_window(
    record: JustificationRecord, acquisition: NewAcquisition, window_days: int
) -> bool:
    """Same material, same plant, and the purchase within ``window_days`` of
    the reason, either side."""
    return (
        record.material_id == acquisition.material_id
        and record.plant == acquisition.plant
        and acquisition.raised_at is not None
        and abs((record.recorded_on - acquisition.raised_at).days) <= window_days
    )


def purchases_answered(
    justifications: Sequence[JustificationRecord],
    acquisitions: Sequence[NewAcquisition],
    *,
    window_days: int,
) -> dict[str, NewAcquisition]:
    """Justification id -> the new purchase it answers (28-Oct-2026: the
    Justifications screen's Purchase column).

    A reason recorded against a bought-new finding's id answers that purchase.
    Otherwise it answers a purchase of the same part at the same plant within
    the window; when several are, the one nearest in date, a purchase raised
    on or after the reason before one raised before it (the assistant records
    the reason first). A reason with no entry has no purchase yet.
    """
    by_finding = {f"{_BOUGHT_NEW_FINDING_PREFIX}{a.reference}": a for a in acquisitions}
    by_part: dict[tuple[str, str | None], list[NewAcquisition]] = {}
    for acquisition in acquisitions:
        by_part.setdefault((acquisition.material_id, acquisition.plant), []).append(acquisition)

    answered: dict[str, NewAcquisition] = {}
    for record in justifications:
        if record.id is None:
            continue
        named = by_finding.get(record.exception_id) if record.exception_id else None
        if named is not None:
            answered[record.id] = named
            continue
        near = [
            a
            for a in by_part.get((record.material_id, record.plant), ())
            if within_justification_window(record, a, window_days)
        ]
        if near:
            answered[record.id] = min(
                near,
                key=lambda a: (
                    abs((a.raised_at - record.recorded_on).days),  # type: ignore[operator]
                    a.raised_at < record.recorded_on,  # type: ignore[operator]
                    a.key,
                ),
            )
    return answered
