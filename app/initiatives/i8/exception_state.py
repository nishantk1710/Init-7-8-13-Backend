"""FR-11 -- working an exception, not merely seeing it.

:mod:`app.initiatives.i8.exceptions` answers "what is wrong". This module
answers "who is dealing with it, and what have they done". The two are
deliberately separate kinds of thing:

====================  ====================================================
the item              recomputed from the register and the attestation
                      table on every read. Never stored. It exists exactly
                      as long as its check still finds it.
its state             stored here. Status, owner, and a history of what
                      people said and did.
====================  ====================================================

Keeping the item computed is what stops the queue drifting away from the data.
A stored queue has to be reconciled -- rows that no longer apply, rows nobody
re-raised, a "resolved" item whose underlying gap came back. A computed queue
cannot drift, because there is nothing to drift.

What that costs, and how it is paid
-----------------------------------
A computed item cannot remember anything, so the moment somebody assigns one
there has to be somewhere to put that. This table. It is keyed by the
exception's own deterministic id, so the state re-attaches whenever the check
raises the same finding again.

The interesting consequence is a state row that **outlives its item**. Record
the declaration and the missing-attestation check stops finding that line: the
item vanishes, and this row is all that remembers somebody was working it.
That is not a leak to be cleaned up -- it is the resolution, and
:func:`auto_resolve` turns it into one explicitly, with its reason in the
history, rather than letting the item disappear silently from under whoever
owned it.

Why nothing here lets you create an item
----------------------------------------
There is no "raise an exception" call, and there must not be. Every item comes
from a check reading SAP data. A hand-typed item would be an assertion nobody
can trace to a rule, and the first one would make every other item in the queue
less trustworthy.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.models import ExceptionEvent, ExceptionState

logger = get_logger(__name__)

#: The states an item moves through. A closed set: unlike fault categories,
#: these are not VZI's vocabulary to extend -- each one has behaviour attached
#: (what the default filter shows, what auto-resolution may overwrite).
STATUSES: tuple[str, ...] = ("OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED")

#: Still being worked. What the queue shows by default, and the only states
#: :func:`auto_resolve` will move -- see its docstring.
ACTIVE_STATUSES: frozenset[str] = frozenset({"OPEN", "IN_PROGRESS"})

#: The actor recorded when the queue resolves an item itself.
SYSTEM_ACTOR = "system"

EVENT_KINDS: tuple[str, ...] = ("ASSIGNED", "COMMENT", "STATUS", "AUTO_RESOLVED")


class ExceptionStateError(ValueError):
    """A request that was well-formed but broke a rule of the queue."""


def validate_status(status: str) -> str:
    """The canonical spelling, or an error naming what is allowed."""
    clean = (status or "").strip().upper()
    if clean not in STATUSES:
        raise ExceptionStateError(
            f"status {status!r} is not one of {', '.join(STATUSES)}"
        )
    return clean


def load_states(
    db: Session, exception_ids: Collection[str] | None = None
) -> dict[str, ExceptionState]:
    """Stored state by exception id.

    With no ids, every row -- which is what the listing wants, because it also
    needs the states whose items are *gone* in order to resolve them. The row
    count here is the number of items anybody has ever touched, not the size of
    the queue, so this stays small while the queue runs to thousands.
    """
    stmt = select(ExceptionState)
    if exception_ids is not None:
        ids = list(exception_ids)
        if not ids:
            return {}
        stmt = stmt.where(ExceptionState.exception_id.in_(ids))
    return {row.exception_id: row for row in db.execute(stmt).scalars()}


def load_histories(
    db: Session, exception_ids: Collection[str]
) -> dict[str, list[ExceptionEvent]]:
    """Each item's history, oldest first -- the order a history is read in."""
    ids = list(exception_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(ExceptionEvent)
        .where(ExceptionEvent.exception_id.in_(ids))
        .order_by(ExceptionEvent.at, ExceptionEvent.id)
    ).scalars()
    histories: dict[str, list[ExceptionEvent]] = {}
    for row in rows:
        histories.setdefault(row.exception_id, []).append(row)
    return histories


