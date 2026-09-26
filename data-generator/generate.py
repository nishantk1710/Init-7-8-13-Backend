#!/usr/bin/env python
"""
VZI Spares AI - synthetic data generator for Initiatives 07, 08 and 13.

    python generate.py

Writes one dataset:

    generated/sap/       synthetic data shaped like what SAP will deliver
                         through CPI / OData
    generated/platform/  the minimum data the Spares AI platform itself owns

One common SAP universe: the same materials, plants, reservations, purchase
requisitions, orders and goods movements serve all three initiatives. A single
80-series material can carry stock context for I07, a repair chain for I08 and
a procurement chain for I13 at the same time.

SAP column names, their order and their keys are READ AT RUN TIME from
discovery/properties.csv. Only the entity sets and fields CPI does not expose
are declared in this script, in NOT_EXPOSED_SETS and PENDING_FIELDS below.

This is a two-step pipeline, and they run separately:

  1. cpi_discovery.py   talks to the live CPI OData endpoint (needs CPI_*
                         credentials in .env - see .env.example) and writes
                         discovery/properties.csv, entity_sets.csv, counts.csv
                         and the metadata XML. Run this whenever the SAP/CPI
                         contract changes - a field is added, exposed, or a
                         service is registered.

                             python cpi_discovery.py --out discovery

  2. generate.py         (this file) reads discovery/properties.csv and
                         generates the CSVs. It never calls CPI itself and
                         needs no credentials - only the discovery/ folder.

So refreshing the dataset after a CPI change is:

    python cpi_discovery.py --out discovery   # step 1, needs .env, hits the network
    python generate.py                         # step 2, offline, deterministic

Step 2 alone is what you run day to day. Step 1 is only needed when the SAP
side of the contract itself has changed.

Standard library only (generate.py). cpi_discovery.py needs requests and
openpyxl - see its own docstring. Deterministic: the same SEED and AS_OF
produce the same dataset.
"""

from __future__ import annotations

import csv
import random
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

# ===========================================================================
# CONFIGURATION
# ===========================================================================

SEED = 42
MATERIAL_COUNT = 2000
HISTORY_MONTHS = 24

# Anchor date for "today". Fixed rather than date.today() so a given seed
# always produces the same dates.
AS_OF = date(2026, 9, 7)

# SAP returns material numbers in internal format, left-padded to 18 with
# zeros. Set False for readable numbers if that is easier to work with.
MATNR_LEADING_ZEROS = True

OUT_DIR = Path(__file__).parent / "generated"

# Output of cpi_discovery.py against the live service. This is the schema
# reference: properties.csv drives the SAP column names, order and keys.
DISCOVERY_DIR = Path(__file__).parent / "discovery"
CURRENCY = "ZAR"
LANGUAGE = "EN"
BASE_UOM = "EA"
PURCHASING_ORG = "Z100"
PURCHASING_GROUPS = ("Z01", "Z02", "Z03")

# Two plants, and they are the REAL codes.
#
# This list used to hold four invented codes (1000 Gamsberg, 2000 BMM, 3000,
# 4000). That is why `consumption_plans.csv` carries plant 4000 and joins to
# nothing -- see the B2 blocker in
# docs/I13_Implementation_Status_and_Blockers_21_Sep_2026.md. Generated data
# that uses codes the real extract has never heard of cannot be reconciled
# against it, and the mismatch surfaces as a silent zero rather than an error.
#
# The scope ruling of 21-Sep-2026 fixes both problems at once: two plants, at
# the codes SAP actually uses. Kept in step with app/shared/plant_scope.py.
PLANTS = [
    {"werks": "1300", "name": "Black Mountain Mining", "lgort": "SP01", "cc": "1300-MECH", "order": "41"},
    {"werks": "1500", "name": "Gamsberg", "lgort": "SP02", "cc": "1500-MECH", "order": "42"},
]

# Movement types used (MSEG.BWART).
GR_PO = "101"            # goods receipt against a purchase order
GI_ORDER = "261"         # issue to a maintenance order
GI_COST_CENTRE = "201"   # issue to a cost centre
REPAIR_REMOVAL = "541"   # stock to subcontractor for repair
SCRAPPING = "551"

# Repair purchase orders are identified by item category 3 with document type
# ZREP (I08 decision D7, proposed on the purchasing-data evidence and pending
# SAP team confirmation). Change these two values if SAP confirms otherwise.
REPAIR_ITEM_CATEGORY = "3"
REPAIR_DOC_TYPE = "ZREP"
REPAIR_ACCOUNT_ASSIGNMENT = "F"

# OAR (Planned on Demand) is identified by MRP type - MARC.DISMM - per the
# team lead's ruling of 08-Sep 2026, which supersedes MARA.EXTWG. Both ND and
# PD count as OAR. DISMM is live on MaterialPlantSet, so unlike EXTWG this
# needs nothing from the SAP team.
#
# PENDING VERIFICATION: three filtered $count calls against live
# MaterialPlantSet (Dismm eq 'ND' / 'PD' / 'VB', ~2,177 rows total) will show
# whether ND+PD is a minority of the catalogue. If it is most of it, the rule
# over-selects and the scope needs narrowing - raise with the team lead before
# trusting these values. See docs-eng/WS2_INTEGRATION_PLAN.md section 1.6.
OAR_MRP_TYPES = ("ND", "PD")

# MRP type for materials outside OAR scope: reorder-point planned.
PLANNED_MRP_TYPE = "VB"

# Obsolete stock carries ND in SAP - no planning is maintained for it - which
# overlaps OAR_MRP_TYPES on purpose. MRP type alone therefore cannot separate
# "ordered on demand" from "no longer used"; MARA.MSTAE ('01' here) is the
# orthogonal signal, and it is already live on MaterialSet. The scope config
# needs both predicates: Dismm in (ND, PD) AND Mstae ne '01'. Keeping the
# overlap in the synthetic data is what makes that second predicate testable.
OBSOLETE_MRP_TYPE = "ND"
OBSOLETE_MATERIAL_STATUS = "01"

# Because DISMM sits on MARC rather than MARA, OAR scope is decided per plant:
# a material can be planned on demand in one plant and reorder-point planned in
# another. This is the share of multi-plant OAR materials given exactly that
# split, so the scope roll-up policy (any-plant / all-plants / per-plant-only)
# has something to be tested against. Set to 0.0 for uniform behaviour.
OAR_SINGLE_PLANT_SHARE = 0.15

if PLANNED_MRP_TYPE in OAR_MRP_TYPES:
    raise SystemExit(
        f"PLANNED_MRP_TYPE {PLANNED_MRP_TYPE!r} is in OAR_MRP_TYPES "
        f"{OAR_MRP_TYPES!r}: every planned material would be selected as OAR. "
        f"Pick an MRP type outside the OAR set."
    )

OVERDUE_GRACE_DAYS = 7   # after EKET.EINDT, before a repair counts as overdue
PLAN_GRACE_DAYS = 14     # after the planned use date, before use counts overdue

rng = random.Random(SEED)


# ===========================================================================
# SAP SCHEMA
#
# All 21 live entity sets are read from discovery/properties.csv: column
# names, column order and key fields all come from there, so a fresh
# discovery run changes the generated CSVs without touching this script.
#
# The rest is what CPI does not expose, declared here with the reason.
# ===========================================================================

# Fallback definitions for entity sets discovery cannot see, because the
# service carrying them is unregistered or unreachable. Columns follow the VZI
# Entity Dictionary and the CamelCase convention the live services use. Once a
# set appears in discovery, delete its entry here and the discovered
# definition takes over automatically.
#
# Empty since the 08-Sep 14:16 sweep: ZMM_KPI02_SRV is registered, so all
# seven of its sets - ReservationItemSet, MaterialValuationSet,
# ChangeDocHeaderSet, ChangeDocItemSet, BatchStockSet,
# MonthlyMovementStatisticSet, StockMovementStatisticSet - now come from
# properties.csv with real names, types and keys.
NOT_EXPOSED_SETS: dict[str, list[str]] = {}

# Keys for the sets above, since discovery cannot tell us.
NOT_EXPOSED_KEYS: dict[str, list[str]] = {}

# Extra columns appended to a set that IS live, because the FRS needs a field
# the current projection leaves out. Delete an entry once SAP exposes it and
# discovery starts reporting it.
#
# MARA.EXTWG was here until 08-Sep 2026. It is gone rather than exposed: the
# team lead replaced the OAR identifier with MRP type (MARC.DISMM), which is
# already live, so the field is no longer wanted. Nothing should read Extwg.
PENDING_FIELDS: dict[str, list[str]] = {
    "MaterialSet": [
        # Pending SAP exposure - relevant to a future serial-grain repair
        # register. Present on about 18% of 80-series purchase order lines.
        # Not blocking: no current I07/I08/I13 requirement depends on it.
        "Sernp",
    ],
    "ReservationItemSet": [
        # Pending SAP exposure - required by I08/I13. The reservation field that
        # carries the AI assistant session identifier has not been designated
        # yet (candidates: SGTXT, WEMPF, ABLAD or a Z-append).
        "Zzaisession",
    ],
}

# Entity sets that discovery/counts.csv itself reports as 0 rows live in SAP
# (as of the 08-Sep 2026 sweep), so an empty CSV here is correct rather than a
# missed generator. check() treats every other discovered set with zero rows
# as a bug - see the completeness check below.
EXPECTED_EMPTY_SETS = {"MonthlyMovementStatisticSet"}

# The order files are written and reported in. Sets not listed follow.
ENTITY_ORDER = [
    "MaterialSet", "MaterialDescriptionSet", "MaterialPlantSet",
    "StorageLocationStockSet", "MaterialValuationSet", "VendorSet",
    "InfoRecordSet", "InfoRecordOrgSet", "ReservationItemSet",
    "PurchaseRequisitionSet", "PurchaseOrderSet", "PurchaseOrderItemSet",
    "POScheduleLineSet", "POHistorySet", "MaterialDocumentHeaderSet",
    "GoodsMovementItemSet", "ChangeDocHeaderSet", "ChangeDocItemSet",
]


def load_discovery() -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
    """Read properties.csv into (columns, keys, types) per entity set."""
    path = DISCOVERY_DIR / "properties.csv"
    if not path.exists():
        raise RuntimeError(
            f"{path} not found. It is the output of cpi_discovery.py and is what "
            f"defines the SAP column names, order and keys. Put the discovery "
            f"folder next to generate.py."
        )
    columns: dict[str, list[str]] = {}
    keys: dict[str, list[str]] = {}
    types: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            entity = row["entity_set"].strip()
            name = row["property"].strip()
            columns.setdefault(entity, []).append(name)
            types[f"{entity}.{name}"] = row["type"].strip()
            if row["is_key"].strip().upper() == "K":
                keys.setdefault(entity, []).append(name)
    if not columns:
        raise RuntimeError(f"{path} contained no properties")
    return columns, keys, types


