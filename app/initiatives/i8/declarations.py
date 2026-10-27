"""W5.3 -- the declaration queue.

One row per repair line, saying whether somebody assessed the part before it was
sent away, and what they concluded.

This is the read side of the attestation. :mod:`app.initiatives.i8.attestation`
records judgements; this turns "which lines have one" into the rows a planner
works through.

Since 08-Oct-2026 there is no separate Declaration Queue screen: each row
travels on its repair line in the register (status, who declared, when, the
condition and the next action), and the attestation form sits on the repair
detail page. ``GET /declarations`` still serves the rows, and the Overview's
outstanding count reads it.

Vocabulary is the frontend's, not ours
---------------------------------------
``DeclarationStatus`` and ``DeclarationCondition`` already exist in
``src/features/initiative-8/types/repair.ts``. They are mapped onto here rather
than reinvented, because W5.4 renders this on the same critical path and a new
word costs somebody else an afternoon.

Two things the frontend mock once carried are gone from both sides (discard
review, 07-Oct-2026): a ``Pending`` status, because the FRS has no approval
step, and a ``source`` (Manual / MRP-generated), because FR-4 does not ask for
it and SAP cannot answer it -- EBAN's creation indicator covers 521 of the 1,201
repair requisitions and every one reads ``F``, created from an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.assistant import departments
from app.core.logging import get_logger
from app.initiatives.i8.attestation import CONDITION_LABELS, AttestationCoverage, Recommendation
from app.initiatives.i8.models import RepairAttestation
from app.initiatives.i8.register import RepairLine
from app.initiatives.i8.requisitions import PR_RAISED, RepairRequest

logger = get_logger(__name__)


#: The three statuses, and what each one means here.
#:
#: There is no "Pending" (submitted, awaiting sign-off): an attestation is
#: recorded or it is not, because no approval workflow exists in SAP, in this
#: platform or in the FRS.
STATUS_NOTES: dict[str, str] = {
    "Required": "No attestation covers this repair line.",
    "Completed": "An attestation covers this line and found the part repairable.",
    "Flagged": (
        "An attestation covers this line but did NOT find the part repairable "
        "-- it is being repaired anyway, or was."
    ),
    # 27-Oct-2026: raised before the attestation cutover, when no declaration
    # was asked for. Labelled, never required -- the same rule the
    # missing-declaration check already applies.
    "Historical": (
        "Raised before declarations were required, and none covers it. Not "
        "waiting on anybody."
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

    related_repair_id: str

    status: str

    declared_by: str | None
    declared_at: datetime | None
    condition: str | None
    """The frontend's DeclarationCondition wording, mapped from the
    attestation's recommendation. None when there is no attestation."""

    next_action: str
    created_at: date | None

    declaration_id: str | None = None
    """The attestation covering this line, when one does.

    Served so a screen can offer to AMEND it. Without the id the only thing a
    reader could do with a declaration they disagree with is record a second,
    unrelated one -- which is how an audit chain turns into a pile.

    Last in the dataclass, with a default, so adding it did not force every
    existing construction site to name it."""

    repair_document: str | None = None
    """EKPO.EBELN of this row's repair line.

    ``related_repair_id`` already carries it, joined to the item by a hyphen.
    These two serve it UNJOINED so the declaration form can send FR-4's link to
    the repair PO without splitting a string on a character a document number
    may one day contain."""

    repair_item: str | None = None
    """EKPO.EBELP of that line."""

    vendor: str | None = None
    """The vendor the part went to, as a code. Null on a line with no PO
    header -- not every repair line has one, and an empty string would read as
    a vendor whose name is blank."""

    vendor_name: str | None = None
    """LFA1's name for that vendor, where one was delivered. The queue shows
    the name when it has one and falls back to the code: a code is a fact, an
    invented name is not."""

    repair_status: str | None = None
    """Where the repair itself stands -- PO Issued or At Vendor.

    Not the declaration status, which is ``status``. The queue shows this so
    somebody working it can see whether the part has gone to the vendor yet.
    Every row is a repair SAP still has open: a closed one leaves the register,
    and this queue with it."""

    requester_name: str | None = None
    """The department name for ``requester``, when the department list has
    the code. None when it does not: the code is shown alone, never a guess."""

    repair_document_type: str = "PO"
    """``PO`` for a repair PO line, ``PR`` for an open repair PR with no PO
    yet (27-Oct-2026). On a PR row ``repair_document``/``repair_item`` are the
    PR's BANFN/BNFPO -- what the declaration form sends to bind to it."""

    declaration_link: str | None = None
    """How the covering declaration was matched: ``BOUND``, ``REFERENCE`` or
    ``WINDOW``. None when nothing covers the row."""

    @property
    def is_outstanding(self) -> bool:
        # Required only since 27-Oct-2026: a Flagged row has a declaration,
        # and its finding is an exception, not a wait for somebody to declare.
        return self.status == "Required"


