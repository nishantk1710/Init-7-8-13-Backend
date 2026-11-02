"""M6: a notification can never break the approval it belongs to.

Two guarantees, both about the outer transaction:

* a failed notification (even the database refusing the insert) rolls back only itself, and
  the approval still commits;
* a failed approval leaves no notification behind, because they are the same transaction.

The "database refuses the insert" case is real: a trigger on the table raises, so the
database error comes from the insert itself, not from a Python stand-in.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from app.initiatives.i7.notifications import notify_transition
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import ledger
from app.initiatives.i7.recommendations.types import ApprovalRole
from app.integrations.notifications import service
from app.integrations.notifications.models import Channel, NotificationEvent, Recipient
from app.models.app_user import AppUser
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation
from app.models.notification import NotificationRow
from tests.notifications.test_i07_approvals import BASE, SUBMITTER, act, configure, notes, recommendation, submit

OID = "11111111-1111-1111-1111-111111111111"


def block_notification_inserts(db):
    """The database itself refuses every insert into ``notification``."""
    db.execute(text("CREATE TRIGGER refuse_notification BEFORE INSERT ON notification BEGIN SELECT RAISE(ABORT, 'refused'); END"))
    db.commit()


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


# --- the notification fails, the approval still commits ----------------------


def test_a_database_level_notification_failure_does_not_stop_the_approval_committing(db, api):
    configure(db)
    rec = recommendation(db)
    block_notification_inserts(db)

    response = api.post(f"{BASE}/{rec}/submit", json={}, headers={"X-Actor-Id": SUBMITTER})

    assert response.status_code == 200, response.text
    db.expire_all()  # read what was actually committed, not what the session remembers
    assert db.scalar(select(Recommendation.status)) == "PENDING_APPROVAL"
    assert count(db, ApprovalLedgerEntry) == 1
    assert count(db, NotificationRow) == 0


def test_the_session_is_still_usable_for_the_next_approval_step_after_a_failure(db, api):
    configure(db)
    rec = recommendation(db)
    block_notification_inserts(db)
    submit(api, rec)

    # the next step, with notifications still refused, also goes through
    assert act(api, rec, ApprovalRole.END_USER, "APPROVE").status_code == 200
    db.expire_all()
    assert count(db, ApprovalLedgerEntry) == 2
    assert db.scalar(select(Recommendation.chain_index)) == 1


def test_a_notification_failure_does_not_discard_other_work_pending_in_the_same_transaction(db):
    """The savepoint rolls back the notification only; what the caller already wrote survives."""
    db.add(AppUser(oid=OID, name="Pending work", email="p@example.com"))  # not yet committed
    block_notification_inserts_without_commit(db)

    with pytest.raises(Exception):
        service.enqueue(
            db,
            NotificationEvent(module="I07", event_type="APPROVAL_REQUIRED", entity_type="RECOMMENDATION", entity_id="R1", trigger_id="1"),
            [Recipient(user_oid=OID, email="p@example.com")],
        )
    db.commit()

    assert db.scalar(select(func.count()).select_from(AppUser)) == 1  # the caller's own write was kept
    assert count(db, NotificationRow) == 0


def block_notification_inserts_without_commit(db):
    db.execute(text("CREATE TRIGGER refuse_notification BEFORE INSERT ON notification BEGIN SELECT RAISE(ABORT, 'refused'); END"))


def test_the_failure_is_logged_with_context_but_not_the_values(db, api, caplog):
    configure(db)
    rec = recommendation(db)
    block_notification_inserts(db)

    with caplog.at_level("WARNING"):
        submit(api, rec)

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "I07 notification not queued" in messages and rec in messages and "IntegrityError" in messages
    assert "@example.com" not in messages and SUBMITTER not in messages  # no addresses or ids from the rows


# --- the approval fails, no notification is left behind ----------------------


def test_a_failed_approval_leaves_no_notification_behind(db):
    configure(db)
    rec = recommendation(db)
    row = db.scalar(select(Recommendation))

    ledger.submit_for_approval(db, row, actor_id=SUBMITTER, policy=PolicyDocument())
    notify_transition(db, row, PolicyDocument())
    assert count(db, NotificationRow) > 0  # queued in the approval's own transaction...

    db.rollback()  # ...and the approval's commit never happens

    assert count(db, NotificationRow) == 0
    assert count(db, ApprovalLedgerEntry) == 0
    assert db.scalar(select(Recommendation.status)) == "READY_FOR_REVIEW"


def test_an_invalid_step_sends_nothing_and_leaves_nothing(db, api):
    configure(db)
    rec = recommendation(db)
    submit(api, rec)
    before = count(db, NotificationRow)

    assert act(api, rec, ApprovalRole.WAREHOUSE_SUPERVISOR, "APPROVE").status_code == 409  # not their turn

    assert count(db, NotificationRow) == before


def test_a_ledger_failure_still_fails_the_approval_it_is_not_hidden_by_the_notification_guard(db):
    """The approval's own write is flushed outside the guard, so its failure surfaces."""
    configure(db)
    recommendation(db)
    db.execute(text("CREATE TRIGGER refuse_ledger BEFORE INSERT ON i7_approval_ledger BEGIN SELECT RAISE(ABORT, 'refused'); END"))
    db.commit()
    row = db.scalar(select(Recommendation))
    ledger.submit_for_approval(db, row, actor_id=SUBMITTER, policy=PolicyDocument())

    with pytest.raises(Exception, match="refused"):
        notify_transition(db, row, PolicyDocument())


