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
caused that, so nothing here sends a blank date: "the whole table" is a window
wide enough to mean everything.

THE WHOLE TABLE, UNLESS A WINDOW IS SET

Every table is pulled whole. Master data always has been; the transaction
tables were limited to the last three years (the 25-Sep instruction) until
2026-11-02, when the full history was asked for instead. A window for the
transaction tables is now a setting -- ``CSV_PULL_WINDOW_YEARS``, or
``CSV_PULL_FROM_DATE`` with ``CSV_PULL_TO_DATE`` -- and ``--years`` or
``--from-date``/``--to-date`` choose one for a single run.

The settings never narrow master data: "the last three years of MARA" is not a
meaningful request -- a material created in 2009 and still stocked today has to
be in the dimension. Only a run's explicit ``--from-date``/``--to-date`` does,
as it always has, because that is a one-off someone typed on purpose.

WHAT THE $count IS FOR

Chunks carry no request id, no sequence number and no total, so the only
completeness evidence available is Interface 1's own ``$count`` for the same
data. That comparison is exact only when the extract is known to be the whole
set:

* ``Reconcile.EXACT``   -- master data, always whole: received rows must EQUAL
  the count.
* ``Reconcile.BOUNDED`` -- transaction tables. The count is an upper bound:
  received must be above zero and no greater than it. They can be windowed,
  and even whole the job sends a subset of some (CDPOS: only the items of the
  tables this extract covers), so equality would fail a correct delivery.

BOUNDED is genuinely weaker, and it is not weaker by choice. A count with a
matching date filter would be the right answer, but MKPF's ``Budat`` filter is
IGNORED by this service (see manifest.DELTAS) -- asking for one would return
the whole table and turn a loose check into a confidently wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

from app.integrations.sap.known_conditions import CSV_UNDELIVERED_TABLES