def _is_historical(raised_on: date | None, cutover: date | None) -> bool:
    """Raised before the attestation cutover: no declaration was asked for.
    No cutover set, or no date, means not historical -- the same rule as
    ``exceptions.is_pre_automation``."""
    return cutover is not None and raised_on is not None and raised_on < cutover


def _status(attestation: RepairAttestation | None, historical: bool) -> tuple[str, str | None]:
    """The row's status and the condition it shows."""
    if attestation is None:
        return ("Historical" if historical else "Required"), None
    if attestation.recommendation == Recommendation.REPAIRABLE.value:
        return "Completed", CONDITION_LABELS[Recommendation.REPAIRABLE]
    # Assessed, but not as repairable -- and it went for repair anyway. That is
    # a real finding, not a missing record, so it is Flagged rather than
    # Required.
    return "Flagged", CONDITION_LABELS[Recommendation(attestation.recommendation)]


def _next_action(status: str, line, attestation: RepairAttestation | None) -> str:
    """What a person should do about this row, in a sentence they can act on.

    Every row is on a repair SAP still has open, so the part is out for repair
    -- or, on a PR row, about to be -- and a declaration can still be made or
    reconsidered.
    """
    if status == "Historical":
        return (
            "None required. This repair was raised before declarations were "
            "asked for; declare it if the part's condition is known."
        )
    if status == "Required" and isinstance(line, RepairRequest):
        return (
            "Declare the condition of this part. A repair has been asked for "
            "and no assessment is on record."
        )
    if status == "Required":
        return (
            "Declare the condition of this part. It is out for repair now "
            "and no assessment is on record."
        )

    if status == "Flagged":
        assert attestation is not None
        verdict = CONDITION_LABELS[Recommendation(attestation.recommendation)]
        return (
            f"Assessed as {verdict} but sent for repair anyway -- confirm "
            f"with {attestation.attestor} before the unit comes back."
        )

    return "None. The condition was declared and the part was found repairable."


def build_queue(
    lines,
    coverage: AttestationCoverage,
    *,
    requests=(),
    cutover: date | None = None,
) -> list[DeclarationRow]:
    """The declaration queue for a set of repair lines.

    Takes the coverage computed once per refresh rather than querying per line
    -- 1,225 lines is a page load, not a batch job.

    ``requests`` are the open repair PRs with no PO yet, passed only while the
    register serves them; each gets a row after the PO lines (27-Oct-2026).
    ``cutover`` is the attestation cutover: an undeclared row raised before it
    reads ``Historical`` (a PO line by its raised date, a PR by its PR date).
    The requester is the department that raised the repair PR -- on a PO row
    the PR it was ordered from, falling back to the PO line's own requisitioner
    when that PR was not read.
    """
    rows: list[DeclarationRow] = []

    for line in lines:
        attestation = coverage.covered.get(line.key)
        status, condition = _status(attestation, _is_historical(line.raised_at, cutover))
        requester = line.pr_department or line.requisitioner

        rows.append(
            DeclarationRow(
                id=f"D-{line.purchasing_document}-{line.item}",
                material_id=line.material_id,
                description=line.description,
                plant=line.plant,
                pr_number=line.pr_number,
                pr_item=line.pr_item,
                requester=requester,
                requester_name=departments.name_for(requester),
                declaration_link=coverage.links.get(line.key),
                related_repair_id=f"{line.purchasing_document}-{line.item}",
                repair_document=line.purchasing_document,
                repair_item=line.item,
                vendor=line.vendor,
                vendor_name=line.vendor_name,
                repair_status=line.repair_status,
                status=status,
                declaration_id=attestation.id if attestation else None,
                declared_by=attestation.attestor if attestation else None,
                declared_at=attestation.attested_at if attestation else None,
                condition=condition,
                next_action=_next_action(status, line, attestation),
                created_at=line.raised_at,
            )
        )

    for request in requests:
        attestation = coverage.requests_covered.get(request.key)
        status, condition = _status(attestation, _is_historical(request.requested_on, cutover))
        rows.append(
            DeclarationRow(
                id=f"D-{request.row_id}",
                material_id=request.material_id,
                description=request.description,
                plant=request.plant,
                pr_number=request.purchase_requisition,
                pr_item=request.item,
                requester=request.department,
                requester_name=departments.name_for(request.department),
                related_repair_id=request.row_id,
                repair_document=request.purchase_requisition,
                repair_item=request.item,
                repair_document_type="PR",
                declaration_link=coverage.request_links.get(request.key),
                repair_status=PR_RAISED,
                status=status,
                declaration_id=attestation.id if attestation else None,
                declared_by=attestation.attestor if attestation else None,
                declared_at=attestation.attested_at if attestation else None,
                condition=condition,
                next_action=_next_action(status, request, attestation),
                created_at=request.requested_on,
            )
        )

    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    logger.info("I08 declaration queue: %d rows %s", len(rows), counts)

    return rows
