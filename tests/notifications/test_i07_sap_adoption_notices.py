"""After the final approval: the Inventory Controller is told to change SAP, then reminded, then told the outcome.

The real approval routes make the final approval; ``run_adoption_notices`` is then called with the clock moved forward
and SAP's change history replaced by a fake, so no test sleeps and none reads real SAP data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.initiatives.i7.adapters.change_documents import ChangeDocumentRow
from app.initiatives.i7.adoption_notices import deadline_of, reminder_days, run_adoption_notices, values_text
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import sap_change_documents
from app.initiatives.i7.recommendations.types import APPROVAL_CHAIN, OAR_APPROVAL_CHAIN, ApprovalRole
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation
from app.models.notification import NotificationRow
from tests.notifications.test_i07_approvals import act, configure, notes, person, recommendation, submit

WINDOW = PolicyDocument().adoption.monitoring_window_days
SETTINGS = Settings(_env_file=None)
KINDS = ("APPROVAL_SAP_UPDATE_REQUIRED", "APPROVAL_SAP_UPDATE_REMINDER", "APPROVAL_SAP_UPDATE_FINAL_REMINDER", "APPROVAL_SAP_UPDATE_RESULT")


@pytest.fixture
def sap(monkeypatch):
    """SAP's change history for the test material, as a list the test edits."""
    rows: list[ChangeDocumentRow] = []
    reach: dict = {"through": None}

    def fake(session, padded_material, tracked_fields, window_start=None, window_end=None):
        return [r for r in rows if (window_start is None or r.date >= window_start) and (window_end is None or r.date <= window_end)]

    monkeypatch.setattr(sap_change_documents, "material_marc_changes", fake)
    monkeypatch.setattr(sap_change_documents, "material_history_through", lambda session: reach["through"])
    fake.rows, fake.reach = rows, reach
    return fake


def set_values(db, rec_id, *, rop="40", current_rop="25", safety="10", current_safety="5"):
    row = db.scalars(select(Recommendation).where(Recommendation.recommendation_id == rec_id)).one()
    row.recommended_rop, row.current_rop = Decimal(rop), Decimal(current_rop)
    row.recommended_safety_stock, row.current_safety_stock = Decimal(safety), Decimal(current_safety)
    db.commit()


def final_approval(db, api, *, is_oar=False, rec_id=None):
    """Drive a recommendation through its whole route; returns its id and the day of the final approval."""
    configure(db)
    chain = OAR_APPROVAL_CHAIN if is_oar else APPROVAL_CHAIN
    rec = recommendation(db, rec_id or ("REC-OAR" if is_oar else "REC-1"), is_oar=is_oar)
    set_values(db, rec)
    submit(api, rec)
    for role in chain:
        assert act(api, rec, role, "APPROVE").status_code == 200
    stamp = db.scalars(
        select(ApprovalLedgerEntry.timestamp).where(ApprovalLedgerEntry.new_status == "SAP_EXECUTION_PENDING")
    ).one()
    return rec, (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc))


def run(db, handed_off, day):
    result = run_adoption_notices(db, handed_off + timedelta(days=day), SETTINGS)
    db.commit()
    return result


def sent(db):
    pairs = [(n.event_type, n.idempotency_key.split("|")[2].split("-", 2)[2]) for n in notes(db) if n.event_type in KINDS]
    return sorted(pairs, key=lambda pair: pair[1])  # the step, in day order


# --- the schedule follows the window --------------------------------------------------


def test_the_default_window_is_seven_days_so_the_days_are_0_3_5_6():
    assert WINDOW == 7
    assert reminder_days(WINDOW, 3, 2) == [0, 3, 5, 6]


@pytest.mark.parametrize(
    "window, expected",
    [(7, [0, 3, 5, 6]), (10, [0, 3, 5, 7, 9]), (4, [0, 3]), (3, [0, 2]), (2, [0, 1]), (1, [0])],
)
def test_the_reminder_days_come_from_the_window_not_from_a_fixed_seven(window, expected):
    assert reminder_days(window, 3, 2) == expected
    assert reminder_days(window, 3, 2)[-1] <= max(window - 1, 0)  # never on or after the closing day


def test_the_deadline_is_the_day_the_adoption_window_closes():
    from datetime import date

    assert deadline_of(date(2026, 10, 20), 7) == date(2026, 10, 27)


