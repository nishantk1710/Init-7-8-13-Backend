"""W6.2 integration tests: real Postgres query shape, and real-data
validation -- proving the Reservation -> PR -> PO -> GR -> GI chain against
actual source identifiers, not just that unit tests pass.

Skipped outright when no ``DATABASE_URL`` is configured (same pattern as the
other Postgres-gated I13 test files).
"""

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i13.models import GiLinkStatus, ReservationPrLinkStatus
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.shared.material_scope import MaterialScope, classify_material_scope
from app.shared.plant_scope import sql_predicate

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@needs_db
@pytest.mark.needs_seed_data
def test_raw_resb_own_key_is_unique() -> None:
    with get_sessionmaker()() as session:
        row = session.execute(
            text(
                "SELECT count(*) total_rows, "
                "count(distinct CONCAT(reservation, '/', item_no_stock_transfer_reserv)) distinct_keys FROM raw_resb"
            )
        ).one()
    assert row.total_rows == row.distinct_keys


@needs_db
@pytest.mark.needs_seed_data
def test_a_real_reservation_links_to_a_real_pr_po_gr_chain() -> None:
    with get_sessionmaker()() as session:
        candidate = session.execute(
            text(
                """
                SELECT TOP 1 r.reservation, r.item_no_stock_transfer_reserv, r.purchase_requisition, r.item_of_requisition,
                       k.purchasing_document, k.item
                FROM raw_resb r
                JOIN raw_eban e ON r.purchase_requisition = e.purchase_requisition
                                AND r.item_of_requisition = e.item_of_requisition
                JOIN raw_ekpo k ON k.purchase_requisition = r.purchase_requisition
                                AND k.item_of_requisition = r.item_of_requisition
                JOIN raw_ekbe b ON b.purchasing_document = k.purchasing_document AND b.item = k.item
                WHERE b.po_history_category = 'E' AND k.material <> '' AND k.plant <> ''
                """
            )
        ).first()
        assert candidate is not None, "expected at least one real Reservation->PR->PO->GR chain"

        reservation_repo = PostgresReservationRepository(session)
        procurement_repo = PostgresProcurementRepository(session)
        scope_index = fetch_material_scope_index(session)
        entries = build_reservation_ledger(
            reservation_repo,
            procurement_repo,
            material_scope_index=scope_index,
            reservation_number=candidate.reservation,
            include_out_of_scope=True,
        )

    matching = [e for e in entries if e.reservation_item == candidate.item_no_stock_transfer_reserv]
    assert len(matching) == 1
    entry = matching[0]
    assert entry.reservation_pr_link_status is ReservationPrLinkStatus.LINKED
    assert entry.pr_number == candidate.purchase_requisition
    assert entry.po_number == candidate.purchasing_document


@needs_db
@pytest.mark.needs_seed_data
def test_gi_resolves_via_reservation_for_real_data() -> None:
    """Proves the headline W6.2 capability: at least one real reservation
    has goods-issue movements resolved via RSNUM/RSPOS -- something W6.1
    could never do (0% PO-referenced issue movements, measured)."""
    with get_sessionmaker()() as session:
        reservation_repo = PostgresReservationRepository(session)
        gi_rows = reservation_repo.get_goods_issue_by_reservation()
    assert len(gi_rows) > 0, "expected real issue movements carrying a reservation reference"


@needs_db
@pytest.mark.needs_seed_data
def test_real_mrp_consolidation_case_is_reported_unresolved() -> None:
    """Confirms the measured real consolidation case (PR 2000028244/10,
    referenced by 3 distinct reservations) is handled honestly."""
    with get_sessionmaker()() as session:
        consolidated = session.execute(
            text(
                """
                SELECT TOP 1 purchase_requisition, item_of_requisition,
                       count(distinct CONCAT(reservation, '/', item_no_stock_transfer_reserv)) as reservations
                FROM raw_resb
                WHERE purchase_requisition IS NOT NULL AND purchase_requisition <> '' AND purchase_requisition <> '0'
                GROUP BY purchase_requisition, item_of_requisition
                HAVING count(distinct CONCAT(reservation, '/', item_no_stock_transfer_reserv)) > 1
                """
            )
        ).first()
        assert consolidated is not None, "expected at least one real MRP-consolidated PR"

        reservation_repo = PostgresReservationRepository(session)
        procurement_repo = PostgresProcurementRepository(session)
        scope_index = fetch_material_scope_index(session)
        entries = build_reservation_ledger(
            reservation_repo,
            procurement_repo,
            material_scope_index=scope_index,
            pr_number=consolidated.purchase_requisition,
            include_out_of_scope=True,
        )

    assert len(entries) == consolidated.reservations
    for entry in entries:
        assert entry.reservation_pr_link_status is ReservationPrLinkStatus.CONSOLIDATION_UNRESOLVED
        assert entry.po_number is None


@needs_db
@pytest.mark.needs_seed_data
def test_oar_scope_excludes_real_non_oar_reservations() -> None:
    with get_sessionmaker()() as session:
        non_oar_material = session.execute(
            text("SELECT TOP 1 material, plant FROM raw_marc WHERE mrp_type = 'VB'")
        ).first()
        assert non_oar_material is not None

        reservation_repo = PostgresReservationRepository(session)
        procurement_repo = PostgresProcurementRepository(session)
        scope_index = fetch_material_scope_index(session, material=non_oar_material.material, plant=non_oar_material.plant)

        default_entries = build_reservation_ledger(
            reservation_repo,
            procurement_repo,
            material_scope_index=scope_index,
            material=non_oar_material.material,
            plant=non_oar_material.plant,
        )
        all_entries = build_reservation_ledger(
            reservation_repo,
            procurement_repo,
            material_scope_index=scope_index,
            material=non_oar_material.material,
            plant=non_oar_material.plant,
            include_out_of_scope=True,
        )
    assert default_entries == []
    if all_entries:
        assert all(e.material_scope is MaterialScope.MIN_MAX for e in all_entries)


@needs_db
@pytest.mark.needs_seed_data
def test_material_scope_index_matches_classify_material_scope() -> None:
    with get_sessionmaker()() as session:
        # In-scope plants only: fetch_material_scope_index returns no row for
        # any other plant, so an unscoped sample would make this a test of the
        # plant filter instead of the DISMM classification it is for.
        row = session.execute(
            text(
                "SELECT TOP 1 material, plant, mrp_type FROM raw_marc "
                f"WHERE mrp_type <> '' AND {sql_predicate('plant')}"
            )
        ).first()
        assert row is not None, "no in-scope MARC row carries an MRP type"
        index = fetch_material_scope_index(session, material=row.material, plant=row.plant)
    assert classify_material_scope(index[(row.material, row.plant)]) == classify_material_scope(row.mrp_type)
