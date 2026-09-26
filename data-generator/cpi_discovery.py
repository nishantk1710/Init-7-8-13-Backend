"""
VZI CPI OData discovery v5.

What changed since v4 (aligned to the FRS revisions of 21-Sep-2026):
  * OAR identification is MARC.DISMM in {ND, PD}. MARA.EXTWG is retired as the OAR key
    (I07 s7 note on supersession; I13 s7.3). EXTWG is still probed, but as an informational
    field, not a blocker.
  * New required-field exposure register (--fields): every field the three FRS documents say
    the build reads, checked against live $metadata, so the SAP/NTT request list is generated
    from evidence rather than typed by hand. Covers MSEG.SOBKZ, EKPO.ERDAT, RESB.BEDNR and
    the RESB session-identifier candidates, MARC.EISBE, MBEW.WAERS, EBAN.BNFPO key membership.
  * New defect regression harness (--defects): re-tests F1, F3, F4, B1 and the
    ReservationItemSet priority fix and reports FIXED / STILL OPEN per defect, so a run after
    an NTT transport answers "is it fixed yet" directly.
  * New ReservationItemSet page-cap test: the set reports exactly 1,000 rows against 105,848
    in the delivered extract (I08 s7). Pages until a short page to prove cap vs true count.
  * New plant coverage profile: per-WERKS row counts across the master and transaction sets.
    The delivered extract has no MARC rows for plant 1500 while 1500 carries 122,868 movements
    (I13 s7.2); this establishes whether OData has the same hole or only the extract does.
  * New history-depth check on MaterialDocumentHeaderSet.Budat: I13 FR-6 needs 25 months to
    separate Slow from Non-moving and the extract holds about 12.5.
  * New description-coverage check (MAKT vs MARA vs MARC) and EKBE VGABE value profile
    (the dictionary says VGABE 1 and 2; I13 s7.1 says E).
  * Probes are tagged by initiative; --initiative I07|I08|I13 scopes a run.
  * fetch_all paging ceiling raised and truncation is now reported instead of silently capping
    at 20,000 rows, which was under the 45,409-row MaterialPlantSet.
  * Consolidated status_report.md written at the end of every run.

Credentials come from a .env file next to this script (or --env-file / real environment
variables, which always win over the file). Never hard-code them. See .env.example:

  CPI_CLIENT_ID=...
  CPI_CLIENT_SECRET=...
  CPI_TOKEN_URL=https://vzicpinonprod.authentication.in30.hana.ondemand.com/oauth/token
  CPI_BASE_URL=https://vzicpinonprod.it-cpi021-rt.cfapps.in30.hana.ondemand.com

Usage:
  python cpi_discovery.py --dictionary VZI_Entity_Dictionary_I07_I08_I13_v1_0.csv --out ./discovery
  python cpi_discovery.py --defects --out ./discovery          # post-transport regression only
  python cpi_discovery.py --fields  --out ./discovery          # exposure register only, ~2 calls
  python cpi_discovery.py --initiative I13 --out ./discovery   # scope probes to one initiative

Outputs in --out:
  status_report.md                 consolidated run summary: services, defects, missing fields, gaps
  required_fields.csv              every FRS-required field vs live $metadata, with FRS reference
  field_requests.txt               the missing subset, written as a request list for SAP/NTT
  defect_status.csv                F1/F3/F4/B1/R1 verdicts with the evidence behind each
  metadata_<service>.xml           raw EDMX as returned
  entity_sets.csv                  service, set, entity type, keys, property count, sap:pageable
  properties.csv                   per-property type, nullability, key flag, sap:filterable/sortable
  counts.csv                       service, set, $count (or error)
  probes.csv                       every behavioural probe: initiative, purpose, path, query, result
  fr9_check.txt                    ChangeDocItemSet counts for I07 FR-9
  key_collapse.txt                 ChangeDocItem declared key vs full CDPOS composite key
  filter_support.csv               per property: $filter honoured, ignored, or rejected
  operator_support.csv             which operators work where eq works
  paging_stability.txt             duplicate/missing keys with and without $orderby
  reservation_cap.txt              ReservationItemSet: page cap vs true count
  plant_coverage.txt               per-WERKS coverage across master and transaction sets
  history_depth.txt                Budat span vs the 25 months I13 FR-6 needs
  coverage_ratios.txt              MAKT vs MARA vs MARC population coverage
  vgabe_profile.txt                EKBE VGABE distinct values and counts
  marc_changes_summary.txt         MATERIAL/MARC CDPOS rows aggregated client-side (I07 FR-9)
  mrp_type_profile.txt             MaterialPlantSet Dismm x Werks, reorder-point population
  material_number_profile.txt      Matnr length/prefix profile, 80-series presence (I08)
  repair_po_check.txt              ZREP / Pstyp 3 repair-PO convention evidence (I08 D7)
  calls.csv                        every HTTP call with elapsed seconds
  errors.txt / errors.csv          every non-2xx call with trace headers and body
  dictionary_gaps.csv              dictionary entity sets and fields vs actual (needs --dictionary)
"""
import argparse, csv, os, sys, time, json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("pip install requests openpyxl")

# SERVICE RENAME, 22-Sep-2026
#
# SAP republished both services under new names:
#
#     ZVZI_KPI02_SHARED_SRV  ->  ZMM_KPI02_ADD_SRV     14 sets
#     ZMM_KPI02_SRV          ->  ZMM_KPI02_TAB_SRV      7 sets
#
# The ENTITY SETS DID NOT CHANGE. Every set name and every key in the new
# services was read from live $metadata on 22-Sep and is identical to the old,
# so nothing below the service name moves: MaterialPlantSet is still MARC,
# PurchaseOrderSet is still EKKO.
#
# Both old and new answered on 22-Sep, so this is additive rather than a
# cutover. The old names are kept here rather than deleted: if a sweep against
# the new names fails, the first question is whether SAP moved something back,
# and that is unanswerable without knowing what the previous names were.
#
# A caution for anyone reading SAP's spreadsheet of this change. Its EntitySet
# column is shifted up by one row from InfoRecord downwards, so it pairs
# MaterialPlantSet with MKPF and MaterialSet with MARC. Both are wrong, and the
# key settles it without needing our records at all -- a set keyed Matnr;Werks
# is MARC, and a set keyed Matnr alone is MARA. The Entity and Tables columns
# in that sheet are correct; only EntitySet is misaligned.
RENAMED_SERVICES = {
    "ZVZI_KPI02_SHARED_SRV": "ZMM_KPI02_ADD_SRV",
    "ZMM_KPI02_SRV": "ZMM_KPI02_TAB_SRV",
}

# The service names as bare strings. Several reports look results up by
# (service, set); during the first attempt at this rename four of those lookups
# still carried the old literal. They did not fail -- they returned None, and
# the report printed a zero. Naming them once means the next rename is one edit
# rather than a hunt through a thousand lines.
ADD_SRV = RENAMED_SERVICES["ZVZI_KPI02_SHARED_SRV"]
TAB_SRV = RENAMED_SERVICES["ZMM_KPI02_SRV"]

SERVICES = {
    ADD_SRV: ["GoodsMovementItemSet", "InfoRecordOrgSet", "InfoRecordSet", "MaterialDescriptionSet",
              "MaterialDocumentHeaderSet", "MaterialPlantSet", "MaterialSet", "POHistorySet",
              "POScheduleLineSet", "PurchaseOrderItemSet", "PurchaseOrderSet", "PurchaseRequisitionSet",
              "StorageLocationStockSet", "VendorSet"],
    TAB_SRV: ["BatchStockSet", "ChangeDocHeaderSet", "ChangeDocItemSet", "MaterialValuationSet",
              "MonthlyMovementStatisticSet", "ReservationItemSet", "StockMovementStatisticSet"],
}

# Registered, reachable, and deliberately not swept.
#
# ZMM_KPI02_GP_SRV went live on 22-Sep with three real sets -- GatePassHeaderSet
# (ZzgpNo;Zzyear), GatePassItemSet (+Zzposnr) and GatePassReturnSet (+Zcount).
# Gate pass was descoped for I08 on 15-Aug, and a service existing again does not
# reverse that: sweeping it would quietly widen the programme by three sets. It
# is named rather than ignored so the next sweep does not rediscover it as news,
# and so re-scoping it later is a one-line change with the decision visible.
#
# ZMM_GET_CSV_SRV is a different shape altogether. Its single set is keyed
# RequestId;TabName;FromDate;ToDate;IsDelta;MaxRows -- a request, not a row. That
# describes a generic table extract with a server-side date range and a delta
# flag, which is precisely what the entity sets above cannot do. Not swept
# because this script profiles entity sets and that is not one; it warrants its
# own investigation rather than a column in these reports.
DESCOPED_SERVICES = {
    "ZMM_KPI02_GP_SRV": "I08 gate pass / RGP lifecycle removed 15-Aug-2026; live again 22-Sep but still out of scope",
    "ZMM_GET_CSV_SRV": "Generic table extract (TableExtractSet); not an entity-set profile, investigate separately",
}

# SAP table -> (service, set). Dictionary rows are keyed on the table, so this survives the
# dictionary's proposed set names differing from the names the services actually publish.
TABLE_TO_SET = {
    "MARA": (ADD_SRV, "MaterialSet"), "MAKT": (ADD_SRV, "MaterialDescriptionSet"),
    "MARC": (ADD_SRV, "MaterialPlantSet"), "MARD": (ADD_SRV, "StorageLocationStockSet"),
    "MSEG": (ADD_SRV, "GoodsMovementItemSet"), "MKPF": (ADD_SRV, "MaterialDocumentHeaderSet"),
    "EBAN": (ADD_SRV, "PurchaseRequisitionSet"), "EKKO": (ADD_SRV, "PurchaseOrderSet"),
    "EKPO": (ADD_SRV, "PurchaseOrderItemSet"), "EKET": (ADD_SRV, "POScheduleLineSet"),
    "EKBE": (ADD_SRV, "POHistorySet"), "EINA": (ADD_SRV, "InfoRecordSet"),
    "EINE": (ADD_SRV, "InfoRecordOrgSet"), "LFA1": (ADD_SRV, "VendorSet"),
    "RESB": (TAB_SRV, "ReservationItemSet"), "MBEW": (TAB_SRV, "MaterialValuationSet"),
    "CDHDR": (TAB_SRV, "ChangeDocHeaderSet"), "CDPOS": (TAB_SRV, "ChangeDocItemSet"),
    "MCHB": (TAB_SRV, "BatchStockSet"), "S031": (TAB_SRV, "MonthlyMovementStatisticSet"),
    "S032": (TAB_SRV, "StockMovementStatisticSet"),
}

# Tables the dictionary proposes that no registered service exposes. Reported as PROPOSED_NOT_EXPOSED
# rather than NO_SET_MAPPING, so the gap report distinguishes "we never asked" from "we asked and it is absent".
PROPOSED_NOT_EXPOSED = {
    "EKKN": "PO account assignment; I13 cost-centre attribution (I13 s7.2: ABSENT, behind a config flag)",
    "AUFK": "Order master; I13 cost-centre attribution (I13 s7.2: ABSENT)",
    "MARDH": "Period-end stock history; I07/I13 backtest baseline (I13 s7.2: ABSENT)",
    "MBEWH": "Period-end valuation history; I07/I13 backtest baseline (I13 s7.2: ABSENT)",
}

# The dictionary's proposed entity-set names against the names the services publish.
# A mismatch here is a documentation defect, not a SAP defect, but it breaks any code that
# takes the set name straight from the dictionary.
DICT_SET_ALIASES = {
    "MaterialTextSet": "MaterialDescriptionSet", "MaterialDocItemSet": "GoodsMovementItemSet",
    "MaterialDocHeaderSet": "MaterialDocumentHeaderSet", "PurchaseReqSet": "PurchaseRequisitionSet",
    "ReservationSet": "ReservationItemSet", "POHeaderSet": "PurchaseOrderSet",
    "POItemSet": "PurchaseOrderItemSet", "MovementStatsSet": "MonthlyMovementStatisticSet",
    "StockStatsSet": "StockMovementStatisticSet",
}

