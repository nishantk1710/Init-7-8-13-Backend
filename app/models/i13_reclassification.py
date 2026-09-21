"""W6.5: the persisted reclassification-evidence mart -- see
``app.initiatives.i13.models.ReclassificationCandidate`` for what each
column means and ``app.initiatives.i13.reclassification`` for how it is
computed.

Same portability rule as ``i13_watch_metric_mart`` (see that module's
docstring): portable constructs only, and refresh
(``app.initiatives.i13.reclassification_mart``) is a delete-then-insert,
never an ``ON CONFLICT``/``MERGE`` upsert.

Grain is (material, plant) -- the same OAR-scope/criticality grain W6.2 and
W3.4 already use (see ``ReclassificationCandidate``'s docstring for why).
``candidate_reasons`` is stored as a comma-joined string of
``ReclassificationReason`` codes rather than an ``ARRAY``/``JSONB`` column,
for the same cross-dialect (Postgres/Azure SQL) reason.
"""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReclassificationCandidateMart(Base):
    __tablename__ = "i13_reclassification"

    material: Mapped[str] = mapped_column(String(40), primary_key=True)
    plant: Mapped[str] = mapped_column(String(10), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date)

    consumption_count_12m: Mapped[int] = mapped_column(Integer)
    consumption_threshold: Mapped[int] = mapped_column(Integer)
    consumed_more_than_threshold: Mapped[bool] = mapped_column(Boolean, index=True)

    critical_impact_indicator: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    hod_justified_request_indicator: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    data_available: Mapped[bool] = mapped_column(Boolean, index=True)
    candidate_flag: Mapped[bool] = mapped_column(Boolean, index=True)
    candidate_reasons: Mapped[str] = mapped_column(String(120))

    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return (
            f"<ReclassificationCandidateMart {self.material}/{self.plant} "
            f"candidate={self.candidate_flag} data_available={self.data_available}>"
        )
