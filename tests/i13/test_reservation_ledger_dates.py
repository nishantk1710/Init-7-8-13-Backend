"""The Reservations tab's dates (27-Nov-2026): the created date (RESB-CRDAT) comes
with the ledger entry from the build, never from a per-request read of the n_resb
view (a full scan: 79 s a page on non-prod), and the plan window from the linked
assistant session."""

from __future__ import annotations

import dataclasses
from datetime import date

from app.api.i13 import reservation_ledger as route
from app.integrations.sap.postgres_reservation import _to_reservation_row
from tests.i13.test_act_service import _ledger_entry


class _Row:
    """A fetched n_resb row: attribute access, as SQLAlchemy returns it."""

    def __init__(self, **values):
        self.__dict__.update(values)


def _resb_row(**overrides):
    values = dict(
        reservation="2041345", item_no_stock_transfer_reserv="1", item_deleted="", final_issue="",
        material="4000002200", plant="1300", storage_location="0001", requirement_date="2026-11-03",
        requirement_quantity="3", base_unit_of_measure="EA", quantity_withdrawn="0", value_withdrawn="0",
        purchase_requisition=None, item_of_requisition="0", order="", movement_type="201",
        receiving_plant="", receiving_stor_loc="", goods_recipient="", text="RRXVC7GKT6",
        created_on="2026-11-03",
    )
    values.update(overrides)
    return _Row(**values)


def test_the_reservation_row_carries_sap_s_created_date() -> None:
    assert _to_reservation_row(_resb_row())["Crdat"] == date(2026, 11, 3)
    assert _to_reservation_row(_resb_row(created_on=date(2026, 11, 3)))["Crdat"] == date(2026, 11, 3)
    assert _to_reservation_row(_resb_row(created_on=None))["Crdat"] is None
    assert _to_reservation_row(_resb_row(created_on=""))["Crdat"] is None


def test_the_page_reads_the_created_date_from_the_entry_and_the_window_from_the_plan(monkeypatch) -> None:
    asked = []

    def windows(db, session_ids):
        asked.append(set(session_ids))
        return {"S1N8AXM48P": (date(2026, 11, 4), date(2026, 11, 17))}

    monkeypatch.setattr(route, "session_plan_windows", windows)
    linked = dataclasses.replace(
        _ledger_entry(reservation_number="2041345", reservation_item="1"), created_on=date(2026, 11, 3)
    )
    other = dataclasses.replace(
        _ledger_entry(ledger_id="L2", reservation_number="219937", reservation_item="2"), created_on=None
    )
    sessions = {("2041345", "1"): "S1N8AXM48P"}

    dates = route._Dates(object(), [linked, other], sessions)

    assert asked == [{"S1N8AXM48P"}]
    assert dates.fields(linked, "S1N8AXM48P") == {
        "created_date": date(2026, 11, 3),
        "plan_recorded": True,
        "planned_start": date(2026, 11, 4),
        "planned_end": date(2026, 11, 17),
    }
    assert dates.fields(other, None) == {
        "created_date": None, "plan_recorded": False, "planned_start": None, "planned_end": None,
    }