# Kept named SHARED and ZMM rather than renamed to ADD and TAB. Every probe
# below reads f"{SHARED}/MaterialPlantSet"; renaming the variables would touch
# a hundred lines to say the same thing, and a large diff over a service rename
# is how a real change gets lost in the noise of a cosmetic one.
SHARED = f"sap/opu/odata/sap/{ADD_SRV}"
ZMM = f"sap/opu/odata/sap/{TAB_SRV}"

# The service lists above are written out for readability. These stop them
# drifting from the constants when the next rename happens.
assert set(SERVICES) == {ADD_SRV, TAB_SRV}, "SERVICES keys and the service constants disagree"
assert {svc for svc, _ in TABLE_TO_SET.values()} == {ADD_SRV, TAB_SRV}, (
    "TABLE_TO_SET names a service that SERVICES does not"
)
JSON = "$format=json"

# ---------------------------------------------------------------------------------------------
# Required-field exposure register.
#
# Every row is a field one of the three FRS documents says the build reads, with the reference
# that says so. The check is against live $metadata: present in the projection, or absent.
# "expected" records what the FRS asserts today, so the run reports CONFIRMED / NOW EXPOSED /
# NEWLY MISSING rather than just present/absent - a field that was there and has gone is a
# different conversation from one that was never added.
#
# (table, field, expected_state, initiatives, why it matters / FRS reference)
# expected_state: "present" | "absent"
# ---------------------------------------------------------------------------------------------
REQUIRED_FIELDS = [
    # --- I08: the three exposure requests in FRS s7 (21-Sep-2026) -------------------------------
    ("MSEG", "SOBKZ", "absent", "I08",
     "Special stock indicator. A 541 dispatch posts a vendor row (SOBKZ 'O') and a plant row; without it "
     "both are summed and every dispatch quantity doubles. Fails silently. I08 FRS s7."),
    ("EKPO", "ERDAT", "absent", "I08",
     "Line creation date. PurchaseOrderItemSet publishes no date property at all; ERDAT anchors aging, "
     "overdue and lead-time comparison, and EKKO has no header row for 37% of repair lines. I08 FRS s7."),
    ("RESB", "BEDNR", "absent", "I08/I13",
     "Requirement tracking number, Edm.String(10). Designated carrier of the assistant session identifier. "
     "Live on PurchaseRequisitionSet and PurchaseOrderItemSet, absent from ReservationItemSet. "
     "Hard dependency for I08 FR-8 and FR-4."),
    # --- I08: reservation session-identifier candidates, s10 -----------------------------------
    ("RESB", "WEMPF", "present", "I08/I13",
     "Goods recipient, 12 chars. Exposed. Used as the I13 requester proxy (73.2% populated). "
     "Candidate session field only if BEDNR is refused."),
    ("RESB", "SGTXT", "absent", "I08/I13", "Item text. Named as a session-field candidate in I08 s10; not in the projection."),
    ("RESB", "ABLAD", "absent", "I08/I13", "Unloading point. Named as a session-field candidate in I08 s10; not in the projection."),
    ("RESB", "ZZAISESSION", "absent", "I13",
     "Z-append session identifier. I13 s7.2: the single field linking the platform consumption plan to the "
     "SAP reservation. Read model carries the placeholder and maps it to null."),
    # --- I07 ------------------------------------------------------------------------------------
    ("MARC", "EISBE", "present", "I07",
     "Current safety stock. Exposed over OData but absent from the delivered 19-column extract. "
     "Without it I07 shows what it recommends but not what SAP holds. I07 FRS s7."),
    ("MARC", "DISMM", "present", "I07/I13",
     "MRP type. THE OAR identifier under the 08-Sep ruling: OAR = DISMM in {ND, PD}. Single most "
     "important field in I13. I13 FRS s7.1."),
    ("MARC", "MINBE", "present", "I07", "Reorder point. FR-3 comparison and FR-9 adoption tracking."),
    ("MARC", "MABST", "present", "I07", "Max stock. FR-3 comparison and FR-9 adoption tracking."),
    ("MARC", "PLIFZ", "present", "I07", "Planned delivery time. Placeholder lead time until the I11 Z-program lands."),
    ("MARA", "EXTWG", "absent", "I07/I13",
     "External material group. RETIRED as the OAR identifier (I07 s7 supersession note, I13 s7.3). "
     "Informational only now; its absence is no longer BLOCKING and s8/s10 of both FRS should be updated."),
    ("MBEW", "WAERS", "absent", "I07",
     "Currency. A price with no currency is ambiguous; ZAR is assumed by context, not by data. I07 FRS s7."),
    ("EKKO", "BEDAT", "absent", "I07",
     "PO document date. Not exposed; EKKO.AEDAT is the interim release-date proxy for the I11 lead-time program."),
    ("EKKO", "AEDAT", "present", "I07", "Interim release-date proxy for the release-to-GRN lead time."),
    # --- I13 ------------------------------------------------------------------------------------
    ("EBAN", "BNFPO", "present", "I13",
     "Requisition item. Exposed, but check the KEY column below: I07 s10 asks for it to be ADDED TO THE "
     "ENTITY KEY so requisition items are uniquely addressable."),
    ("EKPO", "BANFN", "present", "I13", "PR-to-PO stitching key. 37.6% populated in the extract."),
    ("RESB", "AUFNR", "present", "I13",
     "Order number. 39.2% populated; recommended as the next stitching increment (I13 s7.5), roughly thirty "
     "times the linkage of the BANFN route and needs no SAP change."),
    ("RESB", "BANFN", "present", "I13", "Reservation-to-PR link. Only 1.2% populated - the principal FR-5 obstacle."),
    ("MKPF", "BUDAT", "present", "I13", "Posting date. The single date basis for every movement metric."),
    ("MSEG", "BWART", "present", "I13", "Movement type. 201/261 = consumption, 541 = removal to repair (I08)."),
    ("EKBE", "VGABE", "present", "I13", "Transaction/event type. Goods-receipt filter for FR-6; see vgabe_profile.txt."),
]

# Properties whose KEY membership the FRS asks about, separately from their presence.
REQUIRED_KEYS = [
    ("EBAN", "BNFPO", "I07/I13", "I07 s10: add EBAN.BNFPO to the PurchaseRequisitionSet entity key so "
                                 "requisition items are uniquely addressable."),
    ("CDPOS", "TABNAME", "I07", "Declared key is 3 columns; the true CDPOS key is 7. See key_collapse.txt."),
    ("CDPOS", "TABKEY", "I07", "As above."),
    ("CDPOS", "FNAME", "I07", "As above."),
    ("CDPOS", "CHNGIND", "I07", "As above."),
]

# ---------------------------------------------------------------------------------------------
# Confirmed defects, for the post-transport regression run.
# Each entry names the test function that decides it. PASS means the defect is fixed.
# ---------------------------------------------------------------------------------------------
DEFECTS = {
    "B1": "/$count returns HTTP 500 on PurchaseRequisitionSet and GoodsMovementItemSet",
    "F1": "85 of 230 properties silently ignore $filter (impossible-value test returns the set total)",
    "F3": "only eq and substringof are honoured; ne and gt/ge/lt/le on strings are not. "
           "startswith IS honoured -- the earlier probes failed because they passed an "
           "unpadded prefix. MATNR is ALPHA-converted, so '80' matches nothing while "
           "'0000000080' returns a count. SAP confirmed the same in SE11/SE16N.",
    "F4": "$skip without $orderby produces duplicate and missing rows across pages",
    "R1": "ReservationItemSet: ignored $filter property flagged to NTT as the priority fix",
    "R2": "ReservationItemSet returns exactly 1,000 rows against 105,848 in the extract (suspected page cap)",
}

# ---------------------------------------------------------------------------------------------
# Behavioural probes. (initiative, purpose, dev-plan task, service path, entity set, odata query).
# A query ending in /$count returns a bare number; anything else is fetched as JSON.
# ---------------------------------------------------------------------------------------------
PROBES = [
    # E1: separate "no data in client" from "filtered out by the DPC"
    ("I13", "E1 zero-count set: does $top=1 return a row",         "W2.6/W6.2", ZMM, "ReservationItemSet",          f"$top=1&{JSON}"),
    ("I07", "E1 zero-count set: does $top=1 return a row",         "W2.6",      ZMM, "MaterialValuationSet",        f"$top=1&{JSON}"),
    ("I13", "E1 zero-count set: does $top=1 return a row",         "W2.6/W3.5", ZMM, "MonthlyMovementStatisticSet", f"$top=1&{JSON}"),
    # B1: isolate $count handling from the GET_ENTITYSET SELECT
    ("ALL", "B1 failing set: does $top=1 work where $count dumps", "W2.3", SHARED, "PurchaseRequisitionSet", f"$top=1&{JSON}"),
    ("ALL", "B1 failing set: does $top=1 work where $count dumps", "W2.3", SHARED, "GoodsMovementItemSet",   f"$top=1&{JSON}"),
    ("ALL", "B1 failing set: total via $inlinecount=allpages",     "W2.3", SHARED, "PurchaseRequisitionSet", f"$inlinecount=allpages&$top=1&{JSON}"),
    ("ALL", "B1 failing set: total via $inlinecount=allpages",     "W2.3", SHARED, "GoodsMovementItemSet",   f"$inlinecount=allpages&$top=1&{JSON}"),
    # Paging despite sap:pageable="false" on every entity set
    ("ALL", "Paging: first page",                                  "W2.3/W3.2", ZMM, "ChangeDocItemSet", f"$top=5&{JSON}"),
    ("ALL", "Paging: second page, rows must differ from first",    "W2.3/W3.2", ZMM, "ChangeDocItemSet", f"$top=5&$skip=5&{JSON}"),
    ("ALL", "Paging: inline count and __next presence",            "W2.3/W3.2", ZMM, "ChangeDocItemSet", f"$inlinecount=allpages&$top=1&{JSON}"),
    ("ALL", "Paging: large set page-until-short-page viability",   "W2.3/W3.1", SHARED, "MaterialDocumentHeaderSet", f"$top=1000&$skip=40000&{JSON}"),
    ("ALL", "Paging: does $orderby work despite sortable=false",   "W2.3",      SHARED, "PurchaseOrderSet", f"$orderby=Aedat desc&$top=3&{JSON}"),
    # I07 FR-9: adoption tracking on MARC planning fields
    ("I07", "FR-9: MARC field changes DISMM",  "W4.7", ZMM, "ChangeDocItemSet/$count", "$filter=Objectclas eq 'MATERIAL' and Tabname eq 'MARC' and Fname eq 'DISMM'"),
    ("I07", "FR-9: MARC field changes EISBE",  "W4.7", ZMM, "ChangeDocItemSet/$count", "$filter=Objectclas eq 'MATERIAL' and Tabname eq 'MARC' and Fname eq 'EISBE'"),
    ("I07", "FR-9: MARC field changes MINBE",  "W4.7", ZMM, "ChangeDocItemSet/$count", "$filter=Objectclas eq 'MATERIAL' and Tabname eq 'MARC' and Fname eq 'MINBE'"),
    ("I07", "FR-9: MARC field changes MABST",  "W4.7", ZMM, "ChangeDocItemSet/$count", "$filter=Objectclas eq 'MATERIAL' and Tabname eq 'MARC' and Fname eq 'MABST'"),
    ("I07", "FR-9: conversions to Min-Max (DISMM new value VB)", "W4.7", ZMM, "ChangeDocItemSet/$count", "$filter=Objectclas eq 'MATERIAL' and Tabname eq 'MARC' and Fname eq 'DISMM' and Value_new eq 'VB'"),
    ("I07", "FR-9: header join sample, MATERIAL class",          "W4.7", ZMM, "ChangeDocHeaderSet", f"$filter=Objectclas eq 'MATERIAL'&$top=3&{JSON}"),
    # OAR classifier on MRP type. Counts should reconcile to the MaterialPlantSet total.
    ("I07/I13", "OAR classifier: MRP type VB (Min-Max)",          "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=Dismm eq 'VB'"),
    ("I07/I13", "OAR classifier: MRP type ND (OAR)",              "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=Dismm eq 'ND'"),
    ("I07/I13", "OAR classifier: MRP type PD (OAR)",              "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=Dismm eq 'PD'"),
    ("I07/I13", "OAR classifier: MRP type V1 (excluded)",         "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=Dismm eq 'V1'"),
    ("I07/I13", "OAR classifier: MRP type blank (excluded)",      "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=Dismm eq ''"),
    ("I07",     "OAR classifier: VB rows carrying a reorder point", "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=Dismm eq 'VB' and Minbe gt 0"),
    ("I07",     "OAR classifier: ND/PD rows carrying a reorder point (should be ~0)", "W2.4", SHARED, "MaterialPlantSet/$count", "$filter=(Dismm eq 'ND' or Dismm eq 'PD') and Minbe gt 0"),
    # EXTWG: retired as the OAR key, kept as an informational control
    ("I07/I13", "EXTWG (retired key): is the property exposed at all", "W2.4", SHARED, "MaterialSet", f"$select=Matnr,Extwg&$top=2&{JSON}"),
    # I08 repair-PO convention and 80-series detection
    ("I08", "I08: PO items with item category 3",                 "W5.2", SHARED, "PurchaseOrderItemSet/$count", "$filter=Pstyp eq '3'"),
    ("I08", "I08: PO headers with document type ZREP",            "W5.2", SHARED, "PurchaseOrderSet/$count",     "$filter=Bsart eq 'ZREP'"),
    # MATNR goes through conversion exit ALPHA: a purely numeric material is stored
    # right-aligned in 18 characters. VZI materials carry 10 significant digits
    # (2000000270 -> 000000002000000270), so an 80-series prefix is eight zeros then
    # '80'. startswith works; it was the prefix that was wrong. SAP reproduced the
    # same behaviour in SE11/SE16N, where 80* finds nothing and 0000000080* does.
    ("I08", "I08: 80-series material PO lines (startswith, ALPHA-padded)", "W5.1", SHARED, "PurchaseOrderItemSet/$count", "$filter=startswith(Matnr,'0000000080')"),
    ("I08", "I08: 80-series materials in master (startswith, ALPHA-padded)", "W5.1", SHARED, "MaterialSet/$count",          "$filter=startswith(Matnr,'0000000080')"),
    ("I08", "I08 control: unpadded prefix, expected to match nothing", "W5.1", SHARED, "MaterialSet/$count",     "$filter=startswith(Matnr,'80')"),
    ("I08", "I08: text-only PO lines (no material)",              "W5.5", SHARED, "PurchaseOrderItemSet/$count", "$filter=Matnr eq ''"),
    ("I08", "I08 control: startswith executes at all (every numeric MATNR; expect the set total)", "W5.1", SHARED, "MaterialSet/$count", "$filter=startswith(Matnr,'0000000')"),
    ("I08", "I08: 80-series by range on 8-digit numbers",         "W5.1", SHARED, "MaterialSet/$count", "$filter=Matnr ge '80000000' and Matnr le '80999999'"),
    ("I08", "I08: 80-series by range on 18-digit padded numbers", "W5.1", SHARED, "MaterialSet/$count", "$filter=Matnr ge '000000008000000000' and Matnr le '000000008099999999'"),
    ("I08", "I08: 541 removals to repair",                        "W5.3", SHARED, "GoodsMovementItemSet/$count", "$filter=Bwart eq '541'"),
    # I13 consumption movement types
    ("I13", "I13: 201 consumption movements",                     "W3.5", SHARED, "GoodsMovementItemSet/$count", "$filter=Bwart eq '201'"),
    ("I13", "I13: 261 order-issue movements",                     "W3.5", SHARED, "GoodsMovementItemSet/$count", "$filter=Bwart eq '261'"),
    # Envelope samples for the W2.1 client library
    ("ALL", "W2.1 envelope: decimal and date typing sample",      "W2.1", SHARED, "MaterialPlantSet",     f"$top=2&{JSON}"),
    ("ALL", "W2.1 envelope: Netpr/Netwr arrive as strings",       "W2.1", SHARED, "PurchaseOrderItemSet", f"$top=2&{JSON}"),
    ("ALL", "W2.1 envelope: $select support",                     "W2.1", SHARED, "MaterialPlantSet",     f"$select=Matnr,Werks,Dismm&$top=2&{JSON}"),
]

