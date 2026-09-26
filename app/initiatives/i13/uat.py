"""UAT stand-in for "the requester typed the session ID into SAP".

The platform cannot write to SAP, so in UAT nobody can put a session ID into a
reservation's item text (SGTXT), and a reservation created after the
conversation does not exist in the loaded extract. This module fills that gap
-- **only** while ``I13_UAT_SIMULATION_ENABLED`` is on -- by writing to
``uat_reservation_sgtxt``, which ``postgres_reservation`` overlays on
``raw_resb`` (never modified):

* :func:`simulate` -- a reservation the requester would have created in SAP for
  the session's material and plant, numbered from a range no SAP reservation
  uses (99xxxxxx), with SGTXT = the session ID;
* :func:`stamp` -- the session ID typed into an existing reservation's SGTXT;
* :func:`remove` -- undo either.

After each, the material is refreshed in the I13 snapshot (ledger, WATCH,
session links) and detection runs for it, exactly as a new extract and a
detection run would do -- so what UAT then sees on the screens is what the real
flow will produce.

A simulated reservation stops at the reservation stage: nothing here invents a
purchase order, goods receipt or goods issue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.integrations.sap.postgres_reservation import fetch_reservations
from app.models.i13_session_link import UatReservationSgtxt

logger = get_logger(__name__)

#: SAP reservation numbers in this extract are 7 digits; 99xxxxxx cannot collide.
SIMULATED_RANGE_START = 99_000_001


class UatError(Exception):
    """A UAT request that cannot be honoured, with a sentence saying why."""


class UatConflict(UatError):
    pass


@dataclass(frozen=True)
class _Session:
    session_id: str
    material: str
    plant: str
    flow: str


def _session(db: Session, session_id: str) -> _Session:
    from app.assistant.models import AssistantSession

    row = db.get(AssistantSession, session_id.strip().upper())
    if row is None:
        raise UatError(f"No assistant session {session_id!r}.")
    return _Session(row.id, row.material_id, row.plant, row.flow)


def _latest_plan(db: Session, session_id: str):
    from app.assistant.models import ConsumptionPlanRecord

    return db.execute(
        select(ConsumptionPlanRecord)
        .where(ConsumptionPlanRecord.session_id == session_id)
        .order_by(ConsumptionPlanRecord.captured_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _apply(db: Session, material: str, plant: str) -> None:
    """What a new extract plus a detection run would do, for one material."""
    from app.initiatives.i13.act_runner import run_detection
    from app.initiatives.i13.config import get_i13_config
    from app.initiatives.i13.session_link import sync_links
    from app.initiatives.i13.snapshot import peek_i13_snapshot, refresh_material

    settings = get_settings()
    snapshot = peek_i13_snapshot() if settings.i13_snapshot_enabled else None
    if snapshot is not None:
        refresh_material(db, material, plant, reservations=True)
        snapshot = peek_i13_snapshot()
    else:
        sync_links(db, fetch_reservations(db, material=material, plant=plant), scope=(material, plant))
    db.flush()
    run_detection(
        db,
        get_i13_config(),
        Path(settings.i13_data_dir),
        as_of_time=datetime.now(timezone.utc),
        material=material,
        plant=plant,
        snapshot=snapshot,
    )


def simulate(db: Session, session_id: str, *, actor: str) -> UatReservationSgtxt:
    """Create the reservation this session's requester would have created in SAP."""
    session = _session(db, session_id)
    existing = db.execute(
        select(UatReservationSgtxt).where(
            UatReservationSgtxt.session_id == session.session_id, UatReservationSgtxt.simulated
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise UatConflict(
            f"Session {session.session_id} already has simulated reservation {existing.reservation_number}. "
            "Remove it first to simulate again."
        )

    plan = _latest_plan(db, session.session_id)
    highest = db.execute(
        select(func.max(UatReservationSgtxt.reservation_number)).where(UatReservationSgtxt.simulated)
    ).scalar()
    number = max(SIMULATED_RANGE_START, int(highest) + 1 if highest else SIMULATED_RANGE_START)
    row = UatReservationSgtxt(
        reservation_number=str(number),
        reservation_item="1",
        material=session.material,
        plant=session.plant,
        simulated=True,
        # Required when the plan said it would be used; today if no plan.
        requirement_date=(plan.window_start if plan is not None and plan.window_start else date.today()),
        requirement_quantity=(plan.planned_quantity if plan is not None else Decimal("1")),
        sgtxt=session.session_id,
        session_id=session.session_id,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    _apply(db, session.material, session.plant)
    db.commit()
    logger.info("UAT: simulated reservation %s for session %s", row.reservation_number, session.session_id)
    return row


def stamp(db: Session, session_id: str, *, reservation_number: str, reservation_item: str, actor: str) -> UatReservationSgtxt:
    """Type the session ID into an existing reservation's SGTXT."""
    session = _session(db, session_id)
    matches = [
        r
        for r in fetch_reservations(db, reservation_number=reservation_number)
        if r["Rspos"] == reservation_item and not r.get("UatSimulated")
    ]
    if not matches:
        raise UatError(f"No reservation {reservation_number}/{reservation_item} in the loaded extract.")
    reservation = matches[0]
    if (reservation["Matnr"], reservation["Werks"]) != (session.material, session.plant):
        raise UatError(
            f"Reservation {reservation_number}/{reservation_item} is for {reservation['Matnr']} at "
            f"{reservation['Werks']}; session {session.session_id} is for {session.material} at {session.plant}."
        )
    already = db.execute(
        select(UatReservationSgtxt).where(
            UatReservationSgtxt.reservation_number == reservation_number,
            UatReservationSgtxt.reservation_item == reservation_item,
        )
    ).scalar_one_or_none()
    if already is not None:
        raise UatConflict(
            f"Reservation {reservation_number}/{reservation_item} is already stamped with {already.sgtxt}. "
            "Remove that first."
        )

    # The reservation's own item text, kept for display beside the stamp.
    raw_text = db.execute(
        text(
            "SELECT text FROM raw_resb WHERE reservation = :number AND item_no_stock_transfer_reserv = :item"
        ),
        {"number": reservation_number, "item": reservation_item},
    ).scalars().first()
    row = UatReservationSgtxt(
        reservation_number=reservation_number,
        reservation_item=reservation_item,
        material=session.material,
        plant=session.plant,
        simulated=False,
        requirement_date=reservation.get("Bdter"),
        requirement_quantity=reservation.get("Bdmng"),
        sgtxt=session.session_id,
        original_sgtxt=(raw_text or None),
        session_id=session.session_id,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    _apply(db, session.material, session.plant)
    db.commit()
    logger.info("UAT: stamped %s/%s with session %s", reservation_number, reservation_item, session.session_id)
    return row


def remove(db: Session, uat_id: int) -> None:
    row = db.get(UatReservationSgtxt, uat_id)
    if row is None:
        raise UatError(f"No UAT reservation {uat_id}.")
    material, plant = row.material, row.plant
    db.delete(row)
    db.flush()
    _apply(db, material, plant)
    db.commit()


def list_rows(db: Session, *, session_id: str | None = None) -> list[UatReservationSgtxt]:
    stmt = select(UatReservationSgtxt).order_by(UatReservationSgtxt.created_at.desc())
    if session_id:
        stmt = stmt.where(UatReservationSgtxt.session_id == session_id.strip().upper())
    return list(db.execute(stmt).scalars())
