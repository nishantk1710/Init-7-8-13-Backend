"""Goods issued per PO line: which delivery each goods issue came out of.

SAP does not record which purchase order a goods issue used up. A goods issue
(MSEG 201/261) carries the reservation it was issued against (MSEG-RSNUM/RSPOS)
and no PO reference at all (0 of 47,635 issues, ``procurement_chain.py``). The
only way from an issue back to a PO is through a reservation that raised the
PR (RESB-BANFN/BNFPO) -- the order-driven case. Stock material (OAR) is not
bought that way: MRP raises the PR to refill stock, and a separate reservation
later takes stock out. The two meet only at the material and plant.

So a PO line's issued quantity is found one of two ways (``IssueMatch``):

* ``RESERVATION`` -- the PR the line orders was raised by a reservation. The
  line's issues are that reservation's own. Exact.
* ``OLDEST_DELIVERY_FIRST`` -- every other line. Every delivery of the material
  at the plant (EKBE goods receipts, 101 net of 102, dated EKBE-BUDAT) is
  queued oldest first, and every goods issue (MSEG 201/261 net of 202/262,
  dated MKPF-BUDAT) uses up the oldest delivery still holding stock that had
  arrived by the day of the issue. A rule, not an SAP link -- the label says so.

A line delivered before the earliest goods movement the extract holds is
``BEFORE_ISSUE_HISTORY``: what was issued out of it then cannot be seen, so it
is neither queued nor called unissued.

The next planned issue is the open reservations (RESB: not deleted, not final-
issued, requirement quantity BDMNG above withdrawn ENMNG) of the material and
plant, by requirement date BDTER, run through the same queue against the stock
still left; a ``RESERVATION`` line's is its own reservation's.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from app.initiatives.i13.movements import ISSUE_TYPES, RECEIPT_TYPES, REVERSAL_OF, event_dates, net_quantity

Row = dict[str, Any]
PoKey = tuple[str, str]

_ISSUE_REVERSALS = frozenset(rev for rev, base in REVERSAL_OF.items() if base in ISSUE_TYPES)
_RECEIPT_REVERSALS = frozenset(rev for rev, base in REVERSAL_OF.items() if base in RECEIPT_TYPES)


class IssueMatch(str, Enum):
    RESERVATION = "RESERVATION"
    OLDEST_DELIVERY_FIRST = "OLDEST_DELIVERY_FIRST"
    BEFORE_ISSUE_HISTORY = "BEFORE_ISSUE_HISTORY"


@dataclass(frozen=True, slots=True)
class PoLineIssues:
    """One PO line's deliveries and what has been issued out of them."""

    material: str
    plant: str
    po_number: str
    po_item: str
    received_quantity: Decimal
    delivery_count: int
    first_gr_date: date | None
    last_gr_date: date | None
    issued_quantity: Decimal
    first_issue_date: date | None
    last_issue_date: date | None
    #: None when nothing was delivered and no reservation links the line.
    issue_match: IssueMatch | None
    next_planned_issue_date: date | None = None
    next_planned_issue_quantity: Decimal | None = None


@dataclass(slots=True)
class _Delivery:
    key: PoKey
    received_on: date
    quantity: Decimal
    issued: Decimal = Decimal("0")
    #: Set aside for open reservations (the next planned issue).
    planned: Decimal = Decimal("0")
    #: Position in the material-plant's queue, once sorted.
    position: int = 0

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.issued


@dataclass(slots=True)
class _Allocation:
    """Part of one issue, taken from ``delivery`` (None: from stock older than
    any queued delivery). Kept on a stack so a reversal undoes the latest."""

    delivery: _Delivery | None
    quantity: Decimal
    issued_on: date


