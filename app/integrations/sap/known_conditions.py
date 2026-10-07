"""What we have measured about this SAP system, written down so it can be checked.

Every constant here is an observation someone made against live CPI, not a
guess. Their purpose is to make a *change* fail loudly: if SAP starts behaving
differently, the tests that read this module break, and somebody looks.

The rule for maintaining it
---------------------------
When a check fails, the first question is "did SAP change?", not "is the
expectation wrong?". A newly-observed value must be **reasoned about and ruled
on** before it is added here -- particularly for ``DISMM``, where the value set
decides which materials are in OAR scope and therefore which materials three
initiatives act on. Adding an unexplained code to make a test green would quietly
widen or narrow that scope.

That is not hypothetical: the frontend hit exactly this when a seventh MRP type,
``VM``, appeared with a single row. It was deliberately left out, the test was
left failing, and the failure stood in for an open question.
"""

from __future__ import annotations

# --- Shape ----------------------------------------------------------------

EXPECTED_ENTITY_SET_COUNT = 21

# Renamed by SAP 22-Sep-2026: ZVZI_KPI02_SHARED_SRV -> ZMM_KPI02_ADD_SRV,
# ZMM_KPI02_SRV -> ZMM_KPI02_TAB_SRV. Verified against live $metadata the same
# day (data-generator/discovery/entity_sets.csv) and again via a direct probe
# of both new names. Set membership and count (21) are unaffected.
SERVICES = frozenset({"ZMM_KPI02_ADD_SRV", "ZMM_KPI02_TAB_SRV"})

# Registered and responding, but holding no rows. Real answers, not failures --
# the client reports them as empty rather than erroring, and the seed loads
# these tables from the July extract instead.
#
# Empty since 2026-10-07. Both sets that were here now hold rows (counts.csv,
# same sweep): MaterialValuationSet 2,116 and MonthlyMovementStatisticSet
# 1,429, each answering a plain row read. Kept for the next set that is
# registered before it is filled.
EMPTY_SETS: frozenset[str] = frozenset()

# Far too large to read unfiltered. ChangeDocItemSet alone is about 929,000 rows.
HIGH_VOLUME_SETS = frozenset({"ChangeDocItemSet", "ChangeDocHeaderSet"})

# Answers HTTP 400 with an empty body to EVERY request, not just $count.
#
# Empty since 2026-10-07. MaterialValuationSet was here from 2026-09-11 (a
# plain $top=5 failed); it now reads, $count answers 2,116, and its key has
# gained Bwtar (Matnr, Bwkey, Bwtar) -- one row per valuation type, which is
# why the old two-field key could not address a row.
UNREADABLE_SETS: frozenset[str] = frozenset()

# $orderby defects, per set. SAP returns HTTP 500 with an EMPTY body -- a
# backend short dump rather than a rejected query -- for these orderings.
#
# Re-measured 2026-10-07, every key prefix of eight sets: POScheduleLineSet
# rejects Ebeln,Ebelp,Etenr AND Ebeln,Ebelp, and accepts Ebeln alone. Every
# other set accepts its full key -- including MaterialPlantSet, which was the
# set listed here from 2026-09-11 (any two-field $orderby was a 500) and is now
# fixed. So the defect moved rather than went.
#
# The client survives this by falling back to the longest accepted key prefix
# and reporting the degradation -- see paging.read_first_page_negotiating_order.
# Ebeln alone is not unique on EKET, so a full read pages over a non-total
# order; the duplicate-key count on every pull is what proves no row was lost
# (zero on 2026-10-07, 3,631 rows). A delta reads EKET fifty purchase orders
# at a time, which is one page, so it never pages at all. Still SAP's bug to
# fix: an empty-bodied 500 should leave an ST22 short dump.
ORDER_BY_REJECTED_MULTI_FIELD = frozenset({"POScheduleLineSet"})

# Properties that drifted Edm.Decimal -> Edm.String between two sweeps a day
# apart. Kept as a standing reminder that decoding must follow the DECLARED
# type: code that inferred "looks numeric" kept working and silently changed.
DRIFTED_TO_STRING = {
    ("PurchaseOrderItemSet", "Netpr"),
    ("PurchaseOrderItemSet", "Netwr"),
}

