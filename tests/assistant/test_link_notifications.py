"""Telling the requester their request was linked, or reminding them it is not -- app/assistant/link_notifications.py.

Built on the real request from ``test_requests`` (every line case both
initiatives produce), on SQLite, with the notification table alongside.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.assistant import link_notifications
from app.assistant import requests as request_service
from app.assistant.models import AssistantRequest
from app.integrations.notifications import templates
from app.models.app_user import AppUser
from app.models.base import Base
from app.models.i13_session_link import SessionReservationLink
from app.models.notification import NotificationRow
from tests.assistant.test_requests import (  # noqa: F401 -- pytest fixtures
    OAR_ABOVE,
    OAR_NORMAL,
    OAR_NOT_NEEDED,
    PLANT,
    REPAIR_EXISTING,
    REPAIR_NEW,
    TODAY,
    every_case,
    factory,
    settings,
)

REQUESTER = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(factory, settings):
    Base.metadata.create_all(factory.kw["bind"], tables=[NotificationRow.__table__, AppUser.__table__])
    with factory() as session:
        yield session


def make_request(db, settings, *, requester: str = REQUESTER, age: timedelta = timedelta(hours=1)) -> str:
    result = request_service.create(
        db,
        plant=PLANT,
        lines=every_case(),
        idempotency_key=f"draft-{requester}-{age}",
        requester=requester,
        requested_for="J Miller",
        department="10100",
        today=TODAY,
        settings=settings,
    )
    request_id = result.request.id
    db.execute(update(AssistantRequest).where(AssistantRequest.id == request_id).values(issued_at=NOW - age))
    db.commit()
    return request_id


def session_for(db, request_id: str, material: str) -> str:
    return next(s.id for s in request_service.lines_of(db, request_id) if s.material_id == material)


def link(db, request_id: str, material: str, *, item: str = "0001", seen: datetime = NOW - timedelta(minutes=5)) -> None:
    db.add(
        SessionReservationLink(
            session_id=session_for(db, request_id, material),
            reservation_number="12345678",
            reservation_item=item,
            material=material,
            plant=PLANT,
            source="SGTXT",
            sgtxt=request_id,
            first_seen_at=seen,
        )
    )
    db.commit()


def run(db, settings, now: datetime = NOW) -> link_notifications.LinkNoticeResult:
    result = link_notifications.run_link_notices(db, now, settings)
    db.commit()
    return result


def notices(db, event_type: str) -> list[NotificationRow]:
    return list(db.scalars(select(NotificationRow).where(NotificationRow.event_type == event_type)))


class TestLinked:
    def test_a_new_link_tells_the_requester_once(self, db, settings) -> None:
        request_id = make_request(db, settings)
        link(db, request_id, OAR_NORMAL)

        assert run(db, settings).linked == 1
        [notice] = notices(db, templates.RESERVATION_LINKED)
        assert notice.recipient_user_id == REQUESTER
        assert notice.channel == "IN_APP"
        assert notice.module == "ASSISTANT"
        assert notice.entity_id == request_id
        assert notice.action_path == f"/assistant/requests/{request_id}"
        assert request_id in notice.title
        assert f"Material {OAR_NORMAL}: reservation 12345678 item 0001" in notice.description
        assert "Lines with a reservation: 1 of 7." in notice.description

        assert run(db, settings).linked == 0
        assert len(notices(db, templates.RESERVATION_LINKED)) == 1

    def test_lines_linked_in_one_pass_are_one_notice(self, db, settings) -> None:
        request_id = make_request(db, settings)
        link(db, request_id, OAR_NORMAL, item="0001")
        link(db, request_id, OAR_ABOVE, item="0002")

        assert run(db, settings).linked == 1
        [notice] = notices(db, templates.RESERVATION_LINKED)
        assert OAR_NORMAL in notice.description and OAR_ABOVE in notice.description

    def test_a_later_link_is_a_second_notice_naming_only_it(self, db, settings) -> None:
        request_id = make_request(db, settings)
        link(db, request_id, OAR_NORMAL, item="0001", seen=NOW - timedelta(hours=2))
        run(db, settings)
        first = notices(db, templates.RESERVATION_LINKED)[0]
        # The row's time comes from the database clock; pinned so the test does not depend on it.
        db.execute(update(NotificationRow).where(NotificationRow.id == first.id).values(created_at=NOW - timedelta(hours=1)))
        db.commit()
        link(db, request_id, OAR_ABOVE, item="0002", seen=NOW - timedelta(minutes=30))

        assert run(db, settings).linked == 1
        second = next(n for n in notices(db, templates.RESERVATION_LINKED) if n.id != first.id)
        assert OAR_ABOVE in second.description and OAR_NORMAL not in second.description
        assert "Lines with a reservation: 2 of 7." in second.description

    def test_a_link_older_than_the_lookback_is_not_announced(self, db, settings) -> None:
        request_id = make_request(db, settings, age=timedelta(days=5))
        link(db, request_id, OAR_NORMAL, seen=NOW - timedelta(hours=49))

        assert run(db, settings).linked == 0


class TestReminder:
    def test_a_day_old_request_with_unlinked_lines_is_reminded_once(self, db, settings) -> None:
        request_id = make_request(db, settings, age=timedelta(hours=25))
        link(db, request_id, OAR_NORMAL, seen=NOW - timedelta(hours=24))

        assert run(db, settings).reminders == 1
        [reminder] = notices(db, templates.RESERVATION_LINK_REMINDER)
        assert reminder.recipient_user_id == REQUESTER
        assert reminder.action_path == f"/assistant/requests/{request_id}"
        # Still waiting: the other lines that will be reserved.
        assert OAR_ABOVE in reminder.description and REPAIR_NEW in reminder.description
        # Linked already, or not expecting a reservation at all.
        for material in (OAR_NORMAL, OAR_NOT_NEEDED, REPAIR_EXISTING):
            assert material not in reminder.description

        assert run(db, settings, NOW + timedelta(hours=5)).reminders == 0
        assert len(notices(db, templates.RESERVATION_LINK_REMINDER)) == 1

    def test_not_before_a_day(self, db, settings) -> None:
        make_request(db, settings, age=timedelta(hours=23))

        assert run(db, settings).reminders == 0

    def test_not_for_requests_older_than_the_lookback(self, db, settings) -> None:
        make_request(db, settings, age=timedelta(hours=24 + 49))

        assert run(db, settings).reminders == 0

    def test_not_when_only_lines_that_expect_no_reservation_are_unlinked(self, db, settings) -> None:
        request_id = make_request(db, settings, age=timedelta(hours=25))
        needing = [
            s.material_id
            for s in request_service.lines_of(db, request_id)
            if s.material_id not in (OAR_NOT_NEEDED, REPAIR_EXISTING)
        ]
        for n, material in enumerate(needing, start=1):
            link(db, request_id, material, item=f"{n:04d}", seen=NOW - timedelta(hours=30))

        assert run(db, settings).reminders == 0

    def test_the_hours_are_a_setting_and_0_is_off(self, db, settings) -> None:
        make_request(db, settings, age=timedelta(hours=25))

        assert run(db, settings.model_copy(update={"assistant_link_reminder_hours": 0})).reminders == 0
        assert run(db, settings.model_copy(update={"assistant_link_reminder_hours": 30})).reminders == 0


def test_switched_off_nothing_is_sent(db, settings) -> None:
    request_id = make_request(db, settings, age=timedelta(hours=25))
    link(db, request_id, OAR_NORMAL)
    off = settings.model_copy(update={"assistant_link_notifications_enabled": False})

    assert run(db, off) == link_notifications.LinkNoticeResult()
    assert db.scalars(select(NotificationRow)).first() is None


def test_a_requester_who_is_not_an_entra_user_is_recorded_as_failed_once(db, settings) -> None:
    request_id = make_request(db, settings, requester="coordinator-1", age=timedelta(hours=25))
    link(db, request_id, OAR_NORMAL)

    assert run(db, settings) == link_notifications.LinkNoticeResult()
    rows = list(db.scalars(select(NotificationRow)))
    assert {r.event_type for r in rows} == {templates.RESERVATION_LINKED, templates.RESERVATION_LINK_REMINDER}
    assert all(r.status == "FAILED" and r.recipient_user_id is None for r in rows)

    run(db, settings, NOW + timedelta(minutes=1))
    assert len(list(db.scalars(select(NotificationRow)))) == len(rows)
