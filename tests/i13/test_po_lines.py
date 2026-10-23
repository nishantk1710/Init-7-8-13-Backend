"""One rule for "can anything still arrive on this PO line" (UAT, 23 Oct 2026),
and both readers that feed it carrying what it reads."""

import inspect
import re
from decimal import Decimal

import pytest

from app.initiatives.i13.po_lines import PoLineState, po_line_state


def _row(**fields):
    return {"Ebeln": "4100000001", "Ebelp": "10", "Menge": Decimal("5"), "Elikz": "", "Loekz": "", "Bsart": "ZDOM", **fields}


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, None),
        ({"Bsart": "WK"}, PoLineState.NOT_AN_ORDER),
        ({"Bsart": "MK"}, PoLineState.NOT_AN_ORDER),
        ({"Bsart": "AN"}, PoLineState.NOT_AN_ORDER),
        ({"Bsart": "FPA"}, PoLineState.NOT_AN_ORDER),
        ({"Menge": Decimal("0")}, PoLineState.NOT_AN_ORDER),
        ({"Menge": None}, None),  # unknown is not zero
        ({"Elikz": "X"}, PoLineState.CLOSED),
        ({"Loekz": "L"}, PoLineState.CLOSED),
        ({"Loekz": "S"}, PoLineState.CLOSED),
        ({"Bsart": "WK", "Loekz": "L"}, PoLineState.NOT_AN_ORDER),
    ],
)
def test_po_line_state(fields, expected) -> None:
    assert po_line_state(_row(**fields)) is expected


def test_the_doc_types_are_a_setting(monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "i13_non_order_doc_types", "WK, mk")
    assert po_line_state(_row(Bsart="MK")) is PoLineState.NOT_AN_ORDER
    assert po_line_state(_row(Bsart="AN")) is None


def _selected(sql: str) -> set[str]:
    """The bare column names a SELECT ... FROM / INTO lists."""
    head = re.split(r"\b(?:INTO|FROM)\b", sql, maxsplit=1)[0]
    head = head.split("SELECT", 1)[1]
    return {re.split(r"\s+AS\s+", c.strip())[-1].strip() for c in head.split(",") if c.strip()}


def test_both_po_readers_carry_what_the_rule_reads() -> None:
    """The 21 Oct ELIKZ/LOEKZ rule passed its tests and never reached a stored
    build: the snapshot's work table copied EKPO without the flags, and the
    row converter read the missing columns as blanks. The converter now reads
    them strictly; this keeps the three SELECTs in step."""
    from app.initiatives.i13.snapshot_store import work_tables
    from app.initiatives.i13.snapshot_store.batch_repos import (
        BatchProcurementRepository,
    )
    from app.integrations.sap.postgres_procurement import _PO_ITEM_QUERY

    needed = {"delivery_completed", "deletion_indicator", "doc_type", "order_quantity"}
    assert needed <= _selected(_PO_ITEM_QUERY)

    w = work_tables.WorkTables(version=1)
    po_statement = next(sql for table, sql, _ in work_tables._statements(w) if table == w.po)
    assert needed <= _selected(po_statement)

    batch_sql = inspect.getsource(BatchProcurementRepository.get_purchase_order_items)
    for column in needed:
        assert column in batch_sql