def build_schema() -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
    """Discovered sets plus the ones CPI does not expose."""
    columns, keys, types = load_discovery()
    discovered = set(columns)

    for entity, extra in PENDING_FIELDS.items():
        if entity not in columns:
            raise RuntimeError(
                f"PENDING_FIELDS names {entity}, which discovery does not report. "
                f"Either the set has been renamed or the entry is stale."
            )
        for name in extra:
            if name in columns[entity]:
                # SAP now exposes it, so the pending declaration is obsolete.
                raise RuntimeError(
                    f"{entity}.{name} is now in discovery/properties.csv. Remove it "
                    f"from PENDING_FIELDS - it is no longer pending."
                )
            columns[entity].append(name)

    for entity, declared in NOT_EXPOSED_SETS.items():
        if entity in discovered:
            raise RuntimeError(
                f"{entity} is now in discovery/properties.csv. Remove it from "
                f"NOT_EXPOSED_SETS so the discovered definition is used instead."
            )
        columns[entity] = list(declared)
        keys[entity] = list(NOT_EXPOSED_KEYS[entity])

    order = {name: index for index, name in enumerate(ENTITY_ORDER)}
    ordered = sorted(columns, key=lambda name: (order.get(name, len(order)), name))
    return {name: columns[name] for name in ordered}, keys, types


SAP_COLUMNS, SAP_KEYS, SAP_TYPES = build_schema()

PLATFORM_COLUMNS: dict[str, list[str]] = {
    # ---- Initiative 07 ----------------------------------------------------
    "inventory_recommendations": [
        "recommendation_id", "Matnr", "Werks", "material_description",
        "criticality", "demand_pattern", "consumptions_12m", "lead_time_days",
        "current_rop", "recommended_rop", "current_safety_stock",
        "recommended_safety_stock", "current_max_stock", "recommended_max_stock",
        "recommendation_type", "reason", "status", "created_on",
    ],
    "approvals": [
        "approval_id", "recommendation_id", "approver_role", "approver_user",
        "decision", "decision_date", "comment",
    ],
    # ---- Initiative 08 ----------------------------------------------------
    "repair_attestations": [
        "attestation_id", "session_id", "Matnr", "Werks", "quantity",
        "condition", "fault_category", "repairable", "reason", "user",
        "timestamp", "evidence_reference",
    ],
    "repair_cases": [
        "case_id", "Matnr", "Werks", "repair_pr", "repair_po", "repair_po_item",
        "session_id", "attestation_id", "stage", "removed_on",
        "expected_back_on", "returned_on", "overdue", "vendor_turnaround_days",
        "notes",
    ],
    # ---- Initiative 13 ----------------------------------------------------
    "consumption_plans": [
        "plan_id", "session_id", "Rsnum", "Rspos", "Matnr", "Werks",
        "requester", "purpose", "planned_quantity", "planned_use_date",
        "status",
    ],
    "utilisation_status": [
        "Rsnum", "Rspos", "Matnr", "Werks", "status", "confirmed_used",
        "confirmation_date", "replanned_date", "reason",
    ],
    # ---- shared -----------------------------------------------------------
    "exceptions": [
        "exception_id", "exception_type", "Matnr", "Werks", "object_reference",
        "detected_on", "owner", "status", "detail",
    ],
}


# ===========================================================================
# REFERENCE DATA
# ===========================================================================

# Realistic mining and concentrator spares, grouped by material group.
CATALOGUE = {
    "MECH-BRG": [
        "Crusher Main Shaft Bearing", "Screen Exciter Bearing",
        "Ball Mill Trunnion Bearing", "Conveyor Pulley Bearing",
        "Gearbox Bearing", "Spherical Roller Bearing",
    ],
    "MECH-SEAL": [
        "Slurry Pump Mechanical Seal", "Thickener Rake Seal Kit",
        "Agitator Shaft Seal", "Gland Packing Seal Ring",
    ],
    "MECH-PUMP": [
        "Slurry Pump Wet End Assembly", "Pump Impeller",
        "Vertical Sump Pump Column", "Centrifugal Pump Casing",
    ],
    "MECH-GBOX": [
        "Ball Mill Gearbox Assembly", "Conveyor Drive Gearbox",
        "Thickener Rake Drive Gearbox", "Apron Feeder Gear Reducer",
    ],
    "ELEC-MOTR": [
        "Conveyor Drive Motor", "Mill Lube Pump Motor",
        "Flotation Blower Motor", "Ventilation Fan Motor",
    ],
    "ELEC-CTRL": [
        "MCC Feeder Circuit Breaker", "Soft Starter Power Module",
        "Variable Speed Drive Unit", "PLC Digital Output Card",
    ],
    "HYDR-ASSY": [
        "Hydraulic Pump Assembly", "Crusher Hydroset Cylinder",
        "Valve Assembly", "Hydraulic Power Pack",
    ],
    "CONV-COMP": [
        "Conveyor Idler Roller Set", "Belt Scraper Blade Cartridge",
        "Conveyor Take-Up Pulley", "Skirt Rubber Liner Section",
    ],
    "MILL-LINR": [
        "Mill Liner", "Ball Mill Shell Liner Plate", "Cyclone Apex Liner",
        "Flotation Cell Impeller",
    ],
    "INST-XMTR": [
        "Slurry Density Transmitter", "Level Radar Transmitter",
        "Pressure Transmitter Assembly", "Flow Meter Sensor Head",
    ],
}

SIZES = ["150MM", "200MM", "250MM", "DN100", "DN150", "6/4", "8/6", "10/8",
         "55KW", "110KW", "250KW", "TYPE A", "TYPE B", "HD"]
MAKES = ["WEIR", "METSO", "FLSM", "SKF", "SEW", "ABB", "SIEMENS", "KSB",
         "SANDVIK", "MULTOTEC"]

VENDOR_STEMS = ["Kalahari", "Orange River", "Aggeneys", "Springbok", "Namaqua",
                "Gariep", "Karoo", "Cederberg", "Prieska", "Copperton"]
VENDOR_SUPPLY = ["Industrial Supplies", "Bearings and Power Transmission",
                 "Mining Spares", "Fluid Handling", "Process Equipment"]
VENDOR_REPAIR = ["Rotating Equipment Services", "Mechanical Repair Works",
                 "Rewind Services", "Gearbox Refurbishment",
                 "Hydraulic Service Centre"]

EQUIPMENT_TAGS = ["CR-101", "CR-102", "ML-201", "ML-202", "FL-310", "TH-401",
                  "CV-501", "CV-502", "PU-610", "PU-611", "SC-701", "FA-810"]

REQUESTERS = ["VZIREQ01", "VZIREQ02", "VZIREQ03", "VZIREQ04", "VZIREQ05"]
CONTROLLERS = ["VZIINV01", "VZIINV02"]
ATTESTORS = ["VZIATT01", "VZIATT02"]

FAULTS = ["BEARING_FAILURE", "SEAL_LEAK", "WEAR", "ELECTRICAL_FAULT",
          "IMPELLER_EROSION", "GEARBOX_NOISE", "SHAFT_DAMAGE", "CORROSION"]
CONDITIONS = [
    "Outer race spalling, radial play beyond limit",
    "Seal faces scored, leaking under load",
    "Impeller vanes eroded, 30 percent material loss",
    "Casing wear through at discharge throat",
    "Input pinion tooth chipping, backlash high",
    "Stator winding insulation resistance failed at 500V",
    "Shaft journal grooved, runout beyond limit",
]

# Criticality tiers (ZMM065). Target service level per tier is a VZI business
# decision still to be confirmed; the z-scores follow from these values.
CRITICALITY = {
    "CRITICAL":  {"service_level": 0.98, "z": 2.054},
    "IMPACT":    {"service_level": 0.95, "z": 1.645},
    "INSURANCE": {"service_level": 0.90, "z": 1.282},
    "NORMAL":    {"service_level": 0.85, "z": 1.036},
    "OBSOLETE":  {"service_level": 0.00, "z": 0.000},
}

# The situation each material-plant is in. This is what makes the dataset
# useful: every initiative needs materials of a particular shape to work on.
STORIES = {
    "HIGH_CONSUMPTION":     0.14,   # I07 regular, well-populated history
    "INTERMITTENT":         0.18,   # I07 erratic demand, long gaps
    "SLOW_MOVING":          0.10,   # I07 / I13 aging
    "OVERSTOCKED":          0.08,   # I07 reduce max
    "UNDERSTOCKED_CRITICAL": 0.08,  # I07 raise ROP, critical and short
    "LONG_LEAD":            0.08,   # I07 lead-time driven buffer
    "OAR":                  0.16,   # I13 reservation-to-issue chains
    "REPAIRABLE":           0.14,   # I08 80-series repair lifecycle
    "OBSOLETE":             0.04,   # no safety stock recommendation
}


# ===========================================================================
# SMALL HELPERS
# ===========================================================================

class Counter:
    """SAP-style document number range."""

    def __init__(self, start: int, width: int = 10) -> None:
        self._next = start
        self._width = width

    def take(self) -> str:
        value = self._next
        self._next += 1
        return str(value).rjust(self._width, "0")


materials_no = Counter(10000000, 8)      # stocked
oar_no = Counter(30000000, 8)            # OAR / Planned on Demand
repairable_no = Counter(80000000, 8)     # 80-series repairables
obsolete_no = Counter(90000000, 8)
vendor_no = Counter(1000000, 10)
info_no = Counter(5300000000, 10)
res_no = Counter(1000000000, 10)
pr_no = Counter(2000000000, 10)
po_no = Counter(4500000000, 10)
doc_no = Counter(4900000000, 10)
invoice_no = Counter(5100000000, 10)
change_no = Counter(1000000, 10)
batch_no = Counter(6000000000, 10)


def seq(prefix: str, start: int = 1):
    """Readable platform-side identifiers, e.g. REC-000001."""
    counter = {"n": start - 1}

    def take() -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']:06d}"

    return take


next_recommendation = seq("REC")
next_approval = seq("APR")
next_attestation = seq("ATT")
next_case = seq("RPR")
next_plan = seq("PLAN")
next_session = seq("SESS")
next_exception = seq("EXC")
next_evidence = seq("EVD")


def odata_date(value: date | None) -> str:
    return "" if value is None else value.strftime("%Y-%m-%dT00:00:00")


def flag(value: bool) -> str:
    return "true" if value else "false"


def qty(value: float) -> str:
    return f"{value:.3f}"


def money(value: float) -> str:
    return f"{value:.2f}"


def matnr_out(value: str) -> str:
    return value.rjust(18, "0") if MATNR_LEADING_ZEROS else value


def month_start(index: int) -> date:
    """First day of month `index`, counting 0 as the oldest in the window."""
    start = AS_OF - timedelta(days=int(HISTORY_MONTHS * 30.4375))
    month = start.month + index
    year = start.year + (month - 1) // 12
    return date(year, (month - 1) % 12 + 1, 1)


def day_in_month(index: int) -> date:
    start = month_start(index)
    end = month_start(index + 1) if index + 1 < HISTORY_MONTHS else AS_OF + timedelta(days=1)
    span = max(1, (end - start).days)
    return min(AS_OF, start + timedelta(days=rng.randrange(span)))


def split_quantity(total: float, parts: int) -> list[float]:
    """Split `total` into `parts` positive-ish shares that sum back to it
    exactly after rounding, for batch stock spread across several Charg."""
    if parts <= 1 or total <= 0:
        return [round(total, 3)]
    cuts = sorted(rng.uniform(0, total) for _ in range(parts - 1))
    edges = [0.0, *cuts, total]
    shares = [round(edges[i + 1] - edges[i], 3) for i in range(parts)]
    shares[-1] = round(total - sum(shares[:-1]), 3)
    return shares