# --- the first notice, at the final approval ------------------------------------------


def test_a_stock_item_final_approval_by_the_warehouse_supervisor_tells_the_inventory_controller(db, api):
    rec, _ = final_approval(db, api)

    [notice] = notes(db, "APPROVAL_SAP_UPDATE_REQUIRED")
    assert notice.recipient_user_id == person(ApprovalRole.INVENTORY_CONTROLLER)
    assert notice.title == "Approved – Update SAP for Material 5000092261"
    assert notice.plant == "1300"
    assert "Final approver: Warehouse Supervisor" in notice.description
    assert "Reorder point: SAP now 25 → recommended 40" in notice.description
    assert "Safety stock: SAP now 5 → recommended 10" in notice.description
    assert "Deadline:" in notice.description
    assert notice.action_path == f"/inventory-planning/recommendations/{rec}"
    [email] = notes(db, "APPROVAL_SAP_UPDATE_REQUIRED", channel="EMAIL")
    assert email.recipient_email


def test_an_oar_final_approval_by_the_commercial_head_also_tells_the_inventory_controller(db, api):
    final_approval(db, api, is_oar=True)

    [notice] = notes(db, "APPROVAL_SAP_UPDATE_REQUIRED")
    # The Inventory Controller approved first on this route, and is still told at the end.
    assert notice.recipient_user_id == person(ApprovalRole.INVENTORY_CONTROLLER)
    assert "Final approver: Commercial Head" in notice.description


def test_the_notice_goes_to_the_inventory_controller_of_that_plant_only(db, api):
    configure(db)
    from app.models.notification import NotificationRecipient
    from app.initiatives.i7.notifications import APPROVAL_ROLE_TO_ENTRA

    other = "55555555-0000-0000-0000-000000000000"
    db.add(NotificationRecipient(role=APPROVAL_ROLE_TO_ENTRA[ApprovalRole.INVENTORY_CONTROLLER], plant="1500", user_oid=other, active=True))
    db.commit()
    final_approval(db, api)

    assert {n.recipient_user_id for n in notes(db, "APPROVAL_SAP_UPDATE_REQUIRED")} == {person(ApprovalRole.INVENTORY_CONTROLLER)}


def test_nothing_to_change_in_sap_means_no_notice(db, api):
    configure(db)
    rec = recommendation(db)  # no recommended values at all
    submit(api, rec)
    for role in APPROVAL_CHAIN:
        act(api, rec, role, "APPROVE")

    assert notes(db, "APPROVAL_SAP_UPDATE_REQUIRED") == []
    assert len(notes(db, "APPROVAL_COMPLETED")) == 1  # the submitter's own notice is unaffected


def test_it_can_be_switched_off(db, api, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "i7_adoption_notices_enabled", False)
    final_approval(db, api)

    assert notes(db, "APPROVAL_SAP_UPDATE_REQUIRED") == []


def test_values_text_names_the_oar_conversion_and_skips_values_the_recommendation_does_not_set():
    row = Recommendation(is_oar=True, conversion_eligibility="ELIGIBLE", recommended_max_stock=Decimal("12.500"), current_max_stock=None)

    assert values_text(row).splitlines() == [
        "MRP type: change to VB (OAR conversion)",
        "Maximum stock: SAP now not set → recommended 12.5",
    ]


# --- the reminders --------------------------------------------------------------------


def test_reminders_go_out_on_days_3_5_and_6_and_only_then(db, api, sap):
    _, handed_off = final_approval(db, api)

    results = {day: run(db, handed_off, day) for day in range(0, 8)}

    assert {day: r.reminders for day, r in results.items()} == {0: 0, 1: 0, 2: 0, 3: 1, 4: 0, 5: 1, 6: 1, 7: 0}
    assert [key for _, key in sent(db)] == ["d0", "d3", "d5", "d6"]
    assert [kind for kind, _ in sent(db)] == [
        "APPROVAL_SAP_UPDATE_REQUIRED",
        "APPROVAL_SAP_UPDATE_REMINDER",
        "APPROVAL_SAP_UPDATE_REMINDER",
        "APPROVAL_SAP_UPDATE_FINAL_REMINDER",
    ]


