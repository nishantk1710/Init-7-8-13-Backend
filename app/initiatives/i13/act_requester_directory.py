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