# SAP added Bnfpo to this key: a requisition can have more than one line, and
# the original single-field key could not address a line uniquely.
PURCHASE_REQUISITION_KEY = ("Banfn", "Bnfpo")


# --- Declared metadata detail (W2.5: types, lengths, annotations) ---------

# Every property declared these four as false -- all 229 of them, across both
# services, in September. Meanwhile 124 were MEASURED as filterable and several
# sorted fine.
#
# 2026-10-07: 41 properties no longer declare sap:filterable at all (10 of them
# not sap:sortable either), and an absent annotation means true. They are
# exactly the fields SAP opened up for the delta work -- every
# GoodsMovementItemSet property but the amounts, MaterialDocumentHeaderSet,
# the POHistorySet and POScheduleLineSet keys, POHistorySet's dates,
# ChangeDocHeaderSet.Udate. None declares true, so the assertion below still
# holds; the change is the first sign the annotations are being maintained.
#
# So the flags are an untouched SEGW default carrying no information. They are
# captured because W2.5 asks for the annotations, and asserted because their
# uniformity is the very thing that makes them untrustworthy: the day one of
# them turns true, somebody has started maintaining them and they might begin
# to mean something.
#
# Until then: filter_support.csv and operator_support.csv are the source of
# truth for what the service actually does.
DECLARED_FLAGS_ARE_UNIFORMLY_FALSE = True
DECLARED_FLAGS = ("filterable", "sortable", "creatable", "updatable")

# Counted across BOTH services. A value longer than its declared maximum means
# something upstream truncated or corrupted it.
# Re-measured against the 2026-10-07 $metadata (data-generator/discovery/
# properties.csv, 251 rows across the same 21 in-scope sets and two services;
# GatePass and ZMM_GET_CSV_SRV are not in this file, see cpi_discovery.py
# DESCOPED_SERVICES). Up from 237: ReservationItemSet +9 (Bednr, Charg, Ebeln,
# Ebelp, Lifnr, Matkl, Shkzg, Sobkz, waers), MonthlyMovementStatisticSet +2
# (Vrsio, Ssour, both key), MaterialValuationSet +1 (Bwtar, key),
# MaterialPlantSet +1 (Beskz), VendorSet +1 (Land1). Precision fell from 17 to
# 6 because the date and quantity fields SAP re-typed to Edm.String carry none.
PROPERTIES_WITH_MAX_LENGTH = 229
PROPERTIES_WITH_PRECISION = 6
PROPERTIES_WITH_LABEL = 251  # every one, which is what makes labels usable
TOTAL_PROPERTIES = 251

# SAP's business labels are the same words the July extract uses as column
# headers, which is what makes them worth capturing beyond W2.5's requirement.
# Spot-checked pairs, exact matches against the extract headers.
KNOWN_LABELS = {
    ("MaterialPlantSet", "Dismm"): "MRP Type",
    ("MaterialPlantSet", "Minbe"): "Reorder Point",
    ("MaterialSet", "Matkl"): "Material Group",
}

# Declared maxima that downstream code depends on. Matnr in particular: the
# extract carries unpadded 10-character numbers while OData returns them
# zero-padded to 18, and any normalisation has to know which is which.
KNOWN_MAX_LENGTHS = {
    ("MaterialSet", "Matnr"): 18,
    ("MaterialPlantSet", "Werks"): 4,
    ("MaterialPlantSet", "Dismm"): 2,
}


# --- Value domains --------------------------------------------------------

# Every MRP type observed on MaterialPlantSet, with the row count at the sweep.
#
# The OAR rule selects ND and PD. Everything else here has been seen but NOT
# ruled on, which is why the set is written out in full rather than reduced to
# the two that matter: a new value appearing is a question for the team lead.
#
# "" is a genuine value, not a missing one: 47% of rows had no MRP type at all
# in the development client. Note the July production extract tells a very
# different story -- 97.8% ND/PD and almost no blanks -- so these proportions
# describe the dev client only.
# Re-measured 2026-09-25 (discovery/mrp_type_profile.txt and value_domains.csv,
# same sweep as the service rename). V1 79->78 and VM 1->7; every other value
# unchanged. Both are movement within the domain, not a new code -- no ruling
# needed, unlike a value that was not here before.
DISMM_VALUE_DOMAIN: dict[str, int] = {
    "": 1023,
    "PD": 827,
    "ND": 184,
    "V1": 78,
    "VB": 45,
    "M0": 13,
    "RP": 3,
    "VI": 1,
    "VH": 1,
    "V2": 1,
    "VM": 7,
}

