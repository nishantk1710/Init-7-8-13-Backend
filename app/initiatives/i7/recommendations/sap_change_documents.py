"""A real ``SapStateProvider``, backed by SAP change-document evidence.

FR-9 adoption tracking (``adoption.py``) needs *current* SAP planning-field
state to compare an approved recommendation against. There is no OData
"current value" endpoint for this -- I07 reads SAP's own change-document
history instead: CDHDR (one row per change document) joined to CDPOS (one row
per field actually changed within that document), and takes the newest
``new_value`` for each tracked field as "what SAP holds now".

**No raw table is named here.** The actual ``raw_cdhdr``/``raw_cdpos`` read
lives in ``app.initiatives.i7.adapters.change_documents`` -- the one module
in I07 allowed to query a raw extract table directly (see
``adapters/repository.py``'s equivalent rule for staged tables, and
``tests/i7/test_boundaries.py::test_only_the_adapter_reads_raw_tables``).
This module calls that adapter and works only with the plain
``ChangeDocumentRow`` values it returns.

**Scope, exactly as FR-9 defines it:**

    Objectclas (``change_doc_object``) = MATERIAL
    Tabname    (``table_name``)        = MARC
    Fname      (``field_name``)        IN (DISMM, EISBE, MINBE, MABST)

``Fname`` filtering happens in Python (``_TRACKED_FIELDS``), not only in SQL,
so a caller can see exactly which fields this provider recognises without
re-reading the query -- and so a future field addition is a one-line change
here, not a schema change.

**Deduplication.** Raw CDPOS carries no primary key in this extract (no
Document Change Pointer / MPC key has been supplied), so two extract runs, or
a repeated read of the same file, can in principle produce more than one row
for what is really one field-change event. Every row is deduplicated on its
full natural composite key -- (change_doc_object, object_value,
document_number, table_name, table_key, field_name, change_indicator) --
before the newest value is taken, so a duplicate row can never be
double-counted or picked twice as if it were two separate changes.

**Identity normalisation, one seam.** I07's own ``sap_material_number`` is
unpadded (``"1000000000"``); CDHDR's ``object_value`` is the raw SAP MATNR,
zero-padded to 18 characters (``"000000001000000000"``). ``_pad_material``
is the one place that conversion happens, matching SAP's own MATNR
convention -- not an invented mapping.

**On the current extract, this provider genuinely returns no evidence for
any material-plant.** Verified directly: ``raw_cdpos`` contains zero rows
whose ``change_doc_object`` is MATERIAL at all (confirmed three ways -- by
``document_number`` join, by ``object_value`` overlap, and by a direct filter
on ``change_doc_object='MATERIAL'``), so no MARC/DISMM/EISBE/MINBE/MABST
change ever appears in it. That is a fact about this delivery, not a bug in
this module -- ``current_state()`` correctly returns ``None`` (no evidence)
for every material-plant today, and will start returning real values the
day a CDPOS extract that actually includes MARC changes is delivered,
without any change to this code.
"""

from app.initiatives.i7.adapters.change_documents import (
    ChangeDocumentRow,
    material_marc_changes,
)
from sqlalchemy.orm import Session

_TRACKED_FIELDS = ("DISMM", "EISBE", "MINBE", "MABST")
"""The MARC fields FR-9 watches. Matches
``app.initiatives.i7.policy.thresholds.AdoptionPolicy.tracked_fields`` --
kept as a local tuple rather than importing the policy, since this module has
no other dependency on ``policy`` and a hardcoded FRS-named field list is not
a business threshold in the way a monitoring window or a threshold value is."""

_MATNR_WIDTH = 18
"""SAP's internal MATNR field width. Not chosen here -- it is a fixed SAP
data-dictionary fact, the same padding every ABAP report applies."""


def _pad_material(material: str) -> str:
    """I07's unpadded material number to SAP's 18-character MATNR."""
    return material.strip().rjust(_MATNR_WIDTH, "0")


def _dedupe(rows: list[ChangeDocumentRow]) -> list[ChangeDocumentRow]:
    """Drop rows that repeat an identical composite key.

    Keeps the first occurrence in query order (already ``ORDER BY date,
    time``), so a true duplicate never contributes a second, redundant
    "latest value" candidate.
    """
    seen: set[tuple] = set()
    deduped = []
    for row in rows:
        key = (
            row.change_doc_object,
            row.object_value,
            row.document_number,
            row.table_name,
            row.table_key,
            row.field_name,
            row.change_indicator,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _rows_for_plant(rows: list[ChangeDocumentRow], plant: str) -> list[ChangeDocumentRow]:
    """Rows whose MARC table_key names this plant.

    MARC's primary key is MATNR+WERKS; ``table_key`` is CDPOS's own encoding
    of it, concatenated and not delimited in the way SAP change documents
    generally aren't. No real MARC row has been observed in this extract to
    confirm the exact encoding (see the module docstring), so this checks
    for the plant code appearing in table_key at all -- a conservative,
    honest filter that will need to be verified against a real MARC change
    document the day one is delivered, not one that assumes a byte layout no
    data here can confirm.
    """
    return [row for row in rows if plant in (row.table_key or "")]


class RawChangeDocumentProvider:
    """The real ``SapStateProvider``: reads SAP change-document evidence.

    Implements the same ``current_state(material, plant) -> dict | None``
    protocol as ``NoSapStateAvailable`` (see ``adoption.py``) -- callers do
    not need to know which provider they were given.
    """

    def __init__(self, session: Session):
        self._session = session

    def current_state(self, material: str, plant: str) -> dict[str, str] | None:
        padded = _pad_material(material)
        rows = material_marc_changes(self._session, padded, _TRACKED_FIELDS)
        rows = _dedupe(rows)
        rows = _rows_for_plant(rows, plant)

        if not rows:
            return None

        # Latest value per field, in change order (already ORDER BY date,
        # time) -- a later row for the same field overwrites an earlier one,
        # since only "what does SAP hold now" matters.
        latest: dict[str, str] = {}
        for row in rows:
            latest[row.field_name.lower()] = row.new_value

        result: dict[str, str] = {}
        if "dismm" in latest:
            result["mrp_type"] = latest["dismm"]
        if "eisbe" in latest:
            result["eisbe"] = latest["eisbe"]
        if "minbe" in latest:
            result["minbe"] = latest["minbe"]
        if "mabst" in latest:
            result["mabst"] = latest["mabst"]
        return result or None
