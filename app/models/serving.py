"""The serving layer: what the initiatives read.

Migration-managed, unlike ``odata_*``. The raw tables mirror SAP, so their
shape is SAP's decision and they are dropped and rebuilt on every pull. These
are our design -- reviewed, versioned, and changed deliberately.

    odata_material_plant  ┐
    odata_material        ├─ normalise ─> material_plant   (this file)
    odata_material_desc   ┘

One row per material and plant, which is the grain everything else hangs off:
stock, movements, reservations and purchase orders are all counted per
material per plant, and a dimension at any other grain would force every
downstream query to re-derive it.

TYPES ARE NOT COSMETIC HERE

The raw layer is entirely text on purpose. This layer is typed, and that is
where the value is: ``eisbe`` as a Numeric can be compared, summed and ordered,
where ``'              0.000'`` can only be compared to other strings -- and
compares wrongly, since ``'10'`` sorts before ``'9'``.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MaterialPlant(Base):
    """One material at one plant. MARC, widened with MARA and MAKT."""

    __tablename__ = "material_plant"

    # Zero-padded to 18 by app.normalise.matnr on the way in, so a join here is
    # a plain equality. Never store the short form in this column: half-padded
    # data is worse than none, because it joins for some rows and not others.
    matnr: Mapped[str] = mapped_column(String(18), primary_key=True)
    werks: Mapped[str] = mapped_column(String(4), primary_key=True)

    # --- From MARA (material master, plant-independent) -------------------
    mtart: Mapped[str | None] = mapped_column(String(4))
    matkl: Mapped[str | None] = mapped_column(String(9))
    meins: Mapped[str | None] = mapped_column(String(3))

    # --- From MAKT (description) ------------------------------------------
    maktx: Mapped[str | None] = mapped_column(String(40))

    # --- From MARC (plant data) -------------------------------------------
    dismm: Mapped[str | None] = mapped_column(String(2))

    # The OAR predicate, resolved once here rather than re-expressed in every
    # query that needs it. Dismm in (ND, PD) by the 08-Sep VZI ruling; the
    # value domain lives in known_conditions so a new MRP type is a question
    # rather than a silent reclassification.
    is_oar: Mapped[bool] = mapped_column(Boolean, default=False)

    # Planning figures. Numeric, not Float: these are quantities, and a
    # reorder point that reads 2.0000000000000004 is a support ticket.
    eisbe: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))  # safety stock
    minbe: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))  # reorder point
    mabst: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))  # maximum stock
    losgr: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))  # lot size

    # Planned delivery time in days. From MARC, standing in for the I11
    # Z-program output, which is not delivered -- W2.9 records that substitution.
    plifz: Mapped[int | None] = mapped_column()

    # --- Provenance --------------------------------------------------------
    #
    # Which raw pull this row was built from. Without it, "why does this figure
    # disagree with SAP" has no answer short of rebuilding and hoping.
    source_run_date: Mapped[date | None] = mapped_column(Date)
    built_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # Every OAR query filters on these two together, and the scope is the
        # first thing narrowed in each of the three initiatives.
        Index("ix_material_plant_oar", "is_oar", "werks"),
        Index("ix_material_plant_dismm", "dismm"),
    )

    def __repr__(self) -> str:
        return f"<MaterialPlant {self.matnr}/{self.werks} dismm={self.dismm}>"
