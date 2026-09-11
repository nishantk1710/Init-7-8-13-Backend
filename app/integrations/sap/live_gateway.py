"""Reader for the SAP entity sets that are live and usable in this tenant.

Backed today by the CSV replica of the live OData contracts (see
``csv_source.py`` for why); tagged ``SourceMode.LIVE`` because -- unlike
``ReservationItemSet`` / ``MaterialValuationSet`` / ``MonthlyMovementStatisticSet``
-- these entity sets genuinely have usable rows in the real SAP tenant per
``data-generator/discovery/``. Do not add entity sets here that are actually
empty/unusable live -- those belong in ``mock_gateway.py`` instead.
"""

from pathlib import Path

from app.integrations.sap.csv_source import EntitySetSchema, load_entity_set
from app.integrations.sap.source_mode import SapResult, SourceMode, make_status

_PURCHASE_REQUISITION = EntitySetSchema(
    date_fields=frozenset({"Badat", "Lfdat"}),
    decimal_fields=frozenset({"Menge", "Preis", "Peinh", "Bsmng"}),
    bool_fields=frozenset({"Ebakz"}),
)
_PURCHASE_ORDER = EntitySetSchema(date_fields=frozenset({"Aedat"}))
_PURCHASE_ORDER_ITEM = EntitySetSchema(
    decimal_fields=frozenset({"Menge", "Netpr", "Peinh", "Netwr", "Plifz"}),
    bool_fields=frozenset({"Elikz"}),
)
_GOODS_MOVEMENT_ITEM = EntitySetSchema(
    date_fields=frozenset({"BudatMkpf", "CpudtMkpf"}),
    decimal_fields=frozenset({"Dmbtr", "Menge"}),
)
_MATERIAL_PLANT = EntitySetSchema(
    decimal_fields=frozenset({"Plifz", "Webaz", "Minbe", "Eisbe", "Bstmi", "Bstma", "Mabst"}),
    bool_fields=frozenset({"Lvorm"}),
)
_STORAGE_LOCATION_STOCK = EntitySetSchema(decimal_fields=frozenset({"Labst", "Insme", "Speme"}))


class LiveSapGateway:
    """Reads the live-usable SAP entity sets required by the I13 ledger."""

    def __init__(self, data_dir: Path) -> None:
        self._sap_dir = data_dir / "sap"

    def _load(self, entity_set: str, schema: EntitySetSchema) -> SapResult:
        rows = load_entity_set(self._sap_dir / f"{entity_set}.csv", schema)
        return SapResult(rows=rows, status=make_status(entity_set, SourceMode.LIVE, len(rows)))

    def get_purchase_requisitions(self) -> SapResult:
        return self._load("PurchaseRequisitionSet", _PURCHASE_REQUISITION)

    def get_purchase_orders(self) -> SapResult:
        return self._load("PurchaseOrderSet", _PURCHASE_ORDER)

    def get_purchase_order_items(self) -> SapResult:
        return self._load("PurchaseOrderItemSet", _PURCHASE_ORDER_ITEM)

    def get_goods_movements(self) -> SapResult:
        return self._load("GoodsMovementItemSet", _GOODS_MOVEMENT_ITEM)

    def get_material_plants(self) -> SapResult:
        return self._load("MaterialPlantSet", _MATERIAL_PLANT)

    def get_storage_location_stock(self) -> SapResult:
        return self._load("StorageLocationStockSet", _STORAGE_LOCATION_STOCK)
