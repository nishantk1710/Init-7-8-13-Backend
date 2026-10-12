"""FR-11 -- the exception queue's workflow state.

These run against an in-memory SQLite database rather than the configured one.
The two tables this module owns are ordinary and portable -- no triggers, no
server-side defaults beyond ``now()`` -- so SQLite exercises the real logic,
and the tests stay runnable on a laptop that cannot reach Azure SQL.

What is worth pinning here is not "the setter sets". It is the handful of
rules that are easy to get wrong and silent when they are:

* an untouched item has no row, and must still read as OPEN and unassigned
* unassigning is an action with a history entry, not a missing field
* auto-resolution moves only ACTIVE items, and says it was the check and not
  a person who moved them
* a deliberate CLOSED is never overwritten by the machine
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.initiatives.i8.exception_state import (
    ExceptionStateError,
    add_comment,
    assign,
    auto_resolve,
    load_histories,
    load_states,
    set_status,
    validate_status,
)
from app.initiatives.i8.models import (
    ExceptionEvent,
    ExceptionState,
    new_exception_event_id,
)

EX_A = "EX-MISSING_ATTESTATION-4500000677-20"
EX_B = "EX-OVERDUE_REPAIR-4100067718-10"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    # Only this module's tables: the rest of the metadata is SAP landing
    # tables that have nothing to do with the queue's workflow.
    ExceptionState.__table__.create(engine)
    ExceptionEvent.__table__.create(engine)
    with Session(engine) as session:
        yield session


class TestAnUntouchedItem:
    def test_has_no_row_at_all(self, db) -> None:
        """Nothing is seeded. Thousands of rows saying "OPEN, unassigned"
        would be thousands of rows saying nothing."""
        assert load_states(db) == {}

    def test_asking_for_no_ids_does_not_return_everything(self, db) -> None:
        # The easy bug: an empty `in_()` falling through to "no filter".
        assign(db, EX_A, "priya", actor="tester")
        assert load_states(db, []) == {}
        assert set(load_states(db, [EX_A])) == {EX_A}


class TestAssigning:
    def test_creates_the_row_lazily_and_opens_it(self, db) -> None:
        state = assign(db, EX_A, "priya", actor="tester")
        assert state.owner == "priya"
        assert state.status == "OPEN"

    def test_unassigning_is_an_action_with_a_history(self, db) -> None:
        assign(db, EX_A, "priya", actor="tester")
        state = assign(db, EX_A, None, actor="tester")
        assert state.owner is None

        kinds = [e.text for e in load_histories(db, [EX_A])[EX_A]]
        assert kinds == ["Assigned to priya", "Unassigned"]

    def test_blank_is_unassigned_not_an_owner_called_space(self, db) -> None:
        assert assign(db, EX_A, "   ", actor="tester").owner is None


class TestStatus:
    def test_moves_the_item_and_records_why(self, db) -> None:
        set_status(db, EX_A, "IN_PROGRESS", actor="priya", note="Chasing the vendor.")
        state = load_states(db, [EX_A])[EX_A]
        assert state.status == "IN_PROGRESS"

        event = load_histories(db, [EX_A])[EX_A][-1]
        assert event.kind == "STATUS"
        assert event.status == "IN_PROGRESS"
        assert event.actor == "priya"
        assert event.text == "Chasing the vendor."

    def test_refuses_a_status_that_is_not_one(self, db) -> None:
        with pytest.raises(ExceptionStateError) as exc:
            set_status(db, EX_A, "DONE", actor="tester")
        # The message names what IS allowed -- an error that only says "no"
        # sends the caller to the source.
        assert "OPEN" in str(exc.value)

    def test_accepts_the_vocabulary_in_any_case(self, db) -> None:
        assert validate_status(" resolved ") == "RESOLVED"


class TestComments:
    def test_records_what_was_said(self, db) -> None:
        add_comment(db, EX_A, "Vendor says next week.", actor="priya")
        event = load_histories(db, [EX_A])[EX_A][-1]
        assert event.kind == "COMMENT"
        assert event.status is None
        assert event.text == "Vendor says next week."

    def test_refuses_an_empty_one(self, db) -> None:
        with pytest.raises(ExceptionStateError):
            add_comment(db, EX_A, "   ", actor="priya")

    def test_a_comment_alone_opens_the_item(self, db) -> None:
        """Commenting is touching it, so the state row has to exist afterwards
        -- otherwise the next read reports OPEN with no history to explain
        where the comment came from."""
        add_comment(db, EX_A, "Looking at this.", actor="priya")
        assert load_states(db, [EX_A])[EX_A].status == "OPEN"


class TestAutoResolve:
    def test_resolves_an_item_the_checks_no_longer_raise(self, db) -> None:
        assign(db, EX_A, "priya", actor="tester")

        # The declaration was recorded, so the check stops finding EX_A.
        assert auto_resolve(db, [EX_B]) == 1

        state = load_states(db, [EX_A])[EX_A]
        assert state.status == "RESOLVED"

        event = load_histories(db, [EX_A])[EX_A][-1]
        # Not STATUS: "the check stopped finding it" is a different claim from
        # "Priya resolved it", and the history must not blur them.
        assert event.kind == "AUTO_RESOLVED"
        assert event.actor == "system"
        assert "no longer finds" in event.text

    def test_leaves_an_item_that_is_still_raised(self, db) -> None:
        assign(db, EX_A, "priya", actor="tester")
        assert auto_resolve(db, [EX_A, EX_B]) == 0
        assert load_states(db, [EX_A])[EX_A].status == "OPEN"

    def test_does_not_reopen_or_rewrite_a_closed_item(self, db) -> None:
        """A deliberate close outranks the machine.

        Without this, every read of the queue would append another
        AUTO_RESOLVED entry to every closed item, and the history would fill
        with noise nobody wrote.
        """
        set_status(db, EX_A, "CLOSED", actor="priya", note="Reviewed, no action.")
        before = len(load_histories(db, [EX_A])[EX_A])

        assert auto_resolve(db, []) == 0
        assert auto_resolve(db, []) == 0

        assert load_states(db, [EX_A])[EX_A].status == "CLOSED"
        assert len(load_histories(db, [EX_A])[EX_A]) == before

    def test_moves_in_progress_too(self, db) -> None:
        set_status(db, EX_A, "IN_PROGRESS", actor="priya")
        assert auto_resolve(db, []) == 1
        assert load_states(db, [EX_A])[EX_A].status == "RESOLVED"

    def test_touches_nothing_when_nobody_has_worked_anything(self, db) -> None:
        # The common case on a queue of thousands: no state rows at all.
        assert auto_resolve(db, []) == 0
        assert db.execute(select(ExceptionEvent)).scalars().all() == []


class TestHistoryOrder:
    def test_reads_oldest_first(self, db) -> None:
        assign(db, EX_A, "priya", actor="tester")
        add_comment(db, EX_A, "Chasing.", actor="priya")
        set_status(db, EX_A, "RESOLVED", actor="priya", note="Declared.")

        kinds = [e.kind for e in load_histories(db, [EX_A])[EX_A]]
        assert kinds == ["ASSIGNED", "COMMENT", "STATUS"]

    def test_keeps_each_item_to_itself(self, db) -> None:
        add_comment(db, EX_A, "A.", actor="priya")
        add_comment(db, EX_B, "B.", actor="sam")
        histories = load_histories(db, [EX_A, EX_B])
        assert [e.text for e in histories[EX_A]] == ["A."]
        assert [e.text for e in histories[EX_B]] == ["B."]

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
