"""Saved versions of the Overview KPI formulas.

One row per *Save & Apply*. A version holds every editable field (see
``app.initiatives.i7.kpi_formulas.definitions``), so it is complete on its own and never needs the
rows before it. Rows are never edited after they settle: a change is a new version, and "reset to
default" is a new version holding the defaults. The history is therefore the audit trail -- who changed
which formula, and when.

``status``:

* ``applying``   -- saved, and the usage data is being rebuilt for it. The Overview still uses the
  previous active version, so a half-rebuilt table is never read against the new formula.
* ``active``     -- the version the Overview, the information cards and new reports use. The newest
  ``active`` row wins; there is deliberately no unique "one active" column, because SQL Server treats
  every NULL in a unique index as equal and so cannot express it.
* ``superseded`` -- was active, replaced by a newer one.
* ``failed``     -- the rebuild did not finish; ``error`` says why. The previous version stays active.

The document is serialised JSON in a ``Text`` column, like ``i7_policy_version``: it is loaded whole and
never queried inside.

Portable constructs only, per ``app/models/base.py``.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KpiFormulaVersion(Base):
    __tablename__ = "i7_kpi_formula_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    version: Mapped[int] = mapped_column(Integer, unique=True)
    status: Mapped[str] = mapped_column(String(16), index=True)

    document_json: Mapped[str] = mapped_column(Text)
    """``{"fields": {<key>: <formula text or number>, ...}}``."""

    note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    rebuild_needed: Mapped[bool] = mapped_column(Boolean, default=False)
    """True when the usage window changed, so the usage table had to be rebuilt for this version."""
    rows_built: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<KpiFormulaVersion v{self.version} {self.status}>"