def pick_weighted(weights: dict[str, float]) -> str:
    total = sum(weights.values())
    threshold = rng.random() * total
    cumulative = 0.0
    for key, weight in weights.items():
        cumulative += weight
        if threshold <= cumulative:
            return key
    return list(weights)[-1]


# ===========================================================================
# THE DATASET IN MEMORY
#
# Rows are collected per entity set and written at the end, because a
# requisition only learns its purchase order after conversion and a PO item
# only learns its delivered quantity after the last goods receipt.
# ===========================================================================

rows: dict[str, list[dict]] = {name: [] for name in SAP_COLUMNS}
platform_rows: dict[str, list[dict]] = {name: [] for name in PLATFORM_COLUMNS}


def add(entity: str, row: dict) -> None:
    unknown = set(row) - set(SAP_COLUMNS[entity])
    if unknown:
        raise RuntimeError(f"{entity}: unknown columns {sorted(unknown)}")
    rows[entity].append(row)


def add_platform(name: str, row: dict) -> None:
    unknown = set(row) - set(PLATFORM_COLUMNS[name])
    if unknown:
        raise RuntimeError(f"{name}: unknown columns {sorted(unknown)}")
    platform_rows[name].append(row)


@dataclass
class Vendor:
    lifnr: str
    name: str
    is_repair: bool
    reliability: float


@dataclass
class MaterialPlantRow:
    """One material at one plant: MARC and MARD, plus the running position."""

    matnr: str
    werks: str
    lgort: str
    story: str
    minbe: float          # reorder point
    eisbe: float          # safety stock
    mabst: float          # maximum stock
    dismm: str
    on_hand: float
    # OAR scope for THIS plant, which is what DISMM on MARC actually says.
    # Material.is_oar is the material-level story; this can differ from it for
    # the OAR_SINGLE_PLANT_SHARE slice. Plant-grain logic must use this one.
    is_oar: bool = False
    dispo: str = ""
    quality_stock: float = 0.0
    monthly_demand: list[float] = field(default_factory=list)
    issue_dates: list[date] = field(default_factory=list)
    open_po_qty: float = 0.0
    last_issue_before_window: date | None = None


@dataclass
class Material:
    matnr: str
    description: str
    matkl: str
    mtart: str
    price: float
    lead_time: int
    criticality: str
    is_oar: bool
    is_repairable: bool
    serial_profile: str
    vendor: Vendor
    plants: list[MaterialPlantRow] = field(default_factory=list)


@dataclass
class PoItem:
    """Kept so schedule lines, history and Elikz stay consistent."""

    ebeln: str
    ebelp: str
    matnr: str
    werks: str
    lgort: str
    menge: float
    netpr: float
    eindt: date
    aedat: date
    lifnr: str
    received: float = 0.0


vendors: list[Vendor] = []
materials: list[Material] = []
po_items: list[PoItem] = []


# ===========================================================================
# MASTER DATA
# ===========================================================================

def build_vendors(count: int = 60) -> None:
    for index in range(count):
        is_repair = index % 4 == 0
        tail = rng.choice(VENDOR_REPAIR if is_repair else VENDOR_SUPPLY)
        vendor = Vendor(
            lifnr=vendor_no.take(),
            name=f"{rng.choice(VENDOR_STEMS)} {tail} (Pty) Ltd"[:35],
            is_repair=is_repair,
            reliability=round(rng.uniform(0.55, 0.97), 2),
        )
        vendors.append(vendor)
        add("VendorSet", {
            "Lifnr": vendor.lifnr,
            "Name1": vendor.name,
            "Ktokk": "ZSRV" if is_repair else "ZDOM",
            "Loevm": flag(False),
        })


def describe(matkl: str) -> tuple[str, str]:
    """Return (component, full description). MAKTX is 40 characters."""
    component = rng.choice(CATALOGUE[matkl])
    full = f"{component} {rng.choice(SIZES)} {rng.choice(MAKES)}"
    if len(full) > 40:
        full = f"{component} {rng.choice(SIZES)}"[:40]
    return component, full


def build_materials() -> None:
    for _ in range(MATERIAL_COUNT):
        story = pick_weighted(STORIES)
        matkl = rng.choice(list(CATALOGUE))
        component, description = describe(matkl)

        is_repairable = story == "REPAIRABLE"
        is_oar = story == "OAR"
        if is_repairable:
            matnr = repairable_no.take()
        elif is_oar:
            matnr = oar_no.take()
        elif story == "OBSOLETE":
            matnr = obsolete_no.take()
        else:
            matnr = materials_no.take()

        if story == "UNDERSTOCKED_CRITICAL":
            criticality = "CRITICAL"
        elif story == "OBSOLETE":
            criticality = "OBSOLETE"
        elif story == "LONG_LEAD":
            criticality = rng.choice(["CRITICAL", "INSURANCE"])
        elif is_repairable:
            criticality = rng.choice(["CRITICAL", "IMPACT", "IMPACT", "NORMAL"])
        else:
            criticality = rng.choice(["NORMAL", "NORMAL", "IMPACT", "CRITICAL", "INSURANCE"])

        if story == "LONG_LEAD":
            lead_time = rng.randint(120, 240)
        elif criticality in ("CRITICAL", "INSURANCE"):
            lead_time = rng.randint(30, 150)
        else:
            lead_time = rng.randint(7, 60)

        if is_repairable:
            price = round(rng.uniform(20000, 450000), 2)
        elif criticality == "INSURANCE":
            price = round(rng.uniform(50000, 800000), 2)
        elif story == "HIGH_CONSUMPTION":
            price = round(rng.uniform(300, 25000), 2)
        else:
            price = round(rng.uniform(1500, 180000), 2)

        material = Material(
            matnr=matnr,
            description=description,
            matkl=matkl,
            mtart="ERSA" if rng.random() < 0.85 else "HIBE",
            price=price,
            lead_time=lead_time,
            criticality=criticality,
            is_oar=is_oar,
            is_repairable=is_repairable,
            # About 18% of 80-series lines carry a serial number profile.
            serial_profile=rng.choice(["Z00R", "0001"]) if is_repairable and rng.random() < 0.18 else "",
            vendor=rng.choice([v for v in vendors if v.is_repair == is_repairable] or vendors),
        )

        add("MaterialSet", {
            "Matnr": matnr_out(matnr),
            "Lvorm": flag(False),
            "Mtart": material.mtart,
            "Matkl": matkl,
            "Bismt": f"ELL{rng.randint(100000, 999999)}" if rng.random() < 0.3 else "",
            "Meins": BASE_UOM,
            # MSTAE is how obsolescence is signalled, and the second predicate
            # the OAR scope needs alongside MRP type - see OBSOLETE_MRP_TYPE.
            "Mstae": OBSOLETE_MATERIAL_STATUS if story == "OBSOLETE" else "",
            "Sernp": material.serial_profile,
        })
        add("MaterialDescriptionSet", {
            "Matnr": matnr_out(matnr),
            "Spras": LANGUAGE,
            "Maktx": description,
        })

        chosen = rng.sample(PLANTS, 2 if rng.random() < 0.25 else 1)
        # Decide OAR scope per plant, not per material - see
        # OAR_SINGLE_PLANT_SHARE. A slice of the multi-plant OAR materials is
        # OAR in one plant only, which is the case the scope roll-up policy
        # has to answer for.
        if is_oar and len(chosen) > 1 and rng.random() < OAR_SINGLE_PLANT_SHARE:
            oar_werks = {rng.choice(chosen)["werks"]}
        elif is_oar:
            oar_werks = {p["werks"] for p in chosen}
        else:
            oar_werks = set()
        for plant in chosen:
            material.plants.append(
                build_material_plant(
                    material, plant, story, plant["werks"] in oar_werks
                )
            )

        materials.append(material)


def build_material_plant(
    material: Material, plant: dict, story: str, oar_in_plant: bool
) -> MaterialPlantRow:
    """MARC planning parameters, MARD stock, and the demand series."""
    demand = monthly_demand(story)
    mean = sum(demand) / len(demand) if demand else 0.0
    z = CRITICALITY[material.criticality]["z"]
    months_of_lead = material.lead_time / 30.4375

    # A sound reorder point for this material, then deliberately drifted: the
    # current SAP values are imperfect, which is what I07 recommends against.
    sound_safety = z * spread(demand) * (months_of_lead ** 0.5)
    sound_rop = mean * months_of_lead + sound_safety
    sound_max = sound_rop + mean * 3

    if oar_in_plant:
        # OAR rows carry no maintained ROP or maximum, and the MRP type is what
        # identifies them: one of OAR_MRP_TYPES. This is the 08-Sep 2026 rule.
        # Note the generator now derives DISMM from OAR scope rather than the
        # other way round, so the configured value set is the single place the
        # OAR definition lives.
        eisbe = minbe = mabst = 0.0
        dismm = rng.choice(OAR_MRP_TYPES)
    elif story == "OBSOLETE":
        # ND here overlaps OAR_MRP_TYPES deliberately - obsolete stock has no
        # planning maintained either. MSTAE is what separates the two.
        eisbe = minbe = 0.0
        mabst = round(max(1.0, sound_max * 0.4), 3)
        dismm = OBSOLETE_MRP_TYPE
    else:
        # Outside OAR scope, so this must NOT be one of OAR_MRP_TYPES, or the
        # configured rule would select it.
        drift = rng.uniform(0.4, 1.8)
        eisbe = round(max(0.0, sound_safety * drift), 3)
        minbe = round(max(eisbe, sound_rop * drift), 3)
        mabst = round(max(minbe, sound_max * rng.uniform(0.7, 1.8)), 3)
        dismm = PLANNED_MRP_TYPE

    if story == "UNDERSTOCKED_CRITICAL":
        on_hand = round(max(0.0, minbe * rng.uniform(0.1, 0.5)))
    elif story == "OVERSTOCKED":
        on_hand = round(max(1.0, mabst * rng.uniform(1.4, 3.0)))
    elif oar_in_plant:
        # No standing stock where the material is ordered on demand. A plant
        # outside OAR scope falls through and stocks normally, even for an
        # OAR-story material.
        on_hand = 0.0
    elif story == "OBSOLETE":
        on_hand = round(rng.uniform(5, 40))
    else:
        on_hand = round(max(minbe, mean * rng.uniform(1.5, 4)))

    entry = MaterialPlantRow(
        matnr=material.matnr,
        werks=plant["werks"],
        lgort=plant["lgort"],
        story=story,
        minbe=minbe,
        eisbe=eisbe,
        mabst=mabst,
        dismm=dismm,
        on_hand=float(on_hand),
        is_oar=oar_in_plant,
        monthly_demand=demand,
    )

    # Slow movers and obsolete stock last moved before the loaded window,
    # which is how they look in a 24-month extract.
    if story in ("SLOW_MOVING", "OBSOLETE"):
        entry.last_issue_before_window = AS_OF - timedelta(days=rng.randint(400, 1500))

    entry.dispo = f"{rng.randint(1, 10):03d}"
    add("MaterialPlantSet", {
        "Matnr": matnr_out(material.matnr),
        "Werks": plant["werks"],
        "Lvorm": flag(False),
        "Dismm": dismm,
        "Dispo": entry.dispo,
        # Planned delivery time is populated on about 98% of records.
        "Plifz": qty(material.lead_time if rng.random() < 0.98 else 0),
        "Webaz": qty(rng.randint(0, 3)),
        "Minbe": qty(minbe),
        "Eisbe": qty(eisbe),
        "Bstmi": qty(max(1.0, mean)),
        "Bstma": qty(max(1.0, mean * 6)),
        "Mabst": qty(mabst),
    })
    return entry


