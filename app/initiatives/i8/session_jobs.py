"""The job an assistant session was for: its reservation, and the 261/262 on it.

Since 27-Oct-2026 (phase 3 of the complete-flow plan). The maintenance order
that takes a part out of the stores is a different reservation from the
repair order (543), with no key between the two (26-Oct ruling). What joins
them is the assistant's reference: the planner types the session ID -- or a
multi-material request ID -- into the job's reservation line text (RESB.SGTXT),
as the stores clerk does on the repair order's 543 line. Read back here, the
session page can show the job it was for: the reservation line, what was
issued against it (261) and what came back to stock (262 -- posted on the
receipt day, not when the unit left the machine; measured 26-Oct).

The reference is read exactly as on the repair order
(``repair_references.sessions_named``): a checksum-valid ID for a session --
or a request line -- on the same material and plant. Every value read from SAP
that decides something is a setting: the repair order's movement type marks
the lines that are NOT the job, and the issue and return movement types pick
the movements.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.repair_references import sessions_named
from app.initiatives.i8.staged import Sources, reading
from app.initiatives.i13.session_link import (
    INVALID_SESSION,
    MISSING_SESSION,
    RequestLine,
    SessionInfo,
    load_request_lines,
    load_sessions,
    request_refs_in,
    session_ids_in,
)
from app.shared.sql_lists import fetch_in_chunks

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobMovement:
    """One goods movement against a job's reservation line."""

    posted_on: date | None
    quantity: float | None
    """MSEG.MENGE as SAP sent it; None when it did not parse (unknown, not 0)."""


@dataclass(frozen=True)
class SessionJob:
    """One job reservation line whose text names an assistant session."""

    session_id: str
    reference: str
    """The reference as typed: the session ID, or ``{requestId}/{lineNo}``."""
    reservation: str
    item: str
    order: str | None
    material: str
    plant: str | None
    quantity: float | None
    requirement_date: date | None
    issued: tuple[JobMovement, ...] = ()
    returned: tuple[JobMovement, ...] = ()


def _item(item: str | None) -> str:
    item = (item or "").strip()
    return item.lstrip("0") or item