CPI_PATH = "/http/SAPECC/OdataConsumption"
NS = {"edmx": "http://schemas.microsoft.com/ado/2007/06/edmx", "edm": "http://schemas.microsoft.com/ado/2008/09/edm",
      "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"}
SAP_NS = "http://www.sap.com/Protocols/SAPData"

DEFAULT_ENV_FILE = Path(__file__).resolve().parent / ".env"

# The client secret published in the shared Postman collection. If it turns up in a .env we say so:
# a secret that has been circulated in a project file should be rotated, not reused.
LEAKED_SECRET_PREFIX = "5a7178d5-6b87-498f-bfcf"


def load_env_file(path):
    """Minimal .env loader (no dependency). Real environment variables take precedence."""
    path = Path(path)
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        os.environ.setdefault(key, value)
    return True


def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Missing {name}: set it in {DEFAULT_ENV_FILE} (see .env.example) or in the environment")
    return v


def get_token(session):
    secret = env("CPI_CLIENT_SECRET")
    if secret.startswith(LEAKED_SECRET_PREFIX):
        print("WARNING: this client secret is the one published in the shared Postman collection. "
              "It should be rotated by the integration team and never committed to a project file.")
    r = session.post(env("CPI_TOKEN_URL"), data={"grant_type": "client_credentials"},
                     auth=(env("CPI_CLIENT_ID"), secret), timeout=60)
    r.raise_for_status()
    return r.json()["access_token"]


FAILURES = []      # every non-2xx response, for the SAP-team failure report
CALLS = []         # every call, for the performance baseline
COUNT_DUMPS = set()  # sets whose /$count has already returned 500 in this run
NOTES = []         # headline findings, collected into status_report.md
THROTTLE = 0.0     # seconds to sleep between calls (--sleep)

TRACE_HEADERS = ("x-correlationid", "sap-messageprocessinglogid", "x-vcap-request-id", "x-request-id",
                 "sap-message", "dataserviceversion", "content-type", "date")


def utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def note(section, text):
    NOTES.append((section, text))


def record_failure(response, api_path, api_query):
    FAILURES.append({
        "utc": utcnow(),
        "api_path": api_path, "api_query": api_query, "status": response.status_code,
        "elapsed_s": round(response.elapsed.total_seconds(), 1) if response.elapsed is not None else None,
        "headers": {k: v for k, v in response.headers.items() if k.lower() in TRACE_HEADERS},
        "body": response.text[:4000],
    })


def cpi_get(session, token, api_path, api_query="", retries=3):
    url = env("CPI_BASE_URL").rstrip("/") + CPI_PATH
    t0 = time.time()
    for attempt in range(retries):
        r = session.get(url, params={"APIPath": api_path, "APIQuery": api_query},
                        headers={"Authorization": f"Bearer {token}",
                                 "Accept": "application/json, application/xml;q=0.9, */*;q=0.8"},
                        timeout=120)
        if r.status_code == 401 and attempt == 0:
            token = get_token(session)
            continue
        if r.status_code >= 500 and attempt < retries - 1:
            time.sleep(2 ** attempt)
            continue
        break
    CALLS.append([utcnow(), "/" + api_path.lstrip("/"), api_query, r.status_code,
                  round(time.time() - t0, 1), len(r.content)])
    if not r.ok:
        record_failure(r, api_path, api_query)
    if THROTTLE:
        time.sleep(THROTTLE)
    return r, token


def parse_json_feed(text):
    """OData v2 JSON envelope -> (rows, next_link, inline_count). Tolerates single-entity and error shapes."""
    try:
        d = json.loads(text).get("d", {})
    except (ValueError, AttributeError):
        return [], None, None
    if isinstance(d, dict) and "results" in d:
        return d.get("results", []), d.get("__next"), d.get("__count")
    if isinstance(d, list):
        return d, None, None
    return ([d] if d else []), None, None


def strip_meta(row):
    return {k: v for k, v in row.items() if k != "__metadata"}


def parse_edmx(xml_text):
    """Return sets: {set_name: (entity_type, pageable)}, types: {type_name: {keys:set, props:[dict]}}"""
    root = ET.fromstring(xml_text)
    types, sets = {}, {}
    for schema in root.iter(f"{{{NS['edm']}}}Schema"):
        for et in schema.findall("edm:EntityType", NS):
            name = et.get("Name")
            keys = {pr.get("Name") for pr in et.findall("edm:Key/edm:PropertyRef", NS)}
            props = []
            for p in et.findall("edm:Property", NS):
                props.append({
                    "name": p.get("Name"), "type": p.get("Type"), "nullable": p.get("Nullable", "true"),
                    "maxlength": p.get("MaxLength", ""), "precision": p.get("Precision", ""), "scale": p.get("Scale", ""),
                    "filterable": p.get(f"{{{SAP_NS}}}filterable", ""), "sortable": p.get(f"{{{SAP_NS}}}sortable", ""),
                    "label": p.get(f"{{{SAP_NS}}}label", ""),
                })
            types[name] = {"keys": keys, "props": props}
        for es in schema.findall("edm:EntityContainer/edm:EntitySet", NS):
            sets[es.get("Name")] = (es.get("EntityType", "").split(".")[-1], es.get(f"{{{SAP_NS}}}pageable", ""))
    return sets, types


def fetch_metadata(s, token, out):
    """$metadata for every service. Returns (token, actual, actual_props) with no $count sweep."""
    actual, actual_props = {}, {}
    for svc in SERVICES:
        r, token = cpi_get(s, token, f"sap/opu/odata/sap/{svc}/$metadata")
        (out / f"metadata_{svc}.xml").write_text(r.text, encoding="utf-8")
        if r.status_code != 200:
            print(f"[{svc}] $metadata HTTP {r.status_code}: {r.text[:200]}")
            note("Services", f"{svc}: $metadata returned HTTP {r.status_code} - service unreachable, every dependent "
                             f"check below is unevidenced.")
            continue
        sets, types = parse_edmx(r.text)
        note("Services", f"{svc}: reachable, {len(sets)} entity sets in $metadata.")
        for set_name, (type_name, pageable) in sets.items():
            t = types.get(type_name, {"keys": set(), "props": []})
            actual[(svc, set_name)] = {"keys": t["keys"], "props": [p["name"] for p in t["props"]], "pageable": pageable}
            actual_props[(svc, set_name)] = t["props"]
    return token, actual, actual_props


