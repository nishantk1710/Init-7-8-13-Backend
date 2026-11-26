"""The day SAP made a reservation (RESB-CRDAT), read for the keys asked about only (25-Nov-2026)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.integrations.sap.postgres_reservation import reservation_created_dates


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE n_resb (reservation TEXT, item_no_stock_transfer_reserv TEXT, created_on TEXT)"))
        conn.execute(
            text("INSERT INTO n_resb VALUES (:r, :i, :c)"),
            [
                {"r": "2041345", "i": "1", "c": "2026-11-03"},
                {"r": "2041345", "i": "2", "c": "2026-11-04"},
                {"r": "2039763", "i": "1", "c": ""},  # SAP sent no date
                {"r": "2000000", "i": "1", "c": "2026-01-02"},  # not asked about
            ],
        )
    with Session(engine) as session:
        yield session


def test_only_the_keys_asked_about_with_a_date_are_returned(db) -> None:
    found = reservation_created_dates(db, [("2041345", "1"), ("2041345", "2"), ("2039763", "1"), ("9", "1")])
    assert found == {("2041345", "1"): date(2026, 11, 3), ("2041345", "2"): date(2026, 11, 4)}


def test_another_item_of_the_same_reservation_is_not_returned(db) -> None:
    assert reservation_created_dates(db, [("2041345", "2")]) == {("2041345", "2"): date(2026, 11, 4)}


def test_nothing_asked_reads_nothing(db) -> None:
    assert reservation_created_dates(db, []) == {}
