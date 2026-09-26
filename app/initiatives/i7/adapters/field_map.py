"""Raw extract column -> SAP field -> canonical meaning.

The single place in I07 that knows what the extract's columns are called.
Everything above the adapter reads canonical contracts, so when the live OData
feed replaces the extract only this file and its sibling reader change.

The mapping cannot be derived. The extract's headers are the *business labels*
SAP's export produced (``mrp_type``, ``planned_deliv_time``), not SAP field
names (``DISMM``, ``PLIFZ``) and not the OData property names the CPI service
exposes (``Dismm``, ``Plifz``). Three vocabularies for one field, so the
correspondence is written down once, here.

``SAP_FIELD`` on each entry is documentation with a purpose: it is the join back
to the FRS, which specifies requirements in SAP field names, and to the OData
contract Phase 12 will map from.
"""

from typing import NamedTuple


class FieldMapping(NamedTuple):
    """One column of one raw table, and what it means."""

    raw_column: str
    """Column name in the ``raw_*`` table -- the extract's business label."""

    sap_field: str
    """The SAP field this is. Documentation and the join to the OData contract."""

    canonical_name: str
    """The attribute it becomes on a canonical contract."""


# --- MARA: material master, client level ------------------------------
MARA_FIELDS = (
    FieldMapping("material", "MATNR", "sap_material_number"),
    FieldMapping("material_group", "MATKL", "material_group"),
    FieldMapping("base_unit_of_measure", "MEINS", "base_unit_of_measure"),
    FieldMapping("x_plant_matl_status", "MSTAE", "material_status"),
    FieldMapping("ext_material_group", "EXTWG", "external_material_group"),
    FieldMapping("manufacturer", "MFRNR", "manufacturer"),
    FieldMapping("df_at_client_level", "LVORM", "deletion_flag"),
)

# --- MAKT: material description ---------------------------------------
MAKT_FIELDS = (
    FieldMapping("material", "MATNR", "sap_material_number"),
    FieldMapping("material_description", "MAKTX", "description"),
)

# --- MARC: material master, plant level -------------------------------
MARC_FIELDS = (
    FieldMapping("material", "MATNR", "sap_material_number"),
    FieldMapping("plant", "WERKS", "sap_plant_code"),
    FieldMapping("mrp_type", "DISMM", "mrp_type"),
    FieldMapping("planned_deliv_time", "PLIFZ", "planned_delivery_time_days"),
    FieldMapping("reorder_point", "MINBE", "current_reorder_point"),
    FieldMapping("maximum_stock_level", "MABST", "current_maximum_stock"),
    FieldMapping("df_at_plant_level", "LVORM", "deletion_flag"),
)

# EISBE (safety stock) is NOT in the MARC extract. The delivered export carries
# 19 columns and safety stock is not among them, though the FRS names EISBE as a
# required field and FR-9 tracks changes to it.
#
# Consequence: current safety stock stays NULL for every material, so a
# recommendation can show a recommended value but no current one to compare
# against. Not worked around here -- a re-extraction request, not a code fix.
MARC_MISSING_FIELDS = (
    FieldMapping("(absent)", "EISBE", "current_safety_stock"),
)

# --- MARD: storage-location stock (StorageLocationStockSet over OData) -
#
# The extract carries two copies of the current-period stock columns under
# near-identical labels (columns 8-13 and 14-19 of MARD_Extract.XLSX); the
# staging query in extract.py reads the first occurrence of each, which
# ``raw_mard``'s column order already reflects. Columns beyond these six are
# CY/PY warehouse-stock and consignment-stock variants -- a different concept,
# not staged here.
MARD_FIELDS = (
    FieldMapping("material", "MATNR", "sap_material_number"),
    FieldMapping("plant", "WERKS", "sap_plant_code"),
    FieldMapping("storage_location", "LGORT", "storage_location"),
    FieldMapping("unrestricted", "LABST", "unrestricted_use_stock"),
    FieldMapping("stock_in_transfer", "UMLME", "stock_in_transfer"),
    FieldMapping("in_quality_insp", "INSME", "quality_inspection_stock"),
    FieldMapping("restricted_use_stock", "EINME", "restricted_use_stock"),
    FieldMapping("blocked", "SPEME", "blocked_stock"),
    FieldMapping("returns", "RETME", "returns_stock"),
)

