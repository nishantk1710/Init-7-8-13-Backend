"""W6.4: persisted consumption/ownership attribution for the W6.2
reservation ledger -- see ``app.initiatives.i13.consumption_attribution``
for how each row is resolved and ``app.initiatives.i13.models
.ConsumptionAttribution`` for what each field means.

Same portability rule as ``i13_watch_metric_mart`` (see that module's
docstring): portable constructs only, and refresh
(``app.initiatives.i13.consumption_attribution_mart``) is a delete-then-
insert, never an ``ON CONFLICT``/``MERGE`` upsert.

Grain is ``ledger_id`` -- one row per W6.2 ``ReservationLedgerEntry``, the
stable link back to it (never material+plant: the same material can carry
multiple independent reservations, see ``reservation_ledger.py``).
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ConsumptionAttributionRecord(Base):
    __tablename__ = "i13_consumption_attribution"

    ledger_id: Mapped[str] = mapped_column(String(80), primary_key=True)

    material: Mapped[str] = mapped_column(String(40), index=True)
    plant: Mapped[str] = mapped_column(String(10), index=True)

    reservation_number: Mapped[str] = mapped_column(String(20), index=True)
    reservation_item: Mapped[str] = mapped_column(String(10))

    requester_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    order_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cost_centre: Mapped[str | None] = mapped_column(String(20), nullable=True)

    attribution_status: Mapped[str] = mapped_column(String(24), index=True)
    attribution_source: Mapped[str] = mapped_column(String(24))
    evidence: Mapped[str] = mapped_column(String(400))

    cost_centre_attribution_enabled: Mapped[bool] = mapped_column(Boolean)

    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return (
            f"<ConsumptionAttributionRecord {self.ledger_id} status={self.attribution_status} "
            f"source={self.attribution_source}>"
        )
