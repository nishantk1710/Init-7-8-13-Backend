"""Reminders and escalation for approvals that wait too long.

Real approval routes create the waiting recommendations; ``run_reminders`` is then
called with a clock moved forward, so no test sleeps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import Roles
from app.initiatives.i7.reminders import SlaTiming, run_reminders, settings_sla
from app.initiatives.i7.recommendations.types import APPROVAL_CHAIN, ApprovalRole
from app.models.notification import NotificationReminderState, NotificationRecipient, NotificationSla
from tests.notifications.test_i07_approvals import SUBMITTER, act, configure, notes, person, recommendation, submit

PLANT_HEAD_ID = "88888888-0000-0000-0000-000000000000"


def add_sla(db, **overrides):
    fields = dict(approval_route="ALL", role=None, first_reminder_minutes=60, repeat_minutes=120, max_reminders=2,
                  escalate_after_minutes=240, escalation_role=Roles.PLANT_HEAD, active=True)
    fields.update(overrides)
    db.add(NotificationSla(**fields))
    db.add(NotificationRecipient(role=Roles.PLANT_HEAD, plant="1300", user_oid=PLANT_HEAD_ID, active=True))
    db.commit()


def run(db, minutes):
    result = run_reminders(db, datetime.now(timezone.utc) + timedelta(minutes=minutes))
    db.commit()
    return result


def waiting(db):
    configure(db)
    rec = recommendation(db)
    return rec


def test_without_an_active_sla_nothing_is_reminded_or_escalated_however_long_it_waits(db, api):
    rec = waiting(db)
    submit(api, rec)

    result = run(db, minutes=60 * 24 * 30)

    assert (result.reminders, result.escalations) == (0, 0)
    assert notes(db, "APPROVAL_REMINDER") == [] and notes(db, "APPROVAL_ESCALATED") == []
    assert len(notes(db, "APPROVAL_REQUIRED")) == 1  # the event-driven notice is unaffected


def test_an_inactive_sla_is_the_same_as_none(db, api):
    add_sla(db, active=False)
    submit(api, waiting(db))
    assert run(db, minutes=10_000).reminders == 0


def test_no_reminder_before_the_first_interval_then_one_to_the_current_approver(db, api):
    add_sla(db)
    submit(api, waiting(db))

    assert run(db, minutes=30).reminders == 0
    assert run(db, minutes=61).reminders == 1

    [reminder] = notes(db, "APPROVAL_REMINDER")
    assert reminder.recipient_user_id == person(ApprovalRole.END_USER)
    assert reminder.title.startswith("Reminder: Approval Required")
    assert reminder.action_path.endswith("?review=1")


def test_the_same_pass_twice_does_not_remind_twice(db, api):
    add_sla(db)
    submit(api, waiting(db))

    assert run(db, minutes=61).reminders == 1
    assert run(db, minutes=61).reminders == 0
    assert run(db, minutes=62).reminders == 0  # next one is a repeat interval after the first
    assert len(notes(db, "APPROVAL_REMINDER")) == 1


def test_repeat_reminders_stop_at_the_maximum(db, api):
    add_sla(db, escalate_after_minutes=None, escalation_role=None)
    submit(api, waiting(db))

    run(db, minutes=61)
    run(db, minutes=61 + 121)
    run(db, minutes=61 + 121 + 121)
    run(db, minutes=10_000)

    assert len(notes(db, "APPROVAL_REMINDER")) == 2


def test_a_scheduler_that_was_down_sends_one_reminder_not_a_burst(db, api):
    add_sla(db, escalate_after_minutes=None, escalation_role=None)
    submit(api, waiting(db))

    run(db, minutes=10_000)
    run(db, minutes=10_000)

    assert len(notes(db, "APPROVAL_REMINDER")) == 1


def test_escalation_goes_to_the_escalation_role_once_after_the_threshold(db, api):
    add_sla(db)
    submit(api, waiting(db))

    assert run(db, minutes=200).escalations == 0
    assert run(db, minutes=241).escalations == 1
    assert run(db, minutes=500).escalations == 0

    escalations = notes(db, "APPROVAL_ESCALATED")
    # every Plant Head at the plant: the one from configure() and the extra one added here
    assert {n.recipient_user_id for n in escalations} == {PLANT_HEAD_ID, person(ApprovalRole.PLANT_HEAD)}
    assert escalations[0].title.startswith("Escalation Required")
    assert "End User" in escalations[0].description  # names who it is waiting on


def test_nothing_is_sent_once_the_recommendation_is_no_longer_waiting(db, api):
    add_sla(db)
    rec = waiting(db)
    submit(api, rec)
    for role in APPROVAL_CHAIN:
        act(api, rec, role, "APPROVE")  # fully approved

    assert run(db, minutes=10_000).reminders == 0

    rejected = recommendation(db, "REC-REJECTED")
    submit(api, rejected)
    act(api, rejected, ApprovalRole.END_USER, "REJECT", comment="No")
    assert run(db, minutes=10_000).reminders == 0

    held = recommendation(db, "REC-HELD")
    submit(api, held)
    act(api, held, ApprovalRole.END_USER, "HOLD", comment="Waiting")
    assert run(db, minutes=10_000).reminders == 0


def test_after_the_stage_changes_the_new_approver_is_reminded_not_the_old_one(db, api):
    add_sla(db, escalate_after_minutes=None, escalation_role=None)
    rec = waiting(db)
    submit(api, rec)
    run(db, minutes=61)  # End User reminded
    act(api, rec, ApprovalRole.END_USER, "APPROVE")  # now with the Engineering Manager

    run(db, minutes=61)

    assert [n.recipient_user_id for n in notes(db, "APPROVAL_REMINDER")] == [
        person(ApprovalRole.END_USER),
        person(ApprovalRole.ENGINEERING_MANAGER),
    ]
    stages = list(db.scalars(select(NotificationReminderState)))
    assert len(stages) == 2 and all(s.reminders_sent == 1 for s in stages)


def test_a_role_specific_sla_overrides_the_route_default(db, api):
    add_sla(db)  # everyone: first reminder after 60 minutes
    db.add(NotificationSla(approval_route="ROP_MAX", role=Roles.END_USER, first_reminder_minutes=10, repeat_minutes=20,
                           max_reminders=1, escalate_after_minutes=None, escalation_role=None, active=True))
    db.commit()
    submit(api, waiting(db))

    assert run(db, minutes=11).reminders == 1


def test_state_is_in_the_database_so_a_restart_does_not_repeat_a_reminder(db, api):
    add_sla(db)
    submit(api, waiting(db))
    run(db, minutes=61)

    with Session(db.get_bind()) as restarted:  # a new process: nothing in memory
        result = run_reminders(restarted, datetime.now(timezone.utc) + timedelta(minutes=62))
        restarted.commit()

    assert result.reminders == 0
    assert len(notes(db, "APPROVAL_REMINDER")) == 1


def test_an_approver_nobody_configured_is_recorded_as_a_failed_reminder(db, api):
    db.add(NotificationSla(approval_route="ALL", role=None, first_reminder_minutes=1, repeat_minutes=5, max_reminders=1,
                           escalate_after_minutes=None, escalation_role=None, active=True))
    db.commit()
    submit(api, recommendation(db))  # no recipients configured

    run(db, minutes=2)

    failed = [n for n in notes(db, "APPROVAL_REMINDER", channel="EMAIL")]
    assert len(failed) == 1 and failed[0].status == "FAILED"


# --- the I7_* day settings: timings without a notification_sla row ---------------------------------------------------

DAY = 24 * 60


def use_settings(monkeypatch, **values):
    for name, value in values.items():
        monkeypatch.setattr(get_settings(), name, value)


def test_the_day_settings_are_off_by_default_and_become_minutes():
    fields = Settings.model_fields
    assert (fields["i7_reminder_first_days"].default, fields["i7_escalate_after_days"].default) == (0, 0)
    assert fields["i7_escalation_role"].default == ""
    assert settings_sla(Settings()) is None

    on = Settings(
        i7_reminder_first_days=1, i7_reminder_repeat_days=2, i7_reminder_max=3,
        i7_escalate_after_days=3, i7_escalation_role=Roles.PLANT_HEAD,
    )
    assert settings_sla(on) == SlaTiming(DAY, 2 * DAY, 3, 3 * DAY, Roles.PLANT_HEAD)


def test_escalation_needs_both_days_and_a_role_so_it_never_half_escalates():
    assert settings_sla(Settings(i7_reminder_first_days=1, i7_escalate_after_days=3)) == SlaTiming(DAY, DAY, 3, None, None)
    assert settings_sla(Settings(i7_escalate_after_days=3)) is None
    assert settings_sla(Settings(i7_escalation_role=Roles.PLANT_HEAD)) is None
    assert settings_sla(Settings(i7_escalate_after_days=3, i7_escalation_role=Roles.PLANT_HEAD)) == SlaTiming(
        DAY, DAY, 0, 3 * DAY, Roles.PLANT_HEAD
    )  # escalation only: no reminders


def test_the_settings_alone_remind_on_the_day_schedule_and_stop_at_the_maximum(db, api, monkeypatch):
    use_settings(monkeypatch, i7_reminder_first_days=1, i7_reminder_repeat_days=1, i7_reminder_max=2)
    submit(api, waiting(db))

    assert run(db, minutes=23 * 60).reminders == 0  # not yet a day
    assert run(db, minutes=25 * 60).reminders == 1
    assert run(db, minutes=25 * 60).reminders == 0  # the same pass again adds nothing
    assert run(db, minutes=50 * 60).reminders == 1  # a day after the first
    assert run(db, minutes=10 * DAY).reminders == 0  # the maximum of 2 is reached
    assert len(notes(db, "APPROVAL_REMINDER")) == 2


def test_the_settings_alone_escalate_once_after_the_configured_days(db, api, monkeypatch):
    use_settings(monkeypatch, i7_escalate_after_days=3, i7_escalation_role=Roles.PLANT_HEAD)
    submit(api, waiting(db))

    assert run(db, minutes=2 * DAY).escalations == 0
    assert run(db, minutes=3 * DAY + 5).escalations == 1
    assert run(db, minutes=10 * DAY).escalations == 0

    assert {n.recipient_user_id for n in notes(db, "APPROVAL_ESCALATED")} == {person(ApprovalRole.PLANT_HEAD)}
    assert notes(db, "APPROVAL_REMINDER") == []  # escalation only: no reminders were asked for


def test_a_notification_sla_row_wins_over_the_settings(db, api, monkeypatch):
    use_settings(monkeypatch, i7_reminder_first_days=5, i7_reminder_max=3)
    add_sla(db, escalate_after_minutes=None, escalation_role=None)  # first reminder after 60 minutes
    submit(api, waiting(db))

    assert run(db, minutes=61).reminders == 1  # the row's hour, not the setting's five days


def test_with_the_settings_off_and_no_row_nothing_is_reminded(db, api, monkeypatch):
    use_settings(monkeypatch, i7_reminder_first_days=0, i7_escalate_after_days=0, i7_escalation_role="")
    submit(api, waiting(db))

    result = run(db, minutes=60 * DAY)

    assert (result.reminders, result.escalations) == (0, 0)
