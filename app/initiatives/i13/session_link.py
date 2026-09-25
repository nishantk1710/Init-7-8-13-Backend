"""Reading the assistant's session ID back off the reservation (RESB.SGTXT).

The assistant issues a session ID while the reservation is still being
created, so it cannot record a reservation number. The requester types the ID
into the reservation's item text, SGTXT -- loaded by the extract as
``raw_resb.text`` -- and this module reads it back:

1. :func:`session_ids_in` finds session IDs inside the free text. SGTXT is used
   for other things too (names, site notes), so an ID may sit among other
   words, and a person may have mistyped it. The ID format carries a checksum
   (``app.assistant.ids``), so a real ID is recognised exactly and a mistyped
   one is recognised as *session-shaped but invalid*.
2. :func:`sync_links` keeps ``session_reservation_link`` in step: one row per
   reservation item that names a real session **for the same material and
   plant** (a session for another part in someone's notes is not a link).
3. :func:`session_status` says, per reservation, which of I13's no-plan
   reasons applies: covered, ``MISSING_SESSION``, ``INVALID_SESSION`` or
   ``SESSION_WITHOUT_PLAN`` -- the FR-4 check and the sessions screen's
   "reservations with no session" count.

Reads Postgres only. In UAT the reservation rows may include the
``uat_reservation_sgtxt`` overlay (see ``postgres_reservation``); the linking
logic does not care which it is, which is the point of simulating it that way.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.assistant import ids
from app.core.config import get_settings
from app.models.i13_session_link import SessionReservationLink

Row = dict[str, Any]
Key = tuple[str, str]

#: Split SGTXT into words. Separators a person might put around an ID.
_WORD_SPLIT = re.compile(r"[\s,;:/\\()\[\]{}|.#*+=!?\"']+")

COVERED = "COVERED"
MISSING_SESSION = "MISSING_SESSION"
INVALID_SESSION = "INVALID_SESSION"
SESSION_WITHOUT_PLAN = "SESSION_WITHOUT_PLAN"


@dataclass(frozen=True)
class FoundIds:
    valid: tuple[str, ...]
    #: Session-shaped (right length, prefix, alphabet, has a digit) but failing
    #: the checksum -- a mistyped ID, not ordinary text.
    invalid: tuple[str, ...]


def _session_shaped(original: str, normalised: str) -> bool:
    settings = get_settings()
    prefix = settings.assistant_session_id_prefix.strip().upper()
    return (
        len(normalised) == settings.assistant_session_id_length
        and normalised.startswith(prefix)
        and all(character in ids.ALPHABET for character in normalised)
        # Ordinary words ("SPECIALIST") pass every other test once I/L/O are
        # repaired. A real ID almost always carries a digit; a word never does.
        and any(character.isdigit() for character in original)
    )


def session_ids_in(sgtxt: str | None) -> FoundIds:
    """Every session ID in one SGTXT value, canonicalised, and every
    session-shaped string that fails its checksum."""
    if not sgtxt or not sgtxt.strip():
        return FoundIds((), ())
    # (candidate, is_a_single_word). The whole text, separators removed, is a
    # candidate too -- "SE97 FPM9 AB" is one ID typed in groups -- but only a
    # single word can be judged "a mistyped ID"; a joined-up sentence cannot.
    candidates = [(word, True) for word in _WORD_SPLIT.split(sgtxt) if word]
    candidates.append((sgtxt, False))
    valid: list[str] = []
    invalid: list[str] = []
    for candidate, single_word in candidates:
        normalised = ids.normalise(candidate)
        if ids.is_valid(normalised):
            if normalised not in valid:
                valid.append(normalised)
        elif single_word and _session_shaped(candidate, normalised):
            if normalised not in invalid:
                invalid.append(normalised)
    # A mistyped ID that was also found intact elsewhere is not invalid.
    invalid = [candidate for candidate in invalid if candidate not in valid]
    return FoundIds(tuple(valid), tuple(invalid))


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    material: str
    plant: str
    has_plan: bool


def load_sessions(db: Session, session_ids: Iterable[str] | None = None) -> dict[str, SessionInfo]:
    """Assistant sessions (and whether each captured a plan), by ID."""
    from app.assistant.models import AssistantSession, ConsumptionPlanRecord

    stmt = select(AssistantSession.id, AssistantSession.material_id, AssistantSession.plant)
    wanted = list(session_ids) if session_ids is not None else None
    if wanted is not None:
        if not wanted:
            return {}
        stmt = stmt.where(AssistantSession.id.in_(wanted))
    with_plan = set(db.execute(select(ConsumptionPlanRecord.session_id).distinct()).scalars())
    return {
        sid: SessionInfo(sid, material, plant, sid in with_plan)
        for sid, material, plant in db.execute(stmt).all()
    }


def _links_wanted(rows: Iterable[Row], sessions: Mapping[str, SessionInfo]) -> dict[tuple[str, str, str], Row]:
    wanted: dict[tuple[str, str, str], Row] = {}
    for row in rows:
        found = session_ids_in(row.get("Sgtxt"))
        for session_id in found.valid:
            info = sessions.get(session_id)
            if info is None or (info.material, info.plant) != (row["Matnr"], row["Werks"]):
                continue
            wanted[(session_id, row["Rsnum"], row["Rspos"])] = row
    return wanted


@dataclass(frozen=True)
class SyncResult:
    added: int
    removed: int
    kept: int


def sync_links(db: Session, rows: list[Row], *, scope: Key | None = None) -> SyncResult:
    """Make ``session_reservation_link`` match what SGTXT says now.

    ``rows`` are reservation rows (``PostgresReservationRepository``), for the
    whole tenant or -- with ``scope`` -- for one material-plant, in which case
    only that material-plant's links are touched. The caller commits.
    """
    candidate_ids = {sid for row in rows for sid in session_ids_in(row.get("Sgtxt")).valid}
    sessions = load_sessions(db, candidate_ids)
    wanted = _links_wanted(rows, sessions)

    stmt = select(SessionReservationLink)
    if scope is not None:
        stmt = stmt.where(SessionReservationLink.material == scope[0], SessionReservationLink.plant == scope[1])
    existing = {(l.session_id, l.reservation_number, l.reservation_item): l for l in db.execute(stmt).scalars()}

    stale = [link.id for key, link in existing.items() if key not in wanted]
    if stale:
        db.execute(delete(SessionReservationLink).where(SessionReservationLink.id.in_(stale)))
    added = 0
    for key, row in wanted.items():
        if key in existing:
            continue
        session_id, reservation_number, reservation_item = key
        db.add(
            SessionReservationLink(
                session_id=session_id,
                reservation_number=reservation_number,
                reservation_item=reservation_item,
                material=row["Matnr"],
                plant=row["Werks"],
                source="UAT_SGTXT" if row.get("UatSimulated") or row.get("UatStamped") else "SGTXT",
                sgtxt=(row.get("Sgtxt") or "")[:60],
            )
        )
        added += 1
    db.flush()
    return SyncResult(added=added, removed=len(stale), kept=len(existing) - len(stale))


def links_by_session(db: Session) -> dict[str, list[SessionReservationLink]]:
    by_session: dict[str, list[SessionReservationLink]] = {}
    for link in db.execute(
        select(SessionReservationLink).order_by(SessionReservationLink.reservation_number, SessionReservationLink.reservation_item)
    ).scalars():
        by_session.setdefault(link.session_id, []).append(link)
    return by_session


def session_by_reservation(db: Session) -> dict[Key, str]:
    """(reservation number, item) -> the session its SGTXT names."""
    return {
        (link.reservation_number, link.reservation_item): link.session_id
        for link in db.execute(select(SessionReservationLink)).scalars()
    }


def session_status(sgtxt: str | None, linked_session: str | None, sessions: Mapping[str, SessionInfo]) -> str:
    """Which FR-4 outcome applies to one reservation.

    * a linked session that captured a plan -> ``COVERED``
    * a linked session with no plan -> ``SESSION_WITHOUT_PLAN``
    * a session-shaped value, or a valid ID naming no session for this part ->
      ``INVALID_SESSION``
    * nothing session-like at all -> ``MISSING_SESSION``
    """
    if linked_session is not None:
        info = sessions.get(linked_session)
        return COVERED if info is not None and info.has_plan else SESSION_WITHOUT_PLAN
    found = session_ids_in(sgtxt)
    if found.valid or found.invalid:
        return INVALID_SESSION
    return MISSING_SESSION


def go_live_date(db: Session) -> date:
    """From when reservations are expected to carry a session ID.

    ``I13_ASSISTANT_GO_LIVE_DATE`` if set, else the day the first assistant
    session was issued, else today.
    """
    from app.assistant.models import AssistantSession

    pinned = (get_settings().i13_assistant_go_live_date or "").strip()
    if pinned:
        return date.fromisoformat(pinned)
    first = db.execute(select(func.min(AssistantSession.issued_at))).scalar()
    return first.date() if first is not None else date.today()
