"""W6.1 integration tests: real Postgres query shape, and the real-data
validation examples required by the implementation plan (§20) -- proving the
joins against actual source identifiers, not just that unit tests pass.

Skipped outright when no ``DATABASE_URL`` is configured (same pattern as
``tests/test_db.py``, ``test_reservation_postgres.py``,
``test_movement_metrics_postgres.py``).
"""

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i13.models import GiLinkStatus, LifecycleStatus, PrPoLinkStatus
from app.initiatives.i13.procurement_chain import build_procurement_chain, compute_chain_diagnostics
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@needs_db
def test_eban_and_ekpo_own_keys_are_unique() -> None:
    """The uniqueness assumption the whole PR/PO grain design rests on --
    checked against real data, not assumed."""
    with get_sessionmaker()() as session:
        eban = session.execute(
            text(
                "SELECT count(*) total_rows, "
                "count(distinct purchase_requisition||'/'||item_of_requisition) distinct_keys FROM raw_eban"
            )
        ).one()
        ekpo = session.execute(
            text(
                "SELECT count(*) total_rows, "
                "count(distinct purchasing_document||'/'||item) distinct_keys FROM raw_ekpo"
            )
        ).one()
    assert eban.total_rows == eban.distinct_keys, "raw_eban key is not unique -- design assumption violated"
    assert ekpo.total_rows == ekpo.distinct_keys, "raw_ekpo key is not unique -- design assumption violated"


@needs_db
def test_a_real_pr_item_is_split_across_multiple_real_pos() -> None:
    """Confirms real multi-sourcing exists (not a hypothetical) -- the
    reason PartialLedgerEntry does not treat PR number+item as a unique key."""
    with get_sessionmaker()() as session:
        split = session.execute(
            text(
                """
                SELECT purchase_requisition, item_of_requisition, count(distinct purchasing_document) as pos
                FROM raw_ekpo WHERE purchase_requisition <> ''
                GROUP BY purchase_requisition, item_of_requisition
                HAVING count(distinct purchasing_document) > 1
                LIMIT 1
                """
            )
        ).first()
        assert split is not None, "expected at least one real multi-sourced PR item"

        repository = PostgresProcurementRepository(session)
        entries = build_procurement_chain(repository, pr_number=split.purchase_requisition)
        matching = [e for e in entries if e.pr_item == split.item_of_requisition]
    assert len(matching) == split.pos
    assert len({e.po_number for e in matching}) == split.pos


@needs_db
def test_real_po_with_unresolved_pr_reference_exists_and_is_reported() -> None:
    with get_sessionmaker()() as session:
        unresolved = session.execute(
            text(
                """
                SELECT k.purchasing_document, k.item, k.purchase_requisition, k.item_of_requisition
                FROM raw_ekpo k
                LEFT JOIN raw_eban e ON k.purchase_requisition = e.purchase_requisition
                                     AND k.item_of_requisition = e.item_of_requisition
                WHERE k.purchase_requisition <> '' AND e.purchase_requisition IS NULL
                  AND k.material <> '' AND k.plant <> ''
                LIMIT 1
                """
            )
        ).first()
        assert unresolved is not None

        repository = PostgresProcurementRepository(session)
        entries = build_procurement_chain(repository, po_number=unresolved.purchasing_document)
    matching = [e for e in entries if e.po_item == unresolved.item]
    assert len(matching) == 1
    assert matching[0].pr_po_link_status is PrPoLinkStatus.PR_REFERENCE_UNRESOLVED
    assert matching[0].pr_quantity is None


@needs_db
def test_real_chain_pr_to_po_to_gr_matches_manual_sql() -> None:
    """§20 example A/B: a real PR -> PO -> GR chain, quantities cross-checked
    against a hand-written SQL aggregate over raw_ekbe."""
    with get_sessionmaker()() as session:
        candidate = session.execute(
            text(
                """
                SELECT k.purchasing_document, k.item, k.purchase_requisition, k.item_of_requisition, k.order_quantity
                FROM raw_ekpo k
                JOIN raw_eban e ON k.purchase_requisition = e.purchase_requisition
                                AND k.item_of_requisition = e.item_of_requisition
                JOIN raw_ekbe b ON b.purchasing_document = k.purchasing_document AND b.item = k.item
                WHERE b.po_history_category = 'E' AND k.material <> '' AND k.plant <> ''
                GROUP BY k.purchasing_document, k.item, k.purchase_requisition, k.item_of_requisition, k.order_quantity
                LIMIT 1
                """
            )
        ).first()
        assert candidate is not None, "expected at least one real PR->PO->GR chain"

        manual_gr = session.execute(
            text(
                """
                SELECT sum(CASE WHEN movement_type = '101' THEN quantity::numeric
                                WHEN movement_type = '102' THEN -quantity::numeric
                                ELSE 0 END) AS net_qty
                FROM raw_ekbe
                WHERE po_history_category = 'E' AND purchasing_document = :po AND item = :item
                """
            ),
            {"po": candidate.purchasing_document, "item": candidate.item},
        ).scalar_one()

        repository = PostgresProcurementRepository(session)
        entries = build_procurement_chain(repository, po_number=candidate.purchasing_document)

    matching = [e for e in entries if e.po_item == candidate.item]
    assert len(matching) == 1
    entry = matching[0]
    assert entry.pr_number == candidate.purchase_requisition
    assert entry.pr_po_link_status is PrPoLinkStatus.LINKED
    assert entry.received_quantity == Decimal(str(manual_gr))


@needs_db
def test_gi_linkage_is_unresolved_for_real_data_today() -> None:
    """Documents the measured, honest state of this dataset: no real
    procurement chain has a deterministic GI link yet (0 of 47,635 issue
    movements carry a PO reference)."""
    with get_sessionmaker()() as session:
        repository = PostgresProcurementRepository(session)
        gi_candidates = repository.get_deterministic_gi_candidates()
    assert gi_candidates == [], (
        "if this now returns rows, real PO-referenced issue data has appeared -- "
        "update the W6.1 implementation report, this is no longer purely aspirational plumbing"
    )


@needs_db
def test_diagnostics_against_real_data_are_internally_consistent() -> None:
    with get_sessionmaker()() as session:
        repository = PostgresProcurementRepository(session)
        diagnostics = compute_chain_diagnostics(repository, plant="1300")
    assert diagnostics.pr_items_total > 0
    assert (
        diagnostics.pr_items_with_no_po + diagnostics.pr_items_with_single_po + diagnostics.pr_items_with_multiple_po
        == diagnostics.pr_items_total
    )
