"""Who an Entra object ID belongs to -- for display only.

Anything a signed-in user writes records their Entra object ID (36 characters)
as its author: it never changes, and it fits the 40-character author columns
that an e-mail address can overflow. This table turns that ID back into a name
and e-mail for screens and reports.

Insert-only, like every table this platform owns: a row is added the first time
a user is seen and again only when their name or e-mail changes, so the latest
row per ``oid`` is the current one and the older rows are the history.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    oid: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
