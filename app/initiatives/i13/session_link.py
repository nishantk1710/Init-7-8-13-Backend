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

Multi-material requests
-----------------------
A request is one SAP reservation. The requester types the bare request ID
(``RSA1MT7EKP``) into the reservation header, and SAP copies that text to every
item's SGTXT -- so every item of the reservation carries the same reference.
:func:`request_refs_in` reads it, and each item is linked to **its line's own
session**, found by request + material + plant: a request holds each material
once, so that pair names one line. The assistant quotes a line on screen as
``<request>/<n>``. An older SGTXT that carries the ``/n`` still links, and a
``/n`` that names another line is noted (:func:`line_number_note`); a missing
one is the normal case and is not. Because the link row names the line's session,
everything downstream -- the ledger, compliance, ACT, plan matching -- treats it
exactly as a stand-alone session's link.

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


# --- request references: <request>/<n> ----------------------------------------


@dataclass(frozen=True)
class RequestRef:
    request_id: str
    line_no: int | None
    """The ``/n`` typed after it, if any. A cross-check, never the key."""


@dataclass(frozen=True)
class FoundRefs:
    valid: tuple[RequestRef, ...]
    #: Request-shaped but failing the checksum -- a mistyped reference.
    invalid: tuple[str, ...]


def _request_pattern() -> re.Pattern[str]:
    settings = get_settings()
    prefix = re.escape(settings.assistant_request_id_prefix.strip().upper())
    body = settings.assistant_session_id_length - len(settings.assistant_request_id_prefix.strip())
    return re.compile(
        rf"(?<![0-9A-Z])({prefix}[0-9A-Z]{{{body}}})(?![0-9A-Z])(?:\s*[/-]\s*(\d{{1,3}})(?!\d))?"
    )


def _scan_refs(text: str) -> tuple[list[RequestRef], list[str]]:
    valid: list[RequestRef] = []
    invalid: list[str] = []
    for match in _request_pattern().finditer(text.upper()):
        original = match.group(1)
        request_id = ids.normalise(original)
        line_no = int(match.group(2)) if match.group(2) else None
        if ids.is_valid_request(request_id):
            ref = RequestRef(request_id, line_no)
            if ref not in valid:
                valid.append(ref)
        elif any(c.isdigit() for c in original) and all(c in ids.ALPHABET for c in request_id):
            # Same rule as a session: a real reference carries a digit; a word
            # ("REPAIRABLE") never does.
            if request_id not in invalid:
                invalid.append(request_id)
    return valid, invalid


def request_refs_in(sgtxt: str | None) -> FoundRefs:
    """Every request reference in one SGTXT value, and every mistyped one.

    Reads ``R.../2``, ``R...-2``, ``R... / 2``, a bare ``R...``, lower case, and
    the reference among other words. A reference typed in groups
    (``RSA1 MT7E KP/2``) is found on a second pass with the spaces removed --
    and kept only if it passes its checksum, since gluing words together could
    otherwise manufacture a "mistyped" one out of ordinary text.
    """
    if not sgtxt or not sgtxt.strip():
        return FoundRefs((), ())
    valid, invalid = _scan_refs(sgtxt)
    if not valid and not invalid:
        valid, _ = _scan_refs(re.sub(r"\s+", "", sgtxt))
    known = {ref.request_id for ref in valid}
    return FoundRefs(tuple(valid), tuple(i for i in invalid if i not in known))


@dataclass(frozen=True)
class RequestLine:
    request_id: str
    line_no: int
    session_id: str
    material: str
    plant: str


def load_request_lines(db: Session, request_ids: Iterable[str]) -> dict[str, list[RequestLine]]:
    """The lines of these requests, by request -- empty for an unknown one."""
    from app.assistant.models import AssistantSession

    wanted = list(set(request_ids))
    if not wanted:
        return {}
    found: dict[str, list[RequestLine]] = {}
    for session_id, request_id, line_no, material, plant in db.execute(
        select(
            AssistantSession.id,
            AssistantSession.request_id,
            AssistantSession.line_no,
            AssistantSession.material_id,
            AssistantSession.plant,
        ).where(AssistantSession.request_id.in_(wanted))
    ).all():
        found.setdefault(request_id, []).append(RequestLine(request_id, line_no, session_id, material, plant))
    return found


