"""FR-11 -- working an exception, not merely seeing it.

:mod:`app.initiatives.i8.exceptions` answers "what is wrong". This module
answers "who is dealing with it, and what have they done". Three kinds of
thing, and only one of them is stored:

======================  ==================================================
the item                recomputed from the register and the attestation
                        table on every read. Never stored. It exists
                        exactly as long as its check still finds it.
its history             stored, append-only, in ``i8_exception_event``.
its status and owner    neither stored nor computed from SAP -- **folded
                        from the history**. See :func:`fold`.
======================  ==================================================

Keeping the item computed is what stops the queue drifting away from the data.
A stored queue has to be reconciled -- rows that no longer apply, a "resolved"
item whose underlying gap came back. A computed queue cannot drift, because
there is nothing to drift.

Why the state is folded rather than stored
------------------------------------------
The obvious design is a row per item holding ``status`` and ``owner``, UPDATEd
as people work it. It was written that way first, and it was wrong for this
codebase: every table this application owns is append-only, and
``tests/test_write_paths.py`` holds the whole app to it -- no PUT, no PATCH, no
DELETE, anywhere. An UPDATE here would have been the first exception to a rule
the rest of the platform keeps.

Folding costs nothing worth having. The log is small -- it holds rows only for
items somebody has actually touched, not for the thousands the checks raise --
and the fold is a loop over a handful of entries. What it buys is that the
current state and the audit trail cannot disagree, because they are the same
rows read two ways. ``assistant_session`` already works like this: one row per
invocation, never updated, outcome derived from its turns.

The consequence worth knowing
-----------------------------
A history can outlive the item it describes. Record the declaration and the
missing-attestation check stops finding that line: the item vanishes, and these
rows are all that remember somebody was working it. That is not a leak to be
cleaned up -- it is the resolution, and :func:`auto_resolve` writes it down
explicitly, with its reason, rather than letting the item disappear from under
whoever owned it.

Why nothing here lets you create an item
----------------------------------------
There is no "raise an exception" call, and there must not be. Every item comes
from a check reading SAP data. A hand-typed item would be an assertion nobody
can trace to a rule, and the first one would make every other item in the queue
less trustworthy.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.models import ExceptionEvent

logger = get_logger(__name__)

#: The states an item moves through. A closed set: unlike fault categories,
#: these are not VZI's vocabulary to extend -- each one has behaviour attached
#: (what the default filter shows, which ones the queue may resolve itself).
STATUSES: tuple[str, ...] = ("OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED")

#: Still being worked. What the queue shows by default, and the only states
#: :func:`auto_resolve` will move -- see its docstring.
ACTIVE_STATUSES: frozenset[str] = frozenset({"OPEN", "IN_PROGRESS"})

#: The actor recorded when the queue resolves an item itself.
SYSTEM_ACTOR = "system"

#: What an entry can be.
#:
#: ``AUTO_RESOLVED`` is kept apart from ``STATUS`` deliberately. "Priya resolved
#: this" and "the check stopped finding it" are different claims, and a history
#: that renders them the same way invites the first to be read where only the
#: second happened.
EVENT_KINDS: tuple[str, ...] = ("ASSIGNED", "COMMENT", "STATUS", "AUTO_RESOLVED")


class ExceptionStateError(ValueError):
    """A request that was well-formed but broke a rule of the queue."""


@dataclass(frozen=True)
class ExceptionWorkState:
    """Where one item's work stands, folded from its history."""

    status: str = "OPEN"
    owner: str | None = None
    history: tuple[ExceptionEvent, ...] = field(default=())

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


#: What an item nobody has touched reads as. Not a placeholder: an exception
#: starts open and unassigned, which is exactly what no history means.
UNTOUCHED = ExceptionWorkState()


def validate_status(status: str) -> str:
    """The canonical spelling, or an error naming what is allowed."""
    clean = (status or "").strip().upper()
    if clean not in STATUSES:
        raise ExceptionStateError(
            f"status {status!r} is not one of {', '.join(STATUSES)}"
        )
    return clean


def validate_kind(kind: str) -> str:
    clean = (kind or "").strip().upper()
    if clean not in EVENT_KINDS:
        raise ExceptionStateError(f"kind {kind!r} is not one of {', '.join(EVENT_KINDS)}")
    if clean == "AUTO_RESOLVED":
        # Only the queue writes these. A person recording "the check stopped
        # finding it" would be asserting something only the check can know.
        raise ExceptionStateError(
            "AUTO_RESOLVED is written by the queue itself, not by a caller"
        )
    return clean


def fold(events: Iterable[ExceptionEvent]) -> ExceptionWorkState:
    """One item's current state, from its history oldest-first.

    The last entry that set a status wins; the owner is whoever the last
    ``ASSIGNED`` entry named, and that entry naming nobody is an unassignment
    rather than an absence. Comments move neither.
    """
    status = "OPEN"
    owner: str | None = None
    ordered = tuple(events)
    for event in ordered:
        if event.status:
            status = event.status
        if event.kind == "ASSIGNED":
            owner = event.owner
    return ExceptionWorkState(status=status, owner=owner, history=ordered)