# The MRP types the OAR rule treats as in scope (08-Sep VZI ruling).
OAR_MRP_TYPES = frozenset({"ND", "PD"})

# Reorder-point planned, i.e. Min-Max managed -- the opposite of OAR.
PLANNED_MRP_TYPE = "VB"

MSTAE_VALUE_DOMAIN: dict[str, int] = {"": 2032, "01": 3}


# --- Filter behaviour -----------------------------------------------------

# Verdicts filter_support.csv can record. REJECTED_HTTP_400 is SAP refusing the
# probe's impossible literal ('ZZ~NOPE') for a field whose ABAP type cannot
# hold it -- a date or a quantity re-typed to Edm.String -- which says nothing
# about whether a well-formed literal is honoured. For a delta field that
# question is answered by delta_support.csv instead (see manifest.py).
FILTER_VERDICTS = frozenset(
    {
        "HONOURED",
        "IGNORED",
        "REJECTED_HTTP_400",
        "REJECTED_HTTP_500",
        "NOT_TESTED",
        "PARTIAL_OR_ODD",
    }
)

# The measured distribution across every probed property, re-measured
# 2026-10-07. In September roughly a third either lied or failed (62 IGNORED of
# 208); now ONE property is silently ignored (MaterialValuationSet.Bwtar).
# That was SAP's F1 defect, and it reads as fixed.
FILTER_VERDICT_COUNTS: dict[str, int] = {
    "HONOURED": 155,
    "IGNORED": 1,
    "REJECTED_HTTP_400": 49,
    "REJECTED_HTTP_500": 32,
    "NOT_TESTED": 9,
    "PARTIAL_OR_ODD": 5,
}

FILTER_SUPPORT_ROW_COUNT = sum(FILTER_VERDICT_COUNTS.values())

# Named cases worth asserting individually, because code depends on each.
#
# Pstyp was the one Anish called out: the I08 repair-PO convention filters on
# item category, and SAP used to drop that filter and answer 200 with
# everything. Measured HONOURED on 2026-10-05 and again 2026-10-07 (impossible
# value -> 0 of 11,097), so it has left this list. I08 still applies the
# predicate client-side, which stays correct now that SAP honours it too.
#
# Bwtar is the last silently ignored property: `Bwtar eq 'ZZ~NOPE'` returns
# all 2,116 MaterialValuationSet rows.
KNOWN_IGNORED_FILTERS = {
    ("MaterialValuationSet", "Bwtar"),
}

