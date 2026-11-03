"""I13: the requester is asked to confirm an exception, and the HOD is told when they have not responded.

* the requester's notification is titled "Confirm Exception", carries the material, plant, reason and exception
  code, asks the question word for word, and links to that material's exceptions;
* a SAP requester name reaches the right inbox only when ``I13_REQUESTER_RECIPIENTS`` says who it is, and never by
  guesswork;
* after the five-day response period the HOD is told the exception is still pending and the requester has not
  responded, once, and not at all once the requester has answered.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.config import Settings
from app.initiatives.i13.act.domain import ExceptionStatus
from app.initiatives.i13.act.service import (
    REQUESTER_QUESTION,
    _route_to_requester,
    exception_link,
    process_escalations,
    submit_confirmation,
)
from app.initiatives.i13.act_hod_provider import ConfigEscalationRecipientProvider
from app.initiatives.i13.act_requester_directory import parse_requester_recipients
from app.initiatives.i13.config import build_i13_config
from app.integrations.notifications.service import SharedNotificationPort
from app.models.app_user import AppUser
from tests.notifications.test_i08_and_i13 import HOD, NOW, OID, act_exception, repository, shared

OTHER_HOD = "33333333-3333-3333-3333-333333333333"
FIVE_DAYS = timedelta(days=5)


def inbox(db):
    return [r for r in shared(db) if r.channel == "IN_APP"]


def failures(db):
    return [r for r in shared(db) if r.status == "FAILED"]


def open_exception(**overrides):
    return act_exception(
        status=ExceptionStatus.OPEN, owner_requester_id=None, current_assignee_type=None, current_assignee_id=None,
        routing_status=None, requester_due_at=None, detected_at=NOW, **overrides,
    )


def route(db, repo, exception, requester, *, aliases=None):
    return _route_to_requester(
        exception, requester, FIVE_DAYS, NOW, repo, SharedNotificationPort(db, identity_aliases=aliases)
    )


def escalate(db, repo, hods=f"1300:{HOD}", at=NOW):
    return process_escalations(
        at,
        repository=repo,
        escalation_recipient_provider=ConfigEscalationRecipientProvider.from_config_string(hods),
        notification_port=SharedNotificationPort(db),
    )


# --- the requester is asked to confirm ---------------------------------------


def test_the_requester_is_asked_to_confirm_with_the_details_and_the_question(db):
    db.add(AppUser(oid=OID, name="Requester", email="requester@example.com"))
    db.commit()
    repo = repository(open_exception())

    routed = route(db, repo, repo.get("ACT-1"), OID)
    db.commit()

    assert routed.status is ExceptionStatus.AWAITING_REQUESTER and routed.requester_due_at == NOW + FIVE_DAYS
    rows = shared(db)
    assert {(r.channel, r.recipient_user_id) for r in rows} == {("IN_APP", OID), ("EMAIL", OID)}
    note = inbox(db)[0]
    assert note.title == "Confirm Exception"
    assert note.description.splitlines() == [
        "Material: 5000092261",
        "Plant: 1300",
        "Exception: No consumption plan for the reservation.",
        "Exception code: NO_PLAN",
        "Why was this material not consumed as planned, in your own words.",
    ]
    assert note.description.splitlines()[-1] == REQUESTER_QUESTION
    assert note.plant == "1300" and note.module == "I13"
    assert "I13" not in note.title + note.description and "ACT" not in note.title + note.description


def test_the_link_opens_that_materials_exceptions_not_the_whole_list(db):
    repo = repository(open_exception())
    route(db, repo, repo.get("ACT-1"), OID)
    db.commit()

    assert inbox(db)[0].action_path == "/oar-utilization/aging-exceptions?material=5000092261&plant=1300"
    assert exception_link(act_exception(material="A/B 1", plant="15 00")) == (
        "/oar-utilization/aging-exceptions?material=A%2FB%201&plant=15%2000"
    )  # nothing in a material or plant can break out of the query


def test_a_sap_requester_name_reaches_the_mapped_user(db):
    db.add(AppUser(oid=OID, name="Darren", email="darren@example.com"))
    db.commit()
    repo = repository(open_exception())

    route(db, repo, repo.get("ACT-1"), "Darren", aliases=parse_requester_recipients("darren:" + OID))
    db.commit()

    assert [(r.recipient_user_id, r.status) for r in inbox(db)] == [(OID, "SENT")]


def test_a_requester_with_no_mapping_is_a_visible_failure_never_a_guess(db):
    repo = repository(open_exception())

    route(db, repo, repo.get("ACT-1"), "Darren")
    db.commit()

    assert inbox(db) == [] or all(r.status == "FAILED" for r in inbox(db))
    assert failures(db) and "invalid identity" in failures(db)[0].error
    # and the exception still moved on: a notification problem never blocks the workflow
    assert repo.get("ACT-1").status is ExceptionStatus.AWAITING_REQUESTER


def test_the_requester_is_not_notified_twice_for_the_same_exception(db):
    repo = repository(open_exception())
    route(db, repo, repo.get("ACT-1"), OID)
    again = repository(open_exception())
    route(db, again, again.get("ACT-1"), OID)
    db.commit()

    assert len(inbox(db)) == 1
    assert len([r for r in shared(db) if r.channel == "EMAIL"]) == 1


def test_the_requester_mapping_is_parsed_without_guessing():
    assert parse_requester_recipients(f"Darren:{OID}, Jaco Zandber : {HOD},bad,:x,y:") == {"darren": OID, "jaco zandber": HOD}
    assert parse_requester_recipients("") == {}
    assert build_i13_config(Settings(i13_requester_recipients=f"Darren:{OID}")).escalation.requester_recipients_raw == f"Darren:{OID}"


# --- the HOD is told after five days -----------------------------------------


def test_the_response_period_is_five_days_by_default():
    assert Settings.model_fields["i13_requester_response_days"].default == 5
    assert build_i13_config(Settings()).escalation.requester_response_days == 5


def test_the_hod_is_told_it_is_still_pending_and_the_requester_has_not_responded(db):
    db.add(AppUser(oid=HOD, name="HOD", email="hod@example.com"))
    db.commit()
    repo = repository(act_exception(owner_requester_id="Darren", detected_at=NOW - FIVE_DAYS, requester_due_at=NOW))

    result = escalate(db, repo)
    db.commit()

    assert result.escalated == 1 and repo.get("ACT-1").status is ExceptionStatus.ESCALATED
    note = inbox(db)[0]
    assert note.recipient_user_id == HOD
    assert note.title == "Escalation Required – Exception Not Confirmed"
    assert note.description.splitlines() == [
        "This exception is still pending: the requester has not confirmed it or given an explanation.",
        "Material: 5000092261",
        "Plant: 1300",
        "Exception: No consumption plan for the reservation.",
        "Exception code: NO_PLAN",
        "Requester: Darren",
        "Detected: 28 Oct 2026 (5 days ago)",
        "Action required: please follow up with the requester.",
    ]
    assert note.action_path == "/oar-utilization/aging-exceptions?material=5000092261&plant=1300"


def test_an_entra_id_is_not_shown_to_the_hod_as_a_requester_name(db):
    repo = repository(act_exception(owner_requester_id=OID))
    escalate(db, repo)
    db.commit()

    assert "Requester:" not in inbox(db)[0].description


def test_nothing_goes_to_the_hod_before_the_five_days_are_up(db):
    repo = repository(act_exception(detected_at=NOW - timedelta(days=4), requester_due_at=NOW + timedelta(days=1)))

    assert escalate(db, repo).escalated == 0
    assert shared(db) == [] and repo.get("ACT-1").status is ExceptionStatus.AWAITING_REQUESTER

    assert escalate(db, repo, at=NOW + timedelta(days=1)).escalated == 1  # on the due moment itself


def test_the_hod_is_not_told_again_on_later_runs(db):
    repo = repository(act_exception())

    assert [escalate(db, repo).escalated for _ in range(3)] == [1, 0, 0]
    db.commit()

    assert len(inbox(db)) == 1
    assert repo.get("ACT-1").status is ExceptionStatus.ESCALATED


def test_a_requester_who_has_responded_is_never_escalated(db):
    repo = repository(act_exception())
    submit_confirmation(
        exception_id="ACT-1", reason_category="OTHER", free_text="Stock was used on another job.",
        actor_id=OID, as_of_time=NOW - timedelta(hours=1), repository=repo,
    )

    result = escalate(db, repo)

    assert (result.escalated, result.routing_pending) == (0, 0)
    assert repo.get("ACT-1").status is ExceptionStatus.CONFIRMED
    assert shared(db) == []


def test_each_plants_exceptions_go_to_that_plants_hod(db):
    repo = repository(act_exception(), act_exception(exception_id="ACT-2", plant="1500", material="5000011234"))

    result = escalate(db, repo, hods=f"1300:{HOD},1500:{OTHER_HOD}")
    db.commit()

    assert result.escalated == 2
    assert {(r.plant, r.recipient_user_id) for r in inbox(db)} == {("1300", HOD), ("1500", OTHER_HOD)}


def test_the_escalation_settings_exist_and_are_off_until_someone_turns_them_on():
    fields = Settings.model_fields
    assert fields["notification_i13_escalation_enabled"].default is False
    assert fields["i13_hod_recipients"].default == "" and fields["i13_requester_recipients"].default == ""
    enabled = Settings(notification_i13_escalation_enabled=True, i13_hod_recipients=f"1300:{HOD},1500:{OTHER_HOD}")
    assert enabled.notification_i13_escalation_enabled is True
    assert ConfigEscalationRecipientProvider.from_config_string(enabled.i13_hod_recipients).plants() == {"1300", "1500"}
