"""One-off: remove the Quantity overrides raised before their reservation existed.

Until 25-Nov-2026 an assistant quantity override was raised the moment the
request was submitted. Since then it is an exception only once SAP links a
reservation to the request line (``act.service.detect_exceptions``). The ones
raised earlier whose line has no linked reservation are not removed by
detection -- it leaves unlinked overrides alone, because a closed exception
never reopens and so would never come back once the reservation links.

This removes them, with their history, confirmations and notification
attempts, so detection raises each again (with the same reason, from the
assistant) when its reservation links. An override whose line is already linked
is kept, and detection fills its reservation in. Exceptions from the older
quantity-suggestion API, which carry their own reservation, are not touched.

Lists only, unless ``--apply`` is given::

    python -m app.initiatives.i13.act_unlinked_overrides
    python -m app.initiatives.i13.act_unlinked_overrides --apply
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.i13_act_exception import (
    ActConfirmationRecord,
    ActExceptionEventRecord,
    ActExceptionRecord,
    ActNotificationRecord,
)
from app.models.i13_session_link import SessionReservationLink


def unlinked_overrides(db: Session) -> list[ActExceptionRecord]:
    """Quantity overrides with no reservation, for a session SAP has not linked."""
    linked = select(SessionReservationLink.session_id)
    return list(
        db.execute(
            select(ActExceptionRecord)
            .where(
                ActExceptionRecord.exception_type == "QUANTITY_OVERRIDE",
                ActExceptionRecord.reservation_number.is_(None),
                ActExceptionRecord.session_id.is_not(None),
                ActExceptionRecord.session_id.not_in(linked),
            )
            .order_by(ActExceptionRecord.detected_at)
        ).scalars()
    )


def remove(db: Session, exception_ids: list[str]) -> None:
    """Delete the exceptions and every row that points at them. The caller commits."""
    if not exception_ids:
        return
    for child in (ActExceptionEventRecord, ActConfirmationRecord, ActNotificationRecord):
        db.execute(delete(child).where(child.exception_id.in_(exception_ids)))
    db.execute(delete(ActExceptionRecord).where(ActExceptionRecord.exception_id.in_(exception_ids)))


def main(argv: list[str] | None = None) -> int:
    from app.core.db import get_sessionmaker

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete them (default: list only)")
    args = parser.parse_args(argv)

    with get_sessionmaker()() as db:
        rows = unlinked_overrides(db)
        for row in rows:
            print(f"{row.exception_id}  {row.status:<20} {row.material}/{row.plant}  detected {row.detected_at:%Y-%m-%d}")
        print(f"{len(rows)} Quantity override(s) with no linked reservation.")
        if not args.apply:
            print("Nothing changed. Run again with --apply to remove them.")
            return 0
        remove(db, [row.exception_id for row in rows])
        db.commit()
        print(f"Removed {len(rows)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