# Verdict counts PER SET, not just in aggregate -- W2.5 asks for the honoured
# list per set so a regression confined to one set is caught. An aggregate can
# stay identical while two sets swap behaviour.
FILTER_VERDICTS_BY_SET: dict[str, dict[str, int]] = {
    "BatchStockSet": {'HONOURED': 5},
    "ChangeDocHeaderSet": {'HONOURED': 5, 'REJECTED_HTTP_400': 2},
    "ChangeDocItemSet": {'HONOURED': 9},
    "GoodsMovementItemSet": {'HONOURED': 9, 'REJECTED_HTTP_400': 17},
    "InfoRecordOrgSet": {'HONOURED': 4, 'NOT_TESTED': 1, 'REJECTED_HTTP_500': 5},
    "InfoRecordSet": {'HONOURED': 3, 'NOT_TESTED': 1},
    "MaterialDescriptionSet": {'HONOURED': 2, 'REJECTED_HTTP_500': 1},
    "MaterialDocumentHeaderSet": {'HONOURED': 4, 'REJECTED_HTTP_400': 1},
    "MaterialPlantSet": {'HONOURED': 6, 'NOT_TESTED': 1, 'REJECTED_HTTP_400': 6},
    "MaterialSet": {'HONOURED': 5, 'NOT_TESTED': 1, 'REJECTED_HTTP_400': 2},
    "MaterialValuationSet": {'HONOURED': 6, 'IGNORED': 1, 'REJECTED_HTTP_500': 4},
    "MonthlyMovementStatisticSet": {'HONOURED': 7, 'REJECTED_HTTP_500': 7},
    "POHistorySet": {'HONOURED': 6, 'PARTIAL_OR_ODD': 1, 'REJECTED_HTTP_400': 9},
    "POScheduleLineSet": {'HONOURED': 3, 'REJECTED_HTTP_500': 3},
    "PurchaseOrderItemSet": {'HONOURED': 8, 'NOT_TESTED': 1, 'REJECTED_HTTP_400': 11},
    "PurchaseOrderSet": {'HONOURED': 8},
    "PurchaseRequisitionSet": {'HONOURED': 22, 'NOT_TESTED': 1, 'PARTIAL_OR_ODD': 2, 'REJECTED_HTTP_500': 4},
    "ReservationItemSet": {'HONOURED': 25, 'NOT_TESTED': 2, 'PARTIAL_OR_ODD': 2, 'REJECTED_HTTP_500': 2},
    "StockMovementStatisticSet": {'HONOURED': 10, 'REJECTED_HTTP_400': 1, 'REJECTED_HTTP_500': 3},
    "StorageLocationStockSet": {'HONOURED': 4, 'REJECTED_HTTP_500': 3},
    "VendorSet": {'HONOURED': 4, 'NOT_TESTED': 1},
}


# Dismm must stay honoured: the whole OAR scope is selected with it.
KNOWN_HONOURED_FILTERS = {
    ("MaterialPlantSet", "Dismm"),
    ("MaterialPlantSet", "Werks"),
}


# --- Paging ---------------------------------------------------------------

# From discovery/paging_stability.txt, re-measured 2026-09-25 post-rename.
#
# The set has grown to 2,183 rows (was 2,178). The unordered-paging defect this
# constant existed to record -- 1,618 distinct keys out of 2,178, a different
# subset each time -- did NOT reproduce in this sweep: two unordered pulls and
# one ordered pull all returned 2,183 rows, 2,183 distinct keys, zero
# duplicates, zero missing. That is F4 (see DEFECTS above) reading as fixed,
# not merely unlucky -- re-confirm before relying on unordered paging elsewhere.
PAGING_PROOF_SET = "MaterialPlantSet"
PAGING_PROOF_TOTAL = 2183
PAGING_PROOF_UNORDERED_DISTINCT = 2183

# Sets whose $count answered HTTP 500 at some sweep. The client demotes to
# short-page paging for these rather than failing.
COUNT_UNRELIABLE_SETS = frozenset({"PurchaseRequisitionSet", "GoodsMovementItemSet"})

# Sets whose $count answers successfully and WRONGLY. A cap, not an error.
#
# Empty since 2026-09-25, and kept because the failure mode is worth a
# permanent home. Measured 2026-09-21 on ReservationItemSet, on the service
# as it was then (ZMM_KPI02_SRV):
#
#     /$count                : 1000
#     $inlinecount=allpages  : 7088
#     page-until-short-page  : 7088 rows, 7088 distinct (Rsnum, Rspos)
#
# Re-measured 2026-09-25 on its replacement, ZMM_KPI02_TAB_SRV: /$count
# answers 7088, equal to $inlinecount and to the 7,088 rows the CSV route
# delivered the same day. The cap went with the old service.
#
# This is more dangerous than the 500s above, which announce themselves. A
# plausible wrong total is believed: paging stops once it has read as many rows
# as the total claims, so a caller asks for every reservation and is handed 1000
# of 7088 with no indication that 86% is missing. Every reservation-dependent
# figure -- I13 STITCH, I08 session traceability -- would be computed on a
# seventh of the data and look complete.
#
# Listed here rather than fixed in paging: the defect is that a service's
# $count cannot be trusted, which is a fact about SAP, and this module is where
# facts about SAP live. The client turns it into behaviour by declining to ask.
COUNT_CAPPED_SETS: frozenset[str] = frozenset()