def test_every_reminder_carries_the_material_plant_role_values_and_deadline(db, api, sap):
    _, handed_off = final_approval(db, api)
    run(db, handed_off, 3)

    [reminder] = notes(db, "APPROVAL_SAP_UPDATE_REMINDER")
    text = f"{reminder.title}\n{reminder.description}"
    deadline = deadline_of(handed_off.date(), WINDOW).strftime("%d %b %Y")
    assert reminder.recipient_user_id == person(ApprovalRole.INVENTORY_CONTROLLER)
    for expected in ("5000092261", "Final approver: Warehouse Supervisor", "SAP now 25 → recommended 40", f"Deadline: {deadline}", "4 day(s) left", "Not adopted"):
        assert expected in text
    assert reminder.plant == "1300"


def test_the_final_reminder_names_the_deadline_and_warns_about_not_adopted(db, api, sap):
    _, handed_off = final_approval(db, api)
    run(db, handed_off, WINDOW - 1)

    [final] = notes(db, "APPROVAL_SAP_UPDATE_FINAL_REMINDER")
    assert final.title.startswith("Final Reminder: SAP Update Due ")
    assert "1 day(s) left" in final.description and "marked Not adopted" in final.description
    assert "not yet confirmed" in final.description


def test_the_reminders_never_say_sap_was_not_updated(db, api, sap):
    _, handed_off = final_approval(db, api)
    for day in (3, 5, 6):
        run(db, handed_off, day)

    for n in notes(db):
        if n.event_type.startswith("APPROVAL_SAP_UPDATE"):
            assert "has not been updated" not in n.description and "was not updated" not in n.description


def test_running_a_day_again_or_a_day_late_adds_nothing_and_sends_one_catch_up(db, api, sap):
    _, handed_off = final_approval(db, api)

    assert run(db, handed_off, 3).reminders == 1
    assert run(db, handed_off, 3).reminders == 0  # a repeat pass, a second worker
    assert run(db, handed_off, 4).reminders == 0  # nothing new is due
    # The scheduler was down on day 5 and is back on day 6: the day-6 reminder goes, the missed day-5 one does not.
    assert run(db, handed_off, 6).reminders == 1
    assert [key for _, key in sent(db)] == ["d0", "d3", "d6"]


def test_reminders_stop_the_day_sap_matches_and_the_inventory_controller_is_told_once(db, api, sap):
    _, handed_off = final_approval(db, api)
    run(db, handed_off, 3)
    changed_on = (handed_off + timedelta(days=4)).date().isoformat()
    sap.rows += [
        ChangeDocumentRow(
            change_doc_object="MATERIAL", object_value="000000000000000" + "5000092261"[-3:], document_number=f"D{field}",
            date=changed_on, time="103200", table_name="MARC", table_key="50000922611300", field_name=field,
            change_indicator="U", new_value=value, old_value="0",
        )
        for field, value in (("EISBE", "10"), ("MINBE", "40"))
    ]

    day5 = run(db, handed_off, 5)

    assert day5.reminders == 0 and day5.outcomes == 1
    [outcome] = notes(db, "APPROVAL_SAP_UPDATE_RESULT")
    assert outcome.title == "SAP Update Adopted – Material 5000092261"
    assert outcome.recipient_user_id == person(ApprovalRole.INVENTORY_CONTROLLER)
    assert run(db, handed_off, 6).reminders == 0 and run(db, handed_off, 6).outcomes == 0
    assert len(notes(db, "APPROVAL_SAP_UPDATE_FINAL_REMINDER")) == 0


# --- when the window closes -----------------------------------------------------------


def test_an_incomplete_sap_history_never_becomes_not_adopted_and_sends_no_outcome(db, api, sap):
    _, handed_off = final_approval(db, api)
    sap.reach["through"] = None  # SAP's change history does not reach the window

    closed = run(db, handed_off, WINDOW + 1)

    assert closed.outcomes == 0 and closed.reminders == 0
    assert notes(db, "APPROVAL_SAP_UPDATE_RESULT") == []


def test_a_complete_history_with_no_matching_change_after_the_window_is_not_adopted(db, api, sap):
    _, handed_off = final_approval(db, api)
    sap.reach["through"] = (handed_off + timedelta(days=WINDOW + 2)).date().isoformat()  # the history covers the whole window

    closed = run(db, handed_off, WINDOW + 1)

    assert closed.outcomes == 1
    [outcome] = notes(db, "APPROVAL_SAP_UPDATE_RESULT")
    assert outcome.title == "SAP Update Not adopted – Material 5000092261"
    assert run(db, handed_off, WINDOW + 2).outcomes == 0


