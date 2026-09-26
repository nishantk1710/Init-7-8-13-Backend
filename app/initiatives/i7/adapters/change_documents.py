"""SAP change-document evidence (CDHDR/CDPOS), read-only.

The only module that queries ``raw_cdhdr``/``raw_cdpos`` directly -- per the
same rule every other raw-table read in I07 follows (see
``adapters/repository.py``): business logic goes through the adapter layer,
never straight to a raw extract table, so the staging/extract schema can
change without touching the recommendations package.

There is no staged CDHDR/CDPOS table (Phase 2 was scoped to materials,
consumption and purchase orders), so this reads the two raw tables directly,
same as ``extract.py`` does for every other source table -- the difference
is that no staging step exists yet to sit between this and its caller.

Returns plain rows (``ChangeDocumentRow``), not SQLAlchemy objects, so the
caller (``app.initiatives.i7.recommendations.sap_change_documents``) never
learns a column name or a table name either.
"""

from typing import NamedTuple

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

_CHANGE_DOCUMENT_SQL = """
    SELECT h.change_doc_object, h.object_value, h.document_number,
           h.date, h.time,
           p.table_name, p.table_key, p.field_name, p.change_indicator,
           p.new_value, p.old_value
      FROM raw_cdhdr h
      JOIN raw_cdpos p
        ON p.change_doc_object = h.change_doc_object
       AND p.object_value = h.object_value
       AND p.document_number = h.document_number
     WHERE h.change_doc_object = 'MATERIAL'
       AND h.object_value = :padded_material
       AND p.table_name = 'MARC'
       AND p.field_name IN :tracked_fields
       AND (CAST(:window_start AS VARCHAR(10)) IS NULL OR h.date >= CAST(:window_start AS VARCHAR(10)))
       AND (CAST(:window_end AS VARCHAR(10)) IS NULL OR h.date <= CAST(:window_end AS VARCHAR(10)))
     ORDER BY h.date, h.time
"""
# h.date is `text`, not a real date column (see CLAUDE.md's "every raw column
# is text on purpose") -- but it is always a plain ISO "YYYY-MM-DD" string in
# this extract, which sorts identically under text and chronological
# comparison, so a direct >=/<= text comparison against two ISO date strings
# is correct without a CAST. window_start/window_end are None (never
# filtered) when the caller has no recommendation date or window to apply.
# The join key is CDHDR/CDPOS's shared natural key (object class + object
# value + document number) -- CDPOS carries no separate change-document
# identity of its own. table_key (MARC's own primary key, MATNR+WERKS) is
# read but not filtered on here: this extract has never been observed to
# carry a MARC row at all, so filtering table_key to one plant happens in
# the caller, applied to whatever rows a real extract eventually returns,
# rather than guessing table_key's exact encoding from data that does not
# exist yet.


class ChangeDocumentRow(NamedTuple):
    """One field-change event, as plain data -- no SQLAlchemy row, no table
    name, reaches the caller."""

    change_doc_object: str
    object_value: str
    document_number: str
    date: object
    time: object
    table_name: str
    table_key: str | None
    field_name: str
    change_indicator: str
    new_value: str | None
    old_value: str | None


def material_marc_changes(
    session: Session,
    padded_material: str,
    tracked_fields: tuple[str, ...],
    window_start: str | None = None,
    window_end: str | None = None,
) -> list[ChangeDocumentRow]:
    """Every MARC field-change event for one (already zero-padded) material.

    Scoped exactly to FR-9: ``Objectclas=MATERIAL``, ``Tabname=MARC``,
    ``Fname`` in ``tracked_fields``. Ordered oldest-to-newest so a caller
    can take the last value per field as "current".

    ``window_start``/``window_end`` are inclusive ISO date strings
    (``"YYYY-MM-DD"``) -- FR-9's "within a configurable window after the
    recommendation date". ``None`` means unbounded on that side; a caller
    with no recommendation date or no configured window passes ``None`` for
    both, matching the pre-window behaviour exactly.
    """
    rows = session.execute(
        text(_CHANGE_DOCUMENT_SQL).bindparams(bindparam("tracked_fields", expanding=True)),
        {
            "padded_material": padded_material,
            "tracked_fields": list(tracked_fields),
            "window_start": window_start,
            "window_end": window_end,
        },
    ).all()
    return [
        ChangeDocumentRow(
            change_doc_object=row.change_doc_object,
            object_value=row.object_value,
            document_number=row.document_number,
            date=row.date,
            time=row.time,
            table_name=row.table_name,
            table_key=row.table_key,
            field_name=row.field_name,
            change_indicator=row.change_indicator,
            new_value=row.new_value,
            old_value=row.old_value,
        )
        for row in rows
    ]
