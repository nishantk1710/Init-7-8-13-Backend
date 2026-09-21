"""W6.4 integration tests: the persisted consumption-attribution mart against
real Postgres data -- proving the refresh is idempotent, reservation-line
grain is preserved, and cost-centre enrichment is genuinely config-gated.

Skipped outright when no ``DATABASE_URL`` is configured (same pattern as the
other Postgres-gated I13 test files). This exercises the raw ``raw_resb``
extract already loaded into Postgres (105,848 rows -- see
``postgres_reservation.py``'s module docstring), which is NOT empty, even
though the live SAP ``ReservationItemSet`` OData endpoint reports 0 rows.
"""

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i13.config import build_i13_config
from app.initiatives.i13.consumption_attribution_mart import (
    get_attribution_for_entry,
    refresh_consumption_attribution,
)
from app.initiatives.i13.models import ConsumptionAttributionStatus
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.models.i13_consumption_attribution import ConsumptionAttributionRecord

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


def _refresh(session, *, plant: str | None = None, cost_centre_enabled: bool = False):
    settings = get_settings()
    config = build_i13_config(settings)
    if cost_centre_enabled:
        config = replace(config, attribution=replace(config.attribution, cost_centre_enabled=True))
    reservation_repo = PostgresReservationRepository(session)
    procurement_repo = PostgresProcurementRepository(session)
    scope_index = fetch_material_scope_index(session, plant=plant)
    data_dir = Path(settings.i13_data_dir)
    return refresh_consumption_attribution(
        session,
        reservation_repo,
        procurement_repo,
        scope_index,
        config,
        data_dir,
        plant=plant,
        include_out_of_scope=True,
    )


@needs_db
def test_refresh_is_idempotent_across_two_consecutive_runs() -> None:
    with get_sessionmaker()() as session:
        first = _refresh(session, plant="1300")
        session.commit()
        rows_after_first = session.execute(
            select(func.count()).select_from(ConsumptionAttributionRecord).where(ConsumptionAttributionRecord.plant == "1300")
        ).scalar_one()

        second = _refresh(session, plant="1300")
        session.commit()
        rows_after_second = session.execute(
            select(func.count()).select_from(ConsumptionAttributionRecord).where(ConsumptionAttributionRecord.plant == "1300")
        ).scalar_one()

    assert len(first) > 0
    assert len(first) == len(second) == rows_after_first == rows_after_second


@needs_db
def test_mart_has_no_duplicate_ledger_id_rows() -> None:
    with get_sessionmaker()() as session:
        _refresh(session, plant="1300")
        session.commit()
        duplicates = session.execute(
            select(ConsumptionAttributionRecord.ledger_id, func.count())
            .where(ConsumptionAttributionRecord.plant == "1300")
            .group_by(ConsumptionAttributionRecord.ledger_id)
            .having(func.count() > 1)
        ).all()
    assert duplicates == []


@needs_db
def test_reservation_line_grain_is_preserved_not_collapsed_by_rsnum() -> None:
    """Same RSNUM, different RSPOS must remain separate rows -- proves W6.4
    didn't collapse reservation-line grain to reservation-header grain."""
    with get_sessionmaker()() as session:
        multi_item_rsnum = session.execute(
            select(ConsumptionAttributionRecord.reservation_number)
            .group_by(ConsumptionAttributionRecord.reservation_number)
            .having(func.count(func.distinct(ConsumptionAttributionRecord.reservation_item)) > 1)
            .limit(1)
        ).scalar_one_or_none()
        if multi_item_rsnum is None:
            _refresh(session, plant="1300")
            session.commit()
            multi_item_rsnum = session.execute(
                select(ConsumptionAttributionRecord.reservation_number)
                .where(ConsumptionAttributionRecord.plant == "1300")
                .group_by(ConsumptionAttributionRecord.reservation_number)
                .having(func.count(func.distinct(ConsumptionAttributionRecord.reservation_item)) > 1)
                .limit(1)
            ).scalar_one_or_none()
        if multi_item_rsnum is None:
            pytest.skip("no real multi-item reservation available in this dataset")

        items = session.execute(
            select(ConsumptionAttributionRecord.reservation_item)
            .where(ConsumptionAttributionRecord.reservation_number == multi_item_rsnum)
        ).scalars().all()
    assert len(items) == len(set(items))
    assert len(items) > 1


@needs_db
def test_cost_centre_disabled_by_default_never_populates_cost_centre() -> None:
    with get_sessionmaker()() as session:
        _refresh(session, plant="1300", cost_centre_enabled=False)
        session.commit()
        non_null_cost_centres = session.execute(
            select(func.count())
            .select_from(ConsumptionAttributionRecord)
            .where(ConsumptionAttributionRecord.plant == "1300", ConsumptionAttributionRecord.cost_centre.is_not(None))
        ).scalar_one()
    assert non_null_cost_centres == 0


@needs_db
def test_no_attribution_status_is_ever_missing_or_invalid() -> None:
    with get_sessionmaker()() as session:
        _refresh(session, plant="1300")
        session.commit()
        statuses = session.execute(
            select(ConsumptionAttributionRecord.attribution_status)
            .where(ConsumptionAttributionRecord.plant == "1300")
            .distinct()
        ).scalars().all()
    assert statuses
    assert set(statuses) <= {status.value for status in ConsumptionAttributionStatus}


@needs_db
def test_get_attribution_for_entry_reads_back_a_refreshed_row() -> None:
    with get_sessionmaker()() as session:
        attributions = _refresh(session, plant="1300")
        session.commit()
        assert attributions, "expected at least one real reservation ledger entry for plant 1300"

        fetched = get_attribution_for_entry(session, attributions[0].ledger_id)
    assert fetched is not None
    assert fetched.ledger_id == attributions[0].ledger_id
    assert fetched.attribution_status == attributions[0].status.value