def _ensure_state(db: Session, exception_id: str) -> ExceptionState:
    """The item's state row, created as OPEN the first time anyone touches it.

    Created lazily rather than seeded for every computed item: seeding would
    write thousands of rows saying nothing, and would have to be re-run every
    time a check raised something new.
    """
    state = db.get(ExceptionState, exception_id)
    if state is None:
        state = ExceptionState(exception_id=exception_id, status="OPEN")
        db.add(state)
        db.flush()
    return state


def _record(
    db: Session,
    exception_id: str,
    *,
    kind: str,
    actor: str,
    text: str = "",
    status: str | None = None,
) -> ExceptionEvent:
    event = ExceptionEvent(
        exception_id=exception_id,
        kind=kind,
        actor=actor,
        text=text,
        status=status,
    )
    db.add(event)
    return event


def assign(db: Session, exception_id: str, owner: str | None, *, actor: str) -> ExceptionState:
    """Give the item an owner, or take its owner away.

    An empty owner is unassignment, not an error: handing something back is a
    real action and the history should carry it like any other.
    """
    state = _ensure_state(db, exception_id)
    clean = (owner or "").strip() or None
    state.owner = clean
    _record(
        db,
        exception_id,
        kind="ASSIGNED",
        actor=actor,
        text=f"Assigned to {clean}" if clean else "Unassigned",
    )
    db.commit()
    logger.info("I08 exception %s assigned to %s by %s", exception_id, clean, actor)
    return state


def set_status(
    db: Session, exception_id: str, status: str, *, actor: str, note: str = ""
) -> ExceptionState:
    """Move the item, recording who moved it and why."""
    clean = validate_status(status)
    state = _ensure_state(db, exception_id)
    state.status = clean
    _record(
        db,
        exception_id,
        kind="STATUS",
        actor=actor,
        text=note.strip(),
        status=clean,
    )
    db.commit()
    logger.info("I08 exception %s set to %s by %s", exception_id, clean, actor)
    return state


def add_comment(db: Session, exception_id: str, text: str, *, actor: str) -> ExceptionEvent:
    """Say something about the item without moving it."""
    clean = (text or "").strip()
    if not clean:
        raise ExceptionStateError("a comment needs some text")
    _ensure_state(db, exception_id)
    event = _record(db, exception_id, kind="COMMENT", actor=actor, text=clean)
    db.commit()
    return event


def auto_resolve(db: Session, detected_ids: Iterable[str]) -> int:
    """Resolve the items whose checks have stopped finding them.

    Called from the listing, over the ids the checks raised on this read. A
    state row still OPEN or IN_PROGRESS whose id is not among them describes a
    finding that no longer exists -- somebody recorded the declaration, or the
    goods receipt landed, or a justification named the purchase.

    Only active items move. A RESOLVED or CLOSED row is left alone so that
    re-reading the queue cannot keep rewriting history, and so a deliberate
    close is never overwritten by the machine.

    Returns how many were resolved, for the caller to log.
    """
    detected = set(detected_ids)
    stale = [
        state
        for state in db.execute(
            select(ExceptionState).where(ExceptionState.status.in_(tuple(ACTIVE_STATUSES)))
        ).scalars()
        if state.exception_id not in detected
    ]
    if not stale:
        return 0

    for state in stale:
        state.status = "RESOLVED"
        _record(
            db,
            state.exception_id,
            kind="AUTO_RESOLVED",
            actor=SYSTEM_ACTOR,
            status="RESOLVED",
            # Why, not just that. The check's name is the explanation: the
            # data moved, which is the only way an item stops being found.
            text="The check no longer finds this item -- the data it was raised on has changed.",
        )
    db.commit()
    logger.info("I08 exceptions: %d item(s) resolved themselves", len(stale))
    return len(stale)