def run_sweep(s, token, out, skip_counts):
    """$metadata and $count for every service. Returns (token, actual, actual_props, totals)."""
    all_sets_rows, prop_rows, count_rows = [], [], []
    actual, actual_props, totals = {}, {}, {}
    for svc, expected_sets in SERVICES.items():
        r, token = cpi_get(s, token, f"sap/opu/odata/sap/{svc}/$metadata")
        (out / f"metadata_{svc}.xml").write_text(r.text, encoding="utf-8")
        if r.status_code != 200:
            print(f"[{svc}] $metadata HTTP {r.status_code}: {r.text[:200]}")
            note("Services", f"{svc}: $metadata returned HTTP {r.status_code} - service unreachable.")
            continue
        sets, types = parse_edmx(r.text)
        print(f"[{svc}] {len(sets)} entity sets in $metadata; expected {len(expected_sets)} from technical list")
        missing = set(expected_sets) - set(sets)
        extra = set(sets) - set(expected_sets)
        if missing:
            print(f"   MISSING vs technical list: {sorted(missing)}")
            note("Services", f"{svc}: sets in the technical list but not in $metadata: {sorted(missing)}")
        if extra:
            print(f"   EXTRA in $metadata:          {sorted(extra)}")
        note("Services", f"{svc}: reachable, {len(sets)} entity sets in $metadata.")
        for set_name, (type_name, pageable) in sets.items():
            t = types.get(type_name, {"keys": set(), "props": []})
            all_sets_rows.append([svc, set_name, type_name, ";".join(sorted(t["keys"])), len(t["props"]), pageable])
            actual[(svc, set_name)] = {"keys": t["keys"], "props": [p["name"] for p in t["props"]], "pageable": pageable}
            actual_props[(svc, set_name)] = t["props"]
            for p in t["props"]:
                prop_rows.append([svc, set_name, p["name"], p["type"], p["nullable"], "K" if p["name"] in t["keys"] else "",
                                  p["maxlength"], p["precision"], p["scale"], p["filterable"], p["sortable"], p["label"]])
            if not skip_counts:
                rc, token = cpi_get(s, token, f"sap/opu/odata/sap/{svc}/{set_name}/$count")
                count_rows.append([svc, set_name, rc.text.strip() if rc.status_code == 200 else f"HTTP {rc.status_code}",
                                   round(rc.elapsed.total_seconds(), 1)])
                if rc.status_code == 200 and rc.text.strip().isdigit():
                    totals[(svc, set_name)] = int(rc.text.strip())
                else:  # /$count dumps: take the total from $inlinecount instead
                    COUNT_DUMPS.add(f"sap/opu/odata/sap/{svc}/{set_name}")
                    r2, token = cpi_get(s, token, f"sap/opu/odata/sap/{svc}/{set_name}",
                                        f"$inlinecount=allpages&$top=1&{JSON}", retries=1)
                    if r2.status_code == 200:
                        _, _, inline = parse_json_feed(r2.text)
                        if inline is not None and str(inline).isdigit():
                            totals[(svc, set_name)] = int(inline)
                            count_rows[-1][2] = f"HTTP 500 on /$count; $inlinecount={inline}"
                print(f"   {set_name:32s} count = {count_rows[-1][2]}  ({count_rows[-1][3]}s)")

    with open(out / "entity_sets.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["service", "entity_set", "entity_type", "keys", "property_count", "sap_pageable"])
        w.writerows(all_sets_rows)
    with open(out / "properties.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["service", "entity_set", "property", "type", "nullable", "is_key", "maxlength", "precision", "scale",
                    "sap_filterable", "sap_sortable", "sap_label"])
        w.writerows(prop_rows)
    if not skip_counts:
        with open(out / "counts.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["service", "entity_set", "count", "elapsed_s"])
            w.writerows(count_rows)
        dumps = [f"{r[0]}/{r[1]}" for r in count_rows if str(r[2]).startswith("HTTP")]
        if dumps:
            note("Defects", f"B1: /$count still fails on {len(dumps)} set(s): {', '.join(dumps)}")
        else:
            note("Defects", "B1: /$count succeeded on every reachable set - the defect appears FIXED.")
    return token, actual, actual_props, totals


# ---------------------------------------------------------------------------------------------
# Required-field exposure register
# ---------------------------------------------------------------------------------------------
def run_required_fields(actual, out):
    """FRS-declared required fields vs live $metadata. Writes required_fields.csv and field_requests.txt."""
    rows, missing = [], []
    for table, field, expected, inits, why in REQUIRED_FIELDS:
        svc_set = TABLE_TO_SET.get(table)
        if not svc_set:
            rows.append([table, field, inits, expected, "", "NO_SET_MAPPING", why])
            continue
        act = actual.get(svc_set)
        if not act:
            rows.append([table, field, inits, expected, svc_set[1], "SET_UNREACHABLE", why])
            continue
        props_upper = {p.upper(): p for p in act["props"]}
        hit = props_upper.get(field.upper())
        if hit and expected == "present":
            verdict = "CONFIRMED_PRESENT"
        elif hit and expected == "absent":
            verdict = "NOW_EXPOSED"      # good news: the SAP team has added it since the FRS was written
        elif not hit and expected == "absent":
            verdict = "CONFIRMED_ABSENT"
        else:
            verdict = "NEWLY_MISSING"    # regression: the FRS says it is there and it is not
        rows.append([table, field, inits, expected, svc_set[1], verdict, why])
        if verdict in ("CONFIRMED_ABSENT", "NEWLY_MISSING"):
            missing.append((table, field, inits, svc_set[1], verdict, why))

    key_rows = []
    for table, field, inits, why in REQUIRED_KEYS:
        svc_set = TABLE_TO_SET.get(table)
        act = actual.get(svc_set) if svc_set else None
        if not act:
            key_rows.append([table, field, inits, "", "SET_UNREACHABLE", why])
            continue
        keys_upper = {k.upper() for k in act["keys"]}
        props_upper = {p.upper() for p in act["props"]}
        if field.upper() in keys_upper:
            verdict = "IS_KEY"
        elif field.upper() in props_upper:
            verdict = "PRESENT_BUT_NOT_KEY"
        else:
            verdict = "ABSENT"
        key_rows.append([table, field, inits, svc_set[1], verdict, why])

    with open(out / "required_fields.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sap_table", "field", "initiatives", "frs_expected_state", "entity_set", "verdict", "why_it_matters"])
        w.writerows(rows)
        w.writerow([])
        w.writerow(["sap_table", "field", "initiatives", "entity_set", "key_verdict", "why_it_matters"])
        w.writerows(key_rows)

    lines = [f"SAP field exposure requests generated from live $metadata, {utcnow()}", "",
             "Each item below is a field one of the I07/I08/I13 FRS documents says the build reads, which is",
             "not in the OData projection today. These are exposure requests, not design questions.", ""]
    for table, field, inits, set_name, verdict, why in missing:
        flag = " (REGRESSION - the FRS records this as exposed)" if verdict == "NEWLY_MISSING" else ""
        lines += [f"{table}.{field}  ->  {set_name}   [{inits}]{flag}", f"    {why}", ""]
    not_key = [k for k in key_rows if k[4] == "PRESENT_BUT_NOT_KEY"]
    if not_key:
        lines += ["Entity-key changes requested:", ""]
        for table, field, inits, set_name, verdict, why in not_key:
            lines += [f"{table}.{field}  ->  {set_name}   [{inits}]", f"    {why}", ""]
    (out / "field_requests.txt").write_text("\n".join(lines), encoding="utf-8")

    counts = Counter(r[5] for r in rows)
    print("\nRequired-field exposure register:", dict(counts))
    for table, field, inits, set_name, verdict, _ in missing:
        print(f"   MISSING  {table}.{field:12s} on {set_name:28s} [{inits}]"
              + ("  <-- REGRESSION" if verdict == "NEWLY_MISSING" else ""))
    for row in key_rows:
        print(f"   KEY      {row[0]}.{row[1]:12s} on {row[3]:28s} {row[4]}")
    note("Field exposure", f"{len(missing)} FRS-required field(s) absent from the projection; "
                           f"{sum(1 for r in rows if r[5] == 'NOW_EXPOSED')} newly exposed since the FRS; "
                           f"{sum(1 for r in rows if r[5] == 'NEWLY_MISSING')} regression(s).")
    return missing


def run_fr9(s, token, out):
    fr9 = []
    for flt in ["Objectclas eq 'MATERIAL'", "Objectclas eq 'MATERIAL' and Tabname eq 'MARC'", "Objectclas eq 'BANF'"]:
        rc, token = cpi_get(s, token, f"{ZMM}/ChangeDocItemSet/$count", f"$filter={flt}")
        fr9.append(f"{flt:55s} -> {rc.text.strip() if rc.status_code == 200 else 'HTTP ' + str(rc.status_code) + ' ' + rc.text[:120]}")
    (out / "fr9_check.txt").write_text(
        "\n".join(fr9) + "\n\nI07 FRS s7 (21-Sep-2026) records zero CDPOS rows for Objectclas='MATERIAL', which leaves "
        "FR-9 adoption tracking unevidenced and every adoption result reading 'Unknown'. A non-zero MATERIAL count here "
        "would supersede that finding. Field-level counts for DISMM/EISBE/MINBE/MABST are in probes.csv.\n")
    print("\nFR-9 check:\n  " + "\n  ".join(fr9))
    if fr9 and fr9[0].strip().endswith("0"):
        note("I07 FR-9", "Zero CDPOS rows for Objectclas='MATERIAL' over OData - adoption tracking remains unevidenced.")
    return token


def run_probes(s, token, out, initiative=None):
    rows = []
    first_page_keys = None
    print("\nProbes:")
    for init, purpose, task, svc_path, target, query in PROBES:
        if initiative and init not in ("ALL",) and initiative not in init:
            continue
        api_path = f"{svc_path}/{target}"
        r, token = cpi_get(s, token, api_path, query, retries=2)
        elapsed = round(r.elapsed.total_seconds(), 1)
        result, nrows, has_next, note_txt = "", "", "", ""
        if r.status_code != 200:
            result = f"HTTP {r.status_code}"
        elif target.endswith("/$count"):
            result = r.text.strip()
        else:
            data, nxt, inline = parse_json_feed(r.text)
            nrows = len(data)
            has_next = "yes" if nxt else "no"
            result = f"{nrows} row(s)" + (f", __count={inline}" if inline is not None else "")
            if data:
                note_txt = json.dumps(strip_meta(data[0]), default=str)[:300]
            if target == "ChangeDocItemSet" and query.startswith("$top=5&$format"):
                first_page_keys = {tuple(strip_meta(x).values()) for x in data}
            elif target == "ChangeDocItemSet" and "$skip=5" in query and first_page_keys is not None:
                second = {tuple(strip_meta(x).values()) for x in data}
                overlap = len(first_page_keys & second)
                note_txt = f"overlap with first page = {overlap} of {len(second)} ({'SKIP IGNORED' if overlap and nrows else 'skip honoured'})"
        rows.append([utcnow(), init, purpose, task, "/" + api_path, query, r.status_code, elapsed, result, nrows, has_next, note_txt])
        print(f"   [{r.status_code}] {init:8s} {purpose:62s} -> {result}  ({elapsed}s)")

    with open(out / "probes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["utc", "initiative", "purpose", "dev_plan_task", "sap_path", "odata_query", "http_status",
                    "elapsed_s", "result", "rows_returned", "has_next", "note_or_sample"])
        w.writerows(rows)

    # OAR classifier reconciliation: VB + ND + PD + V1 + blank should equal the MaterialPlantSet total
    counts = {r[2]: r[8] for r in rows if r[2].startswith("OAR classifier: MRP type")}
    if counts:
        total_r, token = cpi_get(s, token, f"{SHARED}/MaterialPlantSet/$count")
        try:
            parts = {k.split("MRP type ")[1].split(" ")[0]: int(v) for k, v in counts.items()}
            total = int(total_r.text.strip())
            oar = parts.get("ND", 0) + parts.get("PD", 0)
            verdict = "reconciles" if sum(parts.values()) == total else f"UNEXPLAINED REMAINDER {total - sum(parts.values())}"
            print(f"\nOAR classifier reconciliation: {parts} sum={sum(parts.values())} vs MaterialPlantSet total={total} -> {verdict}")
            note("OAR scope", f"DISMM in (ND, PD) = {oar} of {total} material-plant positions; VB = {parts.get('VB', 0)}; "
                              f"excluded = {total - oar - parts.get('VB', 0)}. Reconciliation: {verdict}.")
        except (ValueError, IndexError):
            print("\nOAR classifier reconciliation: one or more MRP-type counts failed; see probes.csv")
    return token


