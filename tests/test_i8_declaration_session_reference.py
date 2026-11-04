"""The register quotes a declaration's session as the requester saw it.

A declaration made on a line of a request was made in that line's session, whose
ID is internal: the requester only ever saw ``R.../n``. An older
single-material session keeps its own ID. In-memory SQLite, no Azure database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.i8.router import _with_session_references
from app.api.i8.schemas import RepairChain
from app.assistant.models import AssistantSession
from app.models.base import Base

T0 = datetime(2026, 11, 4, tzinfo=timezone.utc)


def session(sid: str, *, request_id: str | None = None, line_no: int | None = None) -> AssistantSession:
    return AssistantSession(
        id=sid,
        flow="i08",
        material_id=f"800000459{line_no or 0}",
        plant="1300",
        eighty_series=True,
        material_scope="EXCLUDED",
        routing_reason="80-series",
        requester="UNAUTHENTICATED-FRONTEND",
        origin="PLATFORM",
        issued_at=T0,
        expires_at=T0 + timedelta(days=1),
        assessment="{}",
        request_id=request_id,
        line_no=line_no,
    )


def chain(session_id: str | None) -> RepairChain:
    return RepairChain.model_construct(declaration_session_id=session_id)


def test_request_line_old_session_and_none():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[AssistantSession.__table__])
    with sessionmaker(bind=engine)() as db:
        db.add_all([session("SLINE2", request_id="RREQ01", line_no=2), session("SOLD")])
        db.commit()

        rows = _with_session_references(
            db, [chain("SLINE2"), chain("SOLD"), chain("SGONE"), chain(None)]
        )

    assert [r.declaration_session_reference for r in rows] == ["RREQ01/2", "SOLD", "SGONE", None]
    assert rows[0].declaration_session_id == "SLINE2"  # the link still opens the line
