"""Unified SAP gateway facade.

One shared client, not one per initiative (see
``docs/repository-structure.md``): ``SapGateway`` is what I13 (and later I07/
I08) should depend on, routing each entity set to whichever sub-gateway
actually serves it -- ``LiveSapGateway`` for the usable live sets,
``ReducedMockSapGateway`` for the three that are empty/unusable today.
"""

from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings
from app.integrations.sap.live_gateway import LiveSapGateway
from app.integrations.sap.mock_gateway import ReducedMockSapGateway
from app.integrations.sap.source_mode import DataSourceStatus, SapResult


class SapGateway:
    """Facade exposing every SAP entity set I13 needs behind one interface."""

    def __init__(self, data_dir: Path) -> None:
        self.live = LiveSapGateway(data_dir)
        self.mock = ReducedMockSapGateway(data_dir)
        self._postgres_reservations = None  # built lazily; see get_reservations()

    # --- Live entity sets ---
    def get_purchase_requisitions(self) -> SapResult:
        return self.live.get_purchase_requisitions()

    def get_purchase_orders(self) -> SapResult:
        return self.live.get_purchase_orders()

    def get_purchase_order_items(self) -> SapResult:
        return self.live.get_purchase_order_items()

    def get_goods_movements(self) -> SapResult:
        return self.live.get_goods_movements()

    def get_material_plants(self) -> SapResult:
        return self.live.get_material_plants()

    def get_storage_location_stock(self) -> SapResult:
        return self.live.get_storage_location_stock()

    # --- Mocked entity sets ---
    def get_reservations(self) -> SapResult:
        """W6.2 reservation source, switched by ``Settings.i13_reservation_source``.

        Default ("mock") keeps today's synthetic ReservationItemSet.csv, tied
        to the same synthetic PR/PO/GR/GI dataset the rest of this gateway
        reads -- so the full Reservation -> PR -> PO -> GR -> GI chain stays
        demonstrable end to end. Setting it to "postgres" swaps in the real
        RESB rows already extracted into ``raw_resb`` (see
        ``postgres_reservation.py``) without any change to the ledger,
        aging, or API code above this gateway -- the swap this config exists
        to prove (implementation plan §11 / W6.2 DoD).
        """
        if get_settings().i13_reservation_source == "postgres":
            return self._get_postgres_reservation_provider().get_reservations()
        return self.mock.get_reservations()

    def _get_postgres_reservation_provider(self):
        """Built on first use -- constructing ``SapGateway`` must never touch
        the database when the reservation source is left at its "mock" default."""
        if self._postgres_reservations is None:
            from app.core.db import get_sessionmaker
            from app.integrations.sap.postgres_reservation import PostgresReservationProvider

            self._postgres_reservations = PostgresReservationProvider(get_sessionmaker())
        return self._postgres_reservations

    def get_material_valuation(self) -> SapResult:
        return self.mock.get_material_valuation()

    def get_monthly_movement_statistics(self) -> SapResult:
        return self.mock.get_monthly_movement_statistics()

    def data_source_statuses(self) -> list[DataSourceStatus]:
        """Diagnostics for every entity set this gateway knows about."""
        results = [
            self.get_purchase_requisitions(),
            self.get_purchase_orders(),
            self.get_purchase_order_items(),
            self.get_goods_movements(),
            self.get_material_plants(),
            self.get_storage_location_stock(),
            self.get_reservations(),
            self.get_material_valuation(),
            self.get_monthly_movement_statistics(),
        ]
        return [result.status for result in results]


@lru_cache
def get_sap_gateway() -> SapGateway:
    """FastAPI dependency: the process-wide SAP gateway instance."""
    settings = get_settings()
    return SapGateway(Path(settings.i13_data_dir))
