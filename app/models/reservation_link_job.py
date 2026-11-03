"""One press of "Link reservation" on a request, while it runs and afterwards.

The job pulls the latest SAP data, links the request's lines to the reservations
that name them, then rebuilds the two snapshots that read reservations. It takes
minutes to hours, so it is a row, not a process variable: the API's workers share
nothing but the database, the job runs in whichever worker received the press,
and the person watching it may be answered by the other one.

Operational records only. A job READS SAP (through the delta cycle) and writes
``session_reservation_link``; this row only describes what it did.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReservationLinkJob(Base):
    __tablename__ = "reservation_link_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(32), index=True)
    # Whoever pressed the button: the Entra object id, or X-Actor-Id while sign-in is off.
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # queued | running | succeeded | failed | abandoned
    status: Mapped[str] = mapped_column(String(16), index=True)
    # The step being worked on: queue | delta | link | i13 | i08 | done
    step: Mapped[str] = mapped_column(String(16))
    # JSON list, one entry per step: key, label, status, started_at, finished_at, detail.
    steps: Mapped[str] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Moved on every pass of the runner, so a job whose worker died can be told from a slow one.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When this job's own SAP pull ran, so a later job can reuse it instead of pulling again.
    delta_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delta_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    links: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