# Sets whose every row read answers HTTP 500, while their $count answers.
#
# EMPTY since 2026-10-07: all three sets below read again. Re-measured that
# day through the client: POHistorySet 3,881 rows, POScheduleLineSet 3,631 and
# ChangeDocHeaderSet (Objectclas eq 'MATERIAL') 7,731, each equal to its
# $count, zero duplicate keys. The history is kept because the rule at the end
# of this note still applies to whatever lands here next.
#
# What it was -- measured 2026-09-26 through the client's own query builder,
# one shape at a time, each with the transport's three attempts:
#
#   POHistorySet        $top=1 | +$orderby (full key, Ebeln) | Ebeln eq '...'
#                       | Ebeln eq +$orderby | two Ebeln (or)        all 500
#   POScheduleLineSet   the same seven shapes                        all 500
#   ChangeDocHeaderSet  Objectclas eq 'MATERIAL' alone | + Udate ge  | + any
#                       $orderby | + $skip                           all 500
#   PurchaseOrderItemSet, the same seven shapes, the same minute:   all 200
#
# $count is fine on all three (3,881 / 3,631 / 241,959), so the sets exist and
# are authorised; the read itself dumps. An empty-bodied 500 is a backend
# short dump -- SAP's to fix (ST22), ours to survive.
#
# A set listed here has no runnable delta and is left out of a sweep with the
# reason shown, rather than failing every cycle: each failure is up to nine
# requests as the client tries every ordering, and a "delta" that fails hourly
# is noise that hides real ones. The CSV route still covers POHistorySet and
# ChangeDocHeaderSet; POScheduleLineSet is undelivered on both routes.
#
# WHEN REMOVING A SET FROM THIS LIST: drop its odata_ table first, so the
# next sweep rebuilds the baseline in full. Its parent's watermark has moved
# on while it was out, and a delta from the parent's current mark would skip
# everything changed in between with no way back.
#
# Removing the three on 2026-10-07 needed no odata_ table drops: the database
# was wiped on 2026-10-06 and reloaded from the CSV route, so no odata_ table
# exists and the next sweep builds every baseline in full anyway.
READ_BROKEN_SETS: dict[str, str] = {}

# Tables the CSV extract job (ZMM_GET_CSV_SRV) acknowledges and never delivers.
#
# EMPTY since 2026-09-30, when SAP rebuilt the job: EKKO and EKET deliver, in
# the narrow layout (exactly the OData projection, no client column), with
# exact counts -- EKKO 3,140 and EKET 3,631, reconciled again on 2026-10-06.
# The history is kept below.
#
# What it was -- measured 2026-09-25/26: 39 requests for EKKO and 21 for EKET across every
# shape the entity key allows -- window wide, one year, recent, ending today,
# from 1900, blank; dates as YYYYMMDD and YYYY-MM-DD; MaxRows 1, 100, 50000
# and none; IsDelta='X'; TabName upper, lower and mixed case; the /sap/ path
# segment in both cases; 13- and 9-character ids. Every one answered
# "Success: extraction started in background"; not one chunk arrived.
# MAKT, fired on the same route in the same minutes, delivered four of four.
# The other 19 tables deliver on the first shape. The failure is inside the
# job, after the acknowledgement, and nothing in the request reaches it.
#
# A table listed here is left out of a sweep with the reason shown, rather
# than fired and timed out fifteen minutes later. An explicit --table still
# fires it, so the day SAP fixes the job the fix is one deletion here.
CSV_UNDELIVERED_TABLES: dict[str, str] = {}

# Entity sets whose DECLARED key does not uniquely address a row.
#
# CDPOS declares (Objectclas, Objectid, Changenr) but one change document
# touches many fields, so those three repeat. Measured 2026-09-21 over a
# 500-row MATERIAL/MARC sample: 478 distinct on the declared key, 500 on the
# composite -- 22 rows (4%) unaddressable.
#
# This matters beyond tidiness. Duplicate-key counting is how every pull proves
# it did not lose rows; run against a key that is not unique it reports
# duplicates that are not there, and a correct extract is refused as corrupt.
NON_UNIQUE_DECLARED_KEYS = {
    "ChangeDocItemSet": (
        "Objectclas",
        "Objectid",
        "Changenr",
        "Tabname",
        "Tabkey",
        "Fname",
        "Chngind",
    ),
}
