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

SERVICES = frozenset({"ZVZI_KPI02_SHARED_SRV", "ZMM_KPI02_SRV"})

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
DISMM_VALUE_DOMAIN: dict[str, int] = {
    "": 1023,
    "PD": 827,
    "ND": 184,
    "V1": 79,
    "VB": 45,
    "M0": 13,
    "RP": 3,
    "VI": 1,
    "VH": 1,
    "V2": 1,
    "VM": 1,
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

# Dismm must stay honoured: the whole OAR scope is selected with it.
KNOWN_HONOURED_FILTERS = {
    ("MaterialPlantSet", "Dismm"),
    ("MaterialPlantSet", "Werks"),
}


# --- Paging ---------------------------------------------------------------

# From discovery/paging_stability.txt. Unordered $skip/$top over this set
# returned the right ROW COUNT and the wrong ROWS -- 1,618 distinct keys out of
# 2,178, a different subset each time. Ordering by the key made it exact.
PAGING_PROOF_SET = "MaterialPlantSet"
PAGING_PROOF_TOTAL = 2178
PAGING_PROOF_UNORDERED_DISTINCT = 1618

# Sets whose $count answered HTTP 500 at some sweep. The client demotes to
# short-page paging for these rather than failing.
COUNT_UNRELIABLE_SETS = frozenset({"PurchaseRequisitionSet", "GoodsMovementItemSet"})
