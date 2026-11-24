"""The acquired-vs-plan tab's rows: every captured plan, and every OAR
reservation raised directly in SAP, each with what was issued against it.

WATCH answers acquired-vs-plan per material and plant, and only where an
assistant plan exists -- every other position reads "no plan". This tab shows
the reservation-level picture underneath, from both sources:

* **Via assistant** -- one row per captured plan: planned quantity against the
  goods issued on the reservations that stand for it (``plan_outcomes``: the
  ones whose item text carries its request or session ID -- nothing else).
* **Direct from SAP** -- one row per OAR reservation item no plan owns: the
  reserved quantity against the goods issued on it.

"Issued" is the goods issue on the reservation (MSEG 201/261, net of 202/262)
-- never a goods receipt: an OAR reservation is filled from stock already on
the shelf. A reservation a plan owns appears on that plan's row only, so no
issue is counted twice. A deleted reservation item with nothing issued is not
a live demand and is left out, as ``plan_outcomes`` leaves it out.

Both sources are read the same way: "required" is what was asked for (a
plan's planned quantity, a reservation's reserved quantity) and "issued" is
what was used, whichever the source. Four statuses, by issue alone:

* **Awaiting reservation** -- a captured plan no reservation stands for yet:
  there is nothing it could have been issued on.
* **Not issued** -- nothing issued yet.
* **Partially issued** -- something issued, less than required.
* **Fully issued** -- issued equal to or more than required (the excess is
  in the variance).

**Overdue** is not a status but a marker, and only on a captured plan: the
consumption plan is "Planned Consumption Overdue" -- its window has ended and
nothing was issued, the same rule that raises the PLAN_BREACH exception
(``act.detection.detect_plan_breach``). A reservation raised directly in SAP has
no consumption plan, so it is never overdue: those are covered by the no-plan
and 30-day exceptions instead. It is worked out when the row is read
(:func:`is_overdue`), against the served version's reference date.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from app.initiatives.i13.plans import (
    ConsumptionPlan,
    PlanMatcher,
    PlanSource,
    plan_outcomes,
)
from app.shared.material_scope import MaterialScope


class RowSource(str, Enum):
    ASSISTANT = "ASSISTANT"
    SAP = "SAP"


class RowStatus(str, Enum):
    NO_RESERVATION = "NO_RESERVATION"
    """Awaiting reservation -- a captured plan only."""
    NOT_ISSUED = "NOT_ISSUED"
    PARTIALLY_ISSUED = "PARTIALLY_ISSUED"
    FULLY_ISSUED = "FULLY_ISSUED"


#: The statuses before layout 8 (2 Nov - 18 Nov 2026), read from a version
#: stored before the rebuild. Below plan splits on the issued quantity.
_LEGACY_STATUS = {"NOT_DUE", "ON_PLAN", "BELOW_PLAN", "ABOVE_PLAN"}


def issue_status(required: Decimal, issued: Decimal) -> RowStatus:
    """Not, partially or fully issued against what was required."""
    if issued <= 0:
        return RowStatus.NOT_ISSUED
    if issued < required:
        return RowStatus.PARTIALLY_ISSUED
    return RowStatus.FULLY_ISSUED


@dataclass(frozen=True)
class PlanVsIssuedRow:
    row_id: str
    source: RowSource
    material: str
    plant: str
    oar: bool
    reservations: tuple[str, ...]
    """``number/item``, sorted. A plan's is empty until a reservation stands for it."""
    session_id: str | None
    """A plan's session. A SAP row's is looked up when it is read (the
    session links change between builds), so it is None here."""
    plan_id: str | None
    planned_quantity: Decimal
    """A plan's planned quantity, or a reservation's reserved quantity."""
    issued_quantity: Decimal
    status: RowStatus
    variance_quantity: Decimal | None
    """None while awaiting a reservation: nothing to vary yet."""
    variance_percentage: Decimal | None
    """Also None when nothing was planned (no percentage of zero)."""
    due_date: date | None
    """When the quantity is wanted by: a plan's window end (else its start), a
    reservation's requirement date."""
    window_start: date | None = None


def compare(
    planned: Decimal, issued: Decimal, *, has_reservation: bool = True
) -> tuple[RowStatus, Decimal | None, Decimal | None]:
    """Status, variance (issued - required) and its percentage of required.
    Exact: no tolerance band is configured anywhere."""
    if not has_reservation:
        return RowStatus.NO_RESERVATION, None, None
    variance = issued - planned
    return issue_status(planned, issued), variance, (variance / planned * Decimal("100")) if planned > 0 else None


def is_overdue(row: PlanVsIssuedRow, as_of: date) -> bool:
    """Planned Consumption Overdue: a captured plan, nothing issued on its
    reservations, and its window ended before ``as_of``. Never a SAP row."""
    return (
        row.source is RowSource.ASSISTANT
        and row.status is RowStatus.NOT_ISSUED
        and row.due_date is not None
        and row.due_date < as_of
    )