def monthly_demand(story: str) -> list[float]:
    """Consumption per month over the window, shaped by the story."""
    months = HISTORY_MONTHS
    if story == "HIGH_CONSUMPTION":
        base = rng.uniform(8, 40)
        return [float(max(1, round(rng.gauss(base, base * 0.25)))) for _ in range(months)]
    if story == "LONG_LEAD":
        base = rng.uniform(0.5, 3)
        return [float(max(1, round(base))) if rng.random() < 0.5 else 0.0 for _ in range(months)]
    if story in ("INTERMITTENT", "UNDERSTOCKED_CRITICAL", "REPAIRABLE", "OAR"):
        base = rng.uniform(1, 4)
        return [float(max(1, round(rng.uniform(0.5, 2) * base))) if rng.random() < 0.4 else 0.0
                for _ in range(months)]
    if story == "OVERSTOCKED":
        base = rng.uniform(0.5, 2)
        return [float(max(1, round(base))) if rng.random() < 0.2 else 0.0 for _ in range(months)]
    if story in ("SLOW_MOVING", "OBSOLETE"):
        return [0.0] * months
    base = rng.uniform(2, 8)
    return [float(max(1, round(rng.gauss(base, base * 0.4)))) if rng.random() < 0.6 else 0.0
            for _ in range(months)]


def spread(series: list[float]) -> float:
    """Standard deviation, used for the safety-stock calculation."""
    if len(series) < 2:
        return 0.0
    mean = sum(series) / len(series)
    return (sum((v - mean) ** 2 for v in series) / (len(series) - 1)) ** 0.5


def build_info_records() -> None:
    """EINA / EINE for the materials that have a regular supplier."""
    for material in materials:
        if rng.random() > 0.3:
            continue
        infnr = info_no.take()
        werks = material.plants[0].werks
        netpr = round(material.price * rng.uniform(0.93, 1.07), 2)
        add("InfoRecordSet", {
            "Infnr": infnr,
            "Matnr": matnr_out(material.matnr),
            "Lifnr": material.vendor.lifnr,
            "Loekz": flag(False),
        })
        add("InfoRecordOrgSet", {
            "Infnr": infnr,
            "Ekorg": PURCHASING_ORG,
            "Esokz": "0",
            "Werks": werks,
            "Loekz": flag(False),
            "Waers": CURRENCY,
            "Aplfz": qty(material.lead_time),
            "Netpr": money(netpr),
            "Peinh": qty(1),
            "Effpr": money(netpr * 1.03),
        })


# ===========================================================================
# DOCUMENTS
# ===========================================================================

def post_movement(
    entry: MaterialPlantRow,
    material: Material,
    bwart: str,
    quantity: float,
    when: date,
    *,
    debit: bool,
    po_item: PoItem | None = None,
    rsnum: str = "",
    rspos: str = "",
    aufnr: str = "",
    kostl: str = "",
    text: str = "",
    lifnr: str = "",
) -> str:
    """Write one material document (MKPF header plus one MSEG item)."""
    mblnr = doc_no.take()
    add("MaterialDocumentHeaderSet", {
        "Mblnr": mblnr,
        "Mjahr": str(when.year),
        "Budat": odata_date(when),
        "Cpudt": odata_date(when),
        "Xblnr": po_item.ebeln if po_item else "",
    })
    add("GoodsMovementItemSet", {
        "Mblnr": mblnr,
        "Mjahr": str(when.year),
        "Zeile": "0001",
        "Bwart": bwart,
        "Matnr": matnr_out(material.matnr),
        "Werks": entry.werks,
        "Lgort": entry.lgort,
        "Lifnr": lifnr or (po_item.lifnr if po_item else ""),
        "Shkzg": "S" if debit else "H",
        "Dmbtr": money(quantity * (po_item.netpr if po_item else material.price)),
        "Menge": qty(quantity),
        "Meins": BASE_UOM,
        "Ebeln": po_item.ebeln if po_item else "",
        "Ebelp": po_item.ebelp if po_item else "",
        "Sgtxt": text,
        "Wempf": rng.choice(REQUESTERS) if not debit else "",
        "Kostl": kostl,
        "Aufnr": aufnr,
        "Rsnum": rsnum,
        "Rspos": rspos,
        "Umwrk": "",
        "Umlgo": "",
        "Grund": "",
        "BudatMkpf": odata_date(when),
        "CpudtMkpf": odata_date(when),
    })
    return mblnr


def create_requisition(
    entry: MaterialPlantRow,
    material: Material,
    quantity: float,
    badat: date,
    text: str,
    *,
    bsart: str = "NB",
    rsnum: str = "",
    lead_days: int | None = None,
) -> dict:
    """Write an EBAN row and return it so the PO can complete it."""
    row = {
        "Banfn": pr_no.take(),
        "Bnfpo": "00010",
        "Bsart": bsart,
        "Loekz": "",
        "Statu": "B",
        "Frgkz": "2",
        "Frgzu": "X",
        "Ekgrp": rng.choice(PURCHASING_GROUPS),
        "Ernam": rng.choice(REQUESTERS),
        "Afnam": rng.choice(REQUESTERS),
        "Txz01": text,
        "Matnr": matnr_out(material.matnr),
        "Werks": entry.werks,
        "Lgort": entry.lgort,
        "Bednr": "",
        "Matkl": material.matkl,
        "Reswk": "",
        "Menge": qty(quantity),
        "Meins": BASE_UOM,
        "Badat": odata_date(badat),
        "Lfdat": odata_date(badat + timedelta(days=lead_days or material.lead_time)),
        "Preis": money(material.price),
        "Peinh": qty(1),
        "Flief": "",
        "Ebeln": "",
        "Ebelp": "",
        "Bsmng": qty(0),
        "Ebakz": flag(False),
        "Rsnum": rsnum,
    }
    add("PurchaseRequisitionSet", row)
    return row


def create_order(
    entry: MaterialPlantRow,
    material: Material,
    quantity: float,
    aedat: date,
    eindt: date,
    text: str,
    *,
    vendor: Vendor | None = None,
    requisition: dict | None = None,
    is_repair: bool = False,
) -> PoItem:
    """Write EKKO, EKPO and EKET, and complete the requisition if given."""
    vendor = vendor or material.vendor
    ebeln = po_no.take()
    ebelp = "00010"
    price = round(material.price * (rng.uniform(0.2, 0.55) if is_repair else 1.0), 2)

    add("PurchaseOrderSet", {
        "Ebeln": ebeln,
        "Bsart": REPAIR_DOC_TYPE if is_repair else rng.choice(["ZDOM", "AN", "WK"]),
        # EKKO.BEDAT is not exposed by CPI; AEDAT is the interim date used for
        # lead-time measurement, pending SAP confirmation.
        "Aedat": odata_date(aedat),
        "Lifnr": vendor.lifnr,
        "Ekorg": PURCHASING_ORG,
        "Ekgrp": rng.choice(PURCHASING_GROUPS),
        "Waers": CURRENCY,
    })
    item = PoItem(
        ebeln=ebeln,
        ebelp=ebelp,
        matnr=material.matnr,
        werks=entry.werks,
        lgort=entry.lgort,
        menge=quantity,
        netpr=price,
        eindt=eindt,
        aedat=aedat,
        lifnr=vendor.lifnr,
    )
    po_items.append(item)

    add("PurchaseOrderItemSet", {
        "Ebeln": ebeln,
        "Ebelp": ebelp,
        "Loekz": "",
        "Txz01": text,
        "Matnr": matnr_out(material.matnr),
        "Werks": entry.werks,
        "Lgort": entry.lgort,
        "Bednr": "",
        "Menge": qty(quantity),
        "Meins": BASE_UOM,
        "Netpr": money(price),
        "Peinh": qty(1),
        "Netwr": money(quantity * price),
        "Elikz": flag(False),          # updated once receipts are known
        "Pstyp": REPAIR_ITEM_CATEGORY if is_repair else "0",
        "Knttp": REPAIR_ACCOUNT_ASSIGNMENT if is_repair else "",
        "Plifz": qty(material.lead_time),
        "Banfn": requisition["Banfn"] if requisition else "",
        "Bnfpo": requisition["Bnfpo"] if requisition else "",
    })
    add("POScheduleLineSet", {
        "Ebeln": ebeln,
        "Ebelp": ebelp,
        "Etenr": "0001",
        "Eindt": odata_date(eindt),
        "Menge": qty(quantity),
        "Wemng": qty(0),               # updated once receipts are known
    })
    if requisition is not None:
        requisition["Ebeln"] = ebeln
        requisition["Ebelp"] = ebelp
        requisition["Bsmng"] = qty(quantity)
        requisition["Ebakz"] = flag(True)
    return item


def receive(item: PoItem, quantity: float, when: date, mblnr: str) -> None:
    """Write the EKBE goods-receipt row and remember the delivered quantity."""
    item.received += quantity
    add("POHistorySet", {
        "Ebeln": item.ebeln,
        "Ebelp": item.ebelp,
        "Zekkn": "01",
        "Vgabe": "1",                  # 1 = goods receipt
        "Gjahr": str(when.year),
        "Belnr": mblnr,
        "Buzei": "0001",
        "Bewtp": "E",
        "Bwart": GR_PO,
        "Budat": odata_date(when),
        "Menge": qty(quantity),
        "Dmbtr": money(quantity * item.netpr),
        "Shkzg": "S",
        "Cpudt": odata_date(when),
        "Matnr": matnr_out(item.matnr),
        "Werks": item.werks,
    })
    if rng.random() < 0.7:
        add("POHistorySet", {
            "Ebeln": item.ebeln,
            "Ebelp": item.ebelp,
            "Zekkn": "01",
            "Vgabe": "2",              # 2 = invoice receipt
            "Gjahr": str(when.year),
            "Belnr": invoice_no.take(),
            "Buzei": "0001",
            "Bewtp": "Q",
            "Bwart": "",
            "Budat": odata_date(min(AS_OF, when + timedelta(days=rng.randint(3, 30)))),
            "Menge": qty(quantity),
            "Dmbtr": money(quantity * item.netpr),
            "Shkzg": "S",
            "Cpudt": odata_date(min(AS_OF, when + timedelta(days=rng.randint(3, 30)))),
            "Matnr": matnr_out(item.matnr),
            "Werks": item.werks,
        })


