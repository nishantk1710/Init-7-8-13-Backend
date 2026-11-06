"""Which Entra user a SAP requester name stands for, so the requester can be notified.

The requester on an OAR exception comes from the reservation, so it is usually a SAP name such as
``Darren``. The shared notification service only accepts an Entra object id as a person, and refuses a name
(see ``app.integrations.notifications.resolver``), so without a mapping the requester's "Confirm Exception"
notification is recorded as a failure and never reaches an inbox.

``Settings.i13_requester_recipients`` is that mapping: comma-separated ``SAP name:Entra object id`` pairs, the
same convention as ``I13_HOD_RECIPIENTS``. Nothing is guessed: a requester who is not in it, and not already an
object id, is left as it is and fails visibly with the usual "invalid identity" reason.
"""

from __future__ import annotations

from collections.abc import Mapping


def named_requesters_by_session(db, aliases: Mapping[str, str] | None) -> dict[str, str]:
    """Session id -> the person named in the assistant's Requester box, where that name is a known alias.

    ``requested_for`` ("who the parts are for") is free text and never identity, so it only counts when an
    administrator has listed the name in ``I13_REQUESTER_RECIPIENTS`` (matched ignoring case and surrounding
    spaces). The value is the name as typed, trimmed: the notification service maps it to the person's identity
    with the same aliases. A session naming nobody, or somebody unknown, is simply absent, so its exceptions
    stay with the person who operated the assistant.
    """
    if not aliases:
        return {}
    from sqlalchemy import func, select

    from app.assistant.models import AssistantSession

    rows = db.execute(
        select(AssistantSession.id, AssistantSession.requested_for).where(
            func.lower(func.trim(AssistantSession.requested_for)).in_(sorted(aliases))
        )
    ).all()
    return {session_id: named.strip() for session_id, named in rows if named and named.strip()}


def parse_requester_recipients(raw: str) -> dict[str, str]:
    """``"Darren:<oid>,Jaco Zandber:<oid>"`` -> lower-cased SAP name -> identity.

    Malformed entries (no ``:``, or an empty side) are skipped, never guessed at.
    """
    mapping: dict[str, str] = {}
    for pair in (raw or "").split(","):
        name, _, identity = pair.strip().rpartition(":")
        name, identity = name.strip().lower(), identity.strip()
        if name and identity:
            mapping[name] = identity
    return mapping
