"""W5.3 -- the exception queue, and the missing-attestation check.

Recording an attestation is half of W5.3. **Detecting its absence is the half
with the value**, because it is what makes an invisible control failure into a
number somebody can act on.

The check, in plain English
---------------------------
For every repair line in the register: if no attestation matches its material
and plant inside the configured window, raise ``MISSING_ATTESTATION``.

It is a **batch check that runs once per refresh cycle**, not per request. The
register is 1,225 lines, the snapshot is already cached for exactly this reason,
and a per-request version would put a query per line onto the page W5.4 renders.

The number this produces is the business case
----------------------------------------------
At UAT this fires on essentially every historical repair line, because no
attestation has ever existed for any of them -- the control did not exist before
this platform. That is correct behaviour and a terrible demo.

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
plant, with no NEW_ACQUISITION justification recorded near it. The purchases
and the repairs they overlapped come from :mod:`app.initiatives.i8.acquisitions`,
which also says why stock on hand cannot be judged for a past purchase.

``MISSING_SESSION_ID`` (FR-8) is ours too and is **not raised yet**, for one
reason outside this repository: it needs the session id read back off the
reservation, and RESB.BEDNR is not exposed on ReservationItemSet. It stays in
:class:`ExceptionType` so adding the detector is new code, not a migration.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum

from app.core.logging import get_logger
from app.initiatives.i8.acquisitions import (
    JustificationRecord,
    NewAcquisition,
    open_repair_at,
    repairs_by_material_plant,
)
from app.initiatives.i8.attestation import AttestationCoverage
from app.initiatives.i8.register import RepairLine

logger = get_logger(__name__)


class ExceptionType(str, Enum):
    """Every exception the queue can carry. All three are I08's -- FR-4 and FR-8.

    ``MISSING_SESSION_ID`` is declared and not yet raised; see the module note.
    """

    MISSING_ATTESTATION = "MISSING_ATTESTATION"
    """A repair line with no condition-to-repair attestation. FR-4 / FR-8."""

    MISSING_SESSION_ID = "MISSING_SESSION_ID"
    """FR-8, an 80-series reservation with no valid session id. Not raised yet:
    blocked on RESB.BEDNR being exposed on ReservationItemSet (SAP team)."""

    UNJUSTIFIED_ACQUISITION = "UNJUSTIFIED_ACQUISITION"
    """FR-8, a new unit bought while a repair was open, with no justification."""


#: Which types this module actually raises. The difference between this and
#: ExceptionType is the point: the vocabulary is wider than the implementation,
#: and that is recorded rather than implied.
RAISED_BY_I8: frozenset[ExceptionType] = frozenset(
    {ExceptionType.MISSING_ATTESTATION, ExceptionType.UNJUSTIFIED_ACQUISITION}
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

    is_open_repair: bool
    """Whether the unit is still out. An exception on an open line is
    actionable; one on a closed line can only be counted."""

    pre_automation: bool = False
    """Raised on a line that predates the attestation control itself.

    Not a lesser finding -- a different one. It says the gap is explained by
    when the line was raised, not by anyone failing to do something, and it is
    what keeps 1,225 historical rows from reading as 1,225 violations. False
    whenever no cutover date is configured, which is the current state.
    """

    acquisition_document: str | None = None
    acquisition_item: str | None = None
    """``UNJUSTIFIED_ACQUISITION`` only: the purchase line that bought the new
    unit. ``purchasing_document``/``item`` are then the repair it overlapped."""


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
                # An open line is a part that is out there now with no assessment
                # on record, which someone can still do something about. A closed
                # one is history: worth counting, not worth paging anybody over.
                #
                # A pre-automation line is INFO whether it is open or not: there
                # was no form to fill in when it was raised, so nothing about it
                # is anybody's to action now.
                severity=(
                    Severity.INFO
                    if historical or not line.is_open
                    else Severity.WARNING
                ).value,
                material_id=line.material_id,
                description=line.description,
                plant=line.plant,
                purchasing_document=line.purchasing_document,
                item=line.item,
                title=(
                    "Raised before Spares Automation"
                    if historical
                    else "No condition-to-repair attestation"
                ),
                detail=(
                    (
                        f"This line was raised on "
                        f"{line.raised_at.isoformat() if line.raised_at else 'an unknown date'}, "
                        f"before the condition-to-repair attestation was introduced on "
                        f"{cutover.isoformat() if cutover else 'the cutover date'}. "
                        "No assessment is on record because none was asked for at "
                        "the time; this is not an outstanding action."
                    )
                    if historical
                    else (
                        f"No attestation was found for material {line.material_id} at "
                        f"plant {line.plant} within {coverage.window_days} days of "
                        f"{line.raised_at.isoformat() if line.raised_at else 'the line being raised'}. "
                        "The part was sent for repair with no recorded assessment of "
                        "its condition."
                    )
                ),
                raised_at=line.raised_at,
                is_open_repair=line.is_open,
                pre_automation=historical,
            )
        )

    logger.info(
        "I08 exceptions: %d MISSING_ATTESTATION of %d repair lines checked "
        "(%d open, %d closed, %d pre-automation)",
        len(items),
        coverage.checked,
        sum(1 for i in items if i.is_open_repair),
        sum(1 for i in items if not i.is_open_repair),
        sum(1 for i in items if i.pre_automation),
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
    the assistant normally records the reason before the PO is raised.
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


def unjustified_acquisitions(
    acquisitions: Sequence[NewAcquisition],
    lines: Sequence[RepairLine],
    justifications: Sequence[JustificationRecord],
    *,
    window_days: int,
    cutover: date | None = None,
) -> list[ExceptionItem]:
    """Raise UNJUSTIFIED_ACQUISITION for every new purchase of a part that was
    already out for repair at the same plant, with no justification near it.

    A purchase with no overlapping repair is not an exception -- buying a part
    nobody is repairing is ordinary procurement. ``cutover`` labels purchases
    raised before the justification control existed, exactly as
    :func:`missing_attestations` labels pre-cutover repair lines.
    """
    repairs = repairs_by_material_plant(lines)
    items: list[ExceptionItem] = []

    for acquisition in sorted(acquisitions, key=lambda a: a.key):
        repair = open_repair_at(acquisition, repairs)
        if repair is None:
            continue

        exception_id = (
            f"EX-{ExceptionType.UNJUSTIFIED_ACQUISITION.value}-"
            f"{acquisition.purchasing_document}-{acquisition.item}"
        )
        if _justified(acquisition, exception_id, justifications, window_days):
            continue

        historical = (
            cutover is not None
            and acquisition.raised_at is not None
            and acquisition.raised_at < cutover
        )
        bought = (
            acquisition.raised_at.isoformat() if acquisition.raised_at else "an unknown date"
        )
        repair_state = (
            "it is still out"
            if repair.is_open
            else f"it came back {repair.received_at.isoformat()}"
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
                purchasing_document=repair.purchasing_document,
                item=repair.item,
                title=(
                    "Raised before Spares Automation"
                    if historical
                    else "New unit bought while a repair was open"
                ),
                detail=(
                    (
                        f"Purchase {acquisition.purchasing_document}/{acquisition.item} "
                        f"was raised on {bought}, before the justification control "
                        f"was introduced on {cutover.isoformat() if cutover else 'the cutover date'}. "
                        "No reason is on record because none was asked for at the "
                        "time; this is not an outstanding action."
                    )
                    if historical
                    else (
                        f"Purchase {acquisition.purchasing_document}/{acquisition.item} "
                        f"bought a new unit of {acquisition.material_id} at plant "
                        f"{acquisition.plant} on {bought}, while repair "
                        f"{repair.purchasing_document}/{repair.item}, raised "
                        f"{repair.raised_at.isoformat() if repair.raised_at else 'on an unknown date'}, "
                        f"was open ({repair_state}). No NEW_ACQUISITION justification "
                        f"was recorded for this material and plant within "
                        f"{window_days} days of the purchase."
                    )
                ),
                raised_at=acquisition.raised_at,
                is_open_repair=repair.is_open,
                pre_automation=historical,
                acquisition_document=acquisition.purchasing_document,
                acquisition_item=acquisition.item,
            )
        )

    logger.info(
        "I08 exceptions: %d UNJUSTIFIED_ACQUISITION of %d new purchases checked "
        "(%d pre-automation)",
        len(items),
        len(acquisitions),
        sum(1 for i in items if i.pre_automation),
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
    """New 80-series purchase lines the UNJUSTIFIED_ACQUISITION check ran over."""

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
) -> tuple[list[ExceptionItem], ExceptionStats]:
    """The whole exception queue for one snapshot, and its counts."""
    items = missing_attestations(lines, coverage, cutover=cutover)
    items += unjustified_acquisitions(
        acquisitions,
        lines,
        justifications,
        window_days=justification_window_days,
        cutover=justification_cutover,
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
