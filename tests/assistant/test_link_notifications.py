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
    assert {r.event_type for r in rows} == {templates.RESERVATION_LINK_REQUIRED, templates.RESERVATION_LINKED, templates.RESERVATION_LINK_REMINDER}
    assert all(r.status == "FAILED" and r.recipient_user_id is None for r in rows)

    run(db, settings, NOW + timedelta(minutes=1))
    assert len(list(db.scalars(select(NotificationRow)))) == len(rows)


NAMED = "99999999-8888-7777-6666-555555555555"


class TestRaiserAndPersonNamedInTheRequesterBox:
    """Khushi raises a request and types "J Miller" in the Requester box. J Miller is in I13_REQUESTER_RECIPIENTS."""

    @pytest.fixture
    def listed(self, settings):
        return settings.model_copy(update={"i13_requester_recipients": f"j miller:{NAMED}"})

    @pytest.fixture(autouse=True)
    def khushi(self, db):
        db.add(AppUser(oid=REQUESTER, name="Khushi Patil", email="khushi@example.com"))
        db.commit()

    def to(self, db, event_type: str) -> dict[str, NotificationRow]:
        return {n.recipient_user_id: n for n in notices(db, event_type)}

    def test_when_raised_the_raiser_is_told_to_link_and_the_named_person_who_raised_it_for_them(self, db, listed) -> None:
        request_id = make_request(db, listed)

        assert run(db, listed).raised == 2

        [action] = notices(db, templates.RESERVATION_LINK_REQUIRED)
        assert action.recipient_user_id == REQUESTER
        assert action.title == f"Action Required: Link the Reservation – Request {request_id}"
        assert f"{request_id} in its header text" in action.description and "Link reservation" in action.description
        assert "for J Miller" in action.description
        assert OAR_NORMAL in action.description and REPAIR_NEW in action.description
        for material in (OAR_NOT_NEEDED, REPAIR_EXISTING):  # lines that expect no reservation are never mentioned
            assert material not in action.description

        [waiting] = notices(db, templates.RESERVATION_REQUESTED)
        assert waiting.recipient_user_id == NAMED
        assert waiting.title == f"Reservation Requested for You – Request {request_id}"
        assert f"Khushi Patil raised request {request_id}" in waiting.description
        assert "waiting for its SAP reservation to be linked" in waiting.description
        assert OAR_NORMAL in waiting.description
        assert waiting.action_path == f"/assistant/requests/{request_id}"

        assert run(db, listed, NOW + timedelta(minutes=1)).raised == 0
        assert len(notices(db, templates.RESERVATION_LINK_REQUIRED)) == len(notices(db, templates.RESERVATION_REQUESTED)) == 1

    def test_when_a_line_is_linked_both_are_told(self, db, listed) -> None:
        request_id = make_request(db, listed)
        link(db, request_id, OAR_NORMAL)

        run(db, listed)

        both = self.to(db, templates.RESERVATION_LINKED)
        assert set(both) == {REQUESTER, NAMED}
        assert "Lines with a reservation: 1 of 7." in both[NAMED].description
        assert run(db, listed, NOW + timedelta(minutes=1)).linked == 0

    def test_a_day_later_with_lines_still_unlinked_both_are_reminded_in_their_own_words(self, db, listed) -> None:
        make_request(db, listed, age=timedelta(hours=25))

        run(db, listed)

        [to_raiser] = notices(db, templates.RESERVATION_LINK_REMINDER)
        assert to_raiser.recipient_user_id == REQUESTER
        assert "Action required: create the reservation in SAP" in to_raiser.description
        [to_named] = notices(db, templates.RESERVATION_LINK_PENDING)
        assert to_named.recipient_user_id == NAMED
        assert "raised for you by Khushi Patil" in to_named.description and "Action required" not in to_named.description

        run(db, listed, NOW + timedelta(hours=5))
        assert len(notices(db, templates.RESERVATION_LINK_REMINDER)) == len(notices(db, templates.RESERVATION_LINK_PENDING)) == 1

    def test_the_whole_story_in_order(self, db, listed) -> None:
        request_id = make_request(db, listed, age=timedelta(minutes=2))
        run(db, listed)  # raised
        for material, item in ((OAR_NORMAL, "0001"), (OAR_ABOVE, "0002")):
            link(db, request_id, material, item=item, seen=NOW - timedelta(minutes=1))
        run(db, listed, NOW + timedelta(minutes=1))  # linked

        told = {(n.recipient_user_id, n.event_type) for n in db.scalars(select(NotificationRow))}
        assert told == {
            (REQUESTER, templates.RESERVATION_LINK_REQUIRED),
            (NAMED, templates.RESERVATION_REQUESTED),
            (REQUESTER, templates.RESERVATION_LINKED),
            (NAMED, templates.RESERVATION_LINKED),
        }

    def test_someone_who_is_both_the_raiser_and_the_named_person_is_only_the_raiser(self, db, settings) -> None:
        same = settings.model_copy(update={"i13_requester_recipients": f"j miller:{REQUESTER}"})
        request_id = make_request(db, same, age=timedelta(hours=25))
        link(db, request_id, OAR_NORMAL)

        run(db, same)

        assert [n.recipient_user_id for n in notices(db, templates.RESERVATION_LINK_REQUIRED)] == [REQUESTER]
        assert notices(db, templates.RESERVATION_REQUESTED) == []
        assert notices(db, templates.RESERVATION_LINK_PENDING) == []
        assert [n.recipient_user_id for n in notices(db, templates.RESERVATION_LINKED)] == [REQUESTER]

    def test_a_name_that_is_not_listed_is_never_guessed_but_the_raiser_is_still_told(self, db, settings) -> None:
        other = settings.model_copy(update={"i13_requester_recipients": f"someone else:{NAMED}"})
        request_id = make_request(db, other, age=timedelta(hours=25))
        link(db, request_id, OAR_NORMAL)

        run(db, other)

        assert notices(db, templates.RESERVATION_REQUESTED) == []
        assert notices(db, templates.RESERVATION_LINK_PENDING) == []
        for event in (templates.RESERVATION_LINK_REQUIRED, templates.RESERVATION_LINKED, templates.RESERVATION_LINK_REMINDER):
            assert {n.recipient_user_id for n in notices(db, event)} == {REQUESTER}

    def test_with_no_list_the_raiser_is_still_told_to_link(self, db, settings) -> None:
        make_request(db, settings)

        assert run(db, settings).raised == 1
        assert {n.recipient_user_id for n in notices(db, templates.RESERVATION_LINK_REQUIRED)} == {REQUESTER}
        assert notices(db, templates.RESERVATION_REQUESTED) == []

    def test_the_name_matches_ignoring_case_and_spaces(self, db, settings) -> None:
        spaced = settings.model_copy(update={"i13_requester_recipients": f"  J MILLER :{NAMED}"})
        make_request(db, spaced)

        run(db, spaced)

        assert len(notices(db, templates.RESERVATION_REQUESTED)) == 1

    def test_a_name_listed_later_is_still_told_though_the_raiser_already_heard(self, db, settings, listed) -> None:
        make_request(db, settings)
        run(db, settings)  # nobody listed yet: only the raiser

        assert run(db, listed, NOW + timedelta(minutes=1)).raised == 1
        assert {n.recipient_user_id for n in notices(db, templates.RESERVATION_REQUESTED)} == {NAMED}
        assert len(notices(db, templates.RESERVATION_LINK_REQUIRED)) == 1  # the raiser is not told twice

    def test_the_raiser_is_named_as_recorded_when_not_an_id(self, db, listed) -> None:
        make_request(db, listed, requester="coordinator-1")

        run(db, listed)

        [waiting] = notices(db, templates.RESERVATION_REQUESTED)
        assert "coordinator-1 raised request" in waiting.description
        # The raiser cannot be notified (not an Entra id): recorded as failed, visibly, and the named person still hears.
        assert any(n.status == "FAILED" for n in notices(db, templates.RESERVATION_LINK_REQUIRED))

    def test_nothing_is_said_when_no_line_needs_a_reservation(self, db, listed) -> None:
        request_id = make_request(db, listed)
        needing = [
            s.material_id for s in request_service.lines_of(db, request_id)
            if s.material_id not in (OAR_NOT_NEEDED, REPAIR_EXISTING)
        ]
        for n, material in enumerate(needing, start=1):
            link(db, request_id, material, item=f"{n:04d}", seen=NOW - timedelta(hours=1))

        assert run(db, listed).raised == 0

    def test_an_old_request_is_not_announced_when_this_is_switched_on(self, db, listed) -> None:
        make_request(db, listed, age=timedelta(days=5))

        assert run(db, listed).raised == 0

    def test_a_listing_that_is_not_an_entra_id_fails_visibly_and_the_raiser_is_still_told(self, db, settings) -> None:
        bad = settings.model_copy(update={"i13_requester_recipients": "j miller:not-an-id"})
        request_id = make_request(db, bad)
        link(db, request_id, OAR_NORMAL)

        run(db, bad)

        rows = notices(db, templates.RESERVATION_LINKED)
        assert {r.recipient_user_id for r in rows if r.status == "SENT"} == {REQUESTER}
        assert any(r.status == "FAILED" and r.recipient_user_id is None for r in rows)

    def test_the_new_wording_has_the_button(self) -> None:
        context = {"request": "R1", "plant": "1300", "lines": "Line 1", "requested_for": "J Miller", "raised_by": "Khushi"}
        for event in (
            templates.RESERVATION_LINK_REQUIRED, templates.RESERVATION_REQUESTED, templates.RESERVATION_LINK_PENDING
        ):
            assert event in templates.known_event_types()
            assert templates.render(event, context).button_label == "Open Request"