@dataclass
class _NextIssue:
    by_line: dict[PoKey, tuple[tuple[str, str], date, Decimal]] = field(default_factory=dict)

    def take(self, key: PoKey, reservation: tuple[str, str], needed_on: date, quantity: Decimal) -> None:
        current = self.by_line.get(key)
        if current is None:
            self.by_line[key] = (reservation, needed_on, quantity)
        elif current[0] == reservation:
            self.by_line[key] = (reservation, needed_on, current[2] + quantity)


def _deliveries(key: PoKey, gr_rows: list[Row]) -> list[_Delivery]:
    """One per 101, oldest first; a 102 takes back from the latest delivery
    still holding quantity (SAP posts the reversal after what it reverses)."""
    deliveries: list[_Delivery] = []
    events = sorted(
        (row["BudatMkpf"], 0 if row.get("Bwart") in RECEIPT_TYPES else 1, row.get("Menge") or Decimal("0"))
        for row in gr_rows
        if row.get("BudatMkpf") is not None and (row.get("Bwart") in RECEIPT_TYPES or row.get("Bwart") in _RECEIPT_REVERSALS)
    )
    for received_on, is_reversal, quantity in events:
        if not is_reversal:
            deliveries.append(_Delivery(key, received_on, quantity))
            continue
        for delivery in reversed(deliveries):
            if quantity <= 0:
                break
            taken = min(quantity, delivery.quantity)
            delivery.quantity -= taken
            quantity -= taken
    return [d for d in deliveries if d.quantity > 0]


def _is_open(reservation: Row) -> bool:
    if reservation.get("Xloek") or reservation.get("Kzear") or reservation.get("Bdter") is None:
        return False
    # A reservation for a transfer or another movement is not an issue.
    if reservation.get("Bwart") not in (None, *ISSUE_TYPES):
        return False
    return _open_quantity(reservation) > 0


def _open_quantity(reservation: Row) -> Decimal:
    return (reservation.get("Bdmng") or Decimal("0")) - (reservation.get("Enmng") or Decimal("0"))


def _issue_key(row: Row) -> tuple:
    return (row.get("Bwart"), row.get("BudatMkpf"), row.get("Menge"))


def _match_material(
    deliveries: list[_Delivery], issue_rows: list[Row], open_reservations: list[Row], next_issue: _NextIssue
) -> list[_Allocation]:
    """Run one material-plant's issues through its delivery queue. Returns the
    allocations still standing (net of reversals); fills ``next_issue``."""
    deliveries.sort(key=lambda d: (d.received_on, d.key))
    for position, delivery in enumerate(deliveries):
        delivery.position = position
    stack: list[_Allocation] = []
    head = 0  # every delivery before it is used up
    events = sorted(issue_rows, key=lambda r: (r["BudatMkpf"], 0 if r.get("Bwart") in ISSUE_TYPES else 1))
    for row in events:
        quantity = row.get("Menge") or Decimal("0")
        issued_on = row["BudatMkpf"]
        if row.get("Bwart") in ISSUE_TYPES:
            index = head
            while quantity > 0 and index < len(deliveries) and deliveries[index].received_on <= issued_on:
                delivery = deliveries[index]
                taken = min(quantity, delivery.remaining)
                if taken > 0:
                    delivery.issued += taken
                    quantity -= taken
                    stack.append(_Allocation(delivery, taken, issued_on))
                if delivery.remaining <= 0 and index == head:
                    head += 1
                index += 1
            if quantity > 0:
                stack.append(_Allocation(None, quantity, issued_on))
            continue
        # A reversal (202/262): undo the latest issue still standing.
        while quantity > 0 and stack:
            latest = stack[-1]
            undone = min(quantity, latest.quantity)
            latest.quantity -= undone
            quantity -= undone
            if latest.delivery is not None:
                latest.delivery.issued -= undone
                head = min(head, latest.delivery.position)
            if latest.quantity <= 0:
                stack.pop()

    # What is still in stock meets the open reservations, earliest need first.
    index = 0
    for reservation in sorted(open_reservations, key=lambda r: (r["Bdter"], r.get("Rsnum") or "", r.get("Rspos") or "")):
        needed = _open_quantity(reservation)
        while needed > 0 and index < len(deliveries):
            delivery = deliveries[index]
            available = delivery.remaining - delivery.planned
            if available <= 0:
                index += 1
                continue
            taken = min(needed, available)
            delivery.planned += taken
            needed -= taken
            next_issue.take(delivery.key, (reservation.get("Rsnum"), reservation.get("Rspos")), reservation["Bdter"], taken)
    return [a for a in stack if a.delivery is not None and a.quantity > 0]