def load_histories(
    db: Session, exception_ids: Collection[str] | None = None
) -> dict[str, list[ExceptionEvent]]:
    """Each item's history, oldest first -- the order a history is read in.

    With no ids, every row. That is what the listing wants, because it also
    needs the histories whose items are *gone* in order to resolve them. The
    row count here is the number of items anybody has ever touched, not the
    size of the queue, so this stays small while the queue runs to thousands.
    """
    stmt = select(ExceptionEvent).order_by(ExceptionEvent.at, ExceptionEvent.id)
    if exception_ids is not None:
        ids = list(exception_ids)
        if not ids:
            return {}
        stmt = stmt.where(ExceptionEvent.exception_id.in_(ids))
    histories: dict[str, list[ExceptionEvent]] = {}
    for row in db.execute(stmt).scalars():
        histories.setdefault(row.exception_id, []).append(row)
    return histories


def load_states(
    db: Session, exception_ids: Collection[str] | None = None
) -> dict[str, ExceptionWorkState]:
    """Folded state by exception id, for the items that have a history."""
    return {
        exception_id: fold(events)
        for exception_id, events in load_histories(db, exception_ids).items()
    }


def state_of(db: Session, exception_id: str) -> ExceptionWorkState:
    """One item's state. ``UNTOUCHED`` when nobody has done anything to it."""
    return load_states(db, [exception_id]).get(exception_id, UNTOUCHED)


def record(
    db: Session,
    exception_id: str,
    *,
    kind: str,
    actor: str,
    text: str = "",
    status: str | None = None,
    owner: str | None = None,
) -> ExceptionEvent:
    """Append one entry to an item's history.

    The single write path. Assigning, commenting and moving an item are all
    the same operation on the same table -- which is why the API has one POST
    rather than a verb per action.
    """
    event = ExceptionEvent(
        exception_id=exception_id,
        kind=kind,
        actor=actor,
        text=text,
        status=status,
        owner=owner,
    )
    db.add(event)
    db.commit()
    return event


def assign(db: Session, exception_id: str, owner: str | None, *, actor: str) -> ExceptionEvent:
    """Give the item an owner, or take its owner away.

    An empty owner is an unassignment, not an error: handing something back is
    a real action and the history should carry it like any other.
    """
    clean = (owner or "").strip() or None
    event = record(
        db,
        exception_id,
        kind="ASSIGNED",
        actor=actor,
        owner=clean,
        text=f"Assigned to {clean}" if clean else "Unassigned",
    )
    logger.info("I08 exception %s assigned to %s by %s", exception_id, clean, actor)
    return event


def set_status(
    db: Session, exception_id: str, status: str, *, actor: str, note: str = ""
) -> ExceptionEvent:
    """Move the item, recording who moved it and why."""
    clean = validate_status(status)
    event = record(
        db, exception_id, kind="STATUS", actor=actor, status=clean, text=note.strip()
    )
    logger.info("I08 exception %s set to %s by %s", exception_id, clean, actor)
    return event


def add_comment(db: Session, exception_id: str, text: str, *, actor: str) -> ExceptionEvent:
    """Say something about the item without moving it."""
    clean = (text or "").strip()
    if not clean:
        raise ExceptionStateError("a comment needs some text")
    return record(db, exception_id, kind="COMMENT", actor=actor, text=clean)


def auto_resolve(db: Session, detected_ids: Iterable[str]) -> int:
    """Resolve the items whose checks have stopped finding them.

    Called from the listing, over the ids the checks raised on this read. A
    history that folds to OPEN or IN_PROGRESS, for an id not among them,
    describes a finding that no longer exists -- somebody recorded the
    declaration, or the goods receipt landed, or a justification named the
    purchase.

    Only active items move. One already RESOLVED or CLOSED is left alone, so
    re-reading the queue cannot keep appending to its history, and a deliberate
    close is never overwritten by the machine.

    Returns how many were resolved, for the caller to log.
    """
    detected = set(detected_ids)
    stale = [
        exception_id
        for exception_id, state in load_states(db).items()
        if state.is_active and exception_id not in detected
    ]
    if not stale:
        return 0

    for exception_id in stale:
        db.add(
            ExceptionEvent(
                exception_id=exception_id,
                kind="AUTO_RESOLVED",
                actor=SYSTEM_ACTOR,
                status="RESOLVED",
                # Why, not just that. The data moving is the only way an item
                # stops being found.
                text=(
                    "The check no longer finds this item -- the data it was "
                    "raised on has changed."
                ),
            )
        )
    db.commit()
    logger.info("I08 exceptions: %d item(s) resolved themselves", len(stale))
    return len(stale)