# --- values are checked before they reach the database -----------------------


def event(**overrides):
    fields = dict(module="I07", event_type="APPROVAL_REQUIRED", entity_type="RECOMMENDATION", entity_id="R1", trigger_id="1")
    fields.update(overrides)
    return NotificationEvent(**fields)


def test_a_long_email_address_cannot_overflow_the_idempotency_key(db):
    long_email = "a" * 300 + "@example.com"  # 312 characters: valid for its own column, too long for a readable key
    recipient = Recipient(email=long_email)

    first = service.enqueue(db, event(), [recipient], channels=[Channel.EMAIL])
    db.commit()
    again = service.enqueue(db, event(), [recipient], channels=[Channel.EMAIL])
    db.commit()

    [row] = db.scalars(select(NotificationRow)).all()
    assert len(row.idempotency_key) <= 255 and row.idempotency_key.startswith("h:")
    assert row.recipient_email == long_email  # stored whole, not cut
    assert len(first) == 1 and again == []  # and still one key per message: no duplicate


def test_the_hashed_key_differs_per_recipient_and_per_trigger(db):
    a, b = "a" * 300 + "@example.com", "b" * 300 + "@example.com"
    service.enqueue(db, event(), [Recipient(email=a), Recipient(email=b)], channels=[Channel.EMAIL])
    service.enqueue(db, event(trigger_id="2"), [Recipient(email=a)], channels=[Channel.EMAIL])
    db.commit()
    assert count(db, NotificationRow) == 3


def test_an_email_address_too_long_for_its_column_is_recorded_as_a_failure_not_cut_short(db):
    too_long = "a" * 330 + "@example.com"
    service.enqueue(db, event(), [Recipient(email=too_long)], channels=[Channel.EMAIL])
    db.commit()
    [row] = db.scalars(select(NotificationRow)).all()
    assert row.status == "FAILED" and "characters" in row.error
    assert row.recipient_email is None


@pytest.mark.parametrize("bad", ["x" * 41, "end-user", "victim@example.com"])
def test_a_user_id_that_is_not_an_object_id_is_never_stored_or_truncated(db, bad):
    service.enqueue(db, event(), [Recipient(user_oid=bad, email="a@example.com")])
    db.commit()
    [row] = db.scalars(select(NotificationRow)).all()
    assert row.status == "FAILED" and row.recipient_user_id is None and row.recipient_email is None
    assert bad not in (row.error or "")


@pytest.mark.parametrize(
    "field, value",
    [("entity_id", "E" * 129), ("action_path", "/" + "p" * 500), ("plant", "123456789"), ("module", "M" * 17), ("event_type", "T" * 49)],
)
def test_an_event_value_that_does_not_fit_is_refused_before_anything_is_written(db, field, value):
    with pytest.raises(service.NotificationValueError, match=field):
        service.enqueue(db, event(**{field: value}), [Recipient(user_oid=OID, email="a@example.com")])
    assert count(db, NotificationRow) == 0