def match_po_line_issues(
    *,
    po_items: list[Row],
    gr_rows: list[Row],
    movements: list[Row],
    reservations: list[Row],
    gi_by_reservation: list[Row],
    history_start: date | None = None,
) -> dict[PoKey, PoLineIssues]:
    """Every PO line's issues -- see the module note. ``history_start`` is the
    earliest goods movement the extract holds; None reads it off
    ``movements``."""
    if history_start is None:
        history_start = min((r["BudatMkpf"] for r in movements if r.get("BudatMkpf") is not None), default=None)

    # The order-driven case: a reservation that raised the PR (RESB-BANFN/BNFPO).
    reservation_by_pr: dict[tuple[str, str], Row] = {
        (r["Banfn"], r["Bnfpo"]): r for r in reservations if r.get("Banfn") and r.get("Bnfpo")
    }
    gi_by_key: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in gi_by_reservation:
        gi_by_key[(row["Rsnum"], row["Rspos"])].append(row)

    gr_by_line: dict[PoKey, list[Row]] = defaultdict(list)
    for row in gr_rows:
        gr_by_line[(row["Ebeln"], row["Ebelp"])].append(row)

    results: dict[PoKey, PoLineIssues] = {}
    queued: dict[tuple[str, str], list[_Delivery]] = defaultdict(list)
    before_history: dict[PoKey, tuple[str, str, list[_Delivery]]] = {}
    for po in po_items:
        key: PoKey = (po["Ebeln"], po["Ebelp"])
        material, plant = po["Matnr"], po["Werks"]
        lines_gr = gr_by_line.get(key, [])
        deliveries = _deliveries(key, lines_gr)
        received = net_quantity(lines_gr, RECEIPT_TYPES)
        first_gr, last_gr = event_dates(lines_gr, RECEIPT_TYPES)
        reservation = reservation_by_pr.get((po.get("Banfn"), po.get("Bnfpo"))) if po.get("Banfn") else None
        if reservation is not None:
            rows = gi_by_key.get((reservation["Rsnum"], reservation["Rspos"]), [])
            first_issue, last_issue = event_dates(rows, ISSUE_TYPES)
            is_open = _is_open(reservation)
            results[key] = PoLineIssues(
                material, plant, key[0], key[1], received, len(deliveries), first_gr, last_gr,
                issued_quantity=net_quantity(rows, ISSUE_TYPES),
                first_issue_date=first_issue,
                last_issue_date=last_issue,
                issue_match=IssueMatch.RESERVATION,
                next_planned_issue_date=reservation["Bdter"] if is_open else None,
                next_planned_issue_quantity=_open_quantity(reservation) if is_open else None,
            )
        elif not deliveries:
            results[key] = PoLineIssues(
                material, plant, key[0], key[1], received, 0, first_gr, last_gr,
                issued_quantity=Decimal("0"), first_issue_date=None, last_issue_date=None, issue_match=None,
            )
        elif history_start is not None and deliveries[0].received_on < history_start:
            before_history[key] = (material, plant, deliveries)
        else:
            queued[(material, plant)].extend(deliveries)

    # Issues the reservation case already accounts for are not in the stock pool.
    direct_issues: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for reservation in reservation_by_pr.values():
        for row in gi_by_key.get((reservation["Rsnum"], reservation["Rspos"]), []):
            direct_issues[(reservation["Matnr"], reservation["Werks"])][_issue_key(row)] += 1

    issue_rows: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in movements:
        if row.get("BudatMkpf") is None or not (row.get("Bwart") in ISSUE_TYPES or row.get("Bwart") in _ISSUE_REVERSALS):
            continue
        material_plant = (row["Matnr"], row["Werks"])
        if material_plant not in queued:
            continue
        already = direct_issues.get(material_plant)
        if already and already[_issue_key(row)] > 0:
            already[_issue_key(row)] -= 1
            continue
        issue_rows[material_plant].append(row)

    open_by_key: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for reservation in reservations:
        if not (reservation.get("Banfn") and reservation.get("Bnfpo")) and _is_open(reservation):
            open_by_key[(reservation["Matnr"], reservation["Werks"])].append(reservation)

    for material_plant, deliveries in queued.items():
        next_issue = _NextIssue()
        standing = _match_material(deliveries, issue_rows.get(material_plant, []), open_by_key.get(material_plant, []), next_issue)
        dates_by_line: dict[PoKey, list[date]] = defaultdict(list)
        for allocation in standing:
            dates_by_line[allocation.delivery.key].append(allocation.issued_on)
        by_line: dict[PoKey, list[_Delivery]] = defaultdict(list)
        for delivery in deliveries:
            by_line[delivery.key].append(delivery)
        for key, line_deliveries in by_line.items():
            line_gr = gr_by_line[key]
            first_gr, last_gr = event_dates(line_gr, RECEIPT_TYPES)
            issue_dates = dates_by_line.get(key, [])
            planned = next_issue.by_line.get(key)
            results[key] = PoLineIssues(
                material_plant[0], material_plant[1], key[0], key[1],
                net_quantity(line_gr, RECEIPT_TYPES), len(line_deliveries), first_gr, last_gr,
                issued_quantity=sum((d.issued for d in line_deliveries), Decimal("0")),
                first_issue_date=min(issue_dates) if issue_dates else None,
                last_issue_date=max(issue_dates) if issue_dates else None,
                issue_match=IssueMatch.OLDEST_DELIVERY_FIRST,
                next_planned_issue_date=planned[1] if planned else None,
                next_planned_issue_quantity=planned[2] if planned else None,
            )

    for key, (material, plant, deliveries) in before_history.items():
        line_gr = gr_by_line[key]
        first_gr, last_gr = event_dates(line_gr, RECEIPT_TYPES)
        results[key] = PoLineIssues(
            material, plant, key[0], key[1],
            net_quantity(line_gr, RECEIPT_TYPES), len(deliveries), first_gr, last_gr,
            issued_quantity=Decimal("0"), first_issue_date=None, last_issue_date=None,
            issue_match=IssueMatch.BEFORE_ISSUE_HISTORY,
        )
    return results


def po_line_issues(
    procurement_repository: Any,
    reservation_repository: Any,
    movement_repository: Any,
    *,
    material: str | None = None,
    plant: str | None = None,
    history_start: date | None = None,
) -> dict[PoKey, PoLineIssues]:
    """``match_po_line_issues`` over the repositories' rows. Narrowed to one
    material or plant, the queue is still that material-plant's whole."""
    po_items = procurement_repository.get_purchase_order_items(material=material, plant=plant)
    gr_rows = procurement_repository.get_goods_receipt_history()
    if material or plant:
        lines = {(po["Ebeln"], po["Ebelp"]) for po in po_items}
        gr_rows = [r for r in gr_rows if (r["Ebeln"], r["Ebelp"]) in lines]
    return match_po_line_issues(
        po_items=po_items,
        gr_rows=gr_rows,
        movements=movement_repository.get_movement_history(material=material, plant=plant),
        reservations=reservation_repository.get_reservations(material=material, plant=plant),
        gi_by_reservation=reservation_repository.get_goods_issue_by_reservation(),
        history_start=history_start,
    )