def test_old_handovers_from_before_this_existed_are_not_announced(db, api, sap):
    _, handed_off = final_approval(db, api)
    sap.reach["through"] = (handed_off + timedelta(days=100)).date().isoformat()

    assert run(db, handed_off, WINDOW * 3).outcomes == 0


def test_the_notifications_are_in_the_notification_table_for_the_inbox(db, api, sap):
    _, handed_off = final_approval(db, api)
    run(db, handed_off, 3)

    rows = list(db.scalars(select(NotificationRow).where(NotificationRow.event_type.like("APPROVAL_SAP_UPDATE%"))))
    assert {r.channel for r in rows} == {"IN_APP", "EMAIL"}


# --- the first deployment, restarts and second workers --------------------------------


def approved_before_this_existed(db, api, monkeypatch):
    """A recommendation whose final approval happened while the notices were switched off, as before the deployment."""
    from app.core.config import get_settings

    with monkeypatch.context() as before:
        before.setattr(get_settings(), "i7_adoption_notices_enabled", False)
        return final_approval(db, api)


def test_a_recommendation_already_in_its_window_gets_one_message_for_where_it_is_not_a_burst(db, api, sap, monkeypatch):
    _, handed_off = approved_before_this_existed(db, api, monkeypatch)
    assert notes(db, "APPROVAL_SAP_UPDATE_REQUIRED") == []

    first = run(db, handed_off, 4)  # deployed on day 4 of its window

    assert first.reminders == 1
    assert sent(db) == [("APPROVAL_SAP_UPDATE_REMINDER", "d3")]  # the latest step due, none of the earlier ones
    [reminder] = notes(db, "APPROVAL_SAP_UPDATE_REMINDER")
    assert "3 day(s) left" in reminder.description  # and it is honest about how much time is left


def test_restarts_and_second_workers_cannot_repeat_anything_over_the_whole_window(db, api, sap, monkeypatch):
    from sqlalchemy.orm import Session

    _, handed_off = approved_before_this_existed(db, api, monkeypatch)

    for day in range(0, WINDOW + 1):
        run(db, handed_off, day)
        # A restart, a second instance, the same tick twice: each is a fresh session on the same database.
        with Session(db.get_bind()) as other:
            run_adoption_notices(other, handed_off + timedelta(days=day), SETTINGS)
            other.commit()
        run(db, handed_off, day)

    steps = [step for _, step in sent(db)]
    assert steps == ["d0", "d3", "d5", "d6"] and len(steps) == len(set(steps))
    keys = [n.idempotency_key for n in notes(db) if n.event_type in KINDS]
    assert len(keys) == len(set(keys))  # one IN_APP message per step; the key is also unique in the database


def test_the_approval_time_notice_and_the_timer_never_both_send_day_zero(db, api, sap):
    _, handed_off = final_approval(db, api)  # the approval itself queued the first notice

    assert run(db, handed_off, 0).reminders == 0
    assert len(notes(db, "APPROVAL_SAP_UPDATE_REQUIRED")) == 1
    assert len(notes(db, "APPROVAL_SAP_UPDATE_REQUIRED", channel="EMAIL")) == 1


def test_a_second_handover_of_the_same_recommendation_is_a_new_cycle_not_silenced_by_the_first(db, api, sap):
    rec, handed_off = final_approval(db, api)
    run(db, handed_off, 3)
    # Approved again later (regenerated, re-submitted): a new handover date for the same recommendation id.
    db.add(ApprovalLedgerEntry(
        recommendation_id=rec, recommendation_version=1, actor_id=person(ApprovalRole.WAREHOUSE_SUPERVISOR),
        actor_role="Warehouse Supervisor", action="APPROVE", previous_status="PENDING_APPROVAL",
        new_status="SAP_EXECUTION_PENDING", timestamp=handed_off + timedelta(days=30),
    ))
    db.commit()

    run(db, handed_off + timedelta(days=30), 3)

    assert [step for _, step in sent(db)].count("d3") == 2