def line_number_note(sgtxt: str | None, request_id: str, line_no: int | None) -> str | None:
    """Why a request line's link deserves a second look, or ``None``.

    The link is made by material and plant, and SGTXT normally carries the bare
    request ID (SAP copies the header text to every item), so no ``/n`` is the
    normal case. A ``/n`` that names another line is worth a note: somebody
    reading the reservation will read it.
    """
    refs = [ref for ref in request_refs_in(sgtxt).valid if ref.request_id == request_id]
    if not refs or line_no is None:
        return None
    typed = refs[0].line_no
    if typed is not None and typed != line_no:
        return f"Typed /{typed}; matched to /{line_no} by material and plant."
    return None


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


def _links_wanted(
    rows: Iterable[Row],
    sessions: Mapping[str, SessionInfo],
    request_lines: Mapping[str, list[RequestLine]] | None = None,
) -> dict[tuple[str, str, str], Row]:
    wanted: dict[tuple[str, str, str], Row] = {}
    for row in rows:
        found = session_ids_in(row.get("Sgtxt"))
        for session_id in found.valid:
            info = sessions.get(session_id)
            if info is None or (info.material, info.plant) != (row["Matnr"], row["Werks"]):
                continue
            wanted[(session_id, row["Rsnum"], row["Rspos"])] = row
        if request_lines:
            for ref in request_refs_in(row.get("Sgtxt")).valid:
                # Request + material + plant names one line; the /n does not
                # decide. A material the request never assessed is no link.
                line = next(
                    (
                        line
                        for line in request_lines.get(ref.request_id, ())
                        if (line.material, line.plant) == (row["Matnr"], row["Werks"])
                    ),
                    None,
                )
                if line is not None:
                    wanted[(line.session_id, row["Rsnum"], row["Rspos"])] = row
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
    request_lines = load_request_lines(
        db, {ref.request_id for row in rows for ref in request_refs_in(row.get("Sgtxt")).valid}
    )
    wanted = _links_wanted(rows, sessions, request_lines)

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


def links_for_sessions(db: Session, session_ids: Iterable[str]) -> dict[str, list[SessionReservationLink]]:
    """``links_by_session`` for the given sessions only -- one query, for a page
    of the session log rather than the whole link table."""
    wanted = sorted(set(session_ids))
    by_session: dict[str, list[SessionReservationLink]] = {}
    # Well under SQL Server's 2,100 parameters per statement.
    for start in range(0, len(wanted), 1000):
        for link in db.execute(
            select(SessionReservationLink)
            .where(SessionReservationLink.session_id.in_(wanted[start : start + 1000]))
            .order_by(SessionReservationLink.reservation_number, SessionReservationLink.reservation_item)
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
    * the same for a request reference: mistyped, unknown, or naming a request
      with no line for this material -> ``INVALID_SESSION``. The requester
      used the assistant; what they typed does not lead back to it, which is a
      different finding from not using it at all.
    * nothing session-like at all -> ``MISSING_SESSION``

    A request line is linked through its own session, so the first two cases
    cover it unchanged -- a line marked "not needed" that was reserved anyway
    has no plan and reads ``SESSION_WITHOUT_PLAN``, which is what it is.
    """
    if linked_session is not None:
        info = sessions.get(linked_session)
        return COVERED if info is not None and info.has_plan else SESSION_WITHOUT_PLAN
    found = session_ids_in(sgtxt)
    if found.valid or found.invalid:
        return INVALID_SESSION
    refs = request_refs_in(sgtxt)
    if refs.valid or refs.invalid:
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