def plan_row(
    plan: ConsumptionPlan, reservations: tuple[tuple[str, str], ...], issued: Decimal, *, as_of: date, oar: bool
) -> PlanVsIssuedRow:
    planned = plan.planned_quantity
    due = plan.window_end or plan.planned_use_date
    status, variance, percentage = compare(planned, issued, has_reservation=bool(reservations))
    return PlanVsIssuedRow(
        row_id=f"PLAN-{plan.plan_id}",
        source=RowSource.ASSISTANT,
        material=plan.material,
        plant=plan.plant,
        oar=oar,
        reservations=tuple(f"{n}/{i}" for n, i in reservations),
        session_id=plan.session_id,
        plan_id=plan.plan_id,
        planned_quantity=planned,
        issued_quantity=issued,
        status=status,
        variance_quantity=variance,
        variance_percentage=percentage,
        due_date=due,
        window_start=plan.planned_use_date,
    )


def reservation_row(entry, *, as_of: date) -> PlanVsIssuedRow | None:
    """None for an item with nothing reserved and nothing issued."""
    reserved, issued = entry.reservation_quantity, entry.issued_quantity
    if reserved <= 0 and issued <= 0:
        return None
    status, variance, percentage = compare(reserved, issued)
    return PlanVsIssuedRow(
        row_id=f"RES-{entry.reservation_number}-{entry.reservation_item}",
        source=RowSource.SAP,
        material=entry.material,
        plant=entry.plant,
        oar=True,
        reservations=(f"{entry.reservation_number}/{entry.reservation_item}",),
        session_id=None,
        plan_id=None,
        planned_quantity=reserved,
        issued_quantity=issued,
        status=status,
        variance_quantity=variance,
        variance_percentage=percentage,
        due_date=entry.requirement_date,
    )


def plan_vs_issued_rows(
    plans: Iterable[ConsumptionPlan], ledger_entries, *, as_of: date, is_oar=lambda material, plant: True
) -> Iterator[PlanVsIssuedRow]:
    """Every captured plan in ``plans`` and every OAR reservation item in
    ``ledger_entries`` no captured plan owns. Reference plans are ignored: they
    are generated data, not something a requester planned."""
    captured = [p for p in plans if p.source is PlanSource.CAPTURED]
    entries = list(ledger_entries)
    for outcome in plan_outcomes(captured, entries):
        yield plan_row(
            outcome.plan, outcome.reservations, outcome.issued_quantity,
            as_of=as_of, oar=is_oar(outcome.plan.material, outcome.plan.plant),
        )
    matcher = PlanMatcher(captured, entries)
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry.reservation_number, entry.reservation_item)
        # A reservation whose PR split across PO lines has one entry per line,
        # each carrying the item's whole issued quantity: take it once.
        if key in seen:
            continue
        seen.add(key)
        if entry.material_scope is not MaterialScope.OAR:
            continue
        if entry.reservation_deleted and entry.issued_quantity <= 0:
            continue
        if matcher.plan_for(entry) is not None:
            continue
        row = reservation_row(entry, as_of=as_of)
        if row is not None:
            yield row


# --- stored form ------------------------------------------------------------------
# Plain JSON beside the filter columns (``kinds.planvs``), not a codec type: a
# new codec type changes the schema signature, which would make the version
# already served unreadable the moment this deploys.


def _num(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _day(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def to_payload(row: PlanVsIssuedRow) -> dict[str, Any]:
    return {
        "id": row.row_id, "src": row.source.value, "m": row.material, "p": row.plant, "oar": row.oar,
        "res": list(row.reservations), "sid": row.session_id, "pid": row.plan_id,
        "pq": _num(row.planned_quantity), "iq": _num(row.issued_quantity), "st": row.status.value,
        "vq": _num(row.variance_quantity), "vp": _num(row.variance_percentage),
        "due": _day(row.due_date), "ws": _day(row.window_start),
    }


def from_payload(d: dict[str, Any]) -> PlanVsIssuedRow:
    def num(v):
        return None if v is None else Decimal(v)

    def day(v):
        return None if v is None else date.fromisoformat(v)

    status = d["st"]
    if status in _LEGACY_STATUS:
        status = issue_status(Decimal(d["pq"]), Decimal(d["iq"])).value
    return PlanVsIssuedRow(
        row_id=d["id"], source=RowSource(d["src"]), material=d["m"], plant=d["p"], oar=d["oar"],
        reservations=tuple(d["res"]), session_id=d["sid"], plan_id=d["pid"],
        planned_quantity=Decimal(d["pq"]), issued_quantity=Decimal(d["iq"]), status=RowStatus(status),
        variance_quantity=num(d["vq"]), variance_percentage=num(d["vp"]),
        due_date=day(d["due"]), window_start=day(d["ws"]),
    )
