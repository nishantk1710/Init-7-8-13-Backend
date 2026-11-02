"""FR-11 notifications: log one per exception, the first time it opens.

FR-11 asks for exceptions to be notified "through the exception queue and
email". This module writes the log of what should be sent; the email sender
comes later and reads the same rows (``emailed_at`` is reserved for it).

Why a write on a GET path
-------------------------
:func:`log_new` is called from ``GET /api/i8/exceptions``, beside
``auto_resolve``, for the same reason that one is: the items are not stored,
they are recomputed on every read, so the read is the only moment the platform
learns that a finding exists. There is no PUT/PATCH/POST behind it, it writes
only to a table this platform owns (``i8_notification``), and it is idempotent
-- one row per exception id, ever -- so re-reading the queue adds nothing.
``tests/test_write_paths.py`` inventories non-GET routes and is unchanged.

Never raises into the caller. The queue must still load if this table is not
there yet (a deploy where the code lands before the migration runs) or the
insert fails for any other reason; a missed notification is logged and the
next read tries again.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.exceptions import ExceptionItem, ExceptionType
from app.initiatives.i8.models import Notification
from app.integrations.notifications import templates
from app.integrations.notifications.models import NotificationEvent
from app.integrations.notifications.resolver import resolve_role
from app.integrations.notifications.service import enqueue
from app.models.notification import NotificationRow

logger = logging.getLogger(__name__)

#: The only channel there is. Named on the row so a second one does not need
#: a migration to say which rows it owns.
EMAIL_CHANNEL = "EMAIL"

#: How many ids go into one ``IN`` list. SQL Server allows 2,100 parameters per
#: statement, and the queue raises thousands of items.
_ID_CHUNK = 1000


def _notifiable(items: Iterable[ExceptionItem], cfg: I8Settings) -> dict[str, ExceptionItem]:
    """Actionable items of a configured type, keyed by id (first wins)."""
    types = set(cfg.notification_type_list)
    wanted: dict[str, ExceptionItem] = {}
    for item in items:
        # An item raised before its control existed is history, not news: it
        # explains a gap by when the line was raised, so nobody is asked to act.
        if item.pre_automation or item.type not in types:
            continue
        wanted.setdefault(item.id, item)
    return wanted


def _already_logged(db: Session, ids: list[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        found.update(
            db.scalars(
                select(Notification.exception_id).where(
                    Notification.exception_id.in_(chunk)
                )
            )
        )
    return found


def log_new(db: Session, items: Iterable[ExceptionItem], cfg: I8Settings) -> int:
    """Log a notification for each actionable, configured item not yet logged.

    Returns how many rows were written to the log; 0 on any database error, which is
    logged as a warning and rolled back rather than raised.

    Writes nothing while the complete flow is off (``I8_COMPLETE_FLOW_ENABLED``,
    29-Oct-2026): the log came with it.

    Shared notifications (the inbox and email) are queued in the *same* transaction as
    the log rows, inside a savepoint. If queuing them fails, the log is still saved and
    the shared notifications are simply missing -- and the next call finds exactly those:
    any current, configured item that is logged but has no shared notification is
    queued then. So nothing is lost to a failure, and nothing is queued twice (each
    notification has an idempotency key).
    """
    if not cfg.complete_flow_enabled:
        return 0
    wanted = _notifiable(items, cfg)
    if not wanted:
        return 0
    roles = _roles_by_type(get_settings().i8_notification_recipient_roles)
    try:
        logged = _already_logged(db, list(wanted))
        new_ids = [i for i in wanted if i not in logged]
        for exception_id in new_ids:
            item = wanted[exception_id]
            db.add(
                Notification(
                    exception_id=item.id,
                    exception_type=item.type,
                    severity=item.severity,
                    title=item.title,
                    material_id=item.material_id,
                    plant=item.plant,
                    channel=EMAIL_CHANNEL,
                )
            )
        to_notify = list(new_ids)
        if roles:
            to_notify += _without_shared_notification(db, [i for i in wanted if i in logged and wanted[i].type in roles])
        queued = _queue_shared(db, [wanted[i] for i in to_notify], roles) if to_notify else 0
        if not new_ids and not queued:
            return 0
        db.commit()
    except Exception:  # noqa: BLE001 -- the queue must load regardless
        logger.warning("I08 notifications: could not log new notifications", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
    if new_ids:
        logger.info("I08 notifications: %d new notification(s) logged", len(new_ids))
    return len(new_ids)


def _roles_by_type(raw: str) -> dict[str, list[str]]:
    """``"OVERDUE_REPAIR:Spares.Stores,MISSING_ATTESTATION:Spares.Stores"`` -> type -> roles."""
    mapping: dict[str, list[str]] = {}
    for pair in raw.split(","):
        exception_type, _, role = pair.partition(":")
        if exception_type.strip() and role.strip():
            mapping.setdefault(exception_type.strip().upper(), []).append(role.strip())
    return mapping


#: At most this many already-logged items are queued per call when catching up (items logged
#: before the roles were configured, or whose earlier queuing failed), so a large backlog is
#: worked through a read at a time instead of stalling one.
RECOVERY_BATCH = 200


def _without_shared_notification(db: Session, ids: list[str]) -> list[str]:
    """The ids, from those already logged, that have no shared notification yet (at most
    :data:`RECOVERY_BATCH`, in the order given)."""
    have: set[str] = set()
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        have.update(
            db.scalars(
                select(NotificationRow.entity_id).where(
                    NotificationRow.module == "I08", NotificationRow.entity_id.in_(chunk)
                )
            )
        )
    missing = [i for i in ids if i not in have]
    if len(missing) > RECOVERY_BATCH:
        logger.info("I08 notifications: %d logged item(s) still need a shared notification; queuing %d now", len(missing), RECOVERY_BATCH)
    return missing[:RECOVERY_BATCH]


def _queue_shared(db: Session, items: list[ExceptionItem], roles: dict[str, list[str]]) -> int:
    """Queue the shared notification for each item. Returns how many rows were created; never raises.

    Who is told comes from ``I8_NOTIFICATION_RECIPIENT_ROLES``; with nothing configured no one is
    guessed. Runs in a savepoint, so a failure rolls back only these notifications and leaves the
    caller's log rows intact. It is logged, and these items are found and queued again on the next call.
    """
    if not roles:
        logger.info("I08 notifications: I8_NOTIFICATION_RECIPIENT_ROLES is empty; no shared notification queued "
                    "(logged items are queued once roles are configured)")
        return 0
    settings = get_settings()
    created = 0
    try:
        with db.begin_nested():
            holders: dict[tuple[str, str | None], list] = {}
            for item in items:
                recipients = []
                for role in roles.get(item.type, []):
                    key = (role, item.plant)
                    if key not in holders:
                        holders[key] = resolve_role(db, role, item.plant, fallback_email=settings.notification_fallback_email)
                    recipients += holders[key]
                if not recipients:
                    continue
                created += len(
                    enqueue(
                        db,
                        NotificationEvent(
                            module="I08",
                            event_type=templates.EXCEPTION_ACTION_REQUIRED,
                            entity_type="EXCEPTION",
                            entity_id=item.id,
                            trigger_id="raised",
                            action_path=f"/repairable-spares/exceptions?id={quote(item.id, safe='')}",
                            plant=item.plant,
                            context={"summary": f"{item.title}. {item.detail}", "material": item.material_id},
                        ),
                        recipients,
                    )
                )
    except Exception as exc:  # noqa: BLE001 -- the exception queue must load regardless
        logger.warning(
            "I08 notifications: could not queue %d shared notification(s) (%s); they stay logged "
            "and are queued again on the next read",
            len(items), type(exc).__name__,
        )
        logger.debug("I08 shared notification failure detail", exc_info=True)
        return 0
    return created


#: The checks whose exception id ends in the repair line it is about, in the
#: register's form (``{EBELN}-{EBELP}`` or ``PR-{BANFN}-{BNFPO}``). A bought-new
#: finding's id names the purchase instead, and the declaration and session
#: checks name no repair line at all.
_ID_NAMES_REPAIR_LINE = frozenset(
    {
        ExceptionType.MISSING_ATTESTATION.value,
        ExceptionType.OVERDUE_REPAIR.value,
        ExceptionType.REPAIRED_AGAINST_ASSESSMENT.value,
        ExceptionType.REPAIR_PR_NOT_DECLARED.value,
    }
)


def repair_line_of(row: Notification, current: ExceptionItem | None = None) -> str | None:
    """The repair line a logged notification is about, or None.

    From the exception as the queue raises it now, when it still does; else
    read back from the exception id, for the checks whose id carries it.
    """
    if current is not None:
        return current.repair_reference if current.purchasing_document else None
    prefix = f"EX-{row.exception_type}-"
    if row.exception_type in _ID_NAMES_REPAIR_LINE and row.exception_id.startswith(prefix):
        return row.exception_id[len(prefix):] or None
    return None


def list_notifications(
    db: Session,
    *,
    page: int,
    page_size: int,
    exception_type: str | None = None,
) -> tuple[list[Notification], int, list[str]]:
    """One page of the log, newest first, with the total and the types logged."""
    query = select(Notification)
    count = select(func.count()).select_from(Notification)
    if exception_type:
        query = query.where(Notification.exception_type == exception_type)
        count = count.where(Notification.exception_type == exception_type)
    total = db.scalar(count) or 0
    rows = list(
        db.scalars(
            query.order_by(Notification.logged_at.desc(), Notification.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    types = sorted(db.scalars(select(Notification.exception_type).distinct()))
    return rows, total, types
