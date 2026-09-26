"""W6.6 ``CrossPlantStockProvider`` adapter -- informational-only cross-plant
stock visibility attached to an exception (FRS requirement; never a
transfer, a new reservation at another plant, or an SAP posting).

Reuses ``PostgresMovementRepository.get_current_stock`` (the same current-
stock read W3.5/W6.3 already use) unfiltered by plant, then excludes the
exception's own plant -- no new stock query or formula is introduced here.
"""

from __future__ import annotations

from app.initiatives.i13.act.domain import CrossPlantStockInfo
from app.integrations.sap.postgres_movements import PostgresMovementRepository


class PostgresCrossPlantStockProvider:
    def __init__(self, movement_repository: PostgresMovementRepository) -> None:
        self._movement_repository = movement_repository

    def get_other_plant_stock(self, *, material: str, exclude_plant: str) -> list[CrossPlantStockInfo]:
        stock = self._movement_repository.get_current_stock(material=material)
        return [
            CrossPlantStockInfo(material=mat, plant=plant, stock_on_hand=qty)
            for (mat, plant), qty in sorted(stock.items())
            if plant != exclude_plant
        ]
