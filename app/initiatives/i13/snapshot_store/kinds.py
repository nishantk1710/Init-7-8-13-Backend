"""What each kind of stored item is, and what its filter columns hold.

Every ``i13_snapshot_record`` row is one item of one kind. ``payload`` is the
item (``codec``); the other columns are only there to be filtered, sorted and
paged on in SQL, and mean something different per kind:

=========== ============================ ===================================================
kind        item                         columns
=========== ============================ ===================================================
watch       WatchMetric                  f1 aging band, f2 acquired-vs-plan, f3 GRNI Y/N
movement    MovementMetrics              f1 aging band
oarkey      (none)                       one per OAR material-plant; f1 its aging band
rledger     ReservationLedgerEntry       rec_key ledger id, f1 lifecycle, f2 reservation,
                                         f3 PR, d1 requirement date, text1 SGTXT
lledger     LegacyLedgerEntry            rec_key ledger id
chain       PartialLedgerEntry           rec_key ledger id, f1 lifecycle, f2 PR, f3 PO
attrib      ConsumptionAttribution       rec_key ledger id, f2 reservation, f3 PR
reclass     ReclassificationCandidate    f1 candidate Y/N
usage       monthly series + stock       f1 WATCH aging band, f2 material scope, n1 issued
grni        GrniEntry                    rec_key ledger id, n1 days since GR
exception   ExceptionQueueItem           rec_key id, f1 type, f2 status, n1 type rank
=========== ============================ ===================================================

``oar`` is the item's material-plant classified OAR, for every kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.initiatives.i13.ledger_compat import LegacyLedgerEntry
from app.initiatives.i13.models import (
    ConsumptionAttribution,
    ExceptionQueueItem,
    ExceptionType,
    MovementMetrics,
    PartialLedgerEntry,
    ReclassificationCandidate,
    ReservationLedgerEntry,
    WatchMetric,
)
from app.initiatives.i13.snapshot_store import codec

WATCH = "watch"
MOVEMENT = "movement"
OAR_KEY = "oarkey"
RLEDGER = "rledger"
LLEDGER = "lledger"
CHAIN = "chain"
ATTRIB = "attrib"
RECLASS = "reclass"
USAGE = "usage"
GRNI = "grni"
EXCEPTION = "exception"

#: Every dataclass a payload can hold -- what the schema signature covers.
STORED_TYPES = (
    WatchMetric,
    MovementMetrics,
    ReservationLedgerEntry,
    LegacyLedgerEntry,
    PartialLedgerEntry,
    ConsumptionAttribution,
    ReclassificationCandidate,
    ExceptionQueueItem,
)

#: The legacy queue's order: plan breaches, then no-plan, then GRNI.
EXCEPTION_RANK = {
    ExceptionType.PLAN_BREACH: 0,
    ExceptionType.NO_PLAN: 1,
    ExceptionType.GR_NOT_ISSUED_30_DAY: 2,
}


def signature() -> str:
    return codec.schema_signature(STORED_TYPES)


@dataclass
class Record:
    """One row of ``i13_snapshot_record``, before ``version``/``seq``."""

    kind: str
    material: str
    plant: str
    oar: bool
    payload: str
    rec_key: str | None = None
    f1: str | None = None
    f2: str | None = None
    f3: str | None = None
    n1: Decimal | None = None
    d1: date | None = None
    text1: str | None = None


def _yn(flag: bool) -> str:
    return "Y" if flag else "N"


def _clip(value: str | None, length: int) -> str | None:
    return value[:length] if value else value


def _p(item: Any) -> str:
    return codec.dumps(codec.encode(item))


def watch(m: WatchMetric, oar: bool) -> Record:
    return Record(
        WATCH, m.material, m.plant, oar, _p(m),
        f1=m.aging_band.value, f2=m.acquired_vs_plan_status.value, f3=_yn(m.gr_not_issued_flag),
    )


def movement(m: MovementMetrics, oar: bool) -> Record:
    return Record(MOVEMENT, m.material, m.plant, oar, _p(m), f1=m.aging_band.value)


def oar_key(material: str, plant: str, band: str) -> Record:
    return Record(OAR_KEY, material, plant, True, "[]", f1=band)


def rledger(e: ReservationLedgerEntry, sgtxt: str | None) -> Record:
    return Record(
        RLEDGER, e.material, e.plant, e.material_scope.value == "OAR", _p(e),
        rec_key=e.ledger_id, f1=e.lifecycle_status.value, f2=e.reservation_number, f3=e.pr_number,
        d1=e.requirement_date, text1=_clip(sgtxt, 400),
    )


def lledger(e: LegacyLedgerEntry, oar: bool) -> Record:
    return Record(LLEDGER, e.material, e.plant, oar, _p(e), rec_key=e.ledger_id)


def chain(e: PartialLedgerEntry, oar: bool) -> Record:
    return Record(
        CHAIN, e.material, e.plant, oar, _p(e),
        rec_key=e.ledger_id, f1=e.lifecycle_status.value, f2=e.pr_number, f3=e.po_number,
    )


def attrib(a: ConsumptionAttribution, oar: bool, pr_number: str | None) -> Record:
    return Record(
        ATTRIB, a.material, a.plant, oar, _p(a),
        rec_key=a.ledger_id, f2=a.reservation_number, f3=pr_number,
    )


def reclass(c: ReclassificationCandidate, oar: bool) -> Record:
    return Record(RECLASS, c.material, c.plant, oar, _p(c), f1=_yn(c.candidate_flag))


def usage(
    material: str, plant: str, scope: str, series, stock: Decimal | None, band: str | None
) -> Record:
    """``series``: MonthlyConsumption items, oldest month first."""
    total = sum((m.issued_quantity for m in series), Decimal(0))
    payload = {
        "s": [[m.month, str(m.issued_quantity), m.issue_count, str(m.received_quantity)] for m in series],
        "st": None if stock is None else str(stock),
    }
    return Record(USAGE, material, plant, scope == "OAR", codec.dumps(payload), f1=band, f2=scope, n1=total)


def grni(entry: ReservationLedgerEntry, outstanding: Decimal, days: int) -> Record:
    payload = codec.dumps([codec.encode(entry), str(outstanding), days])
    return Record(
        GRNI, entry.material, entry.plant, entry.material_scope.value == "OAR", payload,
        rec_key=entry.ledger_id, n1=Decimal(days),
    )


def exception(item: ExceptionQueueItem, oar: bool) -> Record:
    return Record(
        EXCEPTION, item.material, item.plant, oar, _p(item),
        rec_key=item.id, f1=item.type.value, f2=item.status.value, n1=Decimal(EXCEPTION_RANK[item.type]),
    )
