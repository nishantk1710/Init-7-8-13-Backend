"""Who a notification goes to.

Two ways in:

* :func:`resolve_role` -- "the Engineering Head at plant 1300". Looks in the
  ``notification_recipients`` table, which an administrator maintains.
* :func:`resolve_user` -- one named person: the submitter of a recommendation,
  an exception's requester, a configured HOD.

A person is always an **Entra object id** (a GUID). Anything else -- an email
address, a name, a placeholder -- is refused as an invalid identity: an identity
that came from a request is never turned into an address to send to. A person's
email comes from ``app_user`` (filled in when they sign in), from the
administrator-maintained recipient row, or from the configured fallback address.

Nothing is guessed, and nothing is skipped silently. When nobody can be found
the result is a ``Recipient`` with ``error`` set; when the person is known but
has no email it carries ``email_error``. The service records both as failed
notifications with the reason.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.user_directory import names_for
from app.integrations.notifications.models import Recipient
from app.models.notification import NotificationRecipient

_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: Failure reasons, one per cause, so a reader can tell them apart.
MISSING_IDENTITY = "missing identity: no recipient was given"
INVALID_IDENTITY = "invalid identity: not an Entra object id"
UNKNOWN_USER = "unknown user: has not signed in, so no email address is on record"
NO_EMAIL = "no email address on record for this user"


def is_entra_object_id(value: str | None) -> bool:
    """True for a GUID in the 8-4-4-4-12 form Entra uses for an object id."""
    return bool(value) and _OBJECT_ID.match(value) is not None


def _with_email(
    oid: str, email: str | None, no_email_reason: str, *, role: str | None, plant: str | None, fallback_email: str
) -> Recipient:
    """A known person: their own address, else the fallback, else a recorded reason."""
    if email:
        return Recipient(user_oid=oid, email=email, role=role, plant=plant)
    if fallback_email:
        return Recipient(user_oid=oid, email=fallback_email, role=role, plant=plant)
    return Recipient(user_oid=oid, role=role, plant=plant, email_error=no_email_reason)


def resolve_role(db: Session, role: str, plant: str | None, *, fallback_email: str = "") -> list[Recipient]:
    """Everyone holding ``role`` at ``plant``, or the fallback, or one error.

    A row with no plant applies to every plant. Several people may hold a role.
    """
    rows = list(
        db.scalars(
            select(NotificationRecipient)
            .where(NotificationRecipient.role == role, NotificationRecipient.active.is_(True))
            .order_by(NotificationRecipient.id)
        )
    )
    rows = [row for row in rows if row.plant in (None, "", plant)]

    known = names_for(db, [row.user_oid for row in rows if row.user_oid])
    found: list[Recipient] = []
    seen: set[tuple[str | None, str | None]] = set()
    for row in rows:
        if row.user_oid and not is_entra_object_id(row.user_oid):
            found.append(Recipient(role=role, plant=plant, error=f"recipient row {row.id} for {role}: {INVALID_IDENTITY}"))
            continue
        if not row.user_oid and not row.email:
            found.append(
                Recipient(role=role, plant=plant, error=f"recipient row {row.id} for {role} has neither a user nor an email")
            )
            continue
        if row.user_oid:
            on_record = known.get(row.user_oid, ("", ""))[1]
            reason = NO_EMAIL if row.user_oid in known else UNKNOWN_USER
            recipient = _with_email(
                row.user_oid, on_record or row.email, reason, role=role, plant=plant, fallback_email=fallback_email
            )
        else:
            recipient = Recipient(email=row.email, role=role, plant=plant)
        key = (recipient.user_oid, recipient.email)
        if key not in seen:
            seen.add(key)
            found.append(recipient)
    if any(r.error is None for r in found):
        return found

    if fallback_email:
        return [Recipient(email=fallback_email, role=role, plant=plant)] + found
    return found or [Recipient(role=role, plant=plant, error=f"no recipient configured for role {role} at plant {plant or '-'}")]


def resolve_user(
    db: Session,
    identity: str | None,
    *,
    role: str | None = None,
    plant: str | None = None,
    fallback_email: str = "",
) -> list[Recipient]:
    """One named person, by Entra object id.

    * no identity            -> error (``missing identity``)
    * not an object id       -> error (``invalid identity``); an email address is never accepted here
    * a known user           -> their email from ``app_user``
    * unknown / no email     -> the fallback address if one is configured, else ``email_error``
      saying which of the two it was. The person's in-app notification is unaffected.
    """
    identity = (identity or "").strip()
    if not identity:
        return [Recipient(role=role, plant=plant, error=MISSING_IDENTITY)]
    if not is_entra_object_id(identity):
        return [Recipient(role=role, plant=plant, error=INVALID_IDENTITY)]
    known = names_for(db, [identity]).get(identity)
    if known is None:
        return [_with_email(identity, None, UNKNOWN_USER, role=role, plant=plant, fallback_email=fallback_email)]
    return [_with_email(identity, known[1], NO_EMAIL, role=role, plant=plant, fallback_email=fallback_email)]
