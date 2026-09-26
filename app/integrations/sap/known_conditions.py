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
EMPTY_SETS = frozenset({"MaterialValuationSet", "MonthlyMovementStatisticSet"})

# Far too large to read unfiltered. ChangeDocItemSet alone is about 929,000 rows.
HIGH_VOLUME_SETS = frozenset({"ChangeDocItemSet", "ChangeDocHeaderSet"})

# Answers HTTP 400 with an empty body to EVERY request, not just $count -- a
# plain $top=5 fails too. Observed 2026-09-11 on live CPI.
#
# Its snapshot count is 0, so it was already in EMPTY_SETS; this is a stronger
# statement than "empty". Most likely the projection was deactivated. Until
# someone confirms that, it is recorded rather than explained.
#
# Not a data problem for us: MBEW is loaded from the July extract instead
# (raw_mbew, 7,034 rows), which is the only source for I07's valuation figures.
UNREADABLE_SETS = frozenset({"MaterialValuationSet"})

# $orderby defects, per set. SAP returns HTTP 500 with an EMPTY body -- a
# backend short dump rather than a rejected query -- for these orderings.
#
# Measured 2026-09-11: MaterialPlantSet rejects ANY two-field $orderby
# (Matnr,Werks and Werks,Matnr alike) while every single-field ordering works,
# and PurchaseOrderItemSet handles Ebeln,Ebelp perfectly well. So it is
# set-specific, not a general limit on multi-field ordering.
#
# The client survives this by falling back to the longest accepted key prefix
# and reporting the degradation -- see paging.read_first_page_negotiating_order.
# It is still SAP's bug: an empty-bodied 500 should leave an ST22 short dump.
ORDER_BY_REJECTED_MULTI_FIELD = frozenset({"MaterialPlantSet"})

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

# Every property declares these four as false -- all 229 of them, across both
# services. Meanwhile 124 are MEASURED as filterable and several sort fine.
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
# Re-measured against the 22-Sep post-rename $metadata (data-generator/
# discovery/properties.csv, committed alongside this change -- 237 rows across
# the same 21 in-scope sets and two services; GatePass and ZMM_GET_CSV_SRV are
# not in this file, see cpi_discovery.py DESCOPED_SERVICES).
PROPERTIES_WITH_MAX_LENGTH = 211
PROPERTIES_WITH_PRECISION = 17
PROPERTIES_WITH_LABEL = 237  # every one, which is what makes labels usable
TOTAL_PROPERTIES = 237

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

# Verdicts filter_support.csv can record.
FILTER_VERDICTS = frozenset(
    {"HONOURED", "IGNORED", "REJECTED_HTTP_500", "NOT_TESTED", "PARTIAL_OR_ODD"}
)

# The measured distribution across 208 filterable properties. Roughly a third
# either lie or fail, which is the single most important thing to know about
# filtering this service.
FILTER_VERDICT_COUNTS: dict[str, int] = {
    "HONOURED": 124,
    "IGNORED": 62,
    "REJECTED_HTTP_500": 11,
    "NOT_TESTED": 10,
    "PARTIAL_OR_ODD": 1,
}

FILTER_SUPPORT_ROW_COUNT = sum(FILTER_VERDICT_COUNTS.values())

# Named cases worth asserting individually, because code depends on each.
#
# Pstyp is the one Anish called out: the I08 repair-PO convention filters on
# item category, and SAP drops that filter and answers 200 with everything.
KNOWN_IGNORED_FILTERS = {
    ("PurchaseOrderItemSet", "Pstyp"),
}

# Verdict counts PER SET, not just in aggregate -- W2.5 asks for the honoured
# list per set so a regression confined to one set is caught. An aggregate can
# stay identical while two sets swap behaviour.
FILTER_VERDICTS_BY_SET: dict[str, dict[str, int]] = {
    "BatchStockSet": {'HONOURED': 4, 'IGNORED': 1},
    "ChangeDocHeaderSet": {'HONOURED': 6, 'NOT_TESTED': 1},
    "ChangeDocItemSet": {'HONOURED': 9},
    "GoodsMovementItemSet": {'HONOURED': 6, 'IGNORED': 19},
    "InfoRecordOrgSet": {'HONOURED': 4, 'IGNORED': 5, 'NOT_TESTED': 1},
    "InfoRecordSet": {'HONOURED': 3, 'NOT_TESTED': 1},
    "MaterialDescriptionSet": {'HONOURED': 2, 'IGNORED': 1},
    "MaterialDocumentHeaderSet": {'HONOURED': 3, 'IGNORED': 2},
    "MaterialPlantSet": {'HONOURED': 4, 'IGNORED': 7, 'NOT_TESTED': 1},
    "MaterialSet": {'HONOURED': 4, 'IGNORED': 2, 'NOT_TESTED': 1},
    "POHistorySet": {'HONOURED': 5, 'IGNORED': 10, 'PARTIAL_OR_ODD': 1},
    "POScheduleLineSet": {'HONOURED': 4, 'IGNORED': 2},
    "PurchaseOrderItemSet": {'HONOURED': 6, 'IGNORED': 12, 'NOT_TESTED': 1},
    "PurchaseOrderSet": {'HONOURED': 7},
    "PurchaseRequisitionSet": {'HONOURED': 22, 'NOT_TESTED': 1, 'REJECTED_HTTP_500': 6},
    "ReservationItemSet": {'HONOURED': 15, 'NOT_TESTED': 2, 'REJECTED_HTTP_500': 2},
    "StockMovementStatisticSet": {'HONOURED': 13, 'IGNORED': 1},
    "StorageLocationStockSet": {'HONOURED': 4, 'REJECTED_HTTP_500': 3},
    "VendorSet": {'HONOURED': 3, 'NOT_TESTED': 1},
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
# Measured 2026-09-26 through the client's own query builder, one shape at a
# time, each with the transport's three attempts:
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
READ_BROKEN_SETS: dict[str, str] = {
    "POHistorySet": "every row read is HTTP 500 since 2026-09-26; $count answers",
    "POScheduleLineSet": "every row read is HTTP 500 since 2026-09-26; $count answers",
    "ChangeDocHeaderSet": (
        "every row read is HTTP 500 since 2026-09-26, with the Objectclas "
        "predicate it requires; $count answers"
    ),
}

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