# --- MSEG: material document item (consumption) -----------------------
MSEG_FIELDS = (
    FieldMapping("material", "MATNR", "sap_material_number"),
    FieldMapping("plant", "WERKS", "sap_plant_code"),
    FieldMapping("movement_type", "BWART", "movement_type"),
    FieldMapping("quantity", "MENGE", "quantity"),
    FieldMapping("base_unit_of_measure", "MEINS", "unit_of_measure"),
    FieldMapping("posting_date", "BUDAT", "posting_date"),
    FieldMapping("debit_credit_ind", "SHKZG", "debit_credit_indicator"),
    FieldMapping("material_document", "MBLNR", "material_document"),
    FieldMapping("material_doc_year", "MJAHR", "material_document_year"),
    FieldMapping("material_doc_item", "ZEILE", "material_document_item"),
)

# --- EKKO / EKPO / EKBE: purchase orders and receipts -----------------
EKKO_FIELDS = (
    FieldMapping("purchasing_document", "EBELN", "purchasing_document"),
    FieldMapping("created_on", "AEDAT", "created_on"),
    FieldMapping("supplier", "LIFNR", "supplier"),
    FieldMapping("deletion_indicator", "LOEKZ", "is_cancelled"),
)

EKPO_FIELDS = (
    FieldMapping("purchasing_document", "EBELN", "purchasing_document"),
    FieldMapping("item", "EBELP", "item"),
    FieldMapping("material", "MATNR", "sap_material_number"),
    FieldMapping("plant", "WERKS", "sap_plant_code"),
    FieldMapping("order_quantity", "MENGE", "quantity_ordered"),
    FieldMapping("planned_deliv_time", "PLIFZ", "planned_delivery_time_days"),
    FieldMapping("deletion_indicator", "LOEKZ", "is_cancelled"),
)

EKBE_FIELDS = (
    FieldMapping("purchasing_document", "EBELN", "purchasing_document"),
    FieldMapping("item", "EBELP", "item"),
    FieldMapping("posting_date", "BUDAT", "goods_receipt_date"),
    FieldMapping("quantity", "MENGE", "quantity_received"),
    FieldMapping("movement_type", "BWART", "movement_type"),
    FieldMapping("po_history_category", "VGABE", "history_category"),
)

# --- EKET: schedule lines ---------------------------------------------
EKET_FIELDS = (
    FieldMapping("purchasing_document", "EBELN", "purchasing_document"),
    FieldMapping("item", "EBELP", "item"),
    FieldMapping("delivery_date", "EINDT", "planned_delivery_date"),
)


# --- Values the extract uses, named once -------------------------------

GOODS_RECEIPT_HISTORY_CATEGORY = "E"
"""EKBE.VGABE value for a goods receipt. ``E`` is 57,821 of the GR rows; the
other categories (Q invoice, D down-payment, U delivery, L) are not receipts."""

GOODS_RECEIPT_MOVEMENT_TYPE = "101"
GOODS_RECEIPT_REVERSAL_MOVEMENT_TYPE = "102"

CREDIT_INDICATOR = "H"
"""MSEG.SHKZG. ``H`` (Haben) is a stock decrease -- an issue. ``S`` (Soll) is an
increase, which for a consumption movement type means a reversal."""

DEBIT_INDICATOR = "S"

DELETION_FLAG_TRUE = "X"
"""SAP's boolean: ``X`` for true, blank for false.

Applies to MARA.LVORM and MARC.LVORM, which are true booleans.
"""

PURCHASING_DELETION_INDICATORS = frozenset({"L", "S"})
"""EKPO.LOEKZ is NOT a boolean, unlike LVORM.

It is a single-character code: ``L`` means the item is deleted, ``S`` that it is
blocked, and blank that it is live. In this extract that is 10,624 ``L`` and
2,691 ``S`` against 69,403 blank.

Both count as cancelled for lead-time purposes -- neither a deleted nor a
blocked line represents a real replenishment. Reading LOEKZ as a boolean and
testing it against ``X`` marks every one of those 13,315 lines active, which
silently admits cancelled orders into the lead-time population the Formula
Reference explicitly excludes.
"""
