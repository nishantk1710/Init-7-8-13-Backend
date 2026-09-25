"""The ``ConsumptionPlan`` records WATCH and the exception engine compare against.

Two sources, and the difference between them matters
-----------------------------------------------------
1. **``consumption_plans.csv``** -- platform-owned reference data written by the
   data generator. 742 rows, with fabricated ``SESS-000001`` identifiers. Until
   WS7 this was the *only* source, which means every plan-breach and no-plan
   exception the platform has ever raised rested on plans a generator wrote.

2. **The ``consumption_plan`` table** -- plans a real requester captured through
   the reservation assistant. These are the first plans this platform has ever
   been told rather than handed.

Both are projected into the same :class:`ConsumptionPlan` shape so that the six
modules reading plans -- WATCH, the exception rules, ACT detection, consumption
attribution and its mart -- pick up captured plans without any of them changing.

:attr:`ConsumptionPlan.source` says which one a plan came from. That is not
decoration: before anybody demos the exception queue, the difference between
"the engine works" and "these numbers are real" is exactly this field, and only
one of those statements is currently true for the CSV rows.

Reading the FRS-complete plan narrowly
---------------------------------------
A captured plan holds a planned **window** (FR-2(b)), a cost centre and an
order. This reader projects ``window_start`` into ``planned_use_date`` and drops
the rest, so detection behaves exactly as it did before WS7.

That is deliberate, and it is option 2 of the three the plan set out: **capture
everything, read narrowly, widen the reader later with its own tests.** Widening
it now would change detection behaviour -- FR-7 breaches on "window end plus
grace", which is a different date from a use date -- and that change deserves
its own migration of the ACT tests rather than arriving as a side effect of the
chat being built.

Capturing only what ACT reads today would have been faster and would have
under-captured against the FRS permanently. ``consumption_plan`` is append-only,
so those fields could never have been backfilled.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session


class PlanSource(str, Enum):
    """Where a plan came from. Kept on every plan so a demo can say which."""

    CAPTURED = "CAPTURED"
    """Recorded by a real requester through the reservation assistant."""

    REFERENCE_CSV = "REFERENCE_CSV"
    """Generated reference data. **Fabricated**, including its session IDs."""


@dataclass(frozen=True)
class ConsumptionPlan:
    plan_id: str
    session_id: str
    reservation_number: str
    reservation_item: str
    material: str
    plant: str
    requester: str
    purpose: str
    planned_quantity: Decimal
    planned_use_date: date | None
    status: str

    source: PlanSource = PlanSource.REFERENCE_CSV
    """Defaulted to the CSV so existing callers that construct one in a test
    keep working unchanged, and so a plan is never *silently* presented as real."""

    window_end: date | None = None
    """The end of a captured plan's window (FR-2(b)). ``None`` for the CSV,
    which carries a single use date only."""

    captured_on: date | None = None
    """When a captured plan was recorded. ``None`` for the CSV."""

    linked_reservations: tuple[tuple[str, str], ...] = ()
    """Reservation items whose item text (SGTXT) carries this plan's session ID
    -- ``session_reservation_link``. A plan linked this way matches those
    reservations exactly, like a reference plan does."""

    @property
    def is_fabricated(self) -> bool:
        return self.source is PlanSource.REFERENCE_CSV

    @property
    def is_unlinked_capture(self) -> bool:
        """A captured plan not yet linked to a SAP reservation.

        The normal state of every captured plan until FR-8 (blocker B2): the
        assistant runs while the reservation is being created, so there is no
        reservation number to record.
        """
        return self.source is PlanSource.CAPTURED and not self.reservation_number and not self.linked_reservations

    @property
    def breach_reference_date(self) -> date | None:
        """The date a plan breach is measured from: the window END.

        FR-7 breaches on "window end plus grace". The CSV has one date, which
        is both; a captured plan has a window, and measuring from its start
        (as the narrow read used to) raised breaches before the window had
        even closed (gap G4).
        """
        return self.window_end or self.planned_use_date

    def covers(self, entry) -> bool:
        """Whether this unlinked captured plan stands for ``entry``'s reservation.

        Until a captured plan is linked by reservation number (FR-8), it is
        matched the only way the data allows: same material and plant, and a
        reservation requirement date inside the plan's window. With only a
        window start, anything required from that day on; with no window at
        all, anything required on or after the day the plan was captured. A
        reservation with no requirement date is never matched -- there is
        nothing to place it in the window with.
        """
        if not self.is_unlinked_capture or self.status != "OPEN":
            return False
        if (entry.material, entry.plant) != (self.material, self.plant):
            return False
        required = getattr(entry, "requirement_date", None)
        if required is None:
            return False
        start = self.planned_use_date or self.captured_on
        if start is not None and required < start:
            return False
        if self.window_end is not None and required > self.window_end:
            return False
        return start is not None or self.window_end is not None


def load_reference_plans(data_dir: Path) -> list[ConsumptionPlan]:
    """The generated CSV. Fabricated, and labelled as such on every row."""
    path = data_dir / "platform" / "consumption_plans.csv"
    if not path.exists():
        return []

    plans: list[ConsumptionPlan] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            planned_use_date = (
                date.fromisoformat(row["planned_use_date"])
                if row.get("planned_use_date")
                else None
            )
            plans.append(
                ConsumptionPlan(
                    plan_id=row["plan_id"],
                    session_id=row["session_id"],
                    reservation_number=row["Rsnum"],
                    reservation_item=row["Rspos"],
                    material=row["Matnr"],
                    plant=row["Werks"],
                    requester=row["requester"],
                    purpose=row["purpose"],
                    planned_quantity=(
                        Decimal(row["planned_quantity"])
                        if row.get("planned_quantity")
                        else Decimal("0")
                    ),
                    planned_use_date=planned_use_date,
                    status=row["status"],
                    source=PlanSource.REFERENCE_CSV,
                )
            )
    return plans


def load_captured_plans(db: Session) -> list[ConsumptionPlan]:
    """Plans a requester actually gave us, through the assistant.

    ``reservation_number`` and ``reservation_item`` are empty strings rather
    than ``None``: the CSV shape has them as strings and every consumer indexes
    on them, so a None here would be a new shape for those consumers to handle.
    Empty is the honest value -- the reservation did not exist when the plan was
    captured, and linking it is FR-8 (blocker B2).
    """
    from app.assistant.models import ConsumptionPlanRecord
    from app.models.i13_session_link import SessionReservationLink

    rows = db.execute(
        select(ConsumptionPlanRecord).order_by(ConsumptionPlanRecord.captured_at)
    ).scalars()

    # Reservations that carry each session's ID in their item text (SGTXT).
    # Only for the plan's own material and plant -- the linker already
    # guarantees that, and the filter keeps it true if a link row is stale.
    linked: dict[str, list[tuple[str, str, str, str]]] = {}
    for link in db.execute(
        select(SessionReservationLink).order_by(
            SessionReservationLink.reservation_number, SessionReservationLink.reservation_item
        )
    ).scalars():
        linked.setdefault(link.session_id, []).append(
            (link.reservation_number, link.reservation_item, link.material, link.plant)
        )

    def links_for(row) -> tuple[tuple[str, str], ...]:
        return tuple(
            (number, item)
            for number, item, material, plant in linked.get(row.session_id, [])
            if (material, plant) == (row.material, row.plant)
        )

    plans = []
    for row in rows:
        links = links_for(row)
        first = links[0] if links else ("", "")
        plans.append(_captured_plan(row, links, first))
    return plans


def _captured_plan(row, links: tuple[tuple[str, str], ...], first: tuple[str, str]) -> ConsumptionPlan:
    return ConsumptionPlan(
        plan_id=row.id,
        session_id=row.session_id,
        reservation_number=row.reservation_number or first[0],
        reservation_item=row.reservation_item or first[1],
        material=row.material,
        plant=row.plant,
        requester=row.captured_by,
        purpose=row.purpose,
        planned_quantity=row.planned_quantity,
        # The window's start is still the plan's "use date" for anything
        # that reads one date. Breach timing reads `breach_reference_date`
        # (the window end), and reservation matching reads the whole
        # window -- see ConsumptionPlan.covers and PlanMatcher below.
        planned_use_date=row.window_start,
        status=row.status,
        source=PlanSource.CAPTURED,
        window_end=row.window_end,
        captured_on=row.captured_at.date() if row.captured_at else None,
        linked_reservations=links,
    )


class PlanMatcher:
    """Which plan stands for which reservation-ledger entry, and back.

    Reference plans (and any captured plan once FR-8 links it) match on
    reservation number and item, exactly as before. A captured plan with no
    reservation number used to be keyed on ``("", "")``: every one of them
    collided on that key and none could ever match a real reservation, so it
    could neither clear a NO_PLAN exception nor be satisfied by an issue (gaps
    G3/G4). Those now match through :meth:`ConsumptionPlan.covers`.

    A direct reservation match always wins over a window match.
    """

    def __init__(self, plans: list[ConsumptionPlan], ledger_entries) -> None:
        self._direct: dict[tuple[str, str], ConsumptionPlan] = {}
        for plan in plans:
            if plan.is_unlinked_capture:
                continue
            for key in plan.linked_reservations or ((plan.reservation_number, plan.reservation_item),):
                self._direct[key] = plan
        self._unlinked_by_key: dict[tuple[str, str], list[ConsumptionPlan]] = {}
        for plan in plans:
            if plan.is_unlinked_capture:
                self._unlinked_by_key.setdefault((plan.material, plan.plant), []).append(plan)

        self._entries_by_reservation: dict[tuple[str, str], list] = {}
        self._entries_by_key: dict[tuple[str, str], list] = {}
        for entry in ledger_entries:
            self._entries_by_reservation.setdefault((entry.reservation_number, entry.reservation_item), []).append(entry)
            if (entry.material, entry.plant) in self._unlinked_by_key:
                self._entries_by_key.setdefault((entry.material, entry.plant), []).append(entry)

    def plan_for(self, entry) -> ConsumptionPlan | None:
        direct = self._direct.get((entry.reservation_number, entry.reservation_item))
        if direct is not None:
            return direct
        # The most recently captured covering plan, if several overlap.
        for plan in reversed(self._unlinked_by_key.get((entry.material, entry.plant), [])):
            if plan.covers(entry):
                return plan
        return None

    def entries_for(self, plan: ConsumptionPlan) -> list:
        if plan.linked_reservations:
            return [e for key in plan.linked_reservations for e in self._entries_by_reservation.get(key, [])]
        if not plan.is_unlinked_capture:
            return self._entries_by_reservation.get((plan.reservation_number, plan.reservation_item), [])
        return [entry for entry in self._entries_by_key.get((plan.material, plan.plant), []) if plan.covers(entry)]


def load_consumption_plans(
    data_dir: Path, db: Session | None = None
) -> list[ConsumptionPlan]:
    """Every plan the platform knows about, both sources.

    ``db`` is optional so the six existing callers keep working unchanged; those
    that hold a session pass it and immediately see captured plans. A caller
    that does not pass one gets the reference data only, which is what it got
    before -- never a silent failure, just the narrower answer it asked for.

    Captured plans come **last**, so a consumer that takes the first match for a
    key prefers the reference data it was written against. Nothing does that
    today -- every consumer groups rather than picks -- but the ordering is
    stated so the day one does, it is a decision rather than an accident.
    """
    plans = load_reference_plans(data_dir)
    if db is not None:
        plans.extend(load_captured_plans(db))
    return plans
