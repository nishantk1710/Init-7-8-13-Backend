"""The R-reference typed on a repair order, read back as a link to a session.

Since 27-Oct-2026 (phase 3 of the complete-flow plan). The stores clerk types
the assistant's reference -- a session ID, or a multi-material request ID --
into the repair order's line text (RESB.SGTXT) when the 543 line is raised.
On the 15-Oct-2026 extract SGTXT was free on all 6,282 repair lines, so
nothing else competes for it. Read back, it says which assistant visit the
repair came from, and the declaration recorded in that visit then covers the
repair PR -- and the PO ordered from it -- with no date window to guess.

I13 already reads the same text off the maintenance reservations, so its
parsers are reused as they are (``app.initiatives.i13.session_link``): a
checksum-valid ID only, and only when its session -- or, for a request, the
request's line for this material and plant -- is for the same part as the
repair order's line. A session for another part in someone's notes is not a
link, exactly as I13 rules it.

The result is plain tuples ``(BANFN, BNFPO, session_id, reference)`` so the
snapshot stores it without a new class (codec note, version 5).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from sqlalchemy.orm import Session

from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.requisitions import is_repair_reservation_line
from app.initiatives.i13.session_link import (
    RequestLine,
    SessionInfo,
    load_request_lines,
    load_sessions,
    request_refs_in,
    session_ids_in,
)

logger = logging.getLogger(__name__)

#: (BANFN, BNFPO, session_id, the reference as it was found).
RepairReference = tuple[str, str, str, str]


def _part(material: str | None, plant: str | None) -> tuple[str, str]:
    """A material+plant pair as both sides compare it: the register strips a
    numeric material's leading zeros and the assistant may not."""
    material = (material or "").strip()
    return (material.lstrip("0") or material, (plant or "").strip())


def _item(item: str | None) -> str:
    item = (item or "").strip()
    return item.lstrip("0") or item


def _naming_rows(rows: Iterable[Mapping], cfg: I8Settings) -> list[Mapping]:
    """The repair orders' lines that name a repair PR and carry some text."""
    return [
        row
        for row in rows
        if is_repair_reservation_line(row, cfg) and row.get("bnfpo") and (row.get("sgtxt") or "").strip()
    ]


def sessions_named(
    text: str,
    material: str | None,
    plant: str | None,
    sessions: Mapping[str, SessionInfo],
    request_lines: Mapping[str, list[RequestLine]],
) -> list[tuple[str, str]]:
    """(session_id, reference as found) for every session a reservation
    line's text names for the line's own material and plant. Shared by the
    repair order's 543 line and, since 27-Oct-2026, the job's 261 line
    (session_jobs.py), so the two read the R-reference the same way."""
    part = _part(material, plant)
    named: list[tuple[str, str]] = []
    for session_id in session_ids_in(text).valid:
        info = sessions.get(session_id)
        if info is not None and _part(info.material, info.plant) == part:
            named.append((session_id, session_id))
    for ref in request_refs_in(text).valid:
        line = next(
            (line for line in request_lines.get(ref.request_id, ()) if _part(line.material, line.plant) == part),
            None,
        )
        if line is not None:
            named.append((line.session_id, f"{ref.request_id}/{line.line_no}"))
    return named


def resolve_repair_references(
    rows: Iterable[Mapping],
    sessions: Mapping[str, SessionInfo],
    request_lines: Mapping[str, list[RequestLine]],
    cfg: I8Settings | None = None,
) -> tuple[RepairReference, ...]:
    """Pure: every (repair PR, session) pair a repair order's text names.

    A session ID counts when that session is for the line's material and
    plant. A request ID counts through the request's line for that material
    and plant -- the ``/n`` typed after it does not decide, as in I13.
    """
    cfg = cfg or get_i8_settings()
    found: dict[tuple[str, str, str], RepairReference] = {}
    for row in _naming_rows(rows, cfg):
        banfn, bnfpo = row["banfn"], _item(row["bnfpo"])
        for session_id, ref_text in sessions_named(
            row["sgtxt"], row.get("matnr"), row.get("werks"), sessions, request_lines
        ):
            found.setdefault((banfn, bnfpo, session_id), (banfn, bnfpo, session_id, ref_text))
    return tuple(sorted(found.values()))


def load_repair_references(
    db: Session, rows: Iterable[Mapping], cfg: I8Settings | None = None
) -> tuple[RepairReference, ...]:
    """Read the sessions and requests the repair orders' text names, then
    resolve. No query at all when no line names one."""
    cfg = cfg or get_i8_settings()
    rows = _naming_rows(rows, cfg)
    session_ids = {sid for row in rows for sid in session_ids_in(row["sgtxt"]).valid}
    request_ids = {ref.request_id for row in rows for ref in request_refs_in(row["sgtxt"]).valid}
    sessions = load_sessions(db, session_ids) if session_ids else {}
    request_lines = load_request_lines(db, request_ids) if request_ids else {}
    references = resolve_repair_references(rows, sessions, request_lines, cfg)
    logger.info(
        "I08 repair-order R-references: %d repair PR links from %d lines with text",
        len(references),
        len(rows),
    )
    return references