def create_reservation(
    entry: MaterialPlantRow,
    material: Material,
    quantity: float,
    needed_by: date,
    *,
    session_id: str = "",
    bwart: str = GI_ORDER,
    aufnr: str = "",
    requester: str = "",
) -> dict:
    """Write a RESB row and return it so the chain can complete it."""
    row = {
        "Rsnum": res_no.take(),
        "Rspos": "0001",
        "Xloek": flag(False),
        "Kzear": flag(False),
        "Matnr": matnr_out(material.matnr),
        "Werks": entry.werks,
        "Lgort": entry.lgort,
        "Bdter": odata_date(needed_by),
        "Bdmng": qty(quantity),
        "Meins": BASE_UOM,
        "Enmng": qty(0),
        "Enwrt": money(0),
        "Aufnr": aufnr,
        "Bwart": bwart,
        "Wempf": requester or rng.choice(REQUESTERS),
        "Banfn": "",
        "Bnfpo": "",
        "Zzaisession": session_id,
    }
    add("ReservationItemSet", row)
    return row


def equipment_text(action: str, tag: str | None = None) -> str:
    return f"{action} {tag or rng.choice(EQUIPMENT_TAGS)}"[:40]


# ===========================================================================
# CONSUMPTION AND REPLENISHMENT HISTORY (all initiatives)
# ===========================================================================

def build_history() -> None:
    """Walk each material-plant through the window.

    Stock is the result of opening stock plus receipts minus issues, so the
    positions in MARD reconcile with the movements in MSEG.
    """
    for material in materials:
        for entry in material.plants:
            if entry.is_oar:
                # OAR demand runs through reservation chains. Plant-grain, so a
                # material outside OAR scope in THIS plant still simulates.
                continue
            simulate(material, entry)


def simulate(material: Material, entry: MaterialPlantRow) -> None:
    pending: list[tuple[date, float, PoItem]] = []
    replenishes = entry.minbe > 0 and entry.story != "UNDERSTOCKED_CRITICAL"
    plant = next(p for p in PLANTS if p["werks"] == entry.werks)

    for index, demand in enumerate(entry.monthly_demand):
        when = day_in_month(index)
        apply_arrivals(material, entry, pending, when)

        if demand > 0 and entry.on_hand > 0:
            issued = min(demand, entry.on_hand)
            to_order = rng.random() < 0.65
            post_movement(
                entry, material,
                GI_ORDER if to_order else GI_COST_CENTRE,
                issued, when,
                debit=False,
                aufnr=f"{plant['order']}{rng.randint(1000000, 9999999)}" if to_order else "",
                kostl="" if to_order else plant["cc"],
                text=equipment_text(rng.choice(["ISSUED TO", "BREAKDOWN REPL", "CHANGEOUT"])),
            )
            entry.on_hand -= issued
            entry.issue_dates.append(when)

        if replenishes and entry.on_hand + sum(q for _, q, _ in pending) <= entry.minbe:
            raise_replenishment(material, entry, when, pending)

    apply_arrivals(material, entry, pending, AS_OF)
    for _, quantity, _ in pending:
        entry.open_po_qty += quantity      # still on order at the as-of date


def raise_replenishment(
    material: Material,
    entry: MaterialPlantRow,
    when: date,
    pending: list[tuple[date, float, PoItem]],
) -> None:
    quantity = max(1.0, round(max(entry.mabst, entry.minbe * 1.5) - entry.on_hand))
    text = equipment_text("SPARE FOR")
    requisition = create_requisition(entry, material, quantity, when, text)
    aedat = min(AS_OF, when + timedelta(days=rng.randint(1, 12)))
    eindt = aedat + timedelta(days=material.lead_time)
    item = create_order(entry, material, quantity, aedat, eindt, text, requisition=requisition)

    if rng.random() < 0.2:
        return                             # left open on purpose

    # Less reliable vendors deliver late more often.
    late = rng.random() > material.vendor.reliability
    arrival = eindt + timedelta(days=rng.randint(2, 40) if late else -rng.randint(0, 4))
    delivered = quantity if rng.random() > 0.15 else max(1.0, round(quantity * rng.uniform(0.4, 0.8)))
    pending.append((arrival, delivered, item))
    pending.sort(key=lambda triple: triple[0])


def apply_arrivals(
    material: Material,
    entry: MaterialPlantRow,
    pending: list[tuple[date, float, PoItem]],
    until: date,
) -> None:
    while pending and pending[0][0] <= until:
        arrival, quantity, item = pending.pop(0)
        arrival = max(arrival, item.aedat + timedelta(days=1))
        mblnr = post_movement(
            entry, material, GR_PO, quantity, arrival,
            debit=True, po_item=item, text="GR AGAINST PO",
        )
        receive(item, quantity, arrival, mblnr)
        entry.on_hand += quantity


# ===========================================================================
# INITIATIVE 13 - OAR reservation to goods issue chains
# ===========================================================================

PURPOSES = [
    "Planned shutdown replacement", "Breakdown replacement",
    "Condition monitoring intervention", "Statutory inspection rebuild",
    "Capital project spare", "Reliability improvement upgrade",
]

# Each OAR chain gets one of these outcomes.
OAR_OUTCOMES = {
    "RECEIVED_AND_USED":   0.34,
    "RECEIVED_NOT_USED":   0.20,
    "USE_OVERDUE":         0.16,
    "PARTIAL_RECEIPT":     0.10,
    "PARTIAL_ISSUE":       0.08,
    "OPEN_RESERVATION":    0.07,   # reservation and PR only, no PO yet
    "NO_CONSUMPTION_PLAN": 0.05,   # no assistant session, so no plan
}


def build_oar_chains() -> None:
    for material in materials:
        if not material.is_oar:
            continue
        # Plant-grain: only the plants actually in OAR scope get chains.
        for entry in (p for p in material.plants if p.is_oar):
            for _ in range(rng.randint(1, 3)):
                build_oar_chain(material, entry)


def build_oar_chain(material: Material, entry: MaterialPlantRow) -> None:
    outcome = pick_weighted(OAR_OUTCOMES)
    plant = next(p for p in PLANTS if p["werks"] == entry.werks)
    requester = rng.choice(REQUESTERS)
    quantity = float(rng.randint(1, 4))

    # The reservation is old enough for the whole chain to have played out.
    age = material.lead_time + rng.randint(30, 320)
    reserved_on = AS_OF - timedelta(days=min(age, HISTORY_MONTHS * 30))
    aufnr = f"{plant['order']}{rng.randint(1000000, 9999999)}"

    # The consumption plan is captured by the reservation-time assistant, which
    # issues the session identifier the requester puts on the reservation.
    has_plan = outcome != "NO_CONSUMPTION_PLAN"
    session_id = next_session() if has_plan else ""
    reservation = create_reservation(
        entry, material, quantity, reserved_on + timedelta(days=rng.randint(10, 60)),
        session_id=session_id, aufnr=aufnr, requester=requester,
    )

    text = equipment_text("OAR SPARE FOR")
    pr_on = min(AS_OF, reserved_on + timedelta(days=rng.randint(0, 5)))
    requisition = create_requisition(
        entry, material, quantity, pr_on, text, rsnum=reservation["Rsnum"],
    )
    reservation["Banfn"] = requisition["Banfn"]
    reservation["Bnfpo"] = requisition["Bnfpo"]

    planned_use = reserved_on + timedelta(days=material.lead_time + rng.randint(5, 45))
    plan_id = ""
    if has_plan:
        plan_id = next_plan()
        add_platform("consumption_plans", {
            "plan_id": plan_id,
            "session_id": session_id,
            "Rsnum": reservation["Rsnum"],
            "Rspos": reservation["Rspos"],
            "Matnr": matnr_out(material.matnr),
            "Werks": entry.werks,
            "requester": requester,
            "purpose": rng.choice(PURPOSES),
            "planned_quantity": qty(quantity),
            "planned_use_date": planned_use.isoformat(),
            "status": "OPEN" if outcome in ("RECEIVED_NOT_USED", "USE_OVERDUE") else "CLOSED",
        })

    if outcome == "OPEN_RESERVATION":
        utilisation(reservation, material, entry, "AWAITING_PURCHASE_ORDER", None, None)
        return

    # The order follows the requisition, never precedes it.
    aedat = min(AS_OF, pr_on + timedelta(days=rng.randint(1, 14)))
    eindt = aedat + timedelta(days=material.lead_time)
    item = create_order(entry, material, quantity, aedat, eindt, text, requisition=requisition)

    received = quantity
    if outcome == "PARTIAL_RECEIPT":
        received = max(1.0, round(quantity * rng.uniform(0.4, 0.7)))
    gr_on = min(AS_OF, max(eindt + timedelta(days=rng.randint(-3, 25)), aedat + timedelta(days=1)))
    mblnr = post_movement(
        entry, material, GR_PO, received, gr_on,
        debit=True, po_item=item, text="GR AGAINST OAR PO",
    )
    receive(item, received, gr_on, mblnr)
    entry.on_hand += received

    if outcome in ("RECEIVED_NOT_USED", "USE_OVERDUE"):
        # Received into stock and not issued. USE_OVERDUE has also passed its
        # planned use date plus the grace period.
        status = "OVERDUE_USE" if outcome == "USE_OVERDUE" else "AWAITING_USE"
        if outcome == "USE_OVERDUE":
            planned_use = AS_OF - timedelta(days=PLAN_GRACE_DAYS + rng.randint(5, 120))
            for plan in platform_rows["consumption_plans"]:
                if plan["plan_id"] == plan_id:
                    plan["planned_use_date"] = planned_use.isoformat()
            raise_exception(
                "OVERDUE_USE", material, entry,
                f"Rsnum {reservation['Rsnum']}",
                f"Received on {gr_on} against a planned use date of {planned_use}; "
                f"no goods issue after {PLAN_GRACE_DAYS} days grace.",
            )
        utilisation(reservation, material, entry, status, None, planned_use)
        return

    issued = received if outcome != "PARTIAL_ISSUE" else max(1.0, round(received * 0.5))
    gi_on = min(AS_OF, gr_on + timedelta(days=rng.randint(1, 60)))
    post_movement(
        entry, material, GI_ORDER, issued, gi_on,
        debit=False, rsnum=reservation["Rsnum"], rspos=reservation["Rspos"],
        aufnr=aufnr, text=equipment_text("ISSUED TO"),
    )
    entry.on_hand -= issued
    entry.issue_dates.append(gi_on)
    reservation["Enmng"] = qty(issued)
    reservation["Enwrt"] = money(issued * material.price)
    reservation["Kzear"] = flag(outcome != "PARTIAL_ISSUE")

    if not has_plan:
        raise_exception(
            "NO_CONSUMPTION_PLAN", material, entry,
            f"Rsnum {reservation['Rsnum']}",
            "OAR reservation carries no assistant session identifier, so no "
            "consumption plan was captured.",
        )
    utilisation(
        reservation, material, entry,
        "PARTIALLY_USED" if outcome == "PARTIAL_ISSUE" else "USED",
        gi_on, planned_use if has_plan else None,
    )


def utilisation(
    reservation: dict,
    material: Material,
    entry: MaterialPlantRow,
    status: str,
    used_on: date | None,
    planned_use: date | None,
) -> None:
    """What happened to the spare after receipt - which SAP does not track."""
    reason = ""
    replanned = ""
    if status == "OVERDUE_USE":
        reason = rng.choice([
            "SHUTDOWN_DEFERRED_TO_NEXT_WINDOW",
            "EQUIPMENT_STILL_IN_SERVICE",
            "SCOPE_CANCELLED_UNIT_HELD_AS_SPARE",
        ])
        if planned_use and rng.random() < 0.5:
            replanned = (planned_use + timedelta(days=rng.randint(30, 120))).isoformat()
    add_platform("utilisation_status", {
        "Rsnum": reservation["Rsnum"],
        "Rspos": reservation["Rspos"],
        "Matnr": matnr_out(material.matnr),
        "Werks": entry.werks,
        "status": status,
        "confirmed_used": flag(status in ("USED", "PARTIALLY_USED")),
        "confirmation_date": used_on.isoformat() if used_on else "",
        "replanned_date": replanned,
        "reason": reason,
    })