def _number(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _date(value) -> date | None:
    return value if isinstance(value, date) else None


def is_job_line(resb_row: Mapping, cfg: I8Settings) -> bool:
    """A reservation line that could be the job: not the repair order's line,
    with some text to carry a reference."""
    return (
        resb_row.get("bwart") != cfg.repair_reservation_movement_type
        and bool(resb_row.get("rsnum"))
        and bool((resb_row.get("sgtxt") or "").strip())
    )


def build_session_jobs(
    rows: Iterable[Mapping],
    movements: Iterable[Mapping],
    sessions: Mapping[str, SessionInfo],
    request_lines: Mapping[str, list[RequestLine]],
    cfg: I8Settings | None = None,
) -> tuple[SessionJob, ...]:
    """Pure: each job line naming a session, with its issues and returns.

    ``rows`` carry the ``v_resb`` columns, ``movements`` the ``v_mseg`` ones
    (rsnum, rspos, bwart, budat, menge). Items compare with leading zeros
    stripped on both sides.
    """
    cfg = cfg or get_i8_settings()
    by_line: dict[tuple[str, str], dict[str, list[JobMovement]]] = {}
    for movement in movements:
        bwart = movement.get("bwart")
        if bwart not in (cfg.job_issue_movement_type, cfg.job_return_movement_type):
            continue
        key = (movement.get("rsnum") or "", _item(movement.get("rspos")))
        side = "issued" if bwart == cfg.job_issue_movement_type else "returned"
        by_line.setdefault(key, {"issued": [], "returned": []})[side].append(
            JobMovement(posted_on=_date(movement.get("budat")), quantity=_number(movement.get("menge")))
        )

    def ordered(found: list[JobMovement]) -> tuple[JobMovement, ...]:
        return tuple(sorted(found, key=lambda m: (m.posted_on is None, m.posted_on or date.min)))

    jobs: dict[tuple[str, str, str], SessionJob] = {}
    for row in rows:
        if not is_job_line(row, cfg):
            continue
        reservation, item = row["rsnum"], _item(row.get("rspos"))
        moved = by_line.get((reservation, item), {"issued": [], "returned": []})
        for session_id, reference in sessions_named(
            row["sgtxt"], row.get("matnr"), row.get("werks"), sessions, request_lines
        ):
            jobs.setdefault(
                (session_id, reservation, item),
                SessionJob(
                    session_id=session_id,
                    reference=reference,
                    reservation=reservation,
                    item=item,
                    order=row.get("aufnr") or None,
                    material=row.get("matnr") or "",
                    plant=row.get("werks"),
                    quantity=_number(row.get("bdmng")),
                    requirement_date=_date(row.get("bdter")),
                    issued=ordered(moved["issued"]),
                    returned=ordered(moved["returned"]),
                ),
            )
    return tuple(jobs[key] for key in sorted(jobs))


_JOB_LINE_SQL = """
-- Reservation lines with text: the job's line names the assistant session in
-- SGTXT. Which lines are the job (not the repair order's movement type) is
-- judged in Python (session_jobs.is_job_line).
select rsnum, rspos, matnr, werks, bwart, aufnr, bdmng, bdter, sgtxt
from v_resb
where sgtxt is not null
"""

_MOVEMENT_SQL = """
-- The movements posted against those reservations; the issue and return
-- movement types are picked in Python.
select rsnum, rspos, bwart, budat, menge
from v_mseg
where rsnum in :reservations
"""


def load_session_jobs(
    db: Session, cfg: I8Settings | None = None, *, sources: Sources | None = None
) -> tuple[SessionJob, ...]:
    """Read the job lines that name a session, then their movements.

    Staged, both reads are over narrowed copies: v_resb keeps 80-series lines
    with text, v_mseg adds the movements on those reservations (staged.py).
    No session or movement query at all when no line names a valid ID.
    """
    cfg = cfg or get_i8_settings()
    rows = [
        row
        for row in db.execute(text(reading(_JOB_LINE_SQL, sources))).mappings()
        if is_job_line(row, cfg)
    ]
    session_ids = {sid for row in rows for sid in session_ids_in(row["sgtxt"]).valid}
    request_ids = {ref.request_id for row in rows for ref in request_refs_in(row["sgtxt"]).valid}
    if not session_ids and not request_ids:
        logger.info("I08 session jobs: none (%d job lines with text)", len(rows))
        return ()
    sessions = load_sessions(db, session_ids) if session_ids else {}
    request_lines = load_request_lines(db, request_ids) if request_ids else {}
    naming = [
        row
        for row in rows
        if sessions_named(row["sgtxt"], row.get("matnr"), row.get("werks"), sessions, request_lines)
    ]
    reservations = sorted({row["rsnum"] for row in naming})
    movements = (
        fetch_in_chunks(db, reading(_MOVEMENT_SQL, sources), "reservations", reservations)
        if reservations
        else []
    )
    jobs = build_session_jobs(naming, movements, sessions, request_lines, cfg)
    logger.info(
        "I08 session jobs: %d job lines name a session (%d job lines with text, %d movements)",
        len(jobs),
        len(rows),
        len(movements),
    )
    return jobs


# --- MISSING_SESSION_ID: job lines with no R-reference (27-Oct-2026) ----------


@dataclass(frozen=True)
class UnreferencedLine:
    """A job (consumption) reservation line on an 80-series part whose text
    leads back to no assistant session for its part and plant."""

    reservation: str
    item: str
    order: str | None
    material: str
    plant: str | None
    requirement_date: date | None
    finding: str
    """``MISSING_SESSION`` (nothing session-like typed) or ``INVALID_SESSION``
    (a mistyped ID, or one naming no session for this part) -- the I13
    ``session_status`` outcomes, so both screens judge a line the same way."""
    typed: str | None = None
    """The line text, on ``INVALID_SESSION`` only: what was typed."""


def is_consumption_line(resb_row: Mapping, cfg: I8Settings) -> bool:
    """A live job line on an 80-series part: the issue movement type, not
    deleted. On the 15-Oct extract 10,187 such lines were 261s on 80-series
    parts, 2,954 deleted, and 87 of the rest carried any text at all."""
    material = (resb_row.get("matnr") or "").lstrip("0")
    return (
        resb_row.get("bwart") == cfg.job_issue_movement_type
        and bool(resb_row.get("rsnum"))
        and resb_row.get("xloek") != cfg.reservation_deleted_indicator
        and material.startswith(cfg.series_prefix_list)
    )


def _in_check(row: Mapping, cfg: I8Settings, start: date) -> bool:
    """A consumption line needed on or after the check started. A line with no
    requirement date cannot be placed either side of it, so is not judged."""
    needed = _date(row.get("bdter"))
    return is_consumption_line(row, cfg) and needed is not None and needed >= start


def unreferenced_lines(
    rows: Iterable[Mapping],
    sessions: Mapping[str, SessionInfo],
    request_lines: Mapping[str, list[RequestLine]],
    cfg: I8Settings,
    start: date,
) -> tuple[UnreferencedLine, ...]:
    """Pure: the consumption lines needed on or after ``start`` whose text
    names no session -- or request line -- for the same part and plant."""
    found: dict[tuple[str, str], UnreferencedLine] = {}
    for row in rows:
        if not _in_check(row, cfg, start):
            continue
        typed = (row.get("sgtxt") or "").strip()
        if typed and sessions_named(typed, row.get("matnr"), row.get("werks"), sessions, request_lines):
            continue
        ids, refs = session_ids_in(typed), request_refs_in(typed)
        invalid = bool(ids.valid or ids.invalid or refs.valid or refs.invalid)
        key = (row["rsnum"], _item(row.get("rspos")))
        found.setdefault(
            key,
            UnreferencedLine(
                reservation=key[0],
                item=key[1],
                order=row.get("aufnr") or None,
                material=row.get("matnr") or "",
                plant=row.get("werks"),
                requirement_date=_date(row.get("bdter")),
                finding=INVALID_SESSION if invalid else MISSING_SESSION,
                typed=typed if invalid else None,
            ),
        )
    return tuple(found[key] for key in sorted(found))


_CONSUMPTION_SQL = """
-- Every staged reservation line; the job lines (issue movement type, not
-- deleted, 80-series) are picked in Python (session_jobs.is_consumption_line).
select rsnum, rspos, matnr, werks, bwart, aufnr, bdter, sgtxt, xloek
from v_resb
"""


def load_unreferenced_lines(
    db: Session, start: date, cfg: I8Settings | None = None, *, sources: Sources | None = None
) -> tuple[UnreferencedLine, ...]:
    """Read the job lines and judge their text. Sessions and request lines are
    read only for the valid IDs typed on lines inside the check."""
    cfg = cfg or get_i8_settings()
    rows = [row for row in db.execute(text(reading(_CONSUMPTION_SQL, sources))).mappings() if _in_check(row, cfg, start)]
    session_ids = {sid for row in rows for sid in session_ids_in(row["sgtxt"]).valid}
    request_ids = {ref.request_id for row in rows for ref in request_refs_in(row["sgtxt"]).valid}
    sessions = load_sessions(db, session_ids) if session_ids else {}
    request_lines = load_request_lines(db, request_ids) if request_ids else {}
    found = unreferenced_lines(rows, sessions, request_lines, cfg, start)
    logger.info(
        "I08 job lines with no R-reference: %d of %d checked from %s",
        len(found),
        len(rows),
        start.isoformat(),
    )
    return found
