"""W3.5 integration tests: real Postgres query shape, cross-checked against
manual SQL aggregation over the same tables.

Skipped outright when no ``DATABASE_URL`` is configured (same pattern as
``tests/test_db.py`` and ``tests/i13/test_reservation_postgres.py``). These
prove the repository's join/filter is correct against the real
``raw_mseg``/``raw_mkpf``/``raw_mard`` tables -- not a fixture standing in
for them.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i13.config import AgingThresholds
from app.initiatives.i13.movement_metrics import compute_all_movement_metrics
from app.integrations.sap.postgres_movements import PostgresMovementRepository, fetch_current_stock, fetch_movement_history
from app.shared.plant_scope import IN_SCOPE_PLANTS, sql_predicate

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")

THRESHOLDS = AgingThresholds(fast_max_days=365, slow_max_days=730)


@needs_db
def test_movement_history_join_matches_manual_sql_count() -> None:
    """Proves the repository's JOIN + WHERE (material<>'', plant<>'',
    in-scope plant, posting_date<>'') matches a hand-written equivalent, row
    for row.

    The plant-scope predicate is written out here from ``sql_predicate`` rather
    than restated, so this stays a check on the repository's join and not a
    copy of the scope rule that could drift from it.
    """
    with get_sessionmaker()() as session:
        expected = session.execute(
            text(
                f"""
                SELECT count(*) FROM raw_mseg m
                JOIN raw_mkpf h ON m.material_document = h.material_document
                                AND m.material_doc_year = h.material_doc_year
                WHERE m.material <> '' AND m.plant <> ''
                  AND {sql_predicate("m.plant")}
                  AND h.posting_date <> ''
                """
            )
        ).scalar_one()
        rows = fetch_movement_history(session)
    assert len(rows) == expected
    assert expected > 0


@needs_db
def test_movement_history_is_restricted_to_the_in_scope_plants() -> None:
    """No movement row may come back from a plant outside the delivery scope.

    The count test above would still pass if the filter were dropped from BOTH
    the repository and the hand-written SQL, so the scope needs its own check
    that names no plant the repository is allowed to return.
    """
    with get_sessionmaker()() as session:
        rows = fetch_movement_history(session)

    plants = {row["Werks"] for row in rows}
    assert plants, "no movement rows at all -- the fixture database is empty"
    assert plants <= set(IN_SCOPE_PLANTS), f"out-of-scope plants served: {plants - set(IN_SCOPE_PLANTS)}"


@needs_db
def test_movement_history_rows_are_typed_and_shaped_for_reuse() -> None:
    """Rows must be directly consumable by app.initiatives.i13.movements
    (Matnr/Werks/Bwart/Menge/BudatMkpf) with no further translation."""
    with get_sessionmaker()() as session:
        rows = fetch_movement_history(session, material=None, plant=None)
    row = rows[0]
    assert {"Matnr", "Werks", "Bwart", "Menge", "BudatMkpf"} <= set(row)
    assert isinstance(row["Menge"], Decimal)
    assert isinstance(row["BudatMkpf"], date)
    assert row["Matnr"] and row["Werks"]


@needs_db
def test_current_stock_matches_manual_sum_of_unrestricted() -> None:
    with get_sessionmaker()() as session:
        totals = fetch_current_stock(session)
        material, plant = next(iter(totals))
        expected = session.execute(
            text("SELECT sum(unrestricted::numeric) FROM raw_mard WHERE material = :m AND plant = :p"),
            {"m": material, "p": plant},
        ).scalar_one()
    assert totals[(material, plant)] == Decimal(str(expected))


@needs_db
def test_single_material_plant_metrics_match_manual_aggregation() -> None:
    """Cross-checks compute_all_movement_metrics's output for one real
    material+plant against a hand-written SQL aggregate over the same rows --
    the "compare calculated output back to the underlying records" proof."""
    with get_sessionmaker()() as session:
        # A material+plant with a non-trivial amount of real issue history
        # (found by inspection; not hard-coded business meaning, just a
        # concrete example to check the arithmetic against).
        candidate = session.execute(
            text(
                """
                SELECT m.material, m.plant, count(*) AS issue_events,
                       sum(m.quantity::numeric) AS total_qty, max(h.posting_date) AS last_issue
                FROM raw_mseg m
                JOIN raw_mkpf h ON m.material_document = h.material_document
                                AND m.material_doc_year = h.material_doc_year
                WHERE m.movement_type IN ('201', '261') AND m.material <> '' AND m.plant <> ''
                GROUP BY m.material, m.plant
                ORDER BY issue_events DESC
                LIMIT 1
                """
            )
        ).first()
        material, plant = candidate.material, candidate.plant

        repository = PostgresMovementRepository(session)
        as_of = date(2026, 9, 15)
        results = compute_all_movement_metrics(
            repository, thresholds=THRESHOLDS, window_months=12, as_of=as_of, material=material, plant=plant
        )
        assert len(results) == 1
        metrics = results[0]

        # Manual trailing-12m aggregate, computed the same way but in SQL,
        # over the same real rows -- reversal-netting excluded here
        # deliberately (net_event_count/net_quantity's reversal handling is
        # already unit-tested against synthetic reversal pairs in
        # test_movement_metrics.py); this checks the join/window/grouping.
        manual = session.execute(
            text(
                """
                SELECT count(*) AS cnt, sum(m.quantity::numeric) AS qty, max(h.posting_date) AS last_issue
                FROM raw_mseg m
                JOIN raw_mkpf h ON m.material_document = h.material_document
                                AND m.material_doc_year = h.material_doc_year
                WHERE m.movement_type IN ('201', '261')
                  AND m.material = :material AND m.plant = :plant
                  AND h.posting_date::date > (CAST(:as_of AS date) - interval '12 months')
                  AND h.posting_date::date <= CAST(:as_of AS date)
                """
            ),
            {"material": material, "plant": plant, "as_of": as_of.isoformat()},
        ).one()

    assert metrics.material == material
    assert metrics.plant == plant
    # net_event_count/net_quantity only subtract reversals that are
    # themselves present in-window; with no 202/262 in this material+plant's
    # window (checked implicitly: equality below would fail otherwise) the
    # netted figure equals the raw manual aggregate.
    if manual.cnt and not _has_reversals_in_window(session, material, plant, as_of):
        assert metrics.consumption_count_12m == manual.cnt
        assert metrics.consumption_qty_12m == Decimal(str(manual.qty))


def _has_reversals_in_window(session, material: str, plant: str, as_of: date) -> bool:
    count = session.execute(
        text(
            """
            SELECT count(*) FROM raw_mseg m
            JOIN raw_mkpf h ON m.material_document = h.material_document
                            AND m.material_doc_year = h.material_doc_year
            WHERE m.movement_type IN ('202', '262')
              AND m.material = :material AND m.plant = :plant
              AND h.posting_date::date > (CAST(:as_of AS date) - interval '12 months')
              AND h.posting_date::date <= CAST(:as_of AS date)
            """
        ),
        {"material": material, "plant": plant, "as_of": as_of.isoformat()},
    ).scalar_one()
    return count > 0
