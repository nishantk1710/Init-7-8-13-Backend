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

    @property
    def is_fabricated(self) -> bool:
        return self.source is PlanSource.REFERENCE_CSV


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

    rows = db.execute(
        select(ConsumptionPlanRecord).order_by(ConsumptionPlanRecord.captured_at)
    ).scalars()

    return [
        ConsumptionPlan(
            plan_id=row.id,
            session_id=row.session_id,
            reservation_number=row.reservation_number or "",
            reservation_item=row.reservation_item or "",
            material=row.material,
            plant=row.plant,
            requester=row.captured_by,
            purpose=row.purpose,
            planned_quantity=row.planned_quantity,
            # The NARROW read: the window's start stands in for the single
            # planned use date today's detection understands. See the module
            # docstring -- widening this changes breach behaviour and belongs in
            # its own change with its own tests.
            planned_use_date=row.window_start,
            status=row.status,
            source=PlanSource.CAPTURED,
        )
        for row in rows
    ]


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
