"""I07 canonical staging tables.

The layer between the immutable ``raw_*`` extract and the Phase 3 feature store:

    raw_*  ->  extract adapter  ->  THESE TABLES  ->  feature store

Raw stays exactly as loaded. Nothing here writes back to it.

**Every table has a natural key with a unique constraint.** That is what makes
re-running the adapter idempotent: the second run collides on the key rather
than appending a duplicate set. An application-level "does it exist?" check
would race with a concurrent run; a database constraint cannot.

**Portable constructs only**, per ``app/models/base.py``: no ``JSONB``, no
``ARRAY``. Quantities are ``Numeric``, never ``Float`` -- binary floating point
cannot hold 0.1 exactly, and these feed money and stock arithmetic.

Provenance is deliberately small: source table, source record identity, and the
staging run. Enough to answer "where did this value come from?" without building
a metadata framework nobody asked for.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# SAP quantities: 13 integer digits and 3 decimals is the widest MENGE.
QUANTITY = Numeric(18, 3)


class StagingRun(Base):
    """One execution of the extract adapter.

    Every staged row points at the run that produced it, so "which run staged
    this, from which source, under which movement-type policy?" is answerable.
    """

    __tablename__ = "i7_staging_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    source: Mapped[str] = mapped_column(String(64), index=True)
    """``"july_extract"`` today; ``"odata"`` in Phase 12."""

    status: Mapped[str] = mapped_column(String(32), index=True)

    # What the adapter actually did -- counts, not a log.
    materials_staged: Mapped[int] = mapped_column(Integer, default=0)
    material_plants_staged: Mapped[int] = mapped_column(Integer, default=0)
    stock_staged: Mapped[int] = mapped_column(Integer, default=0)
    consumption_staged: Mapped[int] = mapped_column(Integer, default=0)
    purchase_orders_staged: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)

    # The movement-type set used, recorded because it is unconfirmed: a later
    # correction needs to know which rows were built under which definition.
    consumption_movement_types: Mapped[str | None] = mapped_column(String(255), nullable=True)

    source_fingerprint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """Which raw-layer load this run read, as ``app.shared.source_state`` builds
    it. Answers "which ingestion refresh produced these rows?" -- the question
    ``source`` alone cannot, because it names the vocabulary and not the load.

    Nullable: a run staged before this column existed, or one started by hand
    with no fingerprint resolved, genuinely does not know. A wrong value would
    be worse than an absent one.
    """

    snapshot_complete: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0")
    )
    """Whether this run read a COMPLETE snapshot of its sources.

    The deactivation sweep is gated on this and nothing else. A CSV full pull
    replaces ``raw_<table>`` whole (``app.ingest.csv_load``: "Replace, not
    merge"), so absence from it is evidence of deletion. An OData delta merges
    into what is already there, so absence from it means only "unchanged" --
    sweeping on one would deactivate the entire catalogue bar the few rows that
    happened to move. Defaults False: not knowing must never sweep.
    """

    deactivated: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    """Rows this run marked inactive. Zero on a delta run, always.

    Server default as well as a Python one, matching the migration: the column
    is NOT NULL, and an INSERT that does not mention it (any path not going
    through this mapper) would otherwise be refused rather than defaulted."""

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<StagingRun {self.id} source={self.source} status={self.status}>"


class StagedMaterial(Base):
    """Client-level material attributes. One row per SAP material number."""

    __tablename__ = "i7_staged_material"
    __table_args__ = (
        UniqueConstraint("sap_material_number", name="uq_i7_staged_material_sap_material_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    sap_material_number: Mapped[str] = mapped_column(String(40), index=True)

    # Unresolved: no exposed SAP field maps SAP numbers to app identities.
    # Present so the mapping can land later without a schema change.
    app_material_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    material_group: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    base_unit_of_measure: Mapped[str | None] = mapped_column(String(8), nullable=True)

    material_status: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    """MSTAE. An input to the OAR rule -- not evaluated here."""

    external_material_group: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """EXTWG. Retired as an OAR identifier; staged for provenance only."""

    manufacturer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deletion_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    criticality: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    """ZMM065 tier. Reaches ~24% of the MARC population."""

    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    source_table: Mapped[str] = mapped_column(String(64))
    staging_run_id: Mapped[int] = mapped_column(Integer, index=True)


class StagedMaterialPlant(Base):
    """Plant-level attributes. The grain the OAR rule is evaluated at."""

    __tablename__ = "i7_staged_material_plant"
    __table_args__ = (
        UniqueConstraint(
            "sap_material_number", "sap_plant_code", name="uq_i7_staged_material_plant_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    sap_material_number: Mapped[str] = mapped_column(String(40), index=True)
    sap_plant_code: Mapped[str] = mapped_column(String(8), index=True)
    app_plant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    mrp_type: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    """DISMM. Blank means "not maintained" -- unknown, not "not OAR"."""

    planned_delivery_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    current_safety_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    current_reorder_point: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    current_maximum_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)

    deletion_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    """False when a COMPLETE snapshot no longer carried this material-plant.

    Distinct from ``deletion_flag``, which is SAP's own LVORM -- a statement SAP
    made. This is a statement *we* made, by observing that a full extract that
    should have contained the row did not. Keeping them apart matters: LVORM
    coming back means SAP un-flagged it; this coming back means the row
    reappeared in an extract. Conflating them would lose which happened.

    Never set by a delta run -- see ``StagingRun.snapshot_complete``. Soft, so
    the row and its history survive: a material deactivated in error is one
    extract away from returning, and a hard delete would have taken its staged
    consumption with it.

    Owned by the sweep (``extract.deactivate_unseen``), which is the only thing
    that writes it in either direction -- the staged row dictionaries do not
    carry this column, so the upsert never touches it.
    """

    source_table: Mapped[str] = mapped_column(String(64))
    staging_run_id: Mapped[int] = mapped_column(Integer, index=True)
    """Which run last WROTE this row. The upsert refreshes every non-key column,
    so a row re-seen by run N carries N, and a row that has vanished keeps the
    older id. That difference is exactly what the sweep reads."""


class StagedStock(Base):
    """Plant/storage-location stock position, from MARD.

    Grain is material-plant-storage-location: MARD itself carries stock per
    storage location, and a material-plant can have several. The feature store
    aggregates this to material-plant, which is the grain every other I07
    feature uses -- but that aggregation is a Phase 3 decision, not staged in.

    **Only ``unrestricted_use_stock`` is populated as the assumed "available
    stock" figure** (LABST -- the standard SAP definition of freely usable
    stock, the same population MB52 reports use). No document in this
    repository defines a stock-position formula; this is a documented
    assumption, not a discovered rule. The other MARD stock categories are
    staged alongside it so the assumption stays visible and reversible rather
    than silently baked into one merged number.
    """

    __tablename__ = "i7_staged_stock"
    __table_args__ = (
        UniqueConstraint(
            "sap_material_number",
            "sap_plant_code",
            "storage_location",
            name="uq_i7_staged_stock_key",
        ),
        Index(
            "ix_i7_staged_stock_material_plant",
            "sap_material_number",
            "sap_plant_code",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    sap_material_number: Mapped[str] = mapped_column(String(40))
    sap_plant_code: Mapped[str] = mapped_column(String(8))
    storage_location: Mapped[str] = mapped_column(String(8))

    unrestricted_use_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    """LABST. The assumed primary "available stock" figure -- see class docstring."""

    quality_inspection_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    """INSME. Staged for transparency; not part of the available-stock figure."""

    blocked_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    """SPEME."""

    stock_in_transfer: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    """UMLME."""

    restricted_use_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    """EINME."""

    returns_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    """RETME."""

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    """False when a COMPLETE snapshot no longer carried this storage location.
    See the same column on :class:`StagedMaterialPlant`; MARD is swept on the
    same terms, because the feature universe is the UNION of the two and a row
    left active here would hold a deactivated material-plant in scope."""

    source_table: Mapped[str] = mapped_column(String(64))
    staging_run_id: Mapped[int] = mapped_column(Integer, index=True)


class StagedConsumption(Base):
    """Monthly consumption per material-plant.

    Aggregated to the month here rather than in Phase 3 because that is the
    grain every downstream statistic uses, and 233k movement rows collapse to
    far fewer monthly ones. Issues and reversals net within a period.

    Zero-demand months are NOT stored: a zero row is the absence of a movement,
    so materialising every month for every material-plant would be a large
    mostly-empty table. Phase 3 densifies the series over a material's own
    observed range, which is where the range is actually known.
    """

    __tablename__ = "i7_staged_consumption"
    __table_args__ = (
        UniqueConstraint(
            "sap_material_number",
            "sap_plant_code",
            "period",
            name="uq_i7_staged_consumption_key",
        ),
        # Phase 3 reads a whole series per material-plant in period order.
        Index(
            "ix_i7_staged_consumption_series",
            "sap_material_number",
            "sap_plant_code",
            "period",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    sap_material_number: Mapped[str] = mapped_column(String(40))
    sap_plant_code: Mapped[str] = mapped_column(String(8))

    period: Mapped[date] = mapped_column(Date)
    """First day of the month."""

    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    """Net issued quantity: issues minus reversals."""

    unit_of_measure: Mapped[str | None] = mapped_column(String(8), nullable=True)

    movement_count: Mapped[int] = mapped_column(Integer, default=0)
    """How many movements (issues and reversals together) contributed.
    Provenance for a surprising total -- not a consumption-event count; see
    ``issue_count``/``reversal_count`` for that."""

    issue_count: Mapped[int] = mapped_column(Integer, default=0)
    """MSEG rows whose movement type is an issue type (201/261). This is the
    transaction-level count the SOP 3.1.1 ">4 consumptions" trigger needs --
    distinct from ``movement_count`` (issues+reversals) and from the demand
    statistics' ``non_zero_periods`` (a count of non-zero *months*, not
    transactions)."""

    reversal_count: Mapped[int] = mapped_column(Integer, default=0)
    """MSEG rows whose movement type is a reversal type (202/262). Netted
    against ``issue_count`` when computing a consumption-event count, the same
    way ``quantity`` nets issues against reversals."""

    source_table: Mapped[str] = mapped_column(String(64))
    staging_run_id: Mapped[int] = mapped_column(Integer, index=True)


class StagedPurchaseOrder(Base):
    """One PO line and its goods receipt, for future lead-time analysis.

    ``lead_time_days`` is stored as the plain date difference. No filtering:
    the 1-730 day validity window and the PO-count tiers are Phase 3 policy, and
    applying them here would bake a threshold into stored data where it could
    not later be changed without re-staging.
    """

    __tablename__ = "i7_staged_purchase_order"
    __table_args__ = (
        UniqueConstraint(
            "purchasing_document", "item", name="uq_i7_staged_purchase_order_key"
        ),
        Index(
            "ix_i7_staged_purchase_order_material_plant",
            "sap_material_number",
            "sap_plant_code",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    purchasing_document: Mapped[str] = mapped_column(String(20))
    item: Mapped[str] = mapped_column(String(10))

    sap_material_number: Mapped[str] = mapped_column(String(40), index=True)
    sap_plant_code: Mapped[str] = mapped_column(String(8))

    created_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    """EKKO.AEDAT -- the confirmed interim proxy for PO release date."""

    goods_receipt_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Earliest GR posting. Null while the PO is open."""

    planned_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """EKET.EINDT."""

    planned_delivery_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    quantity_ordered: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    quantity_received: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)

    supplier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False)

    lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Unfiltered date difference. Phase 3 decides which values are usable."""

    source_table: Mapped[str] = mapped_column(String(64))
    staging_run_id: Mapped[int] = mapped_column(Integer, index=True)


class StagingRejection(Base):
    """A source record that could not be staged, and why.

    Rejections are recorded, never dropped. A silently discarded row is a
    material that vanishes from the catalogue with no evidence it was ever
    there -- and the counts here are how a data-quality conversation with the
    SAP team starts.
    """

    __tablename__ = "i7_staging_rejection"
    __table_args__ = (
        Index("ix_i7_staging_rejection_run_reason", "staging_run_id", "reason"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    staging_run_id: Mapped[int] = mapped_column(Integer, index=True)
    source_table: Mapped[str] = mapped_column(String(64), index=True)

    source_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Identity of the offending record, as far as it could be read."""

    reason: Mapped[str] = mapped_column(String(64), index=True)
    """A stable code (``missing_material``), not free prose -- these get counted."""

    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