# ===========================================================================
# INITIATIVE 08 - 80-series repair lifecycle
# ===========================================================================

REPAIR_OUTCOMES = {
    "IN_REPAIR":            0.22,   # away at the vendor, due back later
    "EXPECTED_BACK":        0.12,   # due back within the grace period
    "OVERDUE":              0.18,   # past the promised date plus grace
    "RETURNED":             0.14,   # received back, still in inspection
    "BACK_IN_STOCK":        0.20,   # received and back in unrestricted stock
    "NEW_PURCHASE_INSTEAD": 0.14,   # bought new while a unit is in repair
}


def build_repairs() -> None:
    for material in materials:
        if not material.is_repairable:
            continue
        for entry in material.plants:
            if entry.on_hand < 1:
                continue
            build_repair_case(material, entry)


def build_repair_case(material: Material, entry: MaterialPlantRow) -> None:
    outcome = pick_weighted(REPAIR_OUTCOMES)
    quantity = 1.0
    turnaround = rng.randint(21, 95)
    plant = next(p for p in PLANTS if p["werks"] == entry.werks)
    vendor = rng.choice([v for v in vendors if v.is_repair])
    tag = rng.choice(EQUIPMENT_TAGS)   # the same unit throughout this case

    # Work backwards from the promised return date, because that is what
    # defines each stage relative to today.
    if outcome == "IN_REPAIR":
        eindt = AS_OF + timedelta(days=rng.randint(OVERDUE_GRACE_DAYS + 5, turnaround))
    elif outcome == "EXPECTED_BACK":
        eindt = AS_OF + timedelta(days=rng.randint(0, OVERDUE_GRACE_DAYS))
    elif outcome == "OVERDUE":
        eindt = AS_OF - timedelta(days=OVERDUE_GRACE_DAYS + rng.randint(3, 90))
    else:
        eindt = AS_OF - timedelta(days=rng.randint(10, 240))

    po_on = eindt - timedelta(days=turnaround)
    pr_on = po_on - timedelta(days=rng.randint(1, 8))
    removed_on = pr_on - timedelta(days=rng.randint(1, 6))

    # The reservation-time assistant session, and the condition-to-repair
    # attestation the workshop completes before the unit is dispatched.
    session_id = next_session()
    create_reservation(
        entry, material, quantity, removed_on + timedelta(days=rng.randint(1, 20)),
        session_id=session_id, bwart=REPAIR_REMOVAL,
        aufnr=f"{plant['order']}{rng.randint(1000000, 9999999)}",
    )

    if outcome == "NEW_PURCHASE_INSTEAD":
        build_new_purchase_of_repairable(material, entry, session_id, quantity, tag)
        return

    # 541: stock moves to the subcontractor for repair.
    post_movement(
        entry, material, REPAIR_REMOVAL, quantity, removed_on,
        debit=False, text=equipment_text("REMOVED FOR REPAIR EX", tag),
    )
    entry.on_hand -= quantity

    attested = rng.random() < 0.8      # some dispatches skip the attestation
    attestation_id = ""
    if attested:
        attestation_id = next_attestation()
        add_platform("repair_attestations", {
            "attestation_id": attestation_id,
            "session_id": session_id,
            "Matnr": matnr_out(material.matnr),
            "Werks": entry.werks,
            "quantity": qty(quantity),
            "condition": rng.choice(CONDITIONS),
            "fault_category": rng.choice(FAULTS),
            "repairable": flag(True),
            "reason": "Economically repairable; vendor repair recommended.",
            "user": rng.choice(ATTESTORS),
            "timestamp": (removed_on + timedelta(days=rng.randint(0, 3))).isoformat(),
            "evidence_reference": next_evidence(),
        })

    text = equipment_text("REPAIR OF UNIT EX", tag)
    requisition = create_requisition(
        entry, material, quantity, pr_on, text,
        bsart=REPAIR_DOC_TYPE, lead_days=turnaround,
    )
    item = create_order(
        entry, material, quantity, po_on, eindt, text,
        vendor=vendor, requisition=requisition, is_repair=True,
    )

    returned_on = None
    if outcome in ("RETURNED", "BACK_IN_STOCK"):
        returned_on = min(AS_OF, eindt + timedelta(days=rng.randint(0, 25)))
        mblnr = post_movement(
            entry, material, GR_PO, quantity, returned_on,
            debit=True, po_item=item, lifnr=vendor.lifnr,
            text="REPAIRED UNIT RETURNED EX VENDOR",
        )
        receive(item, quantity, returned_on, mblnr)
        if outcome == "BACK_IN_STOCK":
            entry.on_hand += quantity
        else:
            # Received but held in quality inspection, so unrestricted stock
            # has not moved yet.
            entry.quality_stock += quantity

    overdue = outcome == "OVERDUE"
    if overdue:
        raise_exception(
            "OVERDUE_REPAIR", material, entry, f"PO {item.ebeln}/{item.ebelp}",
            f"Promised back on {eindt}, which is "
            f"{(AS_OF - eindt).days} days ago, beyond the {OVERDUE_GRACE_DAYS}-day grace period.",
        )
    if not attested:
        raise_exception(
            "MISSING_ATTESTATION", material, entry, f"PO {item.ebeln}/{item.ebelp}",
            "Repair purchase order raised with no condition-to-repair attestation.",
        )

    add_platform("repair_cases", {
        "case_id": next_case(),
        "Matnr": matnr_out(material.matnr),
        "Werks": entry.werks,
        "repair_pr": requisition["Banfn"],
        "repair_po": item.ebeln,
        "repair_po_item": item.ebelp,
        "session_id": session_id,
        "attestation_id": attestation_id,
        "stage": outcome,
        "removed_on": removed_on.isoformat(),
        "expected_back_on": eindt.isoformat(),
        "returned_on": returned_on.isoformat() if returned_on else "",
        "overdue": flag(overdue),
        "vendor_turnaround_days": (returned_on - po_on).days if returned_on else "",
        "notes": f"Repair vendor {vendor.name}.",
    })


def build_new_purchase_of_repairable(
    material: Material, entry: MaterialPlantRow, session_id: str, quantity: float, tag: str
) -> None:
    """A new unit bought while a repairable one is on hand.

    This is the population the reservation-time assistant challenges: the line
    is on a normal document type with item category 0, so it does not match
    the repair-PO convention.
    """
    when = AS_OF - timedelta(days=rng.randint(20, 300))
    text = equipment_text("NEW UNIT FOR", tag)
    requisition = create_requisition(entry, material, quantity, when, text)
    aedat = min(AS_OF, when + timedelta(days=rng.randint(2, 15)))
    item = create_order(
        entry, material, quantity, aedat,
        aedat + timedelta(days=material.lead_time), text, requisition=requisition,
    )
    add_platform("repair_cases", {
        "case_id": next_case(),
        "Matnr": matnr_out(material.matnr),
        "Werks": entry.werks,
        "repair_pr": "",
        "repair_po": "",
        "repair_po_item": "",
        "session_id": session_id,
        "attestation_id": "",
        "stage": "NEW_PURCHASE_INSTEAD",
        "removed_on": "",
        "expected_back_on": "",
        "returned_on": "",
        "overdue": flag(False),
        "vendor_turnaround_days": "",
        "notes": f"New purchase on {item.ebeln} while a repairable unit was on hand.",
    })


# ===========================================================================
# INITIATIVE 07 - inventory recommendations
# ===========================================================================

PATTERN_NAMES = {
    "HIGH_CONSUMPTION": "REGULAR",
    "INTERMITTENT": "INTERMITTENT",
    "UNDERSTOCKED_CRITICAL": "INTERMITTENT",
    "REPAIRABLE": "INTERMITTENT",
    "LONG_LEAD": "INTERMITTENT",
    "OVERSTOCKED": "SLOW",
    "SLOW_MOVING": "SLOW",
    "OBSOLETE": "NO_DEMAND",
    "OAR": "ON_DEMAND",
}


def build_recommendations() -> None:
    """Recommended ROP, safety stock and maximum, per material and plant.

    Platform-side only: nothing is written back to SAP. The current values in
    MaterialPlantSet stay untouched, and adoption is observed separately from
    the change documents.
    """
    for material in materials:
        for entry in material.plants:
            consumptions = len([d for d in entry.issue_dates if (AS_OF - d).days <= 365])

            if material.criticality == "OBSOLETE":
                add_recommendation(
                    material, entry, consumptions, "NO_SAFETY_STOCK",
                    None, None, None,
                    "Obsolete material: no safety stock recommended. Candidate "
                    "for stock reduction review.",
                )
                continue

            if material.is_oar:
                # More than four consumptions in the trailing twelve months is
                # the SOP indicator for moving an OAR material to Min-Max.
                if consumptions > 4 or material.criticality in ("CRITICAL", "IMPACT"):
                    mean = max(0.5, sum(entry.monthly_demand) / HISTORY_MONTHS or 0.5)
                    rop, safety, maximum = compute(material, entry, mean)
                    trigger = (
                        f"was consumed {consumptions} times in the last 12 months, "
                        f"above the threshold of four"
                        if consumptions > 4
                        else f"is classified {material.criticality} on the critical parts list"
                    )
                    add_recommendation(
                        material, entry, consumptions, "OAR_TO_MIN_MAX",
                        rop, safety, maximum,
                        f"This OAR material {trigger}. Recommend Min-Max management "
                        f"with a reorder point of {rop:g} and a maximum of {maximum:g}.",
                    )
                continue

            if consumptions == 0 and not entry.issue_dates:
                continue      # no usable history, so no recommendation

            mean = sum(entry.monthly_demand) / HISTORY_MONTHS
            rop, safety, maximum = compute(material, entry, mean)
            if entry.on_hand < entry.minbe:
                reason = (
                    f"Stock on hand {entry.on_hand:g} is below the current reorder point "
                    f"{entry.minbe:g}. At the {material.criticality} service level of "
                    f"{CRITICALITY[material.criticality]['service_level']:.0%} and a "
                    f"{material.lead_time}-day lead time, the reorder point should be {rop:g}."
                )
            elif entry.mabst and entry.on_hand > entry.mabst:
                reason = (
                    f"Stock on hand {entry.on_hand:g} exceeds the current maximum "
                    f"{entry.mabst:g}. Consumption of {mean:.1f} per month supports a "
                    f"maximum of {maximum:g}."
                )
            else:
                reason = (
                    f"{PATTERN_NAMES.get(entry.story, 'REGULAR').capitalize()} consumption of "
                    f"{mean:.1f} per month over {HISTORY_MONTHS} months with a "
                    f"{material.lead_time}-day lead time gives a reorder point of {rop:g} "
                    f"against the current {entry.minbe:g}."
                )
            add_recommendation(
                material, entry, consumptions, "ROP_MAX_SAFETY_STOCK",
                rop, safety, maximum, reason,
            )