# "Everything". Not a blank date -- see the module docstring.
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
    """True for transaction data, the tables a configured window narrows.
    False for master data, which is always pulled whole."""

    note: str = ""

    count_filter: str | None = None
    """A $filter for the $count check, where the CSV job extracts a subset the
    OData set does not. The job is not ours and does not say what it filters;
    this records what was measured, so a correct delivery is not failed
    against a total it was never going to reach."""

    count_service: str | None = None
    """The OData service holding ``entity_set``, for a set the recorded
    discovery contract does not cover. Only the gate-pass sets need it: the
    contract was generated from the two KPI02 services, so without this their
    $count would fail the lookup and every delivery would load unverified."""

    @property
    def blocked(self) -> str | None:
        """Why SAP will not deliver this table right now, or None.

        A measured fact about the extract job, from
        known_conditions.CSV_UNDELIVERED_TABLES.
        """
        return CSV_UNDELIVERED_TABLES.get(self.sap_table)

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

    def window(
        self,
        today: date | None = None,
        *,
        years: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> tuple[str, str]:
        """``(FromDate, ToDate)`` as SAP's DATS strings. Pure: the settings are
        applied by :func:`requested_window`, which calls this.

        Nothing passed: the whole table, as a wide window. An explicit
        ``from_date``/``to_date`` wins over everything, master data included.
        That escape hatch is not decoration: a date window built from today
        returns almost nothing in a client whose data is old -- 99.3% of this
        one's purchase orders predate 2026, `Aedat ge 2026-01-01` matches 23
        rows of 3,140, and the ZREP orders are all from 2018. SAP acknowledges
        such a request cheerfully and delivers nothing.

        ``years`` limits a transaction table to the last N years, ending today;
        0 or None is the whole table. Master data ignores it.
        """
        if from_date and to_date:
            return from_date, to_date

        today = today or date.today()
        if self.windowed and years:
            try:
                start = today.replace(year=today.year - years)
            except ValueError:  # 29 February, N years back in a common year
                start = today.replace(year=today.year - years, day=28)
            return start.strftime(DATE_FORMAT), today.strftime(DATE_FORMAT)

        forward = today + timedelta(days=WIDE_WINDOW_FORWARD_DAYS)
        return WIDE_WINDOW_START, forward.strftime(DATE_FORMAT)


# All 21 sets the contract exposes, by the SAP table each mirrors, plus the
# three gate-pass Z-tables. The mapping is the one cpi_discovery.py verified
# against live $metadata; the entity_set column is what contract.py resolves
# for the $count (count_service where the contract does not cover the set).
CSV_TABLES: tuple[CsvTable, ...] = (
    # --- Master data: everything, no meaningful date window ----------------
    CsvTable("MARA", "MaterialSet", windowed=False, note="material master"),
    # English descriptions only. Measured 2026-09-26: the CSV job delivers
    # 2,040 rows whatever window or cap it is given (four shapes, one
    # byte-identical file), while OData holds 2,042 -- the two others are the
    # Afrikaans and German descriptions of one material. $count with
    # Spras eq 'E' is 2,040, and that filter is measured HONOURED.
    CsvTable(
        "MAKT", "MaterialDescriptionSet", windowed=False, note="descriptions, English",
        count_filter="Spras eq 'E'",
    ),
    CsvTable("MARC", "MaterialPlantSet", windowed=False, note="MRP type, OAR scope"),
    CsvTable("MARD", "StorageLocationStockSet", windowed=False, note="stock on hand"),
    CsvTable("MBEW", "MaterialValuationSet", windowed=False, note="valuation"),
    CsvTable("MCHB", "BatchStockSet", windowed=False),
    CsvTable("EINA", "InfoRecordSet", windowed=False),
    CsvTable("EINE", "InfoRecordOrgSet", windowed=False),
    CsvTable("LFA1", "VendorSet", windowed=False),
    CsvTable("S031", "MonthlyMovementStatisticSet", windowed=False, note="LIS"),
    CsvTable("S032", "StockMovementStatisticSet", windowed=False, note="LIS"),
    # --- Transaction data: whole, unless CSV_PULL_* sets a window -----------
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
    # The CSV job extracts change documents for a fixed set of procurement
    # object classes, not the whole table, and $count counts the whole table.
    # Measured 2026-09-26 against the landed rows:
    #
    #   CDHDR  MATERIAL 7,661  EINKBELEG 7,219  KRED 1,344  BANF 935
    #          INFOSATZ 637  = 17,796, and each class equals its OData $count
    #          exactly -- complete for the classes the job is configured for.
    #   CDPOS  filtered further, by TABNAME within the tables this extract
    #          covers (MCHB, EKKO, EKPO, MARC, MARA, EBAN, MBEW, MARD, LFA1,
    #          RESB ...): 71,392 rows, a strict subset of every class but
    #          INFOSATZ and the two ZMM_GP ones.
    #
    # So 7% of $count is the whole of what the job will ever send, not a
    # short delivery. BOUNDED already treats the count as a ceiling; the
    # ratio is the reason it is far below it.
    CsvTable(
        "CDHDR", "ChangeDocHeaderSet", windowed=True,
        note="FR-9 adoption; five object classes only, complete for each",
        # $count over the same five classes is 17,796 -- the delivery exactly.
        count_filter=(
            "Objectclas eq 'MATERIAL' or Objectclas eq 'EINKBELEG' or "
            "Objectclas eq 'KRED' or Objectclas eq 'BANF' or Objectclas eq 'INFOSATZ'"
        ),
    ),
    CsvTable("CDPOS", "ChangeDocItemSet", windowed=True,
             note="FR-9 adoption; items for the extracted tables only"),
    # --- Gate pass: landed as evidence -------------------------------------
    # Descoped from I08 on 15-Aug, but pulled so the files sit in ADLS beside
    # the rest. SAP has delivered all three since 30-Sep, in the wide layout
    # and with exact counts (304 / 319 / 24 on 06-Oct). The wide window keeps
    # the check EXACT, which is what those deliveries met.
    # Back in scope 2026-10-16: the three sets carry ZzdeltaDate and the
    # hourly delta merges into these tables like the rest.
    CsvTable("ZMM_GP_HDR", "GatePassHeaderSet", windowed=False,
             note="gate pass header", count_service="ZMM_KPI02_GP_SRV"),
    CsvTable("ZMM_GP_ITEM", "GatePassItemSet", windowed=False,
             note="gate pass items", count_service="ZMM_KPI02_GP_SRV"),
    CsvTable("ZMM_GP_IN", "GatePassReturnSet", windowed=False,
             note="gate pass returns", count_service="ZMM_KPI02_GP_SRV"),
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


def configured_window() -> tuple[int | None, str | None, str | None]:
    """``(years, from_date, to_date)`` for the transaction tables, from the settings.

    ``CSV_PULL_WINDOW_YEARS`` (the last N years, ending today) or
    ``CSV_PULL_FROM_DATE`` with ``CSV_PULL_TO_DATE`` (a fixed range, YYYYMMDD).
    All unset: ``(None, None, None)``, the whole table. Raises ValueError for a
    setting that cannot be a window -- a half-given range, a date that is not
    one, both kinds at once -- because quietly pulling the whole table when
    someone set a window, or the reverse, is worse than refusing.
    """
    from app.core.config import get_settings

    settings = get_settings()
    years = settings.csv_pull_window_years
    start = settings.csv_pull_from_date.strip()
    end = settings.csv_pull_to_date.strip()

    if bool(start) != bool(end):
        raise ValueError(
            "CSV_PULL_FROM_DATE and CSV_PULL_TO_DATE must be set together "
            f"(got from={start or 'unset'}, to={end or 'unset'})"
        )
    if start:
        if years:
            raise ValueError(
                "Set CSV_PULL_WINDOW_YEARS or CSV_PULL_FROM_DATE/CSV_PULL_TO_DATE, not both"
            )
        for name, value in (("CSV_PULL_FROM_DATE", start), ("CSV_PULL_TO_DATE", end)):
            try:
                datetime.strptime(value, DATE_FORMAT)
            except ValueError:
                raise ValueError(f"{name}={value!r} is not a YYYYMMDD date") from None
        if start > end:
            raise ValueError(f"CSV_PULL_FROM_DATE {start} is after CSV_PULL_TO_DATE {end}")
        return None, start, end
    if years < 0:
        raise ValueError(f"CSV_PULL_WINDOW_YEARS={years} cannot be negative (0 = the whole table)")
    return (years or None), None, None


def requested_window(
    spec: CsvTable,
    today: date | None = None,
    *,
    years: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> tuple[str, str]:
    """The window to ask SAP for: this run's own choice, else the settings,
    else the whole table.

    A run that passes ``years`` (``--years``; 0 = the whole table) or both
    dates (``--from-date``/``--to-date``) gets exactly that, as before. Without
    one, the ``CSV_PULL_*`` settings apply -- to the transaction tables only;
    master data is always whole. Raises ValueError for an unusable setting
    (:func:`configured_window`).
    """
    if years is not None or (from_date and to_date):
        return spec.window(today, years=years, from_date=from_date, to_date=to_date)
    if not spec.windowed:
        return spec.window(today)
    years, from_date, to_date = configured_window()
    return spec.window(today, years=years, from_date=from_date, to_date=to_date)
