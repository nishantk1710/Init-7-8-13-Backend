"""W5.3 -- the exception queue, and the missing-attestation check.

Recording an attestation is half of W5.3. **Detecting its absence is the half
with the value**, because it is what makes an invisible control failure into a
number somebody can act on.

The check, in plain English
---------------------------
For every repair line in the register: if no attestation matches its material
and plant inside the configured window, raise ``MISSING_ATTESTATION``.

It is a **batch check that runs once per refresh cycle**, not per request. The
snapshot is already cached for exactly this reason, and a per-request version
would put a query per line onto the page W5.4 renders.

Every line it checks is open in SAP: since 19-Oct-2026 the register holds no
other kind (see ``register.py``), so neither does this queue. A repair SAP has
closed leaves the queue with it, and a finding somebody was working on is
auto-resolved with a note rather than vanishing (``exception_state``).

The number this produces is the business case
----------------------------------------------
At UAT this fires on essentially every repair line, because no attestation has
ever existed for any of them -- the control did not exist before this
platform. That is correct behaviour and a terrible demo.

The answer is not to soften the check. It is to seed a handful of attestations
against real open repair lines, clearly marked as demo-entered, let the
exception fire on the rest, and **say the real number out loud**: N lines, zero
real attestations, because nobody was ever asked to make one.

That number is the argument for the whole initiative. Hiding it behind seeded
data would be arguing against ourselves.

Labelled, not softened
----------------------
The team lead's ruling of 20-Sep -- *"on them can we show before Spares
Automation"* -- is about how that number reads, not how big it is. A line raised
before the attestation control existed did not fail it. So the exception still
fires, still lists, and still counts; what changes is that it carries
``pre_automation`` and says in its own text that the control post-dates it.

Two consequences worth being deliberate about:

* **Severity drops to INFO.** Nobody can act on a gap in a part that went for
  repair before there was a form to fill in.
* **It leaves the actionable count.** ``ExceptionStats.actionable`` is the
  number an operations queue should show; ``total`` stays honest and keeps the
  full figure. Reporting one without the other is how a queue of 1,225
  un-actionable rows either buries the real misses or disappears them.

The cutover date is ``I8_ATTESTATION_CUTOVER_DATE`` and **is not set yet** --
see the note on the setting. Blank means no line is treated as pre-automation
and the behaviour is exactly what it was before this was built.

Two detectors, one queue
------------------------
``UNJUSTIFIED_ACQUISITION`` (FR-8) is raised beside ``MISSING_ATTESTATION``: a
new 80-series unit bought while a repair of the same part was open at the same
plant, with no NEW_ACQUISITION justification recorded near it. Since
27-Oct-2026 the purchase is usually a new-buy PR, and the repair it overlapped
may be a repair PR with no PO yet; the item says which kind each document is.
The purchases and the repairs come from :mod:`app.initiatives.i8.acquisitions`,
which also says why stock on hand cannot be judged for a past purchase.

``MISSING_SESSION_ID`` (FR-8) is ours too and is **not raised yet**. It needs
the session's R-reference read back off the reservation: requesters type it
into the reservation text (RESB.SGTXT), which the read layer already carries
(26-Oct-2026), and the detector is built in phase 3 of the complete-flow plan.
It stays in :class:`ExceptionType` so adding it is new code, not a migration.

Three declaration findings (27-Oct-2026)
----------------------------------------
``REPAIRED_AGAINST_ASSESSMENT``: a repair PO line or repair PR whose current
declaration says Beyond economical repair or Scrap. The declaration queue
showed these as "Flagged" and nothing else did; the finding belongs here, where
someone can own and close it.

``REPAIR_PR_NOT_DECLARED``: an open repair PR, raised on or after the
attestation cutover, older than ``I8_DECLARATION_GRACE_DAYS`` and with no
declaration. ``DECLARED_NO_REPAIR_PR``: the other half of the same gap -- a
Repairable declaration older than the grace days with no repair PR or PO for
the part at the plant raised on or after it. Both are counted against the
snapshot's reference date, the date every other age in I08 is counted from.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum

from app.core.logging import get_logger
from app.initiatives.i8.acquisitions import (
    PURCHASE_ORDER,
    PURCHASE_REQUISITION,
    JustificationRecord,
    NewAcquisition,
    OpenRepair,
    document_reference,
    open_repair_at,
    repairs_by_material_plant,
)
from app.initiatives.i8.attestation import (
    CONDITION_LABELS,
    LINK_WINDOW,
    AttestationCoverage,
    Recommendation,
)
from app.initiatives.i8.register import RepairLine
from app.initiatives.i8.requisitions import RepairRequest

logger = get_logger(__name__)


class ExceptionType(str, Enum):
    """Every exception the queue can carry. All three are I08's -- FR-4 and FR-8.

    ``MISSING_SESSION_ID`` is declared and not yet raised; see the module note.
    """

    MISSING_ATTESTATION = "MISSING_ATTESTATION"
    """A repair line with no condition-to-repair attestation. FR-4 / FR-8."""

    REPAIRED_AGAINST_ASSESSMENT = "REPAIRED_AGAINST_ASSESSMENT"
    """A part declared beyond economical repair or scrap, sent for repair
    anyway. Since 27-Oct-2026."""

    REPAIR_PR_NOT_DECLARED = "REPAIR_PR_NOT_DECLARED"
    """An open repair PR older than the grace days with no declaration."""

    DECLARED_NO_REPAIR_PR = "DECLARED_NO_REPAIR_PR"
    """A Repairable declaration older than the grace days with no repair PR or
    PO for the same part and plant raised on or after it."""

    MISSING_SESSION_ID = "MISSING_SESSION_ID"
    """FR-8, an 80-series reservation with no valid session id. Not raised yet:
    the R-reference comes from the reservation text (RESB.SGTXT), and the
    detector is built in phase 3."""

    UNJUSTIFIED_ACQUISITION = "UNJUSTIFIED_ACQUISITION"
    """FR-8, a new unit bought while a repair was open, with no justification."""

    OVERDUE_REPAIR = "OVERDUE_REPAIR"
    """FR-9, a repair past its promised return with the unit still at a vendor.

    FR-9 asks for overdue "as an explicit dashboard exception state". It was
    shown as a register column and an overview tile and raised nowhere, so the
    one screen built to list what needs chasing did not list the repairs that
    are late -- the most chaseable thing I08 knows about.
    """


#: Which types this module actually raises. The difference between this and
#: ExceptionType is the point: the vocabulary is wider than the implementation,
#: and that is recorded rather than implied.
RAISED_BY_I8: frozenset[ExceptionType] = frozenset(
    {
        ExceptionType.MISSING_ATTESTATION,
        ExceptionType.UNJUSTIFIED_ACQUISITION,
        ExceptionType.OVERDUE_REPAIR,
        ExceptionType.REPAIRED_AGAINST_ASSESSMENT,
        ExceptionType.REPAIR_PR_NOT_DECLARED,
        ExceptionType.DECLARED_NO_REPAIR_PR,
    }
)


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ExceptionItem:
    """One row of the exception queue."""

    id: str
    type: str
    severity: str

    material_id: str
    description: str | None
    plant: str | None

    purchasing_document: str
    item: str
    """The repair line this is about. The exception is per LINE, not per
    material: the same part can go for repair four times and each dispatch
    needed its own assessment."""

    title: str
    detail: str
    """Says what is missing AND what was searched for, so a reader can tell a
    real gap from a matching rule that did not fit."""

    raised_at: date | None
    """The repair line's own date, not the moment the check ran. The exception
    is as old as the thing it is about -- dating it 'today' would make every
    historical gap look like it appeared this morning."""

    pre_automation: bool = False
    """Raised on a line that predates the attestation control itself.

    Not a lesser finding -- a different one. It says the gap is explained by
    when the line was raised, not by anyone failing to do something, and it is
    what keeps 1,225 historical rows from reading as 1,225 violations. False
    whenever no cutover date is configured, which is the current state.
    """

    acquisition_document: str | None = None
    acquisition_item: str | None = None
    """``UNJUSTIFIED_ACQUISITION`` only: the purchase that bought the new unit.
    ``purchasing_document``/``item`` are then the repair it overlapped."""

    repair_document_type: str = PURCHASE_ORDER
    """``PO`` or ``PR``: what ``purchasing_document``/``item`` name. ``PR`` only
    for an ``UNJUSTIFIED_ACQUISITION`` whose repair has no PO yet; the document
    is then the bare PR number."""

    acquisition_document_type: str = PURCHASE_ORDER
    """``PO`` or ``PR``: what ``acquisition_document``/``acquisition_item``
    name -- since 27-Oct-2026 usually a new-buy PR."""

    declaration_id: str | None = None
    """The declaration the finding is about, on ``REPAIRED_AGAINST_ASSESSMENT``
    and ``DECLARED_NO_REPAIR_PR``. ``DECLARED_NO_REPAIR_PR`` names no repair
    line -- there is none, which is the finding -- so its document is blank."""

    @property
    def repair_reference(self) -> str:
        """The repair as the register names its row: ``{EBELN}-{EBELP}``, or
        ``PR-{BANFN}-{BNFPO}`` for a repair PR with no PO yet."""
        return document_reference(self.repair_document_type, self.purchasing_document, self.item)


def _day(value: date | None, missing: str) -> str:
    """A date the way the screens show one -- ``08 May 2025`` -- or ``missing``.

    The item text is read by people, not parsed, so it does not use ISO dates.
    """
    return value.strftime("%d %b %Y") if value is not None else missing


def is_pre_automation(line: RepairLine, cutover: date | None) -> bool:
    """Whether this line was raised before the attestation control existed.

    A line with no date of its own is NOT treated as pre-automation. Guessing
    in the forgiving direction is still guessing, and the one it would forgive
    is a line we know least about.

    >>> from datetime import date as d
    >>> class L: raised_at = d(2026, 5, 1)
    >>> is_pre_automation(L(), d(2026, 10, 1))
    True
    >>> is_pre_automation(L(), d(2026, 1, 1))
    False
    >>> is_pre_automation(L(), None)
    False
    """
    if cutover is None or line.raised_at is None:
        return False
    return line.raised_at < cutover


def missing_attestations(
    lines,
    coverage: AttestationCoverage,
    *,
    cutover: date | None = None,
) -> list[ExceptionItem]:
    """Raise MISSING_ATTESTATION for every repair line with no attestation.

    ``coverage`` is computed once per refresh by
    :func:`app.initiatives.i8.attestation.coverage`. Lines it never checked --
    those with no plant, where the material-plant key cannot be formed -- are
    not reported here. An exception nobody could ever clear is noise, not a
    finding.

    ``cutover`` is the date the attestation control starts applying. Lines
    raised before it are labelled rather than accused -- see the module
    docstring. None, the default, means no cutover is configured and every line
    is judged as if the control had always existed.
    """
    by_key = {line.key: line for line in lines}
    items: list[ExceptionItem] = []

    for key in sorted(coverage.uncovered):
        line = by_key.get(key)
        if line is None:  # pragma: no cover - coverage is built from these lines
            continue

        historical = is_pre_automation(line, cutover)

        items.append(
            ExceptionItem(
                id=f"EX-{ExceptionType.MISSING_ATTESTATION.value}-{line.purchasing_document}-{line.item}",
                type=ExceptionType.MISSING_ATTESTATION.value,
                # Every line is open in SAP: a part out there now with no
                # assessment on record, which someone can still do something
                # about. A pre-automation line is INFO all the same: there was
                # no form to fill in when it was raised, so nothing about it is
                # anybody's to action now.
                severity=(Severity.INFO if historical else Severity.WARNING).value,
                material_id=line.material_id,
                description=line.description,
                plant=line.plant,
                purchasing_document=line.purchasing_document,
                item=line.item,
                # Written for the people working the queue, who know SAP but
                # not this codebase: no table names, no "attestation" (the
                # screens say "condition declaration"), dates as 08 May 2025.
                title=(
                    "Raised before Spares Automation"
                    if historical
                    else "No condition declaration"
                ),
                detail=(
                    (
                        f"This line was raised on {_day(line.raised_at, 'an unknown date')}, "
                        f"before condition declarations began on "
                        f"{_day(cutover, 'the cutover date')}. No assessment is on record "
                        "because none was asked for at the time, so nothing needs doing."
                    )
                    if historical
                    else (
                        "No condition declaration covers this repair line: none was "
                        f"recorded on it, or for material {line.material_id} at plant "
                        f"{line.plant} within {coverage.window_days} days of "
                        f"{_day(line.raised_at, 'the PO date')}. The part went for repair "
                        "without a recorded assessment of its condition."
                    )
                ),
                raised_at=line.raised_at,
                pre_automation=historical,
            )
        )

    logger.info(
        "I08 exceptions: %d MISSING_ATTESTATION of %d repair lines checked "
        "(%d pre-automation)",
        len(items),
        coverage.checked,
        sum(1 for i in items if i.pre_automation),
    )
    return items


def overdue_repairs(lines, *, grace_days: int) -> list[ExceptionItem]:
    """Raise OVERDUE_REPAIR for every line past its due date plus grace.

    The register already decides this per line -- ``overdue_status`` is
    computed once, against ``EKET.EINDT`` plus ``I8_OVERDUE_GRACE_DAYS`` -- so
    this reads that verdict rather than recomputing it. Two places deciding
    what "overdue" means is how a column and a queue end up disagreeing about
    the same line.

    **Open lines only**, because the register holds nothing else: once SAP
    closes a line ("Delivery completed") it leaves the register and this queue.

    No pre-automation concept here, unlike MISSING_ATTESTATION. A repair being
    late is not a control somebody failed to follow before we built it -- it
    is a fact about a vendor, and it was just as true in 2018.
    """
    items: list[ExceptionItem] = []

    for line in sorted(lines, key=lambda line: (line.purchasing_document, line.item)):
        if not line.is_overdue:
            continue

        days_late = None
        if line.due_date is not None and line.days_remaining is not None:
            # days_remaining goes negative once the date has passed.
            days_late = -line.days_remaining if line.days_remaining < 0 else None

        where = f" from {line.vendor_name or line.vendor}" if (line.vendor_name or line.vendor) else ""
        due = _day(line.due_date, "an unrecorded date")

        items.append(
            ExceptionItem(
                id=f"EX-{ExceptionType.OVERDUE_REPAIR.value}-{line.purchasing_document}-{line.item}",
                type=ExceptionType.OVERDUE_REPAIR.value,
                # Every one of these is open and chaseable by definition, so
                # there is no INFO tier here -- unlike the attestation check,
                # where most rows are history nobody can act on.
                severity=Severity.WARNING.value,
                material_id=line.material_id,
                description=line.description,
                plant=line.plant,
                purchasing_document=line.purchasing_document,
                item=line.item,
                title="Repair overdue",
                detail=(
                    f"Due back on {due}{where}"
                    + (f", {days_late} days ago" if days_late is not None else "")
                    + f". A repair is flagged once it is more than {grace_days} days "
                    "late, and SAP still has this repair line open."
                ),
                raised_at=line.raised_at,
            )
        )

    logger.info(
        "I08 exceptions: %d OVERDUE_REPAIR of %d repair lines checked "
        "(grace %d days)",
        len(items),
        len(list(lines)),
        grace_days,
    )
    return items


def _justified(
    acquisition: NewAcquisition,
    exception_id: str,
    justifications: Sequence[JustificationRecord],
    window_days: int,
) -> bool:
    """Whether a NEW_ACQUISITION justification answers this purchase.

    Either one recorded against this exception's id, or one for the same
    material and plant within ``window_days`` of the purchase, either side --
    the assistant normally records the reason before the purchase is raised.
    """
    for record in justifications:
        if record.exception_id == exception_id:
            return True
        if (
            record.material_id == acquisition.material_id
            and record.plant == acquisition.plant
            and acquisition.raised_at is not None
            and abs((record.recorded_on - acquisition.raised_at).days) <= window_days
        ):
            return True
    return False


def _purchase_phrase(acquisition: NewAcquisition) -> str:
    """``requested on 14 Mar 2026 (PR 1000201234/10)``: a PR is a request to
    buy, a direct PO an order."""
    if acquisition.document_type == PURCHASE_REQUISITION:
        verb, label = "requested", "PR"
    else:
        verb, label = "ordered", "PO"
    return (
        f"{verb} on {_day(acquisition.raised_at, 'an unknown date')} "
        f"({label} {acquisition.purchasing_document}/{acquisition.item})"
    )


def _repair_phrase(repair: OpenRepair) -> str:
    """``repair PR 2000029113/10, requested 04 Feb 2026``."""
    if repair.document_type == PURCHASE_REQUISITION:
        verb, label = "requested", "PR"
    else:
        verb, label = "raised", "PO"
    return (
        f"repair {label} {repair.document}/{repair.item}, "
        f"{verb} {_day(repair.raised_at, 'on an unknown date')}"
    )


def acquisition_exception_id(acquisition: NewAcquisition) -> str:
    """``EX-UNJUSTIFIED_ACQUISITION-{EBELN}-{EBELP}`` for a direct PO -- the id
    such an item has always had -- and ``EX-UNJUSTIFIED_ACQUISITION-PR-{BANFN}-
    {BNFPO}`` for a PR, so the two number series can never share an id."""
    return f"EX-{ExceptionType.UNJUSTIFIED_ACQUISITION.value}-{acquisition.reference}"


def unjustified_acquisitions(
    acquisitions: Sequence[NewAcquisition],
    lines: Sequence[RepairLine],
    justifications: Sequence[JustificationRecord],
    *,
    window_days: int,
    cutover: date | None = None,
    repair_requests: Sequence[RepairRequest] = (),
) -> list[ExceptionItem]:
    """Raise UNJUSTIFIED_ACQUISITION for every new purchase of a part that was
    already out for repair at the same plant, with no justification near it.

    The repairs are the open repair PO lines and, since 27-Oct-2026, the open
    repair PRs with no PO yet (``repair_requests``) -- whether or not the
    register serves them: a repair somebody has asked SAP for is open whatever
    screen shows it.

    A purchase with no overlapping repair is not an exception -- buying a part
    nobody is repairing is ordinary procurement. ``cutover`` labels purchases
    raised before the justification control existed, exactly as
    :func:`missing_attestations` labels pre-cutover repair lines.
    """
    repairs = repairs_by_material_plant(lines, repair_requests)
    items: list[ExceptionItem] = []

    for acquisition in sorted(acquisitions, key=lambda a: a.key):
        repair = open_repair_at(acquisition, repairs)
        if repair is None:
            continue

        exception_id = acquisition_exception_id(acquisition)
        if _justified(acquisition, exception_id, justifications, window_days):
            continue

        historical = (
            cutover is not None
            and acquisition.raised_at is not None
            and acquisition.raised_at < cutover
        )

        items.append(
            ExceptionItem(
                id=exception_id,
                type=ExceptionType.UNJUSTIFIED_ACQUISITION.value,
                # The money is committed at the purchase, whatever has happened
                # to the repair since, so a missing reason is always worth
                # asking for -- unless the control did not exist yet.
                severity=(Severity.INFO if historical else Severity.WARNING).value,
                material_id=acquisition.material_id,
                description=acquisition.description or repair.description,
                plant=acquisition.plant,
                purchasing_document=repair.document,
                item=repair.item,
                # Written for the people working the queue, as
                # missing_attestations is: no table or field names, no rule
                # names, dates as 08 May 2025.
                title=(
                    "Raised before Spares Automation"
                    if historical
                    else "New unit bought while a repair was open"
                ),
                detail=(
                    (
                        f"A new unit was {_purchase_phrase(acquisition)}, before "
                        "reasons for buying new were first asked for on "
                        f"{_day(cutover, 'the cutover date')}. No reason is on record "
                        "because none was asked for at the time, so nothing needs doing."
                    )
                    if historical
                    else (
                        f"A new unit was {_purchase_phrase(acquisition)} while a repair "
                        f"of the same part ({_repair_phrase(repair)}) was still open, "
                        f"and no reason was recorded within {window_days} days."
                    )
                ),
                raised_at=acquisition.raised_at,
                pre_automation=historical,
                acquisition_document=acquisition.purchasing_document,
                acquisition_item=acquisition.item,
                repair_document_type=repair.document_type,
                acquisition_document_type=acquisition.document_type,
            )
        )

    logger.info(
        "I08 exceptions: %d UNJUSTIFIED_ACQUISITION of %d new purchases checked "
        "against %d open repair lines and %d open repair PRs (%d pre-automation)",
        len(items),
        len(acquisitions),
        len(lines),
        len(repair_requests),
        sum(1 for i in items if i.pre_automation),
    )
    return items


# --- Declaration findings (27-Oct-2026) -----------------------------------

#: The verdicts that say "do not repair this part".
_NOT_FOR_REPAIR = frozenset(
    {Recommendation.BEYOND_ECONOMICAL_REPAIR.value, Recommendation.SCRAP.value}
)


def _declared_by(attestation) -> str:
    """The typed name when there is one, else the signed-in user."""
    return getattr(attestation, "attestor_name", None) or attestation.attestor


def _against_item(document_type, document, item, row, raised_at, attestation, link):
    verdict = CONDITION_LABELS[Recommendation(attestation.recommendation)]
    what = "repair PR" if document_type == PURCHASE_REQUISITION else "repair line"
    matched = (
        " The declaration was matched by part, plant and date rather than named on it."
        if link == LINK_WINDOW
        else ""
    )
    return ExceptionItem(
        id=(
            f"EX-{ExceptionType.REPAIRED_AGAINST_ASSESSMENT.value}-"
            f"{document_reference(document_type, document, item)}"
        ),
        type=ExceptionType.REPAIRED_AGAINST_ASSESSMENT.value,
        severity=Severity.WARNING.value,
        material_id=row.material_id,
        description=row.description,
        plant=row.plant,
        purchasing_document=document,
        item=item,
        title="Sent for repair against the assessment",
        detail=(
            f"{_declared_by(attestation)} declared this part {verdict} on "
            f"{_day(attestation.attested_at.date(), 'an unknown date')} "
            f"(declaration {attestation.id}), but this {what} sends it for repair."
            f"{matched} Confirm the repair is wanted, or amend the declaration."
        ),
        raised_at=raised_at,
        repair_document_type=document_type,
        declaration_id=attestation.id,
    )


def repaired_against_assessment(
    lines, coverage: AttestationCoverage, *, requests: Sequence[RepairRequest] = ()
) -> list[ExceptionItem]:
    """Raise REPAIRED_AGAINST_ASSESSMENT for every repair PO line or repair PR
    whose covering declaration is Beyond economical repair or Scrap."""
    items: list[ExceptionItem] = []
    for line in sorted(lines, key=lambda line: (line.purchasing_document, line.item)):
        attestation = coverage.covered.get(line.key)
        if attestation is None or attestation.recommendation not in _NOT_FOR_REPAIR:
            continue
        items.append(
            _against_item(
                PURCHASE_ORDER, line.purchasing_document, line.item, line,
                line.raised_at, attestation, coverage.links.get(line.key),
            )
        )
    for request in sorted(requests, key=lambda request: request.key):
        attestation = coverage.requests_covered.get(request.key)
        if attestation is None or attestation.recommendation not in _NOT_FOR_REPAIR:
            continue
        items.append(
            _against_item(
                PURCHASE_REQUISITION, request.purchase_requisition, request.item, request,
                request.requested_on, attestation, coverage.request_links.get(request.key),
            )
        )
    return items


def undeclared_repair_requests(
    requests: Sequence[RepairRequest],
    coverage: AttestationCoverage,
    *,
    reference_date: date,
    grace_days: int,
    cutover: date | None = None,
) -> list[ExceptionItem]:
    """Raise REPAIR_PR_NOT_DECLARED for every open repair PR checked and found
    with no declaration, raised more than ``grace_days`` ago.

    A PR raised before the cutover is Historical -- no declaration was asked
    for -- and a PR with no date cannot be aged, so neither is raised.
    """
    items: list[ExceptionItem] = []
    for request in sorted(requests, key=lambda request: request.key):
        if request.key not in coverage.requests_uncovered or request.requested_on is None:
            continue
        if cutover is not None and request.requested_on < cutover:
            continue
        waited = (reference_date - request.requested_on).days
        if waited <= grace_days:
            continue
        by = f" by department {request.department}" if request.department else ""
        items.append(
            ExceptionItem(
                id=(
                    f"EX-{ExceptionType.REPAIR_PR_NOT_DECLARED.value}-"
                    f"{document_reference(PURCHASE_REQUISITION, request.purchase_requisition, request.item)}"
                ),
                type=ExceptionType.REPAIR_PR_NOT_DECLARED.value,
                severity=Severity.WARNING.value,
                material_id=request.material_id,
                description=request.description,
                plant=request.plant,
                purchasing_document=request.purchase_requisition,
                item=request.item,
                title="Repair asked for, condition not declared",
                detail=(
                    f"This repair was asked for on {_day(request.requested_on, 'an unknown date')}"
                    f"{by}, {waited} days ago, and no condition declaration is on record "
                    f"for it or for material {request.material_id} at plant {request.plant} "
                    f"within {coverage.window_days} days. A declaration is expected within "
                    f"{grace_days} days of the repair being asked for."
                ),
                raised_at=request.requested_on,
                repair_document_type=PURCHASE_REQUISITION,
            )
        )
    return items


def declarations_without_repair(
    declarations,
    lines,
    requests: Sequence[RepairRequest],
    *,
    reference_date: date,
    grace_days: int,
) -> list[ExceptionItem]:
    """Raise DECLARED_NO_REPAIR_PR for every current Repairable declaration
    older than ``grace_days`` with no repair PR or PO for its part and plant
    raised on or after the day it was made.

    ``declarations`` are the current ones (nothing superseded). A declaration
    that names a repair PR or PO line is not checked: naming one is only
    accepted while it is open in the register, so its repair exists. A PO
    line's date is its PR's date when the PR was read, else the PO line's own.
    """
    raised: dict[tuple[str, str | None], list[date]] = {}
    for line in lines:
        on = line.pr_requested_on or line.raised_at
        if on is not None:
            raised.setdefault((line.material_id, line.plant), []).append(on)
    for request in requests:
        if request.requested_on is not None:
            raised.setdefault((request.material_id, request.plant), []).append(request.requested_on)

    items: list[ExceptionItem] = []
    for attestation in sorted(declarations, key=lambda a: a.id):
        if attestation.recommendation != Recommendation.REPAIRABLE.value:
            continue
        if attestation.repair_document and attestation.repair_item:
            continue
        on = attestation.attested_at.date()
        waited = (reference_date - on).days
        if waited <= grace_days:
            continue
        if any(day >= on for day in raised.get((attestation.material_id, attestation.plant), ())):
            continue
        items.append(
            ExceptionItem(
                id=f"EX-{ExceptionType.DECLARED_NO_REPAIR_PR.value}-{attestation.id}",
                type=ExceptionType.DECLARED_NO_REPAIR_PR.value,
                severity=Severity.WARNING.value,
                material_id=attestation.material_id,
                description=None,
                plant=attestation.plant,
                purchasing_document="",
                item="",
                title="Declared repairable, no repair asked for",
                detail=(
                    f"{_declared_by(attestation)} declared material {attestation.material_id} "
                    f"at plant {attestation.plant} Repairable on {_day(on, 'an unknown date')} "
                    f"(declaration {attestation.id}), {waited} days ago, and no open repair "
                    "PR or PO for that part at that plant has been raised since. Raise the "
                    "repair, or amend the declaration if the part is not going for repair."
                ),
                raised_at=on,
                declaration_id=attestation.id,
            )
        )
    return items


@dataclass(frozen=True)
class ExceptionStats:
    """Counts, with the rule that produced them attached.

    Never quote a count without its source -- so the window that decided these
    numbers travels with them.
    """

    total: int
    by_type: dict[str, int]
    by_severity: dict[str, int]
    lines_checked: int
    lines_covered: int
    attestation_window_days: int

    pre_automation: int = 0
    """Exceptions explained by predating the control rather than by a miss."""

    actionable: int = 0
    """``total`` less the pre-automation ones -- what an operations queue should
    show. Served alongside ``total`` rather than instead of it: the full number
    is the business case for the initiative, and the actionable number is the
    work. Quoting either one alone misleads in a different direction."""

    attestation_cutover_date: date | None = None
    """The cutover the counts above were measured against, so a number can be
    traced to the rule that produced it. None means none is configured."""

    acquisitions_checked: int = 0
    """New 80-series purchases -- new-buy PRs and direct PO lines -- the
    UNJUSTIFIED_ACQUISITION check ran over."""

    justification_window_days: int = 0
    justification_cutover_date: date | None = None


def build_exceptions(
    lines,
    coverage: AttestationCoverage,
    *,
    cutover: date | None = None,
    acquisitions: Sequence[NewAcquisition] = (),
    justifications: Sequence[JustificationRecord] = (),
    justification_window_days: int = 30,
    justification_cutover: date | None = None,
    overdue_grace_days: int = 7,
    repair_requests: Sequence[RepairRequest] = (),
    served_requests: Sequence[RepairRequest] = (),
    declarations=(),
    reference_date: date | None = None,
    declaration_grace_days: int = 14,
) -> tuple[list[ExceptionItem], ExceptionStats]:
    """The whole exception queue for one snapshot, and its counts.

    ``repair_requests`` (the open repair PRs with no PO yet) are read only as
    repairs a purchase overlapped: nothing is declared on them in phase 1, so
    no other check looks at them -- except, since 27-Oct-2026, as the repairs
    a Repairable declaration is waiting for. ``served_requests`` are the repair
    PRs the register serves and the coverage checked; the PR findings read
    those. ``declarations`` are the current declarations. The two grace-day
    checks run only when ``reference_date`` is given.
    """
    items = missing_attestations(lines, coverage, cutover=cutover)
    items += unjustified_acquisitions(
        acquisitions,
        lines,
        justifications,
        window_days=justification_window_days,
        cutover=justification_cutover,
        repair_requests=repair_requests,
    )
    items += overdue_repairs(lines, grace_days=overdue_grace_days)
    items += repaired_against_assessment(lines, coverage, requests=served_requests)
    if reference_date is not None:
        items += undeclared_repair_requests(
            served_requests,
            coverage,
            reference_date=reference_date,
            grace_days=declaration_grace_days,
            cutover=cutover,
        )
        items += declarations_without_repair(
            declarations,
            lines,
            repair_requests,
            reference_date=reference_date,
            grace_days=declaration_grace_days,
        )

    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for item in items:
        by_type[item.type] = by_type.get(item.type, 0) + 1
        by_severity[item.severity] = by_severity.get(item.severity, 0) + 1

    historical = sum(1 for item in items if item.pre_automation)
    stats = ExceptionStats(
        total=len(items),
        by_type=by_type,
        by_severity=by_severity,
        lines_checked=coverage.checked,
        lines_covered=len(coverage.covered),
        attestation_window_days=coverage.window_days,
        pre_automation=historical,
        actionable=len(items) - historical,
        attestation_cutover_date=cutover,
        acquisitions_checked=len(acquisitions),
        justification_window_days=justification_window_days,
        justification_cutover_date=justification_cutover,
    )
    return items, stats