def compute(material: Material, entry: MaterialPlantRow, mean: float) -> tuple[float, float, float]:
    """Safety stock = z x sigma over the lead time; ROP = demand + safety."""
    z = CRITICALITY[material.criticality]["z"]
    months_of_lead = material.lead_time / 30.4375
    safety = round(z * spread(entry.monthly_demand) * (months_of_lead ** 0.5), 1)
    rop = round(mean * months_of_lead + safety, 1)
    maximum = round(rop + mean * 3, 1)
    return rop, safety, max(maximum, rop)


def add_recommendation(
    material: Material,
    entry: MaterialPlantRow,
    consumptions: int,
    kind: str,
    rop: float | None,
    safety: float | None,
    maximum: float | None,
    reason: str,
) -> None:
    recommendation_id = next_recommendation()
    created = AS_OF - timedelta(days=rng.randint(5, 120))
    status = pick_weighted({"APPROVED": 0.4, "PROPOSED": 0.3, "REJECTED": 0.18, "HELD": 0.12})

    add_platform("inventory_recommendations", {
        "recommendation_id": recommendation_id,
        "Matnr": matnr_out(material.matnr),
        "Werks": entry.werks,
        "material_description": material.description,
        "criticality": material.criticality,
        "demand_pattern": PATTERN_NAMES.get(entry.story, "REGULAR"),
        "consumptions_12m": consumptions,
        "lead_time_days": material.lead_time,
        "current_rop": qty(entry.minbe),
        "recommended_rop": "" if rop is None else qty(rop),
        "current_safety_stock": qty(entry.eisbe),
        "recommended_safety_stock": "" if safety is None else qty(safety),
        "current_max_stock": qty(entry.mabst),
        "recommended_max_stock": "" if maximum is None else qty(maximum),
        "recommendation_type": kind,
        "reason": reason,
        "status": status,
        "created_on": created.isoformat(),
    })

    if status in ("APPROVED", "REJECTED", "HELD"):
        role = {
            "CRITICAL": "Plant Head",
            "IMPACT": "Engineering Head",
            "INSURANCE": "Commercial Head",
        }.get(material.criticality, "Inventory Controller")
        decided = min(AS_OF, created + timedelta(days=rng.randint(1, 20)))
        add_platform("approvals", {
            "approval_id": next_approval(),
            "recommendation_id": recommendation_id,
            "approver_role": role,
            "approver_user": rng.choice(CONTROLLERS),
            "decision": status,
            "decision_date": decided.isoformat(),
            "comment": {
                "APPROVED": "Approved; to be applied in SAP by VZI.",
                "REJECTED": "Consumption not considered representative.",
                "HELD": "Held pending further consumption evidence.",
            }[status],
        })

        # Where an approved change was actually made in SAP, a change document
        # records it. This is how adoption is observed - read-only, never by
        # writing back.
        if status == "APPROVED" and rop is not None and rng.random() < 0.6:
            record_marc_change(material, entry, decided, rop, maximum)


def record_marc_change(
    material: Material,
    entry: MaterialPlantRow,
    when: date,
    rop: float,
    maximum: float | None,
) -> None:
    changenr = change_no.take()
    add("ChangeDocHeaderSet", {
        "Objectclas": "MATERIAL",
        "Objectid": matnr_out(material.matnr),
        "Changenr": changenr,
        "Username": rng.choice(CONTROLLERS),
        "Udate": odata_date(when),
        "Utime": f"{rng.randint(7, 17):02d}:{rng.randint(0, 59):02d}:00",
        "Tcode": "MM02",
    })
    for fname, old, new in (
        ("MINBE", entry.minbe, rop),
        ("MABST", entry.mabst, maximum),
    ):
        if new is None:
            continue
        add("ChangeDocItemSet", {
            "Objectclas": "MATERIAL",
            "Objectid": matnr_out(material.matnr),
            "Changenr": changenr,
            "Tabname": "MARC",
            "Tabkey": f"{matnr_out(material.matnr)}{entry.werks}",
            "Fname": fname,
            "Chngind": "U",
            "Value_old": qty(old),
            "Value_new": qty(new),
        })


# ===========================================================================
# EXCEPTIONS (platform)
# ===========================================================================

def raise_exception(
    kind: str,
    material: Material,
    entry: MaterialPlantRow,
    reference: str,
    detail: str,
) -> None:
    add_platform("exceptions", {
        "exception_id": next_exception(),
        "exception_type": kind,
        "Matnr": matnr_out(material.matnr),
        "Werks": entry.werks,
        "object_reference": reference,
        "detected_on": (AS_OF - timedelta(days=rng.randint(0, 10))).isoformat(),
        "owner": rng.choice(CONTROLLERS),
        "status": pick_weighted({"OPEN": 0.5, "AWAITING_REQUESTER": 0.25, "CLOSED": 0.25}),
        "detail": detail,
    })


# ===========================================================================
# CLOSING POSITIONS
# ===========================================================================

def write_stock_and_valuation() -> None:
    """MARD and MBEW, written last so they match the movements."""
    for material in materials:
        for entry in material.plants:
            on_hand = round(max(0.0, entry.on_hand), 3)
            add("StorageLocationStockSet", {
                "Matnr": matnr_out(material.matnr),
                "Werks": entry.werks,
                "Lgort": entry.lgort,
                "Labst": qty(on_hand),
                "Insme": qty(entry.quality_stock),
                "Speme": qty(0),
                "Lgpbe": f"{rng.choice('ABCDE')}-{rng.randint(1, 24):02d}-{rng.randint(1, 6)}",
            })
            add("MaterialValuationSet", {
                "Matnr": matnr_out(material.matnr),
                "Bwkey": entry.werks,
                "Lbkum": qty(on_hand + entry.quality_stock),
                "Salk3": money((on_hand + entry.quality_stock) * material.price),
                "Vprsv": "V",
                "Verpr": money(material.price),
                "Stprs": money(0),
                "Peinh": qty(1),
                "Bklas": "3200",
            })


def build_batch_stock() -> None:
    """LQUA: split each material/plant's closing stock (Labst, written just
    above) across one or more batches, so Clabs always reconciles back to it.
    A separate pass after write_stock_and_valuation so its random batch counts
    and splits can't shift the rng stream write_stock_and_valuation itself
    still depends on (e.g. Lgpbe) for entries processed after this one.
    """
    for material in materials:
        for entry in material.plants:
            on_hand = round(max(0.0, entry.on_hand), 3)
            if on_hand <= 0:
                continue
            batch_count = 1 if on_hand < 5 else rng.choices(
                [1, 2, 3, 4], weights=[45, 30, 17, 8]
            )[0]
            for share in split_quantity(on_hand, batch_count):
                if share <= 0:
                    continue
                add("BatchStockSet", {
                    "Matnr": matnr_out(material.matnr),
                    "Werks": entry.werks,
                    "Lgort": entry.lgort,
                    "Charg": batch_no.take(),
                    "Clabs": qty(share),
                })


def build_movement_statistics() -> None:
    """MVER-style rollup per material/plant, derived from the movements
    already posted (GoodsMovementItemSet) rather than resimulated, so the
    dates agree with what build_history/build_oar_chains/build_repairs wrote.
    """
    last_receipt: dict[tuple[str, str, str], str] = {}
    last_issue: dict[tuple[str, str, str], str] = {}
    for row in rows["GoodsMovementItemSet"]:
        key = (row["Matnr"], row["Werks"], row["Lgort"])
        when = row["BudatMkpf"]
        bucket = last_receipt if row["Shkzg"] == "S" else last_issue
        if when > bucket.get(key, ""):
            bucket[key] = when

    for material in materials:
        for entry in material.plants:
            key = (matnr_out(material.matnr), entry.werks, entry.lgort)
            zugang = last_receipt.get(key, "")
            # SLOW_MOVING/OBSOLETE stock has no in-window movement at all, so
            # fall back to the pre-window date that gives those stories their
            # aged look elsewhere (see last_issue_before_window above).
            abgang = last_issue.get(key) or (
                odata_date(entry.last_issue_before_window)
                if entry.last_issue_before_window else ""
            )
            # No separate "last consumption" signal exists in this dataset;
            # for VZI spares, consumption and goods issue are the same event.
            verbrauch = abgang
            bewegung = max(zugang, abgang)
            on_hand = round(max(0.0, entry.on_hand), 3)
            add("StockMovementStatisticSet", {
                "Werks": entry.werks,
                "Lgort": entry.lgort,
                "Matnr": matnr_out(material.matnr),
                "Dispo": entry.dispo,
                "Mtart": material.mtart,
                "Matkl": material.matkl,
                "Dismm": entry.dismm,
                "Mbwbest": qty(on_hand + entry.quality_stock),
                "Wbwbest": money((on_hand + entry.quality_stock) * material.price),
                "Letztzug": zugang,
                "Letztabg": abgang,
                "Letztver": verbrauch,
                "Letztbew": bewegung,
                "Eisbe": qty(entry.eisbe),
            })


def finalise_po_items() -> None:
    """Fill in the delivered quantity and the delivery-completed flag."""
    delivered = {(item.ebeln, item.ebelp): item.received for item in po_items}
    ordered = {(item.ebeln, item.ebelp): item.menge for item in po_items}
    for row in rows["POScheduleLineSet"]:
        row["Wemng"] = qty(delivered.get((row["Ebeln"], row["Ebelp"]), 0.0))
    for row in rows["PurchaseOrderItemSet"]:
        key = (row["Ebeln"], row["Ebelp"])
        row["Elikz"] = flag(delivered.get(key, 0.0) >= ordered.get(key, 0.0) - 1e-9)


# ===========================================================================
# BASIC CONSISTENCY CHECKS
#
# Enough to catch a broken chain. Anything wrong raises, rather than being
# written to a report.
# ===========================================================================

