"""W6.2 reservation source: real RESB rows from Postgres (``raw_resb``).

Split like ``tests/test_db.py``: skipped outright when no ``DATABASE_URL`` is
configured, since these prove behaviour against the real extracted dataset,
not a fixture. Where ``tests/i13/test_ledger.py`` proves the reservation ->
PR join logic against small synthetic CSVs, these prove the same normalized
shape and a genuine RESB -> EBAN join hold up against real Postgres data --
without fabricating any reservation row.
"""

from pathlib import Path

import pytest

from app.core.config import Settings, get_settings
from app.core.db import get_engine, get_sessionmaker, reset_engine_cache
from app.initiatives.i13.ledger import build_utilisation_ledger
from app.integrations.sap.gateway import SapGateway
from app.integrations.sap.postgres_reservation import PostgresReservationProvider
from app.integrations.sap.source_mode import SourceMode

needs_db = pytest.mark.skipif(
    not get_settings().database_url, reason="DATABASE_URL not set"
)


@needs_db
def test_postgres_reservation_rows_are_real_and_typed() -> None:
    provider = PostgresReservationProvider(get_sessionmaker())
    result = provider.get_reservations()

    assert result.status.mode is SourceMode.LIVE
    assert result.status.row_count > 0
    assert len(result.rows) == result.status.row_count

    row = result.rows[0]
    assert row["Rsnum"], "every raw_resb row has a reservation number"
    assert row["Matnr"] and row["Werks"]
    if row["Bdmng"] is not None:
        from decimal import Decimal

        assert isinstance(row["Bdmng"], Decimal)
    if row["Bdter"] is not None:
        from datetime import date

        assert isinstance(row["Bdter"], date)


@needs_db
def test_at_least_one_reservation_links_to_a_real_purchase_requisition() -> None:
    """Proves the Reservation -> PR join with real data, not a fabricated row.

    raw_resb.purchase_requisition/item_of_requisition -> a real
    raw_eban.purchase_requisition/item_of_requisition row -- both extracted
    from the same SAP tenant, unlike the synthetic CSV chain the default
    ("mock") reservation source is tied to.
    """
    with get_sessionmaker()() as session:
        from sqlalchemy import text

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
    provider = PostgresReservationProvider(get_sessionmaker())
    rows = provider.get_reservations().rows
    assert any(row["Banfn"] is None for row in rows), (
        "expected the query's mixed selection (see postgres_reservation._QUERY) "
        "to include reservations with no purchase requisition yet"
    )


@needs_db
def test_switching_reservation_source_requires_no_ledger_code_change(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    """Implementation plan §11 / W6.2 test #14: swap mock -> postgres via
    config only. The ledger builder is called identically either way; it
    never branches on where reservations came from."""
    from tests.i13.test_ledger import (
        MOVEMENT_HEADER,
        PO_ITEM_HEADER,
        PR_HEADER,
        RESERVATION_HEADER,
    )
    from tests.i13.conftest import write_csv

    write_csv(data_dir / "sap" / "PurchaseRequisitionSet.csv", PR_HEADER, [])
    write_csv(data_dir / "sap" / "PurchaseOrderItemSet.csv", PO_ITEM_HEADER, [])
    write_csv(data_dir / "sap" / "GoodsMovementItemSet.csv", MOVEMENT_HEADER, [])
    write_csv(data_dir / "sap" / "ReservationItemSet.csv", RESERVATION_HEADER, [])

    gateway = SapGateway(data_dir)

    monkeypatch.setattr(
        "app.integrations.sap.gateway.get_settings",
        lambda: Settings(i13_reservation_source="mock", _env_file=None),
    )
    mock_result = gateway.get_reservations()
    assert mock_result.status.mode is SourceMode.MOCK

    monkeypatch.setattr(
        "app.integrations.sap.gateway.get_settings",
        lambda: Settings(
            i13_reservation_source="postgres",
            database_url=get_settings().database_url,
            _env_file=None,
        ),
    )
    live_result = gateway.get_reservations()
    assert live_result.status.mode is SourceMode.LIVE
    assert live_result.status.row_count > 0

    # build_utilisation_ledger's own call signature and behaviour are
    # unchanged by the swap -- it still runs, still returns a list, and
    # reservations that cannot find their PR in this (still-synthetic) PR
    # set are correctly UNMATCHED rather than guessed at.
    entries = build_utilisation_ledger(gateway)
    assert isinstance(entries, list)
