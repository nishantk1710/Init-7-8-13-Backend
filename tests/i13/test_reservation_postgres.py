"""W6.2 reservation source: real RESB rows from Postgres (``raw_resb``).

Skipped outright when no ``DATABASE_URL`` is configured, since these prove
behaviour against the real extracted dataset, not a fixture. The reservation
source switch these once also tested (mock CSV vs. Postgres) was removed
when I13 fully migrated onto Postgres -- there is now exactly one reservation
source, and ``test_reservation_ledger_postgres.py`` covers the full
Reservation -> PR -> PO -> GR -> GI chain end to end.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.integrations.sap.postgres_reservation import fetch_reservations

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@needs_db
def test_postgres_reservation_rows_are_real_and_typed() -> None:
    with get_sessionmaker()() as session:
        rows = fetch_reservations(session)

    assert len(rows) > 0
    row = rows[0]
    assert row["Rsnum"], "every raw_resb row has a reservation number"
    assert row["Matnr"] and row["Werks"]
    if row["Bdmng"] is not None:
        assert isinstance(row["Bdmng"], Decimal)
    if row["Bdter"] is not None:
        assert isinstance(row["Bdter"], date)


@needs_db
def test_at_least_one_reservation_links_to_a_real_purchase_requisition() -> None:
    """Proves the Reservation -> PR join with real data, not a fabricated row.

    raw_resb.purchase_requisition/item_of_requisition -> a real
    raw_eban.purchase_requisition/item_of_requisition row -- both extracted
    from the same SAP tenant.
    """
    with get_sessionmaker()() as session:
        linked = session.execute(
            text(
                """
                SELECT count(*) FROM raw_resb r
                JOIN raw_eban e
                  ON r.purchase_requisition = e.purchase_requisition
                 AND r.item_of_requisition = e.item_of_requisition
                WHERE r.purchase_requisition <> ''
                """
            )
        ).scalar_one()
    assert linked > 0, "expected at least one real RESB -> EBAN join"


@needs_db
def test_some_postgres_reservations_have_no_pr_yet() -> None:
    """W6.2 test case: a reservation without a PR is retained, not dropped."""
    with get_sessionmaker()() as session:
        rows = fetch_reservations(session)
    assert any(row["Banfn"] is None for row in rows), (
        "expected the query's mixed selection (see postgres_reservation._RESERVATION_FETCH_QUERY) "
        "to include reservations with no purchase requisition yet"
    )
