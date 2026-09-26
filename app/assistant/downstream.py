"""What an I13 capture changes elsewhere, applied as soon as it is recorded.

A completed I13 session writes a consumption plan, a quantity decision and
sometimes an override justification (``app.assistant.turns``). Before this
module, none of that reached the OAR dashboards until someone ran a full
refresh and a manual detection run -- neither of which anything triggered
(plan gaps G1, G6). So the moment a session completes, for that one
material-plant:

1. **WATCH** -- the I13 snapshot's row is recomputed with the new plan, so
   acquired-vs-plan and plan status move on the WATCH screen and the
   dashboard (``snapshot.refresh_key``);
2. **ACT exceptions** -- detection runs scoped to the material-plant, so a
   NO_PLAN exception the plan now covers resolves, and a kept override raises
   QUANTITY_OVERRIDE (``act_runner.run_detection``).

The summary's plan-dependent counts need nothing here: they are recomputed
from the live plans on the next request.

Both steps are scoped, so they cost about what the assistant's own assessment
costs. **Neither may fail the conversation**: the requester's answer is already
committed, and a failure here is logged and left for the next detection run to
repair -- it is never reported to them as their reservation failing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session as DbSession

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _confirm_override_from_conversation(db: DbSession, session_id: str) -> bool:
    """Record the chat's override reason as the requester's answer to the
    QUANTITY_OVERRIDE exception detection just raised for this session.

    Without this, a requester who kept their own quantity and explained why in
    the conversation found the same exception on the Exceptions screen,
    AWAITING_REQUESTER, asking them for that explanation a second time. The
    reason was already on file -- it was carried only as evidence text.

    Goes through ``submit_confirmation`` (state-machine validated, audited as
    REQUESTER_CONFIRMED + JUSTIFICATION_ADDED), exactly as if they had answered
    on the Exceptions screen, and only when the exception is waiting on them.
    """
    from sqlalchemy import select

    from app.assistant.models import AssistantSession, Justification
    from app.initiatives.i13.act.detection import build_exception_id
    from app.initiatives.i13.act.domain import ExceptionStatus
    from app.initiatives.i13.act.service import submit_confirmation
    from app.initiatives.i13.act_exception_store import SqlExceptionRepository

    justification = db.execute(
        select(Justification)
        .where(Justification.session_id == session_id, Justification.kind == "QUANTITY_OVERRIDE")
        .order_by(Justification.recorded_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if justification is None:
        return False

    repository = SqlExceptionRepository(db)
    exception_id = build_exception_id("QUANTITY_OVERRIDE", f"SESSION-{session_id}")
    exception = repository.get(exception_id)
    if exception is None or exception.status is not ExceptionStatus.AWAITING_REQUESTER:
        return False

    session = db.get(AssistantSession, session_id)
    submit_confirmation(
        exception_id=exception_id,
        reason_category=justification.reason_category,
        free_text=justification.free_text,
        actor_id=session.requester if session is not None else justification.author,
        as_of_time=datetime.now(timezone.utc),
        repository=repository,
    )
    return True


def apply_capture(
    db: DbSession, *, material: str, plant: str, session_id: str | None = None
) -> dict[str, object]:
    """Refresh WATCH and run scoped detection for one material-plant, then
    carry the conversation's override reason onto the exception it raised.

    Returns what happened, for the log and for tests. Commits its own work.
    """
    from app.initiatives.i13.act_runner import run_detection
    from app.initiatives.i13.config import get_i13_config
    from app.initiatives.i13.snapshot import peek_i13_snapshot, refresh_key

    settings = get_settings()
    outcome: dict[str, object] = {"watch_refreshed": False, "detection": None}

    try:
        outcome["watch_refreshed"] = refresh_key(db, material, plant)
    except Exception:  # noqa: BLE001 -- see module docstring
        logger.exception("Post-capture WATCH refresh failed for %s/%s", material, plant)
        db.rollback()

    try:
        result = run_detection(
            db,
            get_i13_config(),
            Path(settings.i13_data_dir),
            as_of_time=datetime.now(timezone.utc),
            material=material,
            plant=plant,
            snapshot=peek_i13_snapshot() if settings.i13_snapshot_enabled else None,
        )
        db.commit()
        outcome["detection"] = result
        logger.info("Post-capture detection for %s/%s: %s", material, plant, result)
    except Exception:  # noqa: BLE001 -- see module docstring
        logger.exception("Post-capture detection failed for %s/%s", material, plant)
        db.rollback()

    outcome["override_confirmed"] = False
    if session_id is not None:
        try:
            outcome["override_confirmed"] = _confirm_override_from_conversation(db, session_id)
            db.commit()
        except Exception:  # noqa: BLE001 -- see module docstring
            logger.exception("Carrying the override reason onto the exception failed for %s", session_id)
            db.rollback()

    return outcome
