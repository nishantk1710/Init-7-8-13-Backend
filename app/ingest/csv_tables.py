"""Which SAP tables the CSV extract pulls, and with what window.

The CSV route is ``ZMM_GET_CSV_SRV/TableExtractSet``: a request, not a row. It
is keyed

    RequestId ; TabName ; FromDate ; ToDate ; IsDelta ; MaxRows

and answers ``/$value`` with an acknowledgement, not data -- SAP then pushes
the rows to ``/api/events/csv`` in chunks of 50,000. So this module describes
what to ask for; ``csv_pull`` asks, ``csv_upload`` receives, ``csv_load`` files.

WHY EVERY TABLE GETS AN EXPLICIT WINDOW

Every request we have fired with ``FromDate=''`` and ``ToDate=''`` delivered
nothing -- ack returned, chunks never arrived. Every request with real dates
delivered. We have not isolated whether the blank dates or a reused RequestId
caused that, so nothing here sends a blank date: master data gets a window wide
enough to mean "everything" instead.

Transaction tables get three years, per the 25-Sep instruction. Master data
gets the wide window, because "the last three years of MARA" is not a
meaningful request -- a material created in 2009 and still stocked today has to
be in the dimension.

WHAT THE $count IS FOR

Chunks carry no request id, no sequence number and no total, so the only
completeness evidence available is Interface 1's own ``$count`` for the same
data. That comparison is exact only when the CSV window covers the whole table:

* ``Reconcile.EXACT``   -- wide window, received rows must EQUAL the count.
* ``Reconcile.BOUNDED`` -- three-year window, so the extract is a subset. The
  count is an upper bound: received must be above zero and no greater than it.

BOUNDED is genuinely weaker, and it is not weaker by choice. A count with a
matching date filter would be the right answer, but MKPF's ``Budat`` filter is
IGNORED by this service (see manifest.DELTAS) -- asking for one would return
the whole table and turn a loose check into a confidently wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

# Three years, per the 25-Sep instruction. Kept as a constant because the
# figure is a decision, not arithmetic, and the next person will want to find
# it rather than infer it.
TRANSACTION_WINDOW_YEARS = 3

# "Everything" for master data. Not a blank date -- see the module docstring.
WIDE_WINDOW_START = "19000101"

# Far enough ahead that a document posted with a future date still lands.
WIDE_WINDOW_FORWARD_DAYS = 365

DATE_FORMAT = "%Y%m%d"


class Reconcile(str, Enum):
    EXACT = "exact"
    BOUNDED = "bounded"


@dataclass(frozen=True)
class CsvTable:
    """One SAP table, how to ask for it and how to check what came back."""

    sap_table: str
    """TabName in the entity key. Upper case, as SAP names it."""

    entity_set: str
    """The OData set holding the same data, used only for the $count check."""

    windowed: bool
    """True for transaction data (three years), False for master (everything)."""

    note: str = ""

    @property
    def table(self) -> str:
        """Lower-case table name, matching the seed layer's convention."""
        return self.sap_table.lower()

    @property
    def raw_table(self) -> str:
        """Where csv_load files it. Shares the seed's ``raw_`` namespace.

        Deliberately NOT ``odata_<table>``. The CSV extract of EKPO carries 277
        columns; the OData set carries 19. Merging the narrow delta into the
        wide table would null the other 258 columns on every row it touched --
        a merge that reports success and destroys data. Two namespaces, two
        widths, no silent overwrite.
        """
        return f"raw_{self.table}"

    @property
    def reconcile(self) -> Reconcile:
        return Reconcile.BOUNDED if self.windowed else Reconcile.EXACT

    def window(self, today: date | None = None) -> tuple[str, str]:
        """``(FromDate, ToDate)`` as SAP's DATS strings."""
        today = today or date.today()
        if self.windowed:
            start = date(
                today.year - TRANSACTION_WINDOW_YEARS, today.month, today.day
            )
            return start.strftime(DATE_FORMAT), today.strftime(DATE_FORMAT)

        forward = today + timedelta(days=WIDE_WINDOW_FORWARD_DAYS)
        return WIDE_WINDOW_START, forward.strftime(DATE_FORMAT)


# All 21 sets the contract exposes, by the SAP table each mirrors. The mapping
# is the one cpi_discovery.py verified against live $metadata; the entity_set
# column is what contract.py resolves for the $count.
CSV_TABLES: tuple[CsvTable, ...] = (
    # --- Master data: everything, no meaningful date window ----------------
    CsvTable("MARA", "MaterialSet", windowed=False, note="material master"),
    CsvTable("MAKT", "MaterialDescriptionSet", windowed=False, note="descriptions"),
    CsvTable("MARC", "MaterialPlantSet", windowed=False, note="MRP type, OAR scope"),
    CsvTable("MARD", "StorageLocationStockSet", windowed=False, note="stock on hand"),
    CsvTable("MBEW", "MaterialValuationSet", windowed=False, note="valuation"),
    CsvTable("MCHB", "BatchStockSet", windowed=False),
    CsvTable("EINA", "InfoRecordSet", windowed=False),
    CsvTable("EINE", "InfoRecordOrgSet", windowed=False),
    CsvTable("LFA1", "VendorSet", windowed=False),
    CsvTable("S031", "MonthlyMovementStatisticSet", windowed=False, note="LIS"),
    CsvTable("S032", "StockMovementStatisticSet", windowed=False, note="LIS"),
    # --- Transaction data: three years -------------------------------------
    CsvTable("EKKO", "PurchaseOrderSet", windowed=True),
    CsvTable("EKPO", "PurchaseOrderItemSet", windowed=True,
             note="277 columns over CSV against 19 over OData -- BEDNR, "
                  "CREATIONDATE, SOBKZ, PSTYP, KNTTP, BANFN and the Z-appends "
                  "reach us only this way."),
    CsvTable("EKET", "POScheduleLineSet", windowed=True),
    CsvTable("EKBE", "POHistorySet", windowed=True,
             note="OData paging returns 0 rows against a $count of 3,881, so "
                  "this route is the only one that reads EKBE at all."),
    CsvTable("EBAN", "PurchaseRequisitionSet", windowed=True),
    CsvTable("MKPF", "MaterialDocumentHeaderSet", windowed=True,
             note="Budat is IGNORED over OData, so there is no delta for this "
                  "set -- the CSV window is how it stays current."),
    CsvTable("MSEG", "GoodsMovementItemSet", windowed=True,
             note="rides MKPF; same reason."),
    CsvTable("RESB", "ReservationItemSet", windowed=True),
    CsvTable("CDHDR", "ChangeDocHeaderSet", windowed=True, note="FR-9 adoption"),
    CsvTable("CDPOS", "ChangeDocItemSet", windowed=True, note="FR-9 adoption"),
)

BY_TABLE: dict[str, CsvTable] = {t.sap_table: t for t in CSV_TABLES}
BY_ENTITY_SET: dict[str, CsvTable] = {t.entity_set: t for t in CSV_TABLES}


def csv_table(name: str) -> CsvTable:
    """Look up by SAP table name, case-insensitively."""
    try:
        return BY_TABLE[name.strip().upper()]
    except KeyError:
        raise KeyError(
            f"{name!r} is not a table this extract knows. Known: "
            f"{', '.join(sorted(BY_TABLE))}"
        ) from None
