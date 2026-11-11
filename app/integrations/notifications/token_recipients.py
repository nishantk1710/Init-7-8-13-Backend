"""Recipient rows kept in step with the roles Entra puts in each sign-in token.

Entra is the source of truth for who holds a role, but the backend cannot read the
directory (no Graph permission has been granted), so it learns from the one thing it
is handed on every request: the validated token. :func:`remember` is called next to
``user_directory.remember`` on every validated sign-in (``app.core.security``) and
keeps ``notification_recipients`` in step with the token's ``Spares.*`` roles. It
never slows or fails a request: an in-process memory skips a person whose roles were
synced recently, and the database write runs on a background thread whose errors
are logged and dropped.

The rules, applied per person (``user_oid``) and role:

* **Role in the token, no active row** -- rows this module itself deactivated earlier
  are switched back on (their plant is kept); when there are none, one all-plant row
  is added with ``created_by = SOURCE``. A row an administrator retired
  (``active = false`` with ``updated_by != SOURCE``) is an administrator's decision:
  it is never switched back on here, and no new row is added beside it.
* **Role no longer in the token** -- every active row for that person and role is
  deactivated with ``updated_by = SOURCE``, whoever created it: a person who no
  longer holds the role must not be notified for it.
* **Roles the app does not know** (``security.USER_ROLES``) and rows that belong to
  other people are left alone.

Plant is not in the token, so a row added here applies to every plant. An
administrator can still add plant-specific rows through the admin API; they are left
alone while the role is held. Someone who never signs in is never added here: they
could not act on a notification anyway, and ``docs/notifications.md`` says how to
add them by hand. Every change writes a ``notification_config_audit`` row with
``actor = SOURCE``, the same as a change made through the admin API.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.integrations.notifications.config_store import (
    RECIPIENTS_TABLE,
    describe,
    record_audit,
)
from app.models.notification import NotificationRecipient

if TYPE_CHECKING:
    from app.core.security import CurrentUser

logger = get_logger(__name__)

#: Written to ``created_by`` / ``updated_by`` / audit ``actor`` for every change made here.
SOURCE = "entra-token"

#: How long a person's roles, once synced, are not checked again by this process
#: unless the roles in their token change.
RECHECK_SECONDS = 6 * 3600

_seen: dict[str, tuple[frozenset[str], float]] = {}
_seen_lock = threading.Lock()
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="token-recipients")


@dataclass(frozen=True)
class SyncResult:
    added: int = 0
    reactivated: int = 0
    deactivated: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.reactivated or self.deactivated)


def _known_roles() -> frozenset[str]:
    from app.core.security import USER_ROLES

    return USER_ROLES


def sync_roles(db: Session, oid: str, roles: Iterable[str]) -> SyncResult:
    """Bring ``notification_recipients`` for ``oid`` in step with ``roles``. Commits when anything changed."""
    held = frozenset(roles) & _known_roles()
    rows = list(
        db.scalars(
            select(NotificationRecipient)
            .where(NotificationRecipient.user_oid == oid)
            .order_by(NotificationRecipient.id)
        )
    )
    added = reactivated = deactivated = 0

    for role in sorted(held):
        mine = [row for row in rows if row.role == role]
        if any(row.active for row in mine):
            continue
        retired_by_admin = [row for row in mine if row.updated_by != SOURCE]
        if retired_by_admin:
            # An administrator switched this person off for this role; their decision stands.
            continue
        if mine:
            for row in mine:
                row.active = True
                row.updated_by = SOURCE
                record_audit(db, RECIPIENTS_TABLE, row.id, "UPDATE", SOURCE, describe(row, _FIELDS))
                reactivated += 1
            continue
        row = NotificationRecipient(
            role=role, plant=None, user_oid=oid, active=True, created_by=SOURCE, updated_by=SOURCE
        )
        db.add(row)
        db.flush()
        record_audit(db, RECIPIENTS_TABLE, row.id, "CREATE", SOURCE, describe(row, _FIELDS))
        added += 1

    for row in rows:
        if row.active and row.role in _known_roles() and row.role not in held:
            row.active = False
            row.updated_by = SOURCE
            record_audit(db, RECIPIENTS_TABLE, row.id, "UPDATE", SOURCE, describe(row, _FIELDS))
            deactivated += 1

    result = SyncResult(added=added, reactivated=reactivated, deactivated=deactivated)
    if result.changed:
        db.commit()
        logger.info(
            "token recipients: %s -- added %d, reactivated %d, deactivated %d",
            oid, result.added, result.reactivated, result.deactivated,
        )
    return result


_FIELDS = ("plant", "role", "user_oid", "email", "active")


def _default_lock(name: str):
    """The SQL Server application lock the schedulers use, or ``None`` where there is nothing to serialise.

    A person's first page load after a deploy fans out over both gunicorn workers, and each has its
    own memory of who was synced; without the lock both would read "no row yet" and both would add
    one. Development runs one process on another database, so the lock is skipped there.
    """
    from app.core.db import get_engine

    if get_engine().dialect.name != "mssql":
        return None
    from app.ingest.scheduler import _Lock

    return _Lock(name)


def _sync_in_background(oid: str, roles: frozenset[str]) -> None:
    lock = None
    acquired = False
    try:
        lock = _default_lock(f"spares_ai_token_recipients:{oid}")
        if lock is not None:
            acquired = lock.acquire()
            if not acquired:
                # Another worker is syncing this person right now. Forget them here, so the next
                # request on this worker looks again and finds the rows already in place.
                with _seen_lock:
                    _seen.pop(oid, None)
                return
        from app.core.db import get_sessionmaker

        with get_sessionmaker()() as db:
            sync_roles(db, oid, roles)
    except Exception:  # noqa: BLE001 -- a recipient sync must never matter to the request
        logger.warning("token recipients: could not sync %s", oid, exc_info=True)
        with _seen_lock:
            _seen.pop(oid, None)  # try again on the next request
    finally:
        if lock is not None and acquired:
            with contextlib.suppress(Exception):
                lock.release()


def remember(user: CurrentUser) -> None:
    """Sync ``user``'s roles if this process has not recently done so for the same roles. Never raises."""
    settings = get_settings()
    if not settings.database_url or not settings.notification_recipients_from_token:
        return
    if user.is_application or not user.authenticated:
        return
    from app.integrations.notifications.resolver import is_entra_object_id

    if not is_entra_object_id(user.oid):
        return
    roles = frozenset(user.roles)
    now = time.monotonic()
    with _seen_lock:
        seen = _seen.get(user.oid)
        if seen is not None and seen[0] == roles and now - seen[1] < RECHECK_SECONDS:
            return
        _seen[user.oid] = (roles, now)
    try:
        _writer.submit(_sync_in_background, user.oid, roles)
    except RuntimeError:  # the executor is shut down at interpreter exit
        pass


def forget_all() -> None:
    """Clear the per-process memory (tests)."""
    with _seen_lock:
        _seen.clear()
