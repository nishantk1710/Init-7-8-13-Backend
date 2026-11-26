"""Test helpers for the Min-Max -> OAR recommendation: a fake data source and small builders.

No database. The fake source answers exactly what :class:`ReverseConversionSource` asks, and applies the
same cut the SQL does (months after the anchor are not returned).
"""

from collections import Counter
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

from app.initiatives.i7.reverse_conversion.repository import AttributeRow
from app.initiatives.i7.reverse_conversion.types import MonthlyMovement

ANCHOR = date(2026, 10, 1)


def attr(
    material: str,
    plant: str = "1300",
    *,
    mrp: str | None = "VM",
    criticality: str | None = "NORMAL",
    lead_time: float | int | Decimal | None = 30,
    stock: float | int | Decimal | None = None,
    price: float | int | Decimal | None = None,
    minbe: float | None = 3,
    mabst: float | None = 9,
    deleted: bool | None = False,
    description: str | None = "TEST PART",
    currency: str | None = "ZAR",
) -> AttributeRow:
    def dec(value):
        return None if value is None else Decimal(str(value))

    return AttributeRow(
        material=material,
        plant=plant,
        mrp_type=mrp,
        description=description,
        criticality=criticality,
        lead_time_days=dec(lead_time),
        current_stock=dec(stock),
        unit_price=dec(price),
        minbe=dec(minbe),
        mabst=dec(mabst),
        deletion_flag=deleted,
        currency=currency,
    )


def month(year: int, m: int, *, quantity: float = 5, issues: int = 1, reversals: int = 0) -> MonthlyMovement:
    return MonthlyMovement(date(year, m, 1), Decimal(str(quantity)), issues, reversals)


class FakeSource:
    """A :class:`ReverseConversionSource` over in-memory data. ``calls`` counts each method's use."""

    def __init__(
        self,
        attributes: list[AttributeRow],
        movements: dict[tuple[str, str], list[MonthlyMovement]] | None = None,
        *,
        latest_staged_month: date | None = date(2026, 11, 1),
        data_loaded_at: datetime | None = None,
        changes: dict[tuple[str, str], date] | None = None,
    ) -> None:
        self._changes = changes or {}
        self._attributes = attributes
        self._movements = movements or {}
        self._latest = latest_staged_month
        self._loaded_at = data_loaded_at
        self.calls: Counter[str] = Counter()

    def attributes(self) -> list[AttributeRow]:
        self.calls["attributes"] += 1
        return list(self._attributes)

    def consumption(self, anchor: date) -> Iterator[tuple[tuple[str, str], list[MonthlyMovement]]]:
        self.calls["consumption"] += 1
        for key, months in sorted(self._movements.items()):
            kept = [m for m in months if m.period <= anchor]
            if kept:
                yield key, kept

    def material_consumption(self, material: str, plant: str, anchor: date, first_month: date) -> list[MonthlyMovement]:
        self.calls["material_consumption"] += 1
        return [m for m in self._movements.get((material, plant), []) if first_month <= m.period <= anchor]

    def group_changes(self) -> dict[tuple[str, str], date]:
        self.calls["group_changes"] += 1
        return dict(self._changes)

    def latest_staged_month(self) -> date | None:
        self.calls["latest_staged_month"] += 1
        return self._latest

    def data_loaded_at(self) -> datetime | None:
        self.calls["data_loaded_at"] += 1
        return self._loaded_at


#: A history long enough to pass the 12-month span, ending well before the anchor.
LONG_AGO = [month(2015, 1), month(2016, 6), month(2018, 3), month(2019, 9), month(2021, 4)]


def sparse(*extra: MonthlyMovement) -> list[MonthlyMovement]:
    """An established material: a long history, then the given months."""
    return [*LONG_AGO, *extra]


def population() -> tuple[list[AttributeRow], dict[tuple[str, str], list[MonthlyMovement]]]:
    """One material-plant for every route of the decision table, at anchor Oct 2026.

    Returns the attributes and the movements, keyed by material number (all at plant 1300 unless noted).
    """
    attributes = [
        attr("REC1", stock=100, price=10),  # recommended, stock value 1000
        attr("REC2", stock=500, price=10),  # recommended, stock value 5000
        attr("REC3", stock=40, price=None),  # recommended, stock but no price
        attr("REC4", stock=0, price=10),  # recommended, zero stock
        attr("REC5", plant="1500", stock=None, price=None),  # recommended at the other plant, no stock figure
        attr("UNKCRIT", criticality=None),  # manual: unknown criticality
        attr("PROT", criticality="CRITICAL"),  # not eligible: protected
        attr("OBS", criticality="OBSOLETE"),  # obsolescence: obsolete
        attr("NEVER"),  # obsolescence: never moved (no movements)
        attr("ZERO24"),  # obsolescence: zero use in 24 months
        attr("NOHIST"),  # not eligible: insufficient history
        attr("LONGLT", lead_time=120),  # manual: lead time
        attr("NOLT", lead_time=None),  # manual: lead time unknown
        attr("LAPSED"),  # manual: lapsed
        attr("FREQ"),  # not eligible: frequent
        attr("BORDER"),  # manual: borderline frequency
        attr("STALE"),  # manual: stale last movement
        attr("INCONS"),  # manual: inconsistent movement data
        attr("DELETED", deleted=True),  # excluded
    ]
    movements = {
        ("REC1", "1300"): sparse(month(2026, 3), month(2026, 8)),
        ("REC2", "1300"): sparse(month(2026, 5), month(2026, 9)),
        ("REC3", "1300"): sparse(month(2026, 4), month(2026, 7)),
        ("REC4", "1300"): sparse(month(2026, 2), month(2026, 6)),
        ("REC5", "1500"): sparse(month(2026, 1), month(2026, 8)),
        ("UNKCRIT", "1300"): sparse(month(2026, 5)),
        ("PROT", "1300"): sparse(month(2026, 5)),
        ("OBS", "1300"): sparse(month(2026, 5)),
        ("ZERO24", "1300"): [month(2015, 1), month(2019, 4), month(2023, 2)],
        ("NOHIST", "1300"): [month(2026, 8)],
        ("LONGLT", "1300"): sparse(month(2026, 5)),
        ("NOLT", "1300"): sparse(month(2026, 5)),
        ("LAPSED", "1300"): sparse(month(2025, 4)),
        ("FREQ", "1300"): sparse(*[month(2026, m) for m in (1, 3, 5, 7, 9)]),
        ("BORDER", "1300"): sparse(month(2026, 2), month(2026, 5), month(2026, 8)),
        ("STALE", "1300"): sparse(month(2025, 12)),
        # Issues and reversals cancel in the count, but the quantity netted positive: the two disagree.
        ("INCONS", "1300"): sparse(month(2026, 5, quantity=3, issues=1, reversals=1)),
        ("DELETED", "1300"): sparse(month(2026, 5)),
    }
    return attributes, movements
