"""Session <-> reservation linking, via the reservation's item text (SGTXT).

The assistant issues a session ID before the SAP reservation exists; the
requester types it into the reservation's item text (``RESB.SGTXT``, loaded as
``raw_resb.text``). Two tables support reading it back:

``session_reservation_link``
    The **result**: which session each reservation item carries. Derived --
    ``app.initiatives.i13.session_link.sync_links`` re-derives it from SGTXT and
    keeps it in step (a link whose SGTXT no longer names the session is
    removed; a link that still holds keeps its ``first_seen_at``). Not
    append-only for exactly that reason: removing a UAT test reservation must
    be able to remove the link it produced.

``uat_reservation_sgtxt``
    **UAT only.** Stands in for SAP while the platform cannot write to it: each
    row either *simulates* a new reservation the requester would have created
    (``simulated=True``) or *stamps* an SGTXT value onto a real reservation from
    the extract (``simulated=False``). Read only when
    ``I13_UAT_SIMULATION_ENABLED`` is on, overlaid on ``raw_resb`` -- which is
    never modified.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SessionReservationLink(Base):
    __tablename__ = "session_reservation_link"
    __table_args__ = (
        UniqueConstraint("session_id", "reservation_number", "reservation_item", name="uq_session_reservation_link"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True)
    reservation_number: Mapped[str] = mapped_column(String(20), index=True)
    reservation_item: Mapped[str] = mapped_column(String(10))
    material: Mapped[str] = mapped_column(String(40), index=True)
    plant: Mapped[str] = mapped_column(String(10), index=True)
    #: ``SGTXT`` (the loaded extract) or ``UAT_SGTXT`` (the UAT overlay).
    source: Mapped[str] = mapped_column(String(16))
    #: The item text the session ID was read from, as found.
    sgtxt: Mapped[str | None] = mapped_column(String(60), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UatReservationSgtxt(Base):
    __tablename__ = "uat_reservation_sgtxt"
    __table_args__ = (UniqueConstraint("reservation_number", "reservation_item", name="uq_uat_reservation_sgtxt"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reservation_number: Mapped[str] = mapped_column(String(20), index=True)
    reservation_item: Mapped[str] = mapped_column(String(10))
    material: Mapped[str] = mapped_column(String(40), index=True)
    plant: Mapped[str] = mapped_column(String(10))
    #: True: a reservation that exists only here (simulating one created in SAP).
    #: False: an SGTXT value stamped onto a real reservation from raw_resb.
    simulated: Mapped[bool] = mapped_column(Boolean)
    requirement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    requirement_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 3), nullable=True)
    sgtxt: Mapped[str] = mapped_column(String(60))
    #: A stamped reservation's own item text before the stamp, for display.
    original_sgtxt: Mapped[str | None] = mapped_column(String(60), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