def check() -> None:
    problems: list[str] = []

    def keys(entity: str, *columns: str) -> set:
        return {tuple(row[c] for c in columns) for row in rows[entity]}

    material_keys = keys("MaterialSet", "Matnr")
    plant_keys = keys("MaterialPlantSet", "Matnr", "Werks")
    order_keys = keys("PurchaseOrderSet", "Ebeln")
    item_keys = keys("PurchaseOrderItemSet", "Ebeln", "Ebelp")
    requisition_keys = keys("PurchaseRequisitionSet", "Banfn")
    document_keys = keys("MaterialDocumentHeaderSet", "Mblnr", "Mjahr")
    reservation_keys = keys("ReservationItemSet", "Rsnum")
    plant_codes = {plant["werks"] for plant in PLANTS}

    # Every discovered entity set must produce at least one row, unless
    # counts.csv itself reports 0 live rows for it (EXPECTED_EMPTY_SETS). This
    # is what catches a set whose schema is now live but whose generator was
    # never written - the exact gap BatchStockSet and StockMovementStatisticSet
    # sat in silently before.
    for entity in SAP_COLUMNS:
        if entity not in EXPECTED_EMPTY_SETS and not rows[entity]:
            problems.append(f"{entity}: no rows generated")

    # Primary keys must be unique. The key fields come from discovery, so a
    # changed key is picked up here rather than silently ignored.
    for entity, key_fields in SAP_KEYS.items():
        # EBAN's OData key is BANFN alone even though BNFPO is exposed, so the
        # business key is used where it is wider than the declared one.
        if entity == "PurchaseRequisitionSet":
            key_fields = ["Banfn", "Bnfpo"]
        # CDPOS's OData key omits Tabname/Tabkey/Fname even though a single
        # change document (Changenr) covers multiple fields/tables, so the
        # business key is used where it is wider than the declared one.
        if entity == "ChangeDocItemSet":
            key_fields = ["Objectclas", "Objectid", "Changenr", "Tabname", "Tabkey", "Fname"]
        seen: set = set()
        for row in rows[entity]:
            key = tuple(row[c] for c in key_fields)
            if key in seen:
                problems.append(f"{entity}: duplicate key {key_fields}={key}")
            seen.add(key)

    # References must resolve.
    for entity, columns, parent, parent_name in (
        ("MaterialDescriptionSet", ("Matnr",), material_keys, "MaterialSet"),
        ("MaterialPlantSet", ("Matnr",), material_keys, "MaterialSet"),
        ("StorageLocationStockSet", ("Matnr", "Werks"), plant_keys, "MaterialPlantSet"),
        ("MaterialValuationSet", ("Matnr",), material_keys, "MaterialSet"),
        ("ReservationItemSet", ("Matnr",), material_keys, "MaterialSet"),
        ("PurchaseRequisitionSet", ("Matnr",), material_keys, "MaterialSet"),
        ("PurchaseOrderItemSet", ("Ebeln",), order_keys, "PurchaseOrderSet"),
        ("PurchaseOrderItemSet", ("Matnr",), material_keys, "MaterialSet"),
        ("POScheduleLineSet", ("Ebeln", "Ebelp"), item_keys, "PurchaseOrderItemSet"),
        ("POHistorySet", ("Ebeln", "Ebelp"), item_keys, "PurchaseOrderItemSet"),
        ("GoodsMovementItemSet", ("Mblnr", "Mjahr"), document_keys, "MaterialDocumentHeaderSet"),
        ("GoodsMovementItemSet", ("Matnr",), material_keys, "MaterialSet"),
        ("ChangeDocItemSet", ("Objectclas", "Objectid", "Changenr"),
         keys("ChangeDocHeaderSet", "Objectclas", "Objectid", "Changenr"), "ChangeDocHeaderSet"),
        ("BatchStockSet", ("Matnr", "Werks"), plant_keys, "MaterialPlantSet"),
        ("StockMovementStatisticSet", ("Matnr", "Werks"), plant_keys, "MaterialPlantSet"),
    ):
        for row in rows[entity]:
            key = tuple(row[c] for c in columns)
            if key not in parent:
                problems.append(f"{entity}: {columns}={key} is not in {parent_name}")

    # Optional references, present only where the chain got that far.
    for entity, column, parent, parent_name in (
        ("PurchaseOrderItemSet", "Banfn", requisition_keys, "PurchaseRequisitionSet"),
        ("PurchaseRequisitionSet", "Ebeln", order_keys, "PurchaseOrderSet"),
        ("PurchaseRequisitionSet", "Rsnum", reservation_keys, "ReservationItemSet"),
        ("ReservationItemSet", "Banfn", requisition_keys, "PurchaseRequisitionSet"),
        ("GoodsMovementItemSet", "Ebeln", order_keys, "PurchaseOrderSet"),
        ("GoodsMovementItemSet", "Rsnum", reservation_keys, "ReservationItemSet"),
    ):
        for row in rows[entity]:
            if row[column] and (row[column],) not in parent:
                problems.append(f"{entity}: {column}={row[column]} is not in {parent_name}")

    # Every plant must be a real plant.
    for entity in ("MaterialPlantSet", "StorageLocationStockSet", "PurchaseOrderItemSet",
                   "GoodsMovementItemSet", "ReservationItemSet", "PurchaseRequisitionSet",
                   "BatchStockSet", "StockMovementStatisticSet"):
        for row in rows[entity]:
            if row["Werks"] not in plant_codes:
                problems.append(f"{entity}: unknown plant {row['Werks']}")

    # Batch stock must add back up to the unrestricted-use stock it was split
    # from - Clabs is a breakdown of Labst by Charg, not an independent figure.
    batch_totals: dict[tuple[str, str, str], float] = {}
    for row in rows["BatchStockSet"]:
        key = (row["Matnr"], row["Werks"], row["Lgort"])
        batch_totals[key] = batch_totals.get(key, 0.0) + float(row["Clabs"])
    for row in rows["StorageLocationStockSet"]:
        key = (row["Matnr"], row["Werks"], row["Lgort"])
        labst = float(row["Labst"])
        total = batch_totals.get(key, 0.0)
        if abs(total - labst) > 0.01:
            problems.append(
                f"BatchStockSet {key}: batches sum to {total:g}, Labst is {labst:g}"
            )

    # Dates: PR date <= PO date <= goods receipt date.
    pr_dates = {row["Banfn"]: row["Badat"] for row in rows["PurchaseRequisitionSet"]}
    po_dates = {row["Ebeln"]: row["Aedat"] for row in rows["PurchaseOrderSet"]}
    for row in rows["PurchaseOrderItemSet"]:
        if row["Banfn"] and pr_dates.get(row["Banfn"], "") > po_dates[row["Ebeln"]]:
            problems.append(
                f"PurchaseOrderItemSet {row['Ebeln']}/{row['Ebelp']}: requisition dated after the order"
            )
    for row in rows["POScheduleLineSet"]:
        if row["Eindt"] < po_dates[row["Ebeln"]]:
            problems.append(f"POScheduleLineSet {row['Ebeln']}: delivery date before the order date")
    for row in rows["POHistorySet"]:
        if row["Vgabe"] == "1" and row["Budat"] < po_dates[row["Ebeln"]]:
            problems.append(f"POHistorySet {row['Ebeln']}: goods receipt before the order date")
    for row in rows["MaterialDocumentHeaderSet"]:
        if row["Budat"] > odata_date(AS_OF):
            problems.append(f"MaterialDocumentHeaderSet {row['Mblnr']}: posted in the future")

    # Quantities must make sense.
    for row in rows["PurchaseOrderItemSet"] + rows["PurchaseRequisitionSet"]:
        if float(row["Menge"]) <= 0:
            problems.append(f"non-positive quantity on {row.get('Ebeln') or row.get('Banfn')}")
    for row in rows["POScheduleLineSet"]:
        if float(row["Wemng"]) > float(row["Menge"]) + 1e-6:
            problems.append(f"POScheduleLineSet {row['Ebeln']}: delivered more than ordered")
    for row in rows["StorageLocationStockSet"]:
        if float(row["Labst"]) < 0:
            problems.append(f"StorageLocationStockSet {row['Matnr']}: negative stock")

    # Stock must equal opening stock plus receipts minus issues.
    movement: dict[tuple[str, str, str], float] = {}
    for row in rows["GoodsMovementItemSet"]:
        key = (row["Matnr"], row["Werks"], row["Lgort"])
        sign = 1.0 if row["Shkzg"] == "S" else -1.0
        movement[key] = movement.get(key, 0.0) + sign * float(row["Menge"])
    for material in materials:
        for entry in material.plants:
            key = (matnr_out(entry.matnr), entry.werks, entry.lgort)
            expected = entry.on_hand + entry.quality_stock
            actual = opening_stock[key] + movement.get(key, 0.0)
            if abs(expected - actual) > 0.01:
                problems.append(
                    f"stock does not reconcile for {entry.matnr}/{entry.werks}: "
                    f"closing {expected:g} against opening plus movements {actual:g}"
                )

    # Platform references must resolve.
    recommendation_ids = {r["recommendation_id"] for r in platform_rows["inventory_recommendations"]}
    for row in platform_rows["approvals"]:
        if row["recommendation_id"] not in recommendation_ids:
            problems.append(f"approvals: unknown recommendation {row['recommendation_id']}")
    attestation_ids = {r["attestation_id"] for r in platform_rows["repair_attestations"]}
    for row in platform_rows["repair_cases"]:
        if row["attestation_id"] and row["attestation_id"] not in attestation_ids:
            problems.append(f"repair_cases: unknown attestation {row['attestation_id']}")
    reservation_pairs = {(r["Rsnum"], r["Rspos"]) for r in rows["ReservationItemSet"]}
    for name in ("consumption_plans", "utilisation_status"):
        for row in platform_rows[name]:
            if (row["Rsnum"], row["Rspos"]) not in reservation_pairs:
                problems.append(f"{name}: unknown reservation {row['Rsnum']}/{row['Rspos']}")

    if problems:
        shown = "\n  ".join(problems[:20])
        raise RuntimeError(
            f"{len(problems)} consistency problem(s) found, dataset not written:\n  {shown}"
        )


# Opening stock is captured before the simulation runs so the closing position
# can be checked against it.
opening_stock: dict[tuple[str, str, str], float] = {}


# ===========================================================================
# WRITING
# ===========================================================================

def write() -> None:
    sap_dir = OUT_DIR / "sap"
    platform_dir = OUT_DIR / "platform"
    for directory in (sap_dir, platform_dir):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)

    for entity, columns in SAP_COLUMNS.items():
        write_csv(sap_dir / f"{entity}.csv", columns, rows[entity])
    for name, columns in PLATFORM_COLUMNS.items():
        write_csv(platform_dir / f"{name}.csv", columns, platform_rows[name])


def write_csv(path: Path, columns: list[str], data: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in data:
            writer.writerow({column: row.get(column, "") for column in columns})


# ===========================================================================
# MAIN
# ===========================================================================

def main() -> None:
    print(f"seed {SEED} | {MATERIAL_COUNT} materials | {HISTORY_MONTHS} months | as of {AS_OF}")
    discovered = len(SAP_COLUMNS) - len(NOT_EXPOSED_SETS)
    pending = sum(len(v) for v in PENDING_FIELDS.values())
    print(
        f"schema: {discovered} entity sets from {DISCOVERY_DIR.name}/properties.csv"
        f" + {len(NOT_EXPOSED_SETS)} not yet exposed by CPI"
        f" + {pending} pending field(s)"
    )

    build_vendors()
    build_materials()
    build_info_records()

    for material in materials:
        for entry in material.plants:
            opening_stock[(matnr_out(entry.matnr), entry.werks, entry.lgort)] = entry.on_hand

    build_history()        # consumption and replenishment for stocked materials
    build_oar_chains()     # I13 reservation to goods issue
    build_repairs()        # I08 80-series repair lifecycle
    build_recommendations()  # I07 platform recommendations

    finalise_po_items()
    write_stock_and_valuation()
    build_batch_stock()
    build_movement_statistics()
    check()
    write()

    print()
    print(f"generated/sap ({sum(len(v) for v in rows.values()):,} rows)")
    for entity in SAP_COLUMNS:
        print(f"  {len(rows[entity]):>8,}  {entity}.csv")
    print(f"generated/platform ({sum(len(v) for v in platform_rows.values()):,} rows)")
    for name in PLATFORM_COLUMNS:
        print(f"  {len(platform_rows[name]):>8,}  {name}.csv")


if __name__ == "__main__":
    main()
