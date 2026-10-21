"""FR-11 -- the exception queue's workflow, folded from an append-only log.

These run against an in-memory SQLite database rather than the configured one.
The one table this module owns is ordinary and portable -- no triggers, no
server-side defaults beyond ``now()`` -- so SQLite exercises the real logic,
and the tests stay runnable on a laptop that cannot reach Azure SQL.

What is worth pinning here is not "the setter sets". There is no setter: the
status and owner of an item are a fold over its history, and the rules that
are easy to get wrong are the ones about what that fold means.

* an untouched item has no rows at all, and must still read OPEN and unassigned
* unassigning is an entry in the history, not a missing field
* auto-resolution moves only OPEN items, and says it was the check and not a
  person who moved them
* a deliberate CLOSED is never overwritten by the machine
* a person closing an item says why
* history written under the four statuses retired on 21-Oct-2026 still folds,
  into the two that replaced them
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.initiatives.i8.exception_state import (
    UNTOUCHED,
    ExceptionStateError,
    add_comment,
    assign,
    auto_resolve,
    canonical_status,
    fold,
    load_histories,
    load_states,
    set_status,
    state_of,
    validate_kind,
    validate_status,
)
from app.initiatives.i8.models import ExceptionEvent, new_exception_event_id

EX_A = "EX-MISSING_ATTESTATION-4500000677-20"
EX_B = "EX-OVERDUE_REPAIR-4100067718-10"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    # Only this module's table: the rest of the metadata is SAP landing tables
    # that have nothing to do with the queue's workflow.
    ExceptionEvent.__table__.create(engine)
    with Session(engine) as session:
        yield session


class TestAnUntouchedItem:
    def test_has_no_rows_at_all(self, db) -> None:
        """Nothing is seeded. Thousands of rows saying "OPEN, unassigned"
        would be thousands of rows saying nothing."""
        assert load_states(db) == {}

    def test_still_reads_open_and_unassigned(self, db) -> None:
        # Not a placeholder: an exception starts open and owned by nobody,
        # which is exactly what an empty history means.
        state = state_of(db, EX_A)
        assert state == UNTOUCHED
        assert state.status == "OPEN"
        assert state.owner is None
        assert state.is_active

    def test_asking_for_no_ids_does_not_return_everything(self, db) -> None:
        # The easy bug: an empty `in_()` falling through to "no filter".
        assign(db, EX_A, "priya", actor="tester")
        assert load_states(db, []) == {}
        assert set(load_states(db, [EX_A])) == {EX_A}


class TestAssigning:
    def test_folds_to_the_owner_named_last(self, db) -> None:
        assign(db, EX_A, "priya", actor="tester")
        assign(db, EX_A, "sam", actor="tester")
        assert state_of(db, EX_A).owner == "sam"

    def test_unassigning_is_an_entry_not_an_absence(self, db) -> None:
        assign(db, EX_A, "priya", actor="tester")
        assign(db, EX_A, None, actor="tester")

        assert state_of(db, EX_A).owner is None
        assert [e.text for e in load_histories(db, [EX_A])[EX_A]] == [
            "Assigned to priya",
            "Unassigned",
        ]

    def test_blank_is_unassigned_not_an_owner_called_space(self, db) -> None:
        assign(db, EX_A, "   ", actor="tester")
        assert state_of(db, EX_A).owner is None

    def test_assigning_does_not_move_the_item(self, db) -> None:
        assign(db, EX_A, "priya", actor="tester")
        assert state_of(db, EX_A).status == "OPEN"


class TestStatus:
    def test_folds_to_the_status_set_last(self, db) -> None:
        set_status_calls = [
            ("CLOSED", "Reviewed, no action."),
            ("OPEN", "The check still finds it."),
            ("CLOSED", "Declared."),
        ]
        for status, note in set_status_calls:
            set_status(db, EX_A, status, actor="priya", note=note)

        state = state_of(db, EX_A)
        assert state.status == "CLOSED"

        event = state.history[-1]
        assert event.kind == "STATUS"
        assert event.status == "CLOSED"
        assert event.actor == "priya"
        assert event.text == "Declared."

    def test_a_status_change_does_not_clear_the_owner(self, db) -> None:
        assign(db, EX_A, "priya", actor="tester")
        set_status(db, EX_A, "CLOSED", actor="priya", note="Declared.")
        assert state_of(db, EX_A).owner == "priya"

    def test_refuses_a_status_that_is_not_one(self, db) -> None:
        with pytest.raises(ExceptionStateError) as exc:
            validate_status("DONE")
        # The message names what IS allowed -- an error that only says "no"
        # sends the caller to the source.
        assert "OPEN" in str(exc.value)

    @pytest.mark.parametrize("retired", ["IN_PROGRESS", "RESOLVED"])
    def test_refuses_the_retired_statuses(self, retired) -> None:
        # Read through LEGACY_STATUSES, never written: a new entry saying
        # IN_PROGRESS would put a third state back into the history.
        with pytest.raises(ExceptionStateError):
            validate_status(retired)

    def test_accepts_the_vocabulary_in_any_case(self) -> None:
        assert validate_status(" closed ") == "CLOSED"


class TestClosingByHand:
    def test_needs_a_note(self, db) -> None:
        """A person's close is the ending the data did not produce -- the
        check may still find the item -- so it has to say why."""
        with pytest.raises(ExceptionStateError) as exc:
            set_status(db, EX_A, "CLOSED", actor="priya", note="   ")
        assert "note" in str(exc.value)
        # Refused before anything was written.
        assert load_histories(db, [EX_A]) == {}

    def test_reopening_needs_none(self, db) -> None:
        set_status(db, EX_A, "CLOSED", actor="priya", note="Not a real gap.")
        set_status(db, EX_A, "OPEN", actor="priya")
        assert state_of(db, EX_A).status == "OPEN"


class TestKinds:
    def test_refuses_a_kind_that_is_not_one(self) -> None:
        with pytest.raises(ExceptionStateError):
            validate_kind("SHOUTED_AT")

    def test_refuses_auto_resolved_from_a_caller(self) -> None:
        # Only a check can know it has stopped finding something. A person
        # writing this entry would be asserting something they cannot know.
        with pytest.raises(ExceptionStateError) as exc:
            validate_kind("AUTO_RESOLVED")
        assert "queue itself" in str(exc.value)


class TestComments:
    def test_records_what_was_said_without_moving_anything(self, db) -> None:
        assign(db, EX_A, "priya", actor="tester")
        add_comment(db, EX_A, "Vendor says next week.", actor="priya")

        state = state_of(db, EX_A)
        assert state.status == "OPEN"
        assert state.owner == "priya"

        event = state.history[-1]
        assert event.kind == "COMMENT"
        assert event.status is None
        assert event.text == "Vendor says next week."

    def test_refuses_an_empty_one(self, db) -> None:
        with pytest.raises(ExceptionStateError):
            add_comment(db, EX_A, "   ", actor="priya")


class TestAutoResolve:
    def test_resolves_an_item_the_checks_no_longer_raise(self, db) -> None:
        assign(db, EX_A, "priya", actor="tester")

        # The declaration was recorded, so the check stops finding EX_A.
        assert auto_resolve(db, [EX_B]) == 1

        state = state_of(db, EX_A)
        assert state.status == "CLOSED"
        # The owner survives: somebody was working it, and the history should
        # still say who.
        assert state.owner == "priya"

        event = state.history[-1]
        # Not STATUS: "the check stopped finding it" is a different claim from
        # "Priya resolved it", and the history must not blur them.
        assert event.kind == "AUTO_RESOLVED"
        assert event.actor == "system"
        assert "no longer finds" in event.text

    def test_leaves_an_item_that_is_still_raised(self, db) -> None:
        assign(db, EX_A, "priya", actor="tester")
        assert auto_resolve(db, [EX_A, EX_B]) == 0
        assert state_of(db, EX_A).status == "OPEN"

    def test_does_not_rewrite_a_closed_item(self, db) -> None:
        """A deliberate close outranks the machine.

        Without this, every read of the queue would append another
        AUTO_RESOLVED entry to every closed item, and the history would fill
        with noise nobody wrote.
        """
        set_status(db, EX_A, "CLOSED", actor="priya", note="Reviewed, no action.")
        before = len(state_of(db, EX_A).history)

        assert auto_resolve(db, []) == 0
        assert auto_resolve(db, []) == 0

        state = state_of(db, EX_A)
        assert state.status == "CLOSED"
        assert len(state.history) == before

    def test_moves_an_item_left_in_progress_before_the_change(self, db) -> None:
        # IN_PROGRESS is no longer written, but rows saying it are still in
        # the table. They fold to OPEN, so the check stopping still closes them.
        db.add(ExceptionEvent(exception_id=EX_A, kind="STATUS", status="IN_PROGRESS", actor="p"))
        db.commit()

        assert auto_resolve(db, []) == 1
        assert state_of(db, EX_A).status == "CLOSED"

    def test_leaves_an_item_it_resolved_before_the_change(self, db) -> None:
        # Every AUTO_RESOLVED entry written before 21-Oct says RESOLVED. If
        # that folded to anything but CLOSED, each read would close it again.
        db.add(
            ExceptionEvent(
                exception_id=EX_A, kind="AUTO_RESOLVED", status="RESOLVED", actor="system"
            )
        )
        db.commit()

        assert auto_resolve(db, []) == 0
        assert state_of(db, EX_A).status == "CLOSED"

    def test_touches_nothing_when_nobody_has_worked_anything(self, db) -> None:
        # The common case on a queue of thousands: no history rows at all.
        assert auto_resolve(db, []) == 0
        assert db.execute(select(ExceptionEvent)).scalars().all() == []


class TestHistoryOrder:
    def test_reads_oldest_first(self, db) -> None:
        assign(db, EX_A, "priya", actor="tester")
        add_comment(db, EX_A, "Chasing.", actor="priya")
        set_status(db, EX_A, "CLOSED", actor="priya", note="Declared.")

        kinds = [e.kind for e in state_of(db, EX_A).history]
        assert kinds == ["ASSIGNED", "COMMENT", "STATUS"]

    def test_keeps_each_item_to_itself(self, db) -> None:
        add_comment(db, EX_A, "A.", actor="priya")
        add_comment(db, EX_B, "B.", actor="sam")
        histories = load_histories(db, [EX_A, EX_B])
        assert [e.text for e in histories[EX_A]] == ["A."]
        assert [e.text for e in histories[EX_B]] == ["B."]


class TestFold:
    """The fold itself, without a database."""

    def test_an_empty_history_is_open_and_unassigned(self) -> None:
        assert fold(()) == UNTOUCHED

    def test_a_comment_after_a_close_does_not_reopen_it(self) -> None:
        # The bug this guards: folding on "the last entry" rather than "the
        # last entry that SET a status". Somebody adding a note to a closed
        # item must not drag it back into the queue.
        events = [
            ExceptionEvent(exception_id=EX_A, kind="STATUS", status="CLOSED", actor="p"),
            ExceptionEvent(exception_id=EX_A, kind="COMMENT", actor="p", text="For the record."),
        ]
        assert fold(events).status == "CLOSED"

    @pytest.mark.parametrize(
        ("stored", "folds_to"), [("IN_PROGRESS", "OPEN"), ("RESOLVED", "CLOSED")]
    )
    def test_a_retired_status_folds_to_the_one_it_became(self, stored, folds_to) -> None:
        events = [ExceptionEvent(exception_id=EX_A, kind="STATUS", status=stored, actor="p")]
        assert fold(events).status == folds_to

    def test_canonical_status_leaves_the_current_two_and_no_status_alone(self) -> None:
        assert canonical_status("OPEN") == "OPEN"
        assert canonical_status(" closed ") == "CLOSED"
        assert canonical_status(None) is None


class TestEventIds:
    """The ordering key behind every history on screen.

    The history tests above catch a broken id only by luck -- they need two
    events to land in the same clock tick, which happened in roughly one run
    in three. This pins the property directly.
    """

    def test_ids_increase_even_when_the_clock_does_not(self) -> None:
        # time.time_ns() is backed by the system clock, and on Windows that
        # advances in ~15ms steps: a tight loop reads the same value every
        # time. Minting faster than the clock ticks is the normal case for
        # two events recorded in one request, not an edge case.
        ids = [new_exception_event_id() for _ in range(500)]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)

    def test_sorts_as_text_the_way_it_sorts_as_time(self) -> None:
        # The database orders these as strings, so a fixed width matters: an
        # id one character shorter would sort before everything else.
        ids = [new_exception_event_id() for _ in range(50)]
        assert len({len(i) for i in ids}) == 1
        assert all(i.startswith("EXE-") for i in ids)
