"""Stored I07 policy versions.

The minimum Phase 1 needs: a recommendation cites ``(policy_id,
policy_version)``, so those two must resolve to the actual rules months later.
No recommendation, approval or feature table yet -- those belong to the phases
that use them.

The document is stored as serialised JSON in a ``Text`` column, not ``JSONB``.
Postgres stands in for a database that may not be Postgres, and ``JSONB`` would
turn that swap into a rewrite. I07 never queries *inside* the policy -- it loads
a version whole and hands it to Pydantic -- so the indexing ``JSONB`` would buy
is of no use here.

Immutable by convention: a policy change means a new row with a higher version,
never an update in place. A recommendation that cited version 3 must still find
version 3 unchanged.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PolicyVersion(Base):
    """One immutable version of the I07 policy document."""

    __tablename__ = "i7_policy_version"
    __table_args__ = (
        UniqueConstraint("policy_id", "policy_version", name="uq_i7_policy_version_id_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    policy_id: Mapped[str] = mapped_column(String(64), index=True)
    policy_version: Mapped[int] = mapped_column(Integer)

    # DRAFT / SIGNED / SUPERSEDED. Only SIGNED may produce recommendations.
    status: Mapped[str] = mapped_column(String(32), index=True)

    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    # The serialised PolicyDocument. Text for portability -- see module docstring.
    document_json: Mapped[str] = mapped_column(Text)

    # Who signed it off, for the audit trail. Null while DRAFT.
    signed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<PolicyVersion {self.policy_id}@v{self.policy_version} status={self.status}>"
