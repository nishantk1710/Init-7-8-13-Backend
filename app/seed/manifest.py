"""Which extract workbook lands in which table.

This mapping cannot be derived. The July delivery names files four different
ways -- ``Mara.XLSX``, ``MARC Extract.XLSX``, ``EXPORT_EKPO.XLSX``,
``Mseg_1.XLSX`` -- and four tables arrive split across two files because SAP's
export hit Excel's 1,048,576-row ceiling. So it is written down, once, here.

Every table lands in the RAW layer, named ``raw_<table>``:

    XLSX extract  ->  raw_<table>   mirrors the extract exactly, every column, text
                          |
                     normalise      translation map + MATNR padding  (NOT YET BUILT)
                          v
                      <table>       matches the OData contract; what initiatives read
                          ^
    CPI live pull  -------+         same shape, different loader

The raw layer is deliberately a faithful copy. The extract is far richer than
the OData projection -- MARA has 244 columns against the 7 CPI exposes, and it
carries ``Ext. Material Group`` (EXTWG), the field two FRSs call BLOCKING. Land
all of it; decide what to promote afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExtractSpec:
    """One target table and the workbook(s) that fill it."""

    table: str
    """Table name WITHOUT the ``raw_`` prefix, lower case."""

    files: tuple[str, ...]
    """Storage keys, in load order. More than one where SAP split the export."""

    sap_table: str
    """The SAP table this mirrors. Documentation, and the join to the OData contract."""

    sheet: str | None = None
    """Worksheet to read. ``None`` means the first one.

    The SAP table extracts are single-sheet. The ZMM065 and GR reports are real
    workbooks with pivots and working sheets alongside the data -- BMM's data is
    the third sheet of five -- so those must name it.
    """

    header_row: int = 1
    """1-based row holding the column headers. Both ZMM065 reports need 2."""

    note: str = ""

    initiatives: tuple[str, ...] = field(default_factory=tuple)
    """Which initiatives read it. Purely informational -- helps triage a failed load."""

    @property
    def raw_table(self) -> str:
        return f"raw_{self.table}"


# STORAGE_URL points at the Vedanta folder, and every key is prefixed with the
# sub-folder it was delivered in. That mirrors how a cloud container is laid out
# -- one root, many prefixes -- and it is what lets one Storage reach both the
# SAP table extracts and the reports Rohit shared, which live side by side.
TABLES = "KPI 02 Data Extract/Tables"
REPORTS = "Resources Shared - Rohit"


# Order matters only for readability; loads are independent.
EXTRACTS: tuple[ExtractSpec, ...] = (
    # --- Material master -------------------------------------------------
    ExtractSpec(
        table="mara",
        files=(f"{TABLES}/Mara.XLSX",),
        sap_table="MARA",
        initiatives=("I07", "I08", "I13"),
        note="244 columns. Carries 'Ext. Material Group' (EXTWG) and 'Serial No. Profile'.",
    ),
    ExtractSpec(
        table="makt",
        files=(f"{TABLES}/Makt.XLSX",),
        sap_table="MAKT",
        initiatives=("I07", "I08", "I13"),
    ),
    ExtractSpec(
        table="marc",
        files=(f"{TABLES}/MARC Extract.XLSX",),
        sap_table="MARC",
        initiatives=("I07", "I08", "I13"),
        note="Carries 'MRP Type' (DISMM) -- the OAR rule -- and Reorder Point / "
        "Planned Deliv. Time. PLANT 1300 ONLY: 45,352 rows for 1300, 57 for 1200, "
        "ZERO for Gamsberg (1500). Every DISMM/OAR statistic from this table is a "
        "Black Mountain figure, not a business-wide one.",
    ),
    ExtractSpec(
        table="mard",
        files=(f"{TABLES}/MARD_Extract.XLSX",),
        sap_table="MARD",
        initiatives=("I07", "I08", "I13"),
    ),
    ExtractSpec(
        table="mbew",
        files=(f"{TABLES}/MBEW.XLSX",),
        sap_table="MBEW",
        initiatives=("I07", "I13"),
        note="MaterialValuationSet returns 0 rows over OData; this extract is the only source.",
    ),
    ExtractSpec(
        table="mchb",
        files=(f"{TABLES}/MCHB.XLSX",),
        sap_table="MCHB",
        initiatives=("I13",),
    ),
    # --- Movements -------------------------------------------------------
    ExtractSpec(
        table="mkpf",
        files=(f"{TABLES}/MKPF.XLSX",),
        sap_table="MKPF",
        initiatives=("I07", "I08", "I13"),
    ),
    ExtractSpec(
        table="mseg",
        files=(f"{TABLES}/Mseg_1.XLSX", f"{TABLES}/MSEG_2.XLSX"),
        sap_table="MSEG",
        initiatives=("I07", "I08", "I13"),
        note="Split across two files -- 256 MB combined, the largest load by far.",
    ),
    # --- Purchasing ------------------------------------------------------
    ExtractSpec(
        table="eban",
        files=(f"{TABLES}/EBAN_1300.XLSX",),
        sap_table="EBAN",
        initiatives=("I08", "I13"),
        note="PLANT 1300 ONLY -- all 234,310 rows are Black Mountain. Gamsberg "
        "requisitions are in no delivery. Confirmed by query, not inferred.",
    ),
    ExtractSpec(
        table="ekko",
        files=(f"{TABLES}/EKKO.XLSX",),
        sap_table="EKKO",
        initiatives=("I07", "I08", "I13"),
        note="Document type (BSART) lives here -- the ZREP question.",
    ),
    ExtractSpec(
        table="ekpo",
        files=(f"{TABLES}/EXPORT_EKPO.XLSX",),
        sap_table="EKPO",
        initiatives=("I07", "I08", "I13"),
        note="Item category (PSTYP) lives here -- the repair-PO convention.",
    ),
    ExtractSpec(
        table="eket",
        files=(f"{TABLES}/EKET.XLSX",),
        sap_table="EKET",
        initiatives=("I08", "I13"),
    ),
    ExtractSpec(
        table="ekbe",
        files=(f"{TABLES}/EKBE.XLSX",),
        sap_table="EKBE",
        initiatives=("I07", "I08", "I13"),
    ),
    ExtractSpec(
        table="eina",
        files=(f"{TABLES}/EINA.XLSX",),
        sap_table="EINA",
        initiatives=("I07", "I08"),
    ),
    ExtractSpec(
        table="eine",
        files=(f"{TABLES}/EINE.XLSX",),
        sap_table="EINE",
        initiatives=("I07", "I08"),
    ),
    ExtractSpec(
        table="lfa1",
        files=(f"{TABLES}/EXPORT_LFA1.XLSX",),
        sap_table="LFA1",
        initiatives=("I08",),
        note="POPIA: business-entity fields only when promoted to the canonical layer.",
    ),
    # --- Reservations ----------------------------------------------------
    ExtractSpec(
        table="resb",
        files=(f"{TABLES}/RESB.XLSX",),
        sap_table="RESB",
        initiatives=("I08", "I13"),
        note="ReservationItemSet reports only 1,000 rows over OData; this extract is fuller.",
    ),
    # --- Change documents ------------------------------------------------
    ExtractSpec(
        table="cdhdr",
        files=(f"{TABLES}/CDHDR1.XLSX", f"{TABLES}/CDHDR2.XLSX"),
        sap_table="CDHDR",
        initiatives=("I07", "I13"),
        note="Split across two files. Feeds I07 FR-9 adoption tracking.",
    ),
    ExtractSpec(
        table="cdpos",
        files=(f"{TABLES}/EXPORT_CDPOS.XLSX",),
        sap_table="CDPOS",
        initiatives=("I07", "I13"),
    ),
    # --- LIS statistics --------------------------------------------------
    ExtractSpec(
        table="s031",
        files=(f"{TABLES}/EXPORT_S031_1.XLSX", f"{TABLES}/EXPORT_S031_2.XLSX"),
        sap_table="S031",
        initiatives=("I13",),
        note="MonthlyMovementStatisticSet returns 0 rows over OData; this extract is the only source.",
    ),
    ExtractSpec(
        table="s032",
        files=(f"{TABLES}/EXPORT_S032_1.XLSX", f"{TABLES}/EXPORT_S032_2.XLSX"),
        sap_table="S032",
        initiatives=("I13",),
    ),
    # --- Gate pass -------------------------------------------------------
    # Descoped from I08 on 15-Aug, loaded anyway: they are small, and
    # re-requesting an extract later costs more than the disk does now.
    ExtractSpec(
        table="zmm_gp_hdr",
        files=(f"{TABLES}/EXPORT_ZMM_GP_HDR.XLSX",),
        sap_table="ZMM_GP_HDR",
        note="Gate pass -- descoped from I08 on 15-Aug. Loaded for completeness.",
    ),
    ExtractSpec(
        table="zmm_gp_item",
        files=(f"{TABLES}/EXPORT_ZMM_GP_ITEM.XLSX",),
        sap_table="ZMM_GP_ITEM",
        note="Gate pass -- descoped from I08 on 15-Aug. Loaded for completeness.",
    ),
    ExtractSpec(
        table="zmm_gp_in",
        files=(f"{TABLES}/EXPORT_ZMM_GP_IN.XLSX",),
        sap_table="ZMM_GP_IN",
        note="Gate pass -- descoped from I08 on 15-Aug. Loaded for completeness.",
    ),
    # --- Reports (shared separately by Rohit, not part of the table extract) ---
    #
    # These are reports, not table dumps: multi-sheet workbooks with title rows
    # and pivots. They are also the only source for two things the initiatives
    # need -- the SOP reconciliation baselines, and the criticality tiers that
    # the FRSs call the "platform-side critical parts list" (D3 interim).
    ExtractSpec(
        table="zmm065_bmm",
        files=(f"{REPORTS}/BMM-ZMM065_Aging_Jul 26.xlsx",),
        sap_table="ZMM065",
        sheet="Sheet1",
        header_row=2,
        initiatives=("I07", "I08", "I13"),
        note="Black Mountain (plant 1300), 34 columns. Carries Criticality (D3). "
        "Data is the 3rd of 5 sheets; the others are pivots.",
    ),
    ExtractSpec(
        table="zmm065_gb",
        files=(f"{REPORTS}/GB-ZMM065 - July 2026.XLSX",),
        sap_table="ZMM065",
        sheet="Sheet2",
        header_row=2,
        initiatives=("I07", "I08", "I13"),
        note="Gamsberg (plant 1500), 30 columns -- a different shape from BMM, so "
        "a separate table. Unioning them is the normalise layer's job.",
    ),
    ExtractSpec(
        table="gr_30day",
        files=(f"{REPORTS}/30 Day GR Report.xlsx",),
        sap_table="(report)",
        sheet="GR REPORT",
        header_row=1,
        initiatives=("I13",),
        note="I13 FR-6 reconciles the 30-day goods-received-not-issued check against this.",
    ),
)

BY_TABLE: dict[str, ExtractSpec] = {spec.table: spec for spec in EXTRACTS}

ALL_FILES: frozenset[str] = frozenset(f for spec in EXTRACTS for f in spec.files)

# Everything named in Anish's brief is now accounted for: ZMM065 for both plants
# and the 30-day GR report arrived separately from Rohit, outside the SAP table
# extraction request.
#
# What remains genuinely partial is the requisition extract: EBAN_1300 holds
# 234,310 rows and every one is plant 1300 (Black Mountain). Gamsberg
# requisitions are not in any delivery, and both I08 and I13 read PRs.
MISSING_FROM_DELIVERY: tuple[str, ...] = (
    "MARC for Gamsberg (1500) -- the extract holds plant 1300 and 1200 only, so "
    "no MRP type, reorder point or lead time exists for Gamsberg",
    "EBAN for plants other than 1300 -- requisitions are Black Mountain only",
)

# Measured, not assumed. MARD, MSEG, RESB, EKPO and the ZMM065 reports all carry
# multiple plants; MARC and EBAN do not. That asymmetry is what makes the gap
# easy to miss: stock and movements look complete while the planning parameters
# behind them cover one site.
PLANT_COVERAGE_NOTE = """raw_marc  plant 1300 (45,352) + 1200 (57).  Gamsberg 1500: ZERO rows.
raw_eban  plant 1300 only (234,310).
raw_mard  1300, 3000, 2000, 1500, 1600, ... multi-plant.
raw_resb  1300, 1500, 3000, 1200, 2000, ... multi-plant.
raw_ekpo  13 plants incl. 1500 (35,083).
"""


def spec_for(table: str) -> ExtractSpec:
    try:
        return BY_TABLE[table]
    except KeyError:
        known = ", ".join(sorted(BY_TABLE))
        raise KeyError(f"Unknown table {table!r}. Known tables: {known}") from None