def run_key_collapse(s, token, out, sample=500):
    """Quantify the ChangeDocItem key collapse: declared 3-column key vs the full 7-column CDPOS key."""
    declared = ("Objectclas", "Objectid", "Changenr")
    composite = declared + ("Tabname", "Tabkey", "Fname", "Chngind")
    r, token = cpi_get(s, token, f"{ZMM}/ChangeDocItemSet",
                       f"$filter=Objectclas eq 'MATERIAL' and Tabname eq 'MARC'&$top={sample}&{JSON}", retries=2)
    lines = [f"ChangeDocItemSet key collapse check, {utcnow()}, sample = MATERIAL/MARC $top={sample}"]
    if r.status_code != 200:
        lines.append(f"HTTP {r.status_code}: could not fetch sample")
    else:
        data, _, _ = parse_json_feed(r.text)
        d_keys = {tuple(x.get(k) for k in declared) for x in data}
        c_keys = {tuple(x.get(k) for k in composite) for x in data}
        lines += [f"rows returned                      : {len(data)}",
                  f"distinct on declared key (3 cols)  : {len(d_keys)}",
                  f"distinct on composite key (7 cols) : {len(c_keys)}",
                  f"rows unaddressable under declared  : {len(data) - len(d_keys)} ({(len(data) - len(d_keys)) / len(data):.0%})" if data else "no rows",
                  "", "Fields changed in sample (Fname -> rows):"]
        for fname, n in Counter(x.get("Fname") for x in data).most_common(20):
            lines.append(f"  {fname:12s} {n}")
    (out / "key_collapse.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    return token


def fetch_all(s, token, api_path, flt="", page=1000, max_pages=200, orderby=""):
    """Client-driven paging with $top/$skip (the gateway emits no __next).
    Returns (rows, token, pages, truncated). Truncation is reported, never silent."""
    rows, skip, pages, truncated = [], 0, 0, False
    while True:
        if pages >= max_pages:
            truncated = True
            break
        q = (f"$filter={flt}&" if flt else "") + (f"$orderby={orderby}&" if orderby else "") + f"$top={page}&$skip={skip}&{JSON}"
        r, token = cpi_get(s, token, api_path, q, retries=2)
        if r.status_code != 200:
            break
        data, _, _ = parse_json_feed(r.text)
        rows += [strip_meta(x) for x in data]
        pages += 1
        if len(data) < page:
            break
        skip += page
    if truncated:
        print(f"   WARNING: {api_path} truncated at {max_pages} pages ({len(rows)} rows). Raise --max-pages.")
        note("Paging", f"{api_path}: pull truncated at {len(rows)} rows ({max_pages} pages). Figures derived from it are partial.")
    return rows, token, pages, truncated


def count_of(s, token, api_path, flt):
    """Filtered count: /$count first, $inlinecount fallback for sets whose /$count dumps."""
    status = 200
    if api_path not in COUNT_DUMPS:
        r, token = cpi_get(s, token, f"{api_path}/$count", f"$filter={flt}", retries=1)
        if r.status_code == 200 and r.text.strip().isdigit():
            return int(r.text.strip()), 200, token
        status = r.status_code
        if r.status_code >= 500:
            COUNT_DUMPS.add(api_path)
    r2, token = cpi_get(s, token, api_path, f"$filter={flt}&$inlinecount=allpages&$top=1&{JSON}", retries=1)
    if r2.status_code == 200:
        _, _, inline = parse_json_feed(r2.text)
        if inline is not None and str(inline).isdigit():
            return int(inline), 200, token
    return None, (status if status != 200 else r2.status_code), token


IMPOSSIBLE = {"Edm.String": "eq 'ZZ~NOPE'", "Edm.Decimal": "eq 987654321.123M",
              "Edm.DateTime": "eq datetime'1900-01-01T00:00:00'"}


def run_filter_support(s, token, out, actual_props, totals, only_sets=None):
    """Impossible-value test per property. Honoured -> 0. Ignored -> the set total. Anything else -> odd."""
    rows = []
    print("\nFilter support sweep (impossible-value test):")
    for (svc, set_name), props in actual_props.items():
        if only_sets and set_name not in only_sets:
            continue
        total = totals.get((svc, set_name))
        if not total:
            continue  # empty or unreachable set: the test cannot distinguish anything
        api_path = f"sap/opu/odata/sap/{svc}/{set_name}"
        for p in props:
            lit = IMPOSSIBLE.get(p["type"])
            if not lit:
                rows.append([svc, set_name, p["name"], p["type"], total, "", "NOT_TESTED"])
                continue
            n, status, token = count_of(s, token, api_path, f"{p['name']} {lit}")
            if status != 200:
                verdict = f"REJECTED_HTTP_{status}"
            elif n == 0:
                verdict = "HONOURED"
            elif n == total:
                verdict = "IGNORED"
            else:
                verdict = "PARTIAL_OR_ODD"
            rows.append([svc, set_name, p["name"], p["type"], total, n if n is not None else "", verdict])
        v = [r[6] for r in rows if r[0] == svc and r[1] == set_name]
        print(f"   {set_name:32s} honoured={v.count('HONOURED'):2d} ignored={v.count('IGNORED'):2d} "
              f"rejected={sum(x.startswith('REJECTED') for x in v):2d} not_tested={v.count('NOT_TESTED')}")
    with open(out / "filter_support.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["service", "entity_set", "property", "type", "set_total", "count_with_impossible_filter", "verdict"])
        w.writerows(rows)
    ignored = [r for r in rows if r[6] == "IGNORED"]
    tested = [r for r in rows if r[6] in ("HONOURED", "IGNORED", "PARTIAL_OR_ODD")]
    note("Defects", f"F1: {len(ignored)} of {len(tested)} tested properties silently ignore $filter."
                    + (" Previously 85 of 230." if not only_sets else ""))
    resb_ignored = [r for r in ignored if r[1] == "ReservationItemSet"]
    note("Defects", f"R1 (ReservationItemSet priority fix): {len(resb_ignored)} ignored propert(ies)"
                    + (f" - {', '.join(r[2] for r in resb_ignored)}" if resb_ignored else " - appears FIXED."))
    return token, rows


# {MATNR} is filled at run time with a material taken from the MaterialPlantSet
# pull -- the median of the sorted keys, so "ne" excludes exactly one row and
# "gt" excludes about half. A hardcoded literal cannot do that here: MATNR is
# ALPHA-converted, so an unpadded constant matches nothing. That made the
# expected count equal the set total, which is also what an ignored filter
# returns -- the probe could not tell the two apart.
MATNR_SLOT = "{MATNR}"

OPERATOR_PROBES = [
    ("MaterialPlantSet", "Matnr ne '{MATNR}'",                              "ne"),
    ("MaterialPlantSet", "Matnr ge '000000008000000000' and Matnr le '000000008099999999'", "ge/le range on string"),
    ("MaterialPlantSet", "Matnr gt '{MATNR}'",                              "gt on string"),
    ("MaterialPlantSet", "Dismm eq 'ND' or Dismm eq 'PD'",                  "or on honoured property"),
    ("MaterialPlantSet", "startswith(Matnr,'0000000080')",                   "startswith"),
    ("MaterialPlantSet", "substringof('800',Matnr)",                        "substringof"),
    ("MaterialPlantSet", "Werks eq '1300' and Dismm eq 'PD'",               "and across two honoured properties"),
    ("MaterialDocumentHeaderSet", "Budat ge datetime'2026-01-01T00:00:00'", "date ge (delta load by posting date)"),
    ("MaterialDocumentHeaderSet", "Budat ge datetime'2013-01-01T00:00:00' and Budat lt datetime'2014-01-01T00:00:00'", "date range"),
    ("MaterialDocumentHeaderSet", "Budat eq datetime'2013-09-27T00:00:00'", "date eq (control, sample row date)"),
    ("PurchaseOrderSet", "Aedat ge datetime'2026-01-01T00:00:00'",          "date ge on PO header"),
    ("ChangeDocHeaderSet", "Udate ge datetime'2026-01-01T00:00:00'",        "date ge on CDHDR (delta for FR-9)"),
    ("ChangeDocHeaderSet", "Objectclas eq 'MATERIAL' and Udate ge datetime'2018-01-01T00:00:00'", "eq + date ge"),
    ("ChangeDocItemSet", "Objectclas eq 'MATERIAL' and (Tabname eq 'MARC' or Tabname eq 'MARA')", "or inside parentheses"),
    ("PurchaseOrderItemSet", "Ebeln eq '4500000001' or Ebeln eq '4500000002'", "or on key"),
    ("StockMovementStatisticSet", "Letztbew ge datetime'2020-01-01T00:00:00'", "date ge on S032"),
]
SET_TO_SVC = {name: svc for svc, names in SERVICES.items() for name in names}


def run_operator_support(s, token, out, totals, mp_rows):
    rows = []
    print("\nOperator support:")
    keys = sorted({r.get("Matnr", "") for r in mp_rows if r.get("Matnr")})
    probe_matnr = keys[len(keys) // 2] if keys else None
    if probe_matnr:
        print(f"   MATNR probe value drawn from the pull: {probe_matnr}")
    for set_name, expr, label in OPERATOR_PROBES:
        svc = SET_TO_SVC[set_name]
        if MATNR_SLOT in expr:
            if not probe_matnr:
                rows.append([svc, set_name, label, expr,
                             totals.get((svc, set_name)), "", "", "NOT_TESTED_NO_MATNR_SAMPLE"])
                print(f"   {set_name:28s} {label:44s} -> NOT_TESTED (no rows to draw a key from)")
                continue
            expr = expr.replace(MATNR_SLOT, probe_matnr)
        api_path = f"sap/opu/odata/sap/{svc}/{set_name}"
        n, status, token = count_of(s, token, api_path, expr)
        total = totals.get((svc, set_name))
        expected = ""
        if set_name == "MaterialPlantSet" and mp_rows:
            m = [r.get("Matnr", "") for r in mp_rows]
            exp_map = {
                "ne": sum(x != probe_matnr for x in m),
                "ge/le range on string": sum("000000008000000000" <= x <= "000000008099999999" for x in m),
                "gt on string": sum(x > probe_matnr for x in m),
                "or on honoured property": sum(r.get("Dismm") in ("ND", "PD") for r in mp_rows),
                "startswith": sum(x.startswith("0000000080") for x in m),
                "substringof": sum("800" in x for x in m),
                "and across two honoured properties": sum(r.get("Werks") == "1300" and r.get("Dismm") == "PD" for r in mp_rows),
            }
            expected = exp_map.get(label, "")
        if status != 200:
            verdict = f"REJECTED_HTTP_{status}"
        elif expected != "":
            # Test the ambiguous case first. When the expected count equals the
            # set total, "n == expected" is also exactly what an ignored filter
            # returns, so checking WORKS first would report that as a pass.
            if n == total and expected == total:
                verdict = "AMBIGUOUS_EXPECTED_EQUALS_TOTAL"
            elif n == expected:
                verdict = "WORKS"
            elif n == total:
                verdict = "IGNORED"
            elif n == 0:
                verdict = "UNSUPPORTED_EMPTY"
            else:
                verdict = "WRONG_RESULT"
        elif n == 0:
            verdict = "UNSUPPORTED_EMPTY_OR_NO_DATA"
        elif n == total:
            verdict = "IGNORED"
        else:
            verdict = "PLAUSIBLE"
        rows.append([svc, set_name, label, expr, total, n if n is not None else "", expected, verdict])
        print(f"   {set_name:28s} {label:44s} -> {n} (expected {expected if expected != '' else '?'}, total {total}) {verdict}")
    with open(out / "operator_support.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["service", "entity_set", "operator", "filter", "set_total", "count", "expected_client_side", "verdict"])
        w.writerows(rows)
    works = [r[2] for r in rows if r[7] == "WORKS"]
    note("Defects", f"F3: operators honoured on MaterialPlantSet: {', '.join(works) if works else 'none beyond eq'}.")
    return token, rows


def run_paging_stability(s, token, out, totals):
    """Pull MaterialPlantSet twice without $orderby and once with; a stable pull has zero duplicate/missing keys."""
    api_path = f"{SHARED}/MaterialPlantSet"
    total = totals.get((ADD_SRV, "MaterialPlantSet"))
    def key(r):
        return (r.get("Matnr"), r.get("Werks"))
    a, token, _, _ = fetch_all(s, token, api_path)
    b, token, _, _ = fetch_all(s, token, api_path)
    c, token, _, _ = fetch_all(s, token, api_path, orderby="Matnr,Werks")
    lines = [f"MaterialPlantSet paging stability, {utcnow()}, $count total = {total}"]
    dupes_unordered = None
    for name, rws in (("pull 1, no $orderby", a), ("pull 2, no $orderby", b), ("pull 3, $orderby=Matnr,Werks", c)):
        ks = [key(r) for r in rws]
        d = len(ks) - len(set(ks))
        if name.startswith("pull 1"):
            dupes_unordered = d
        lines.append(f"  {name:30s} rows={len(rws):6d} distinct keys={len(set(ks)):6d} duplicates={d:5d} "
                     f"missing vs total={(total or 0) - len(set(ks)):6d}")
    lines.append(f"  keys in pull 1 not in pull 2: {len(set(map(key, a)) - set(map(key, b)))}; "
                 f"pull 2 not in pull 1: {len(set(map(key, b)) - set(map(key, a)))}")
    lines += ["", "Filtered pull vs filtered $count, Dismm values (a mismatch means the filter or the paging is not exact):"]
    for v in ("VB", "ND", "PD", "V1"):
        n, _, token = count_of(s, token, api_path, f"Dismm eq '{v}'")
        pulled, token, _, _ = fetch_all(s, token, api_path, f"Dismm eq '{v}'", orderby="Matnr,Werks")
        in_full = sum(r.get("Dismm") == v for r in c)
        lines.append(f"  {v:3s} $count={n}  filtered pull rows={len(pulled)}  in the ordered full pull={in_full}  "
                     f"{'consistent' if n == len(pulled) == in_full else 'MISMATCH'}")
        if pulled and any(r.get("Dismm") != v for r in pulled):
            lines.append(f"      filtered pull contains rows whose Dismm is not {v}: {dict(Counter(r.get('Dismm') for r in pulled))}")
    (out / "paging_stability.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    note("Defects", f"F4: MaterialPlantSet pulled without $orderby carries {dupes_unordered} duplicate key(s)"
                    + (" - the defect appears FIXED." if dupes_unordered == 0 else " - STILL OPEN; every pull needs $orderby."))
    return token, c, dupes_unordered


# ---------------------------------------------------------------------------------------------
# New checks (v5)
# ---------------------------------------------------------------------------------------------
def run_reservation_cap(s, token, out):
    """I08 s7: ReservationItemSet reports exactly 1,000 rows against 105,848 in the delivered extract.
    Page until a short page and compare, to separate a service-side cap from a true count."""
    api_path = f"{ZMM}/ReservationItemSet"
    lines = [f"ReservationItemSet page-cap test, {utcnow()}",
             "I08 FRS s7 records exactly 1,000 rows over OData; the delivered extract holds 105,848 RESB rows.",
             "A cap means every reservation-dependent figure (I13 STITCH, I08 session traceability) is computed on 1%.", ""]
    rc, token = cpi_get(s, token, f"{api_path}/$count", retries=1)
    declared = rc.text.strip() if rc.status_code == 200 else f"HTTP {rc.status_code}"
    lines.append(f"  /$count                    : {declared}")
    r2, token = cpi_get(s, token, api_path, f"$inlinecount=allpages&$top=1&{JSON}", retries=1)
    _, _, inline = parse_json_feed(r2.text) if r2.status_code == 200 else ([], None, None)
    lines.append(f"  $inlinecount=allpages      : {inline}")
    for skip in (0, 999, 1000, 1500, 5000):
        r, token = cpi_get(s, token, api_path, f"$top=5&$skip={skip}&{JSON}", retries=1)
        data, _, _ = parse_json_feed(r.text) if r.status_code == 200 else ([], None, None)
        lines.append(f"  $top=5&$skip={skip:<6d}        : HTTP {r.status_code}, {len(data)} row(s)")
    rows, token, pages, truncated = fetch_all(s, token, api_path, orderby="Rsnum,Rspos")
    keys = {(r.get("Rsnum"), r.get("Rspos")) for r in rows}
    lines += ["", f"  page-until-short-page      : {len(rows)} rows in {pages} page(s), {len(keys)} distinct (Rsnum, Rspos)"
                  + ("  [TRUNCATED at --max-pages]" if truncated else "")]
    try:
        declared_n = int(declared)
        if len(rows) > declared_n:
            verdict = (f"CAP CONFIRMED on $count: paging returned {len(rows)} rows against a declared {declared_n}. "
                       f"$count is not a usable total on this set.")
        elif len(rows) == declared_n == 1000:
            verdict = ("BOTH report 1,000. Either a hard service-side cap or a genuinely 1,000-row selection. "
                       "Ask NTT for the RESB selection criteria in the DPC - the extract holds 105,848 rows.")
        else:
            verdict = f"$count and paging agree at {len(rows)} rows."
    except ValueError:
        verdict = f"$count unavailable ({declared}); paging returned {len(rows)} rows."
    lines += ["", f"  VERDICT: {verdict}"]
    (out / "reservation_cap.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    note("Defects", f"R2 (ReservationItemSet volume): {verdict}")
    return token, len(rows)


PLANT_SETS = [
    ("MaterialPlantSet", SHARED, "Werks", "MARC - OAR scope. I13 s7.2: no MARC rows for plant 1500 in the extract."),
    ("StorageLocationStockSet", SHARED, "Werks", "MARD - stock on hand"),
    ("GoodsMovementItemSet", SHARED, "Werks", "MSEG - movements. 1500 carries 122,868 movements in the extract."),
    ("PurchaseRequisitionSet", SHARED, "Werks", "EBAN - PRs. Extract is plant 1300 only."),
    ("ReservationItemSet", ZMM, "Werks", "RESB - reservations. 1500 carries 10,333 in the extract."),
    ("PurchaseOrderItemSet", SHARED, "Werks", "EKPO - PO items. 1500 carries 35,083 in the extract."),
]


def run_plant_coverage(s, token, out, mp_rows=None):
    """Per-WERKS counts, aggregated client-side because Werks filters may be ignored.
    Answers whether OData has the same plant hole as the delivered extract."""
    lines = [f"Plant coverage, {utcnow()}", "",
             "Counts are aggregated client-side from a full pull, because a server-side Werks filter may be ignored",
             "(see filter_support.csv). The question this answers: does OData expose plant 1500 (Gamsberg) on MARC,",
             "or is the hole in the extract only? I13 s7.2 records every 1500 material classifying as Excluded.", ""]
    coverage = {}
    for set_name, svc_path, prop, why in PLANT_SETS:
        if set_name == "MaterialPlantSet" and mp_rows is not None:
            rows, pages, truncated = mp_rows, "reused", False
        else:
            rows, token, pages, truncated = fetch_all(s, token, f"{svc_path}/{set_name}")
        c = Counter(r.get(prop) or "<blank>" for r in rows)
        coverage[set_name] = c
        lines.append(f"{set_name}  ({why})")
        lines.append(f"  total rows: {len(rows)}" + ("  [TRUNCATED]" if truncated else "") + f"  pages: {pages}")
        for w, n in sorted(c.items()):
            lines.append(f"    {w:8s} {n}")
        lines.append("")
    marc_plants = set(coverage.get("MaterialPlantSet", {}))
    txn_plants = set()
    for s_name in ("GoodsMovementItemSet", "ReservationItemSet", "PurchaseOrderItemSet", "StorageLocationStockSet"):
        txn_plants |= set(coverage.get(s_name, {}))
    orphan = sorted(txn_plants - marc_plants - {"<blank>"})
    lines += ["Plants transacting with no MARC coverage (invisible to I07 and I13 OAR scope):",
              "  " + (", ".join(orphan) if orphan else "none - every transacting plant has MARC rows")]
    if "1500" in marc_plants:
        lines.append("\nPlant 1500 (Gamsberg) HAS MARC rows over OData. The gap recorded in I13 s7.2 is an extract gap, "
                     "not a source gap - re-extract rather than raise it with NTT.")
        note("Plant coverage", "Plant 1500 has MARC rows over OData: the I13 s7.2 gap is an extract problem, not a SAP one.")
    else:
        lines.append("\nPlant 1500 (Gamsberg) has NO MARC rows over OData either. The gap is in the source or in the DPC "
                     "selection - raise it with NTT as a selection question, not as a re-extract.")
        note("Plant coverage", "Plant 1500 has no MARC rows over OData either - raise the DPC selection with NTT.")
    if orphan:
        note("Plant coverage", f"Transacting plants with no MARC coverage: {', '.join(orphan)}.")
    (out / "plant_coverage.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines[:40]) + ("\n  ..." if len(lines) > 40 else ""))
    return token


def run_history_depth(s, token, out):
    """I13 FR-6 needs 731 days of movement history to separate Slow from Non-moving.
    Establish the Budat span available over OData, by $orderby if it works and client-side otherwise."""
    api_path = f"{SHARED}/MaterialDocumentHeaderSet"
    lines = [f"Movement history depth, {utcnow()}", "",
             "I13 s7.2: the SOP bands need 731 days to separate Slow (366-730) from Non-moving (over 730).",
             "The delivered extract spans 08-Aug-2025 to 20-Aug-2026, about 409 days, so 40,868 of 44,394 OAR",
             "positions default to Non-moving. This checks what OData itself can supply.", ""]
    span = {}
    for label, order in (("earliest", "Budat asc"), ("latest", "Budat desc")):
        r, token = cpi_get(s, token, api_path, f"$orderby={order}&$top=1&{JSON}", retries=1)
        data, _, _ = parse_json_feed(r.text) if r.status_code == 200 else ([], None, None)
        val = data[0].get("Budat") if data else None
        span[label] = val
        lines.append(f"  $orderby={order:12s} -> HTTP {r.status_code}, Budat = {val}")
    lines.append("")
    lines.append("  Caution: if $orderby is not honoured (see operator_support.csv) these two are the same arbitrary row.")
    if span.get("earliest") and span["earliest"] == span.get("latest"):
        lines.append("  Both returned the same value - $orderby is being ignored. Falling back to a full client-side pull.")
        rows, token, pages, truncated = fetch_all(s, token, api_path)
        vals = sorted(v for v in (r.get("Budat") for r in rows) if v)
        if vals:
            span = {"earliest": vals[0], "latest": vals[-1]}
            lines.append(f"  client-side over {len(rows)} rows{' [TRUNCATED]' if truncated else ''}: "
                         f"{vals[0]} .. {vals[-1]}")
    def to_epoch_ms(v):
        # OData v2 JSON dates arrive as /Date(1234567890000)/
        try:
            return int(str(v).split("(")[1].split(")")[0].split("+")[0])
        except (IndexError, ValueError):
            return None
    a, b = to_epoch_ms(span.get("earliest")), to_epoch_ms(span.get("latest"))
    if a is not None and b is not None:
        days = abs(b - a) / 86400000
        lines += ["", f"  Span available over OData: {days:.0f} days ({days / 30.44:.1f} months)",
                  f"  I13 FR-6 requirement     : 731 days (25 months)",
                  f"  VERDICT: {'SUFFICIENT - re-extract over this window and the aging bands become valid.' if days >= 731 else 'INSUFFICIENT over OData as well - the Non-moving band cannot be evidenced from this source.'}"]
        note("I13 aging", f"Movement history over OData spans {days:.0f} days against the 731 FR-6 needs - "
                          f"{'sufficient, re-extract' if days >= 731 else 'insufficient at source'}.")
    else:
        lines.append("\n  Could not resolve the Budat span; see the raw values above.")
    (out / "history_depth.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    return token


def run_coverage_ratios(s, token, out, totals):
    """MAKT vs MARA vs MARC populations. I13 s7.1: MAKT covers 8.2% of materials, so most dashboard rows have no label."""
    g = lambda svc, st: totals.get((svc, st))
    mara = g(ADD_SRV, "MaterialSet")
    makt = g(ADD_SRV, "MaterialDescriptionSet")
    marc = g(ADD_SRV, "MaterialPlantSet")
    lines = [f"Population coverage, {utcnow()}", "",
             f"  MaterialSet (MARA)            : {mara}",
             f"  MaterialDescriptionSet (MAKT) : {makt}",
             f"  MaterialPlantSet (MARC)       : {marc}", ""]
    if mara and makt:
        lines.append(f"  MAKT / MARA  : {makt / mara:.1%}   (I13 FR-10 dashboard labels; extract showed 8.2%)")
    if mara and marc:
        lines.append(f"  MARA / MARC  : {mara / marc:.1%}   (I07 s7: MARA covers only ~8% of the MARC population)")
    lines += ["", "If these ratios are materially better over OData than in the delivered extract, the coverage problem",
              "is an extraction problem and is fixed by re-extracting, not by a SAP change request."]
    (out / "coverage_ratios.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    if mara and makt:
        note("Coverage", f"MAKT covers {makt / mara:.1%} of MaterialSet over OData (extract showed 8.2%).")
    return token


def run_vgabe_profile(s, token, out):
    """The dictionary says EKBE VGABE 1 (GR) and 2 (IR); I13 s7.1 filters to VGABE = E. Settle it from the data."""
    rows, token, pages, truncated = fetch_all(s, token, f"{SHARED}/POHistorySet")
    c = Counter(r.get("Vgabe") for r in rows)
    lines = [f"POHistorySet (EKBE) Vgabe profile, {utcnow()}: {len(rows)} rows in {pages} page(s)"
             + ("  [TRUNCATED]" if truncated else ""), "",
             "The entity dictionary scopes EKBE to VGABE 1 (GR) and 2 (IR). I13 FRS s7.1 filters to VGABE = E.",
             "Only one of those can be right against this service. Distinct values:", ""]
    for v, n in c.most_common():
        lines.append(f"  {str(v) or '<blank>':8s} {n}")
    bwart = Counter(r.get("Bwart") for r in rows if str(r.get("Vgabe")) in ("1", "E"))
    lines += ["", "Bwart on the goods-receipt rows (whichever Vgabe value carries them):"]
    for v, n in bwart.most_common(10):
        lines.append(f"  {str(v) or '<blank>':8s} {n}")
    (out / "vgabe_profile.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    vals = [str(v) for v in c if v is not None]
    note("Dictionary", f"EKBE Vgabe distinct values over OData: {', '.join(sorted(vals)) or 'none'}. "
                       f"Reconcile against the dictionary (1, 2) and I13 s7.1 (E).")
    return token


def run_marc_changes(s, token, out):
    """Server-side Fname filters may be ignored, so pull every MATERIAL/MARC change row and aggregate client-side."""
    rows, token, pages, truncated = fetch_all(s, token, f"{ZMM}/ChangeDocItemSet",
                                              "Objectclas eq 'MATERIAL' and Tabname eq 'MARC'", orderby="Changenr")
    lines = [f"MATERIAL/MARC change items, {utcnow()}: {len(rows)} rows in {pages} page(s)"
             + ("  [TRUNCATED]" if truncated else ""), "", "Fname x Chngind:"]
    for (fn, ci), n in Counter((r.get("Fname"), r.get("Chngind")) for r in rows).most_common():
        lines.append(f"  {fn or '<blank>':12s} {ci or '-':2s} {n}")
    planning = [r for r in rows if r.get("Fname") in ("DISMM", "EISBE", "MINBE", "MABST")]
    lines += ["", f"Planning-field changes (DISMM/EISBE/MINBE/MABST): {len(planning)}"]
    if planning:
        lines.append("DISMM transitions old -> new:")
        for (o, n_), cc in Counter((r.get("Value_old"), r.get("Value_new")) for r in planning if r.get("Fname") == "DISMM").most_common():
            lines.append(f"  {o or '<blank>':4s} -> {n_ or '<blank>':4s} {cc}")
        lines.append(f"Distinct materials (Objectid) with a planning-field change: {len({r.get('Objectid') for r in planning})}")
    else:
        lines.append("No planning-field change history in this client: I07 FR-9 can be built but not validated here, "
                     "and every adoption result reads 'Unknown' (matches I07 FRS s7).")
    (out / "marc_changes_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines[:16]) + ("\n  ..." if len(lines) > 16 else ""))
    return token


def run_mrp_profile(s, token, out, rows=None):
    if rows is None:
        rows, token, pages, truncated = fetch_all(s, token, f"{SHARED}/MaterialPlantSet", orderby="Matnr,Werks")
    else:
        pages, truncated = "reused", False
    lines = [f"MaterialPlantSet full pull, {utcnow()}: {len(rows)} rows in {pages} page(s)"
             + ("  [TRUNCATED]" if truncated else ""), "", "Dismm x Werks:"]
    for (d, w), n in sorted(Counter((r.get("Dismm") or "<blank>", r.get("Werks")) for r in rows).items()):
        lines.append(f"  {d:8s} {w:6s} {n}")
    lines += ["", "Per MRP type: rows, with reorder point (Minbe>0), max stock (Mabst>0), safety stock (Eisbe>0):"]
    def pos(v):
        try:
            return float(v) > 0
        except (TypeError, ValueError):
            return False
    for d, grp in sorted(Counter(r.get("Dismm") or "<blank>" for r in rows).items()):
        sub = [r for r in rows if (r.get("Dismm") or "<blank>") == d]
        lines.append(f"  {d:8s} {grp:6d}  rop={sum(pos(r.get('Minbe')) for r in sub):5d}  "
                     f"max={sum(pos(r.get('Mabst')) for r in sub):5d}  ss={sum(pos(r.get('Eisbe')) for r in sub):5d}")
    oar = [r for r in rows if r.get("Dismm") in ("ND", "PD")]
    vb = sum(r.get("Dismm") == "VB" for r in rows)
    lines += ["", f"OAR population under the 08-Sep ruling (Dismm in ND, PD): {len(oar)} of {len(rows)}; "
                  f"VB (Min-Max): {vb}; excluded (V1, blank, other): {len(rows) - len(oar) - vb}",
              "", "Eisbe population above matters for I07: the FRS records EISBE as exposed over OData but absent from "
                  "the delivered 19-column extract. A zero here would mean exposed-but-unpopulated, which is a different problem."]
    (out / "mrp_type_profile.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))

    write_value_domains(out, ADD_SRV, "MaterialPlantSet", "Dismm", rows)
    return token, rows


def write_value_domains(out, service, set_name, field, rows):
    """Record ``field``'s value distribution, from rows already pulled.

    Restores the value_domains.csv this discovery folder shipped until 18-Sep,
    when a refactor left no code path writing it -- test_sap_contract.py's
    TestValueDomains and its self-consistency check have named the missing
    file ever since (see the 18-Sep handover note in git log).

    Deliberately reuses whatever full pull the caller already made rather than
    re-fetching: MaterialPlantSet's Dismm distribution is exactly what
    run_mrp_profile just computed for mrp_type_profile.txt, and a second live
    pull to populate a second report would be the same 2,183-row cost paid
    twice for one answer. "(blank)" matches the value known_conditions.py's
    DISMM_VALUE_DOMAIN maps to "" -- see test_sap.py's translation of it.

    Only one field is profiled here today (Dismm). This module previously also
    profiled Mstae from a standing VALUE_DOMAIN_PROBES list; that list is not
    restored, so a caller adding a second field back must decide then whether
    this write should accumulate across a run or, as here, start the file
    fresh each sweep -- "w" mode, matching every other report in this script.
    """
    tally = Counter(r.get(field) or "(blank)" for r in rows)
    total = sum(tally.values())
    with open(out / "value_domains.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["service", "entity_set", "field", "value", "count", "pct_of_scanned_total"])
        for value, count in tally.most_common():
            pct = f"{count / total * 100:.1f}%" if total else ""
            w.writerow([service, set_name, field, value, count, pct])


def run_material_profile(s, token, out):
    rows, token, pages, truncated = fetch_all(s, token, f"{SHARED}/MaterialSet", orderby="Matnr")
    nums = [r.get("Matnr", "") for r in rows]
    lines = [f"MaterialSet full pull, {utcnow()}: {len(rows)} rows in {pages} page(s)"
             + ("  [TRUNCATED]" if truncated else ""), "", "Matnr length distribution:"]
    for ln, n in sorted(Counter(len(x) for x in nums).items()):
        lines.append(f"  len {ln:2d}: {n}")
    lines += ["", "Leading two characters after stripping leading zeros:"]
    for pre, n in Counter(x.lstrip("0")[:2] for x in nums).most_common(15):
        lines.append(f"  {pre or '<none>':4s} {n}")
    eighty = [x for x in nums if x.lstrip("0").startswith("80")]
    lines += ["", f"80-series materials present (after stripping leading zeros): {len(eighty)}"
                  + ("  e.g. " + ", ".join(eighty[:5]) if eighty else "  -> I08 80-series detection cannot be validated in this client")]
    lines += ["", "Material type (Mtart) distribution:"]
    for mt, n in Counter(r.get("Mtart") for r in rows).most_common():
        lines.append(f"  {mt or '<blank>':6s} {n}")
    extwg = Counter(r.get("Extwg") for r in rows) if rows and "Extwg" in rows[0] else None
    lines += ["", "Extwg (retired as the OAR key on 08-Sep; informational only):"]
    if extwg:
        for v, n in extwg.most_common(10):
            lines.append(f"  {str(v) or '<blank>':8s} {n}")
    else:
        lines.append("  property not present on MaterialSet - as the FRS records. No longer a blocker.")
    (out / "material_number_profile.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    note("I08 80-series", f"{len(eighty)} 80-series materials visible on MaterialSet.")
    return token


def run_repair_po_check(s, token, out):
    """Pstyp filters may be ignored server-side, so read every line of every ZREP PO and tabulate client-side."""
    heads, token, _, _ = fetch_all(s, token, f"{SHARED}/PurchaseOrderSet", "Bsart eq 'ZREP'")
    lines = [f"ZREP purchase orders, {utcnow()}: {len(heads)} header(s)",
             "I08 D7 proposes Pstyp 3 as the primary repair-PO filter, corroborated by Bsart ZREP and Knttp F.", ""]
    all_items = []
    for h in heads[:200]:
        items, token, _, _ = fetch_all(s, token, f"{SHARED}/PurchaseOrderItemSet", f"Ebeln eq '{h.get('Ebeln')}'")
        all_items += items
        lines.append(f"  {h.get('Ebeln')}  vendor {h.get('Lifnr')}  {str(h.get('Aedat'))[:22]}  items={len(items)}  "
                     f"pstyp={dict(Counter(i.get('Pstyp') for i in items))}  knttp={dict(Counter(i.get('Knttp') for i in items))}")
    if all_items:
        lines += ["", "Across all ZREP lines: Pstyp x Knttp:"]
        for (p, k), n in Counter((i.get("Pstyp"), i.get("Knttp")) for i in all_items).most_common():
            lines.append(f"  pstyp={p or '<blank>'} knttp={k or '<blank>'} {n}")
        lines.append(f"Lines with a material number: {sum(bool(i.get('Matnr')) for i in all_items)} of {len(all_items)}; "
                     f"80-series: {sum(i.get('Matnr', '').lstrip('0').startswith('80') for i in all_items)}")
        lines.append("Note: if every Ebeln filter returned the full set, the Ebeln filter is ignored too; see filter_support.csv.")
        lines.append("Note: PurchaseOrderItemSet publishes no date property (I08 s7, EKPO.ERDAT request), so per-stage "
                     "aging cannot be computed from this set as exposed today.")
    (out / "repair_po_check.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines[:30]) + ("\n  ..." if len(lines) > 30 else ""))
    return token


# ---------------------------------------------------------------------------------------------
# Dictionary gap report: now accepts the CSV (entity-set sheet) as well as the xlsx (field sheet)
# ---------------------------------------------------------------------------------------------
def load_dictionary_sets_csv(path):
    """Sheet-1 CSV export: rows -> dict with SAP object, proposed set name, key fields, declared field count."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) < 13 or not row[0].strip().isdigit():
                continue
            out.append({
                "table": row[1].strip(), "entity_type": row[2].strip(), "set": row[3].strip(),
                "i07": bool(row[5].strip()), "i08": bool(row[6].strip()), "i13": bool(row[7].strip()),
                "keys": [k.strip().lstrip("+ ").strip() for k in row[8].split(",") if k.strip()],
                "status": row[11].strip(), "field_count": row[12].strip(),
            })
    return out


def load_dictionary_fields_xlsx(path):
    """Sheet '2. Field Specification': rows -> (SAP Object, Field, key flag)."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["2. Field Specification"]
    out = []
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row[0] or not row[1]:
            continue
        out.append((str(row[0]).strip(), str(row[1]).strip(), row[3]))
    return out


def write_dictionary_gaps(actual, dictionary, out):
    path = Path(dictionary)
    rows = []
    if path.suffix.lower() == ".csv":
        for d in load_dictionary_sets_csv(path):
            table = d["table"]
            if table in PROPOSED_NOT_EXPOSED:
                rows.append([table, d["set"], "", "", "", "PROPOSED_NOT_EXPOSED", PROPOSED_NOT_EXPOSED[table]])
                continue
            svc_set = TABLE_TO_SET.get(table)
            if not svc_set:
                rows.append([table, d["set"], "", "", "", "NO_SET_MAPPING", ""])
                continue
            act = actual.get(svc_set)
            real = svc_set[1]
            if not act:
                rows.append([table, d["set"], real, "", "", "SET_UNREACHABLE", ""])
                continue
            name_status = "NAME_MATCH" if d["set"] == real else (
                "NAME_ALIAS" if DICT_SET_ALIASES.get(d["set"]) == real else "NAME_MISMATCH")
            keys_upper = {k.upper() for k in act["keys"]}
            dict_keys = [k.upper() for k in d["keys"]]
            missing_keys = [k for k in dict_keys if k and k not in keys_upper]
            extra_keys = sorted(keys_upper - set(dict_keys))
            detail = []
            if name_status != "NAME_MATCH":
                detail.append(f"dictionary set name '{d['set']}' -> actual '{real}'")
            if missing_keys:
                detail.append(f"dictionary keys not in the SAP entity key: {', '.join(missing_keys)}")
            if extra_keys:
                detail.append(f"SAP entity key has extra: {', '.join(extra_keys)}")
            try:
                declared_n = int(d["field_count"])
                if declared_n != len(act["props"]):
                    detail.append(f"dictionary declares {declared_n} fields, projection publishes {len(act['props'])}")
            except (ValueError, TypeError):
                pass
            verdict = "OK" if not detail else ("NAME_AND_KEY_DRIFT" if name_status != "NAME_MATCH" and missing_keys
                                               else ("KEY_DRIFT" if missing_keys else name_status))
            rows.append([table, d["set"], real, ";".join(sorted(act["keys"])), len(act["props"]), verdict, " | ".join(detail)])
        header = ["sap_table", "dictionary_set", "actual_set", "actual_keys", "actual_property_count", "status", "detail"]
    else:
        for table, field, key in load_dictionary_fields_xlsx(path):
            if table in PROPOSED_NOT_EXPOSED:
                rows.append([table, field, "", "", "PROPOSED_NOT_EXPOSED", PROPOSED_NOT_EXPOSED[table]])
                continue
            svc_set = TABLE_TO_SET.get(table)
            if not svc_set:
                rows.append([table, field, "", "", "NO_SET_MAPPING", ""])
                continue
            act = actual.get(svc_set)
            if not act:
                rows.append([table, field, svc_set[1], "", "SET_UNREACHABLE", ""])
                continue
            props_upper = {p.upper(): p for p in act["props"]}
            hit = props_upper.get(field.upper())
            status = "MATCH" if hit else "MISSING_IN_SAP"
            if hit and key == "K" and hit not in act["keys"]:
                status = "MATCH_BUT_NOT_KEY_IN_SAP"
            rows.append([table, field, svc_set[1], hit or "", status, ""])
        header = ["sap_table", "dictionary_field", "entity_set", "actual_property", "status", "detail"]

    with open(out / "dictionary_gaps.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    summary = dict(Counter(r[-2] for r in rows))
    print("\nDictionary gap summary:", summary)
    note("Dictionary", f"Gap report ({path.suffix.lstrip('.')} input): {summary}")


def write_logs(out):
    with open(out / "calls.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["utc", "sap_path", "odata_query", "http_status", "elapsed_s", "bytes"])
        w.writerows(CALLS)
    if FAILURES:
        entry = env("CPI_BASE_URL").rstrip("/") + CPI_PATH
        lines = [f"{len(FAILURES)} failed call(s). CPI entry point: {entry}",
                 "SAP path = what CPI forwards to the ECC gateway; paste it into /IWFND/GW_CLIENT to reproduce.", ""]
        for i, f in enumerate(FAILURES, 1):
            q = f"?{f['api_query']}" if f["api_query"] else ""
            lines += [f"[{i}] HTTP {f['status']} after {f['elapsed_s']}s at {f['utc']}",
                      f"    SAP path : /{f['api_path'].lstrip('/')}{q}",
                      f"    headers  : {json.dumps(f['headers'])}",
                      f"    body     : {f['body'].strip()[:1000] or '<empty - CPI did not forward the backend error>'}", ""]
        (out / "errors.txt").write_text("\n".join(lines), encoding="utf-8")
        with open(out / "errors.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["utc", "sap_path", "api_query", "http_status", "elapsed_s", "body"])
            w.writerows([[f["utc"], "/" + f["api_path"].lstrip("/"), f["api_query"], f["status"], f["elapsed_s"],
                          f["body"].strip()[:500]] for f in FAILURES])
        print(f"\n{len(FAILURES)} failed call(s) -> {out / 'errors.txt'} (also errors.csv)")


def write_status_report(out, args):
    """One consolidated summary per run. This is the artefact to paste into the SAP/NTT thread."""
    slowest = sorted(CALLS, key=lambda c: c[4], reverse=True)[:5]
    lines = [f"# VZI CPI OData status, {utcnow()}", "",
             f"Run mode: {' '.join(sys.argv[1:]) or 'full sweep'}", "",
             f"Calls: {len(CALLS)}  |  failures: {len(FAILURES)}  |  "
             f"total elapsed: {sum(c[4] for c in CALLS):.0f}s", ""]
    by_section = {}
    for sec, txt in NOTES:
        by_section.setdefault(sec, []).append(txt)
    for sec in ("Services", "Defects", "Field exposure", "OAR scope", "Plant coverage", "I13 aging",
                "Coverage", "I07 FR-9", "I08 80-series", "Dictionary", "Paging"):
        if sec in by_section:
            lines.append(f"## {sec}")
            lines += [f"- {t}" for t in by_section[sec]]
            lines.append("")
    leftover = {k: v for k, v in by_section.items() if k not in
                {"Services", "Defects", "Field exposure", "OAR scope", "Plant coverage", "I13 aging",
                 "Coverage", "I07 FR-9", "I08 80-series", "Dictionary", "Paging"}}
    for sec, txts in leftover.items():
        lines.append(f"## {sec}")
        lines += [f"- {t}" for t in txts]
        lines.append("")
    if slowest:
        lines += ["## Slowest calls (W8.1 performance baseline)"]
        lines += [f"- {c[4]:.1f}s  {c[1]}  {c[2][:80]}" for c in slowest]
        lines.append("")
    lines += ["## Confirmed defect register", ""]
    for k, v in DEFECTS.items():
        lines.append(f"- **{k}**: {v}")
    (out / "status_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nStatus report -> {out / 'status_report.md'}")


def write_defect_status(out, results):
    with open(out / "defect_status.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["defect", "description", "verdict", "evidence"])
        for k, (verdict, ev) in results.items():
            w.writerow([k, DEFECTS.get(k, ""), verdict, ev])
    print("\nDefect regression:")
    for k, (verdict, ev) in results.items():
        print(f"   {k:3s} {verdict:12s} {ev}")


def main():
    global THROTTLE
    ap = argparse.ArgumentParser()
    ap.add_argument("--dictionary", help="Entity dictionary .csv (entity-set sheet) or .xlsx (field sheet)")
    ap.add_argument("--out", default="./discovery")
    ap.add_argument("--initiative", choices=["I07", "I08", "I13"], help="scope the probes to one initiative")
    ap.add_argument("--skip-counts", action="store_true", help="run $metadata but not the per-set $count")
    ap.add_argument("--skip-probes", action="store_true", help="run the sweep only")
    ap.add_argument("--only-probes", action="store_true", help="skip $metadata and $count; probes and profiles only")
    ap.add_argument("--skip-filter-sweep", action="store_true", help="skip the per-property filter test (~230 calls)")
    ap.add_argument("--skip-profiles", action="store_true", help="skip the full pulls")
    ap.add_argument("--fields", action="store_true", help="required-field exposure register only (~2 calls)")
    ap.add_argument("--defects", action="store_true", help="defect regression only: F1, F3, F4, B1, R1, R2")
    ap.add_argument("--max-pages", type=int, default=200, help="paging ceiling per full pull (default 200 x 1000 rows)")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds between calls, if CPI is rate limiting")
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="path to .env (default: alongside this script)")
    args = ap.parse_args()
    THROTTLE = args.sleep

    if load_env_file(args.env_file):
        print(f"loaded env from {Path(args.env_file).resolve()}")
    elif args.env_file != str(DEFAULT_ENV_FILE):
        sys.exit(f"--env-file not found: {args.env_file}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    s = requests.Session()
    token = get_token(s)
    print("token OK")

    # --- fast path: exposure register only ----------------------------------------------------
    if args.fields:
        token, actual, _ = fetch_metadata(s, token, out)
        run_required_fields(actual, out)
        if args.dictionary and actual:
            write_dictionary_gaps(actual, args.dictionary, out)
        write_logs(out)
        write_status_report(out, args)
        print(f"\nDone. Outputs in {out.resolve()}")
        return

    # --- fast path: defect regression ---------------------------------------------------------
    if args.defects:
        token, actual, actual_props, totals = run_sweep(s, token, out, skip_counts=False)
        results = {}
        b1_sets = ["PurchaseRequisitionSet", "GoodsMovementItemSet"]
        b1_fail = [x for x in b1_sets if f"{SHARED}/{x}" in COUNT_DUMPS]
        results["B1"] = ("STILL OPEN" if b1_fail else "FIXED",
                         f"/$count failing on: {', '.join(b1_fail) or 'none'}")
        token, frows = run_filter_support(s, token, out, actual_props, totals)
        ignored = [r for r in frows if r[6] == "IGNORED"]
        tested = [r for r in frows if r[6] in ("HONOURED", "IGNORED", "PARTIAL_OR_ODD")]
        results["F1"] = ("STILL OPEN" if ignored else "FIXED",
                         f"{len(ignored)} of {len(tested)} tested properties ignore $filter")
        resb = [r for r in ignored if r[1] == "ReservationItemSet"]
        results["R1"] = ("STILL OPEN" if resb else "FIXED",
                         f"ReservationItemSet ignored propert(ies): {', '.join(r[2] for r in resb) or 'none'}")
        token, mp_rows, dupes = run_paging_stability(s, token, out, totals)
        results["F4"] = ("STILL OPEN" if dupes else "FIXED",
                         f"{dupes} duplicate key(s) pulling MaterialPlantSet without $orderby")
        token, orows = run_operator_support(s, token, out, totals, mp_rows)
        works = [r[2] for r in orows if r[7] == "WORKS"]
        beyond_eq = [w for w in works if w not in ("substringof",)]
        results["F3"] = ("FIXED" if beyond_eq else "STILL OPEN",
                         f"operators honoured: {', '.join(works) or 'none beyond eq'}")
        token, resb_rows = run_reservation_cap(s, token, out)
        results["R2"] = ("STILL OPEN" if resb_rows <= 1000 else "FIXED",
                         f"page-until-short-page returned {resb_rows} rows")
        write_defect_status(out, results)
        run_required_fields(actual, out)
        write_logs(out)
        write_status_report(out, args)
        print(f"\nDone. Outputs in {out.resolve()}")
        return

    # --- full run ------------------------------------------------------------------------------
    actual, actual_props, totals = {}, {}, {}
    if not args.only_probes:
        token, actual, actual_props, totals = run_sweep(s, token, out, args.skip_counts)
        if actual:
            run_required_fields(actual, out)

    token = run_fr9(s, token, out)

    if not args.skip_probes:
        token = run_probes(s, token, out, args.initiative)
        token = run_key_collapse(s, token, out)

    mp_rows = None
    if not args.skip_profiles:
        token, _ = run_reservation_cap(s, token, out)
        token = run_marc_changes(s, token, out)
        if totals:
            token, mp_rows, _ = run_paging_stability(s, token, out, totals)
        token, mp_rows = run_mrp_profile(s, token, out, mp_rows)
        token = run_material_profile(s, token, out)
        token = run_plant_coverage(s, token, out, mp_rows)
        token = run_history_depth(s, token, out)
        token = run_vgabe_profile(s, token, out)
        if totals:
            token = run_coverage_ratios(s, token, out, totals)
        token = run_repair_po_check(s, token, out)
        if totals:
            token, _ = run_operator_support(s, token, out, totals, mp_rows)
        else:
            print("\nOperator support and paging stability need the $count totals: run without --only-probes / --skip-counts")

    if not args.skip_filter_sweep:
        if not actual_props:
            print("\nFilter sweep needs $metadata and $count: run without --only-probes / --skip-counts")
        else:
            token, _ = run_filter_support(s, token, out, actual_props, totals)

    if args.dictionary and actual:
        write_dictionary_gaps(actual, args.dictionary, out)
    elif args.dictionary:
        print("\n--dictionary given with --only-probes: gap report needs $metadata, skipped")

    write_logs(out)
    write_status_report(out, args)
    print(f"\nDone. Outputs in {out.resolve()}")


if __name__ == "__main__":
    main()