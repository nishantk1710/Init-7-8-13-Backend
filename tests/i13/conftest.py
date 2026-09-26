"""Shared fixtures for Initiative 13 tests.

Unit tests build small fake repositories (see each test module) rather than
hitting Postgres; ``write_csv``/``data_dir`` remain for the one thing still
genuinely CSV-based -- ``consumption_plans.csv`` (platform-owned, not a SAP
extract; see ``app/initiatives/i13/plans.py``). Postgres integration/
real-data tests live in the ``*_postgres.py`` files, skipped when no database
is configured.
"""

import csv
from pathlib import Path

import pytest

from app.initiatives.i13.config import I13Config, build_i13_config


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in header})


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def i13_config() -> I13Config:
    from app.core.config import Settings

    return build_i13_config(Settings())


class FakeMovementRepository:
    """Stands in for PostgresMovementRepository -- shared by test_watch.py,
    test_exceptions.py and test_reclassification.py."""

    def __init__(self, movements=(), stock=None):
        self._movements = list(movements)
        self._stock = dict(stock or {})

    def get_movement_history(self, *, material=None, plant=None):
        rows = self._movements
        if material:
            rows = [r for r in rows if r["Matnr"] == material]
        if plant:
            rows = [r for r in rows if r["Werks"] == plant]
        return rows

    def get_current_stock(self, *, material=None, plant=None):
        return {
            key: qty
            for key, qty in self._stock.items()
            if (not material or key[0] == material) and (not plant or key[1] == plant)
        }


class FakeProcurementRepository:
    """Stands in for PostgresProcurementRepository."""

    def __init__(self, prs=(), po_items=(), gr_rows=(), gi_rows=()):
        self._prs = list(prs)
        self._po_items = list(po_items)
        self._gr_rows = list(gr_rows)
        self._gi_rows = list(gi_rows)

    def get_purchase_requisitions(self, *, pr_number=None, material=None, plant=None):
        rows = self._prs
        if pr_number:
            rows = [r for r in rows if r["Banfn"] == pr_number]
        if material:
            rows = [r for r in rows if r["Matnr"] == material]
        if plant:
            rows = [r for r in rows if r["Werks"] == plant]
        return rows

    def get_purchase_order_items(self, *, po_number=None, pr_number=None, material=None, plant=None):
        rows = self._po_items
        if po_number:
            rows = [r for r in rows if r["Ebeln"] == po_number]
        if pr_number:
            rows = [r for r in rows if r.get("Banfn") == pr_number]
        if material:
            rows = [r for r in rows if r["Matnr"] == material]
        if plant:
            rows = [r for r in rows if r["Werks"] == plant]
        return rows

    def get_goods_receipt_history(self, *, po_number=None):
        rows = self._gr_rows
        if po_number:
            rows = [r for r in rows if r["Ebeln"] == po_number]
        return rows

    def get_deterministic_gi_candidates(self, *, po_number=None):
        rows = self._gi_rows
        if po_number:
            rows = [r for r in rows if r["Ebeln"] == po_number]
        return rows


class FakeReservationRepository:
    """Stands in for PostgresReservationRepository."""

    def __init__(self, reservations=(), gi_rows=()):
        self._reservations = list(reservations)
        self._gi_rows = list(gi_rows)

    def get_reservations(self, *, reservation_number=None, pr_number=None, material=None, plant=None):
        rows = self._reservations
        if reservation_number:
            rows = [r for r in rows if r["Rsnum"] == reservation_number]
        if pr_number:
            rows = [r for r in rows if r.get("Banfn") == pr_number]
        if material:
            rows = [r for r in rows if r["Matnr"] == material]
        if plant:
            rows = [r for r in rows if r["Werks"] == plant]
        return rows

    def get_goods_issue_by_reservation(self, *, reservation_number=None):
        rows = self._gi_rows
        if reservation_number:
            rows = [r for r in rows if r["Rsnum"] == reservation_number]
        return rows


def oar_scope_index(*keys, dismm: str = "ND") -> dict:
    """(material, plant) -> an OAR-classifying DISMM value for every key given."""
    return {key: dismm for key in keys}


