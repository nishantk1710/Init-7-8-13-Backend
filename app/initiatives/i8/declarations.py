"""W5.3 -- the declaration queue the UI renders.

One row per repair line, saying whether somebody assessed the part before it was
sent away, and what they concluded.

This is the read side of the attestation. :mod:`app.initiatives.i8.attestation`
records judgements; this turns "which lines have one" into the queue a planner
works through.

Vocabulary is the frontend's, not ours
---------------------------------------
``DeclarationStatus``, ``DeclarationCondition`` and ``DeclarationSource`` already
exist in ``src/features/initiative-8/types/repair.ts``. They are mapped onto
here rather than reinvented, because W5.4 renders this on the same critical path
and a new word costs somebody else an afternoon.

Two of those types do not survive contact with the data, and both are stated
rather than papered over. See :data:`STATUS_NOTES` and the note on ``source``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.core.logging import get_logger
from app.initiatives.i8.attestation import CONDITION_LABELS, AttestationCoverage, Recommendation
from app.initiatives.i8.models import RepairAttestation
from app.initiatives.i8.register import RepairLine

logger = get_logger(__name__)


#: The four statuses the frontend defines, and what each one means here.
#:
#: "Pending" is deliberately NEVER EMITTED. It means "submitted, awaiting
#: sign-off", and there is no such state: an attestation is recorded or it is
#: not, because no approval workflow exists in SAP or in this platform. Emitting
#: it would invent a stage of a process that nobody operates. It stays in the
#: type so the UI keeps compiling, and so it is there the day a review step is
#: actually built.
STATUS_NOTES: dict[str, str] = {
    "Required": "No attestation covers this repair line.",
    "Pending": "Not emitted -- there is no submitted-awaiting-approval state.",
    "Completed": "An attestation covers this line and found the part repairable.",
    "Flagged": (
        "An attestation covers this line but did NOT find the part repairable "
        "-- it is being repaired anyway, or was."
    ),
}


@dataclass(frozen=True)
class DeclarationRow:
    """One row of the condition-to-repair declaration queue."""

    id: str
    material_id: str
    description: str | None
    plant: str | None

    pr_number: str | None
    pr_item: str | None

    requester: str | None
    """EKPO.AFNAM, a code. None only if the line has no requisitioner, which no
    repair line in the July extract does."""

    source: str | None
    """``Manual`` or ``MRP-generated`` -- and NULL on every row today.

    A fourth deliberate departure from the frontend type, in the same spirit as
    the three already recorded in the API schemas.

    The honest reason: EBAN carries the creation indicator that would answer
    this, and it covers only 521 of the 1,201 repair requisitions. Every one of
    those 521 reads ``F`` -- created from an order -- which is neither "Manual"
    (somebody typed it) nor "MRP-generated" (planning raised it). So both labels
    are false for every row we can see, and the ones we cannot see are unknown.

    Sending either would be inventing a provenance for a purchase, which is
    exactly the class of mistake I08 exists to stop."""

    has_active_repair: bool
    related_repair_id: str

    status: str
    declared_by: str | None
    declared_at: datetime | None
    condition: str | None
    """The frontend's DeclarationCondition wording, mapped from the
    attestation's recommendation. None when there is no attestation."""

    next_action: str
    created_at: date | None

    @property
    def is_outstanding(self) -> bool:
        return self.status in ("Required", "Flagged")


def _next_action(status: str, line: RepairLine, attestation: RepairAttestation | None) -> str:
    """What a person should do about this row, in a sentence they can act on."""
    if status == "Required":
        if line.is_open:
            return (
                "Declare the condition of this part. It is out for repair now "
                "and no assessment is on record."
            )
        return (
            "No assessment was ever recorded for this repair. It is closed, so "
            "this cannot be corrected -- it is counted to size the gap."
        )

    if status == "Flagged":
        assert attestation is not None
        verdict = CONDITION_LABELS[Recommendation(attestation.recommendation)]
        if line.is_open:
            return (
                f"Assessed as {verdict} but sent for repair anyway -- confirm "
                f"with {attestation.attestor} before the unit comes back."
            )
        return f"Assessed as {verdict} and repaired anyway. Review the decision."

    return "None. The condition was declared and the part was found repairable."


def build_queue(
    lines,
    coverage: AttestationCoverage,
) -> list[DeclarationRow]:
    """The declaration queue for a set of repair lines.

    Takes the coverage computed once per refresh rather than querying per line
    -- 1,225 lines is a page load, not a batch job.
    """
    rows: list[DeclarationRow] = []

    for line in lines:
        attestation = coverage.covered.get(line.key)

        if attestation is None:
            status = "Required"
            condition = None
        elif attestation.recommendation == Recommendation.REPAIRABLE.value:
            status = "Completed"
            condition = CONDITION_LABELS[Recommendation.REPAIRABLE]
        else:
            # Assessed, but not as repairable -- and it went for repair anyway.
            # That is a real finding, not a missing record, so it is Flagged
            # rather than Required.
            status = "Flagged"
            condition = CONDITION_LABELS[Recommendation(attestation.recommendation)]

        rows.append(
            DeclarationRow(
                id=f"D-{line.purchasing_document}-{line.item}",
                material_id=line.material_id,
                description=line.description,
                plant=line.plant,
                pr_number=line.pr_number,
                pr_item=line.pr_item,
                requester=line.requisitioner,
                # Null on every row. See the field docstring -- both available
                # labels are false for every repair line we can see.
                source=None,
                has_active_repair=line.is_open,
                related_repair_id=f"{line.purchasing_document}-{line.item}",
                status=status,
                declared_by=attestation.attestor if attestation else None,
                declared_at=attestation.attested_at if attestation else None,
                condition=condition,
                next_action=_next_action(status, line, attestation),
                created_at=line.raised_at,
            )
        )

    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    logger.info("I08 declaration queue: %d rows %s", len(rows), counts)

    return rows
