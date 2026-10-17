"""What the High Stockout Risk card reads about each material-plant's usage.

One row per material-plant that has at least one goods issue on or before the
anchor date. Derived data: rebuilt whole by
``python -m app.initiatives.i7.stockout_risk`` (a delete-then-insert, so it is
safe to rerun at any time), never written by anything else.

A material-plant with NO row here has never been issued as far as the movement
data goes. That is only a statement about usage where the plant's movements were
loaded at all -- see ``app.initiatives.i7.stockout_risk.counts`` for how a plant
with no rows at all is told apart from a plant whose materials simply never
moved.

Portable constructs only, per ``app/models/base.py``.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.i7_staging import QUANTITY


class StockoutRiskInput(Base):
    __tablename__ = "i7_stockout_risk_input"
    __table_args__ = (
        UniqueConstraint("sap_material_number", "sap_plant_code", name="uq_i7_stockout_risk_input_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    sap_material_number: Mapped[str] = mapped_column(String(40), index=True)
    sap_plant_code: Mapped[str] = mapped_column(String(8), index=True)

    as_of_date: Mapped[date] = mapped_column(Date)
    """The anchor the row was built with. Every row of one build carries the same
    value; the card shows it as "As of ...", so the label always matches the
    data even when the setting has been changed since."""

    last_issue_date: Mapped[date] = mapped_column(Date)
    """Posting date of the newest goods issue (201/261) on or before ``as_of_date``.
    Reversals are not netted against it. The Fast / Slow / Non-moving band is the
    gap between this and ``as_of_date``."""

    consumption_90d: Mapped[Decimal] = mapped_column(QUANTITY)
    """Net quantity issued in the ``window_days`` ending on ``as_of_date``: issues
    less reversals, never below zero. Divided by ``window_days`` it is the average
    daily consumption. Zero is a real answer (issued earlier, not in the window)."""

    window_days: Mapped[int] = mapped_column(Integer)

    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