class FakeCriticalitySource:
    """Stands in for a real ``CriticalitySource`` (W3.4) -- an in-memory
    (material, plant) -> tier map, so W6.5 tests never need a database."""

    name = "fake"

    def __init__(self, tiers: dict | None = None):
        from app.core.criticality import CriticalityResult, parse_tier

        self._tiers = dict(tiers or {})
        self._parse_tier = parse_tier
        self._CriticalityResult = CriticalityResult

    def get(self, sap_material_number, sap_plant_code=None):
        raw = self._tiers.get((sap_material_number, sap_plant_code))
        tier = self._parse_tier(raw) if isinstance(raw, str) else raw
        return self._CriticalityResult(
            sap_material_number=sap_material_number,
            sap_plant_code=sap_plant_code,
            tier=tier,
            source=self.name,
            reason=None if tier else f"{sap_material_number} not present",
        )

    def check_connection(self) -> None:
        return None


class FakeExceptionRepository:
    """In-memory ``ExceptionRepository`` (W6.6) -- stands in for
    ``app.initiatives.i13.act_exception_store.SqlExceptionRepository`` so
    ACT service/state-machine tests never need a database."""

    def __init__(self):
        import collections

        self._exceptions: dict = {}
        self._events: dict = collections.defaultdict(list)
        self._confirmations: dict = {}
        self._notifications: dict = collections.defaultdict(list)
        self._event_seq = 0

    def get(self, exception_id: str):
        return self._exceptions.get(exception_id)

    def upsert(self, exception) -> None:
        self._exceptions[exception.exception_id] = exception

    def list(self, *, material=None, plant=None, exception_type=None, status=None, owner_requester_id=None):
        items = list(self._exceptions.values())
        if material:
            items = [i for i in items if i.material == material]
        if plant:
            items = [i for i in items if i.plant == plant]
        if exception_type:
            items = [i for i in items if i.exception_type == exception_type]
        if status:
            items = [i for i in items if i.status == status]
        if owner_requester_id:
            items = [i for i in items if i.owner_requester_id == owner_requester_id]
        return items

    def append_event(self, event) -> None:
        import dataclasses

        self._event_seq += 1
        self._events[event.exception_id].append(dataclasses.replace(event, event_id=str(self._event_seq)))

    def list_events(self, exception_id: str):
        return list(self._events.get(exception_id, []))

    def save_confirmation(self, confirmation) -> None:
        self._confirmations[confirmation.exception_id] = confirmation

    def get_confirmation(self, exception_id: str):
        return self._confirmations.get(exception_id)

    def record_notification(self, attempt) -> None:
        self._notifications[attempt.exception_id].append(attempt)

    def list_notifications(self, exception_id: str):
        return list(self._notifications.get(exception_id, []))


class FakeNotificationPort:
    """Stands in for a real ``NotificationPort`` (W6.6) -- records every
    intent it was asked to send, and can be configured to simulate a
    provider failure without raising (``outcome=FAILED``) or a raising
    adapter (``raises=True``)."""

    def __init__(self, *, fail: bool = False, raises: bool = False):
        self.sent = []
        self._fail = fail
        self._raises = raises

    def send(self, intent):
        from app.initiatives.i13.act.domain import NotificationOutcome, NotificationResult

        self.sent.append(intent)
        if self._raises:
            raise RuntimeError("simulated notification provider failure")
        if self._fail:
            return NotificationResult(outcome=NotificationOutcome.FAILED, detail="simulated failure")
        return NotificationResult(outcome=NotificationOutcome.SENT, detail="ok")


class FakeEscalationRecipientProvider:
    """Stands in for a real ``EscalationRecipientProvider`` (W6.6) -- an
    in-memory plant -> HOD identity map. Missing plants resolve to ``None``
    (routing pending), matching ``ConfigEscalationRecipientProvider``'s
    no-fabrication posture."""

    def __init__(self, plant_to_hod: dict | None = None):
        self._plant_to_hod = dict(plant_to_hod or {})

    def get_hod(self, *, material: str, plant: str, requester_id):
        return self._plant_to_hod.get(plant)


class FakeHodJustificationProvider:
    """Stands in for a real ``HodJustificationProvider`` (W6.6) -- an
    in-memory (material, plant) -> bool|None map. Defaults to ``None``
    (unavailable/unknown) for any key not explicitly given, matching
    ``NullHodJustificationProvider``'s no-fabrication posture."""

    def __init__(self, answers: dict | None = None):
        self._answers = dict(answers or {})

    def get_hod_justification(self, *, material: str, plant: str) -> bool | None:
        return self._answers.get((material, plant))
