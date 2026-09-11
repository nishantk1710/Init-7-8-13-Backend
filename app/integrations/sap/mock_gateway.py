"""Reduced mock gateway for the SAP entity sets that are empty/unusable live.

``ReservationItemSet`` (RESB), ``MaterialValuationSet`` (MBEW) and
``MonthlyMovementStatisticSet`` (S031) have no usable rows in this SAP
tenant today (S031 has zero rows outright). Everything returned from here is
tagged ``SourceMode.MOCK`` so callers and ``/api/i13/data-sources`` never
mistake it for production truth. Do not add entity sets here that already
have usable live data -- those belong in ``live_gateway.py``.
"""

from pathlib import Path

from app.integrations.sap.csv_source import EntitySetSchema, load_entity_set
from app.integrations.sap.source_mode import SapResult, SourceMode, make_status

_RESERVATION_ITEM = EntitySetSchema(
    date_fields=frozenset({"Bdter"}),
    decimal_fields=frozenset({"Bdmng", "Enmng", "Enwrt"}),
    bool_fields=frozenset({"Xloek", "Kzear"}),
)
_MATERIAL_VALUATION = EntitySetSchema(decimal_fields=frozenset({"Lbkum", "Salk3", "Verpr", "Stprs", "Peinh"}))
_MONTHLY_MOVEMENT_STATISTIC = EntitySetSchema(
    decimal_fields=frozenset({"Basme", "Mzubb", "Wzubb", "Magbb", "Wagbb", "Ambwg", "Muvbr", "Wuvbr"})
)


class ReducedMockSapGateway:
    """Mock fallback for the currently unavailable/empty SAP entity sets."""

    def __init__(self, data_dir: Path) -> None:
        self._sap_dir = data_dir / "sap"

    def _load(self, entity_set: str, schema: EntitySetSchema) -> SapResult:
        rows = load_entity_set(self._sap_dir / f"{entity_set}.csv", schema)
        return SapResult(rows=rows, status=make_status(entity_set, SourceMode.MOCK, len(rows)))

    def get_reservations(self) -> SapResult:
        return self._load("ReservationItemSet", _RESERVATION_ITEM)

    def get_material_valuation(self) -> SapResult:
        return self._load("MaterialValuationSet", _MATERIAL_VALUATION)

    def get_monthly_movement_statistics(self) -> SapResult:
        """S031 -- legitimately empty. Never consumed by aging/WATCH; exposed
        only so ``/api/i13/data-sources`` can report its true (unavailable)
        state instead of hiding it."""
        return self._load("MonthlyMovementStatisticSet", _MONTHLY_MOVEMENT_STATISTIC)
