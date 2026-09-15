"""Demo attestations for UAT -- and the honest reason this file has to exist.

The problem
-----------
At UAT the missing-attestation exception fires on **all 1,225 repair lines**,
because no attestation has ever existed for any historical repair. That is
correct, and it is a terrible demo: a screen where every single row is an
exception teaches nobody what the screen is for.

The task plan's answer is right -- seed a handful of attestations against real
open repair lines, clearly marked as demo-entered, and let the exception fire on
the rest. But it cannot be done through the API, and the reason is worth stating
because it is not obvious:

    An attestation's timestamp is SERVER-SET. That is the guarantee that makes
    it an audit record -- the attestor does not get to choose when they say they
    looked at the part.

    The extract is a frozen July-2026 snapshot, and its repair lines were raised
    from April 2025 onwards. So an attestation POSTed today is dated today, and
    today is between two and seventeen months outside the +/-30 day matching
    window of every line in the register. It never matches. The exception never
    clears.

Both halves of that are working as designed. The timestamp must not be
client-settable, and the window must not be widened to eighteen months to paper
over it -- a window that wide would let an assessment from a completely
different repair cycle count, which is the thing it exists to prevent.

The resolution
--------------
So demo history is manufactured *here*, in a module that is obviously not a user
path, rather than by weakening either rule:

* The API keeps its guarantee. ``POST /api/i8/attestations`` still stamps the
  server clock, always, with no override.
* This module writes rows dated to match the repair lines they are about,
  because manufacturing history is exactly what it is for and it says so.
* Every row it writes is **marked**: the attestor is ``DEMO_SEED``, and the
  evidence reference says so too. Nobody can mistake one for a real assessment,
  and :func:`clear` removes precisely those rows and nothing else.

What to say when demonstrating this
-----------------------------------
The real number, out loud: **1,225 repair lines, 0 real attestations**, because
the control did not exist before this platform. That number is the business
case. It should be reported, not hidden behind the handful of rows this file
creates.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.models import RepairAttestation, new_attestation_id
from app.initiatives.i8.register import load_repair_lines

logger = get_logger(__name__)

#: Every seeded row carries this as its attestor. It is the marker `clear()`
#: deletes on, and the reason a demo row can never be mistaken for a real one.
DEMO_ATTESTOR = "DEMO_SEED"

DEMO_EVIDENCE = "DEMO DATA -- seeded for UAT, not a real assessment"

#: Assessments written against the seeded lines, in order. Deliberately not all
#: REPAIRABLE: a queue where every declared row says the same thing shows only
#: half of what the screen does. The third produces a Flagged row -- a part
#: assessed as beyond economical repair and sent for repair anyway -- which is
#: the case the queue exists to surface.
DEMO_ASSESSMENTS: tuple[tuple[str, str, str], ...] = (
    (
        "BEARING_FAILURE",
        "REPAIRABLE",
        "Drive-end bearing collapsed. Shaft and housing measured within "
        "tolerance, unit is worth rebuilding.",
    ),
    (
        "SEAL_LEAK",
        "REPAIRABLE",
        "Mechanical seal weeping under pressure test. Faces resurfaceable.",
    ),
    (
        "IMPACT_DAMAGE",
        "BEYOND_ECONOMICAL_REPAIR",
        "Casing cracked through the mounting boss. Quoted repair exceeds "
        "replacement -- sent to vendor before this was assessed.",
    ),
    (
        "WEAR",
        "REPAIRABLE",
        "Impeller vanes eroded, hub sound. Standard refurbishment.",
    ),
    (
        "ELECTRICAL_FAULT",
        "REPAIRABLE",
        "Stator winding failed insulation resistance test. Rewind quoted.",
    ),
    (
        "CORROSION",
        "SCRAP",
        "Wall loss beyond minimum thickness across the body. Not repairable, "
        "not re-certifiable.",
    ),
)


@dataclass(frozen=True)
class SeedResult:
    created: int
    lines: list[str]
    skipped_existing: int


def seed(
    db: Session,
    *,
    count: int = 6,
    cfg: I8Settings | None = None,
) -> SeedResult:
    """Write demo attestations against real OPEN repair lines.

    Open lines on purpose: they are the ones a planner could still act on, so
    they are what a demo should be about. A cleared exception on a repair that
    came back last year proves nothing.

    Each row is dated to its repair line's own raised date, which is what makes
    it fall inside the matching window. That is manufacturing history, and it is
    why this is a seeding command and not an API call.
    """
    cfg = cfg or get_i8_settings()

    lines, _stats = load_repair_lines(db, cfg)
    candidates = [
        line
        for line in lines
        if line.is_open and line.plant is not None and line.raised_at is not None
    ]
    # Deterministic: the same lines every run, so a demo is reproducible and two
    # people comparing screens are looking at the same rows.
    candidates.sort(key=lambda line: line.key)

    existing = {
        (row.material_id, row.plant)
        for row in db.execute(
            select(RepairAttestation).where(RepairAttestation.attestor == DEMO_ATTESTOR)
        ).scalars()
    }

    created: list[str] = []
    skipped = 0
    for line in candidates:
        if len(created) >= count:
            break
        if (line.material_id, line.plant) in existing:
            skipped += 1
            continue

        fault, recommendation, description = DEMO_ASSESSMENTS[
            len(created) % len(DEMO_ASSESSMENTS)
        ]

        db.add(
            RepairAttestation(
                id=new_attestation_id(),
                material_id=line.material_id,
                plant=line.plant,
                quantity=Decimal(1),
                condition_description=description,
                fault_category=fault,
                recommendation=recommendation,
                evidence_reference=DEMO_EVIDENCE,
                attestor=DEMO_ATTESTOR,
                # Dated to the repair line itself, so it falls inside the
                # matching window. The API cannot do this and must not be able
                # to -- see the module docstring.
                attested_at=datetime.combine(
                    line.raised_at, time(9, 0), tzinfo=timezone.utc
                ),
                session_id=None,
                supersedes=None,
            )
        )
        existing.add((line.material_id, line.plant))
        created.append(f"{line.purchasing_document}-{line.item}")

    db.commit()
    logger.info(
        "I08 demo attestations: %d created, %d skipped (already seeded)",
        len(created),
        skipped,
    )
    return SeedResult(created=len(created), lines=created, skipped_existing=skipped)


def clear(db: Session) -> int:
    """Remove every seeded row, and only those.

    Deletes on the ``DEMO_SEED`` attestor, so a real attestation can never be
    caught by it. The immutability trigger blocks DELETE -- correctly, since the
    table is append-only -- so this disables it for the statement and puts it
    back. That is a deliberate, narrow exception for demo data, and it is the
    only place in the codebase that does it.
    """
    from sqlalchemy import text

    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(
            text(
                "ALTER TABLE i8_attestation DISABLE TRIGGER "
                "i8_attestation_no_update_or_delete"
            )
        )
    try:
        removed = db.execute(
            text("DELETE FROM i8_attestation WHERE attestor = :attestor"),
            {"attestor": DEMO_ATTESTOR},
        ).rowcount
    finally:
        if dialect == "postgresql":
            db.execute(
                text(
                    "ALTER TABLE i8_attestation ENABLE TRIGGER "
                    "i8_attestation_no_update_or_delete"
                )
            )
    db.commit()
    logger.info("I08 demo attestations: %d removed", removed)
    return removed


def main(argv: list[str] | None = None) -> int:
    """``python -m app.initiatives.i8.demo_seed [--count N | --clear]``."""
    parser = argparse.ArgumentParser(
        prog="python -m app.initiatives.i8.demo_seed",
        description=(
            "Seed clearly-marked demo attestations against real open repair "
            "lines, so the declaration queue and exception queue have something "
            "to show at UAT. Not a user path -- see the module docstring."
        ),
    )
    parser.add_argument(
        "--count", type=int, default=6, help="how many lines to attest (default 6)"
    )
    parser.add_argument(
        "--clear", action="store_true", help="remove every seeded row instead"
    )
    args = parser.parse_args(argv)

    with get_sessionmaker()() as db:
        if args.clear:
            print(f"Removed {clear(db)} demo attestations.")
            return 0

        result = seed(db, count=args.count)
        print(f"Created {result.created} demo attestations.")
        for line in result.lines:
            print(f"  repair line {line}")
        if result.skipped_existing:
            print(f"({result.skipped_existing} lines already had one, skipped.)")
        print(
            "\nThese are marked DEMO_SEED. The real number to report is that "
            "there are ZERO genuine attestations across the register, because "
            "the control did not exist before this platform."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
