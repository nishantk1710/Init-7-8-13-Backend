"""FR-7 on the register -- the justifications, and the unjustified purchases,
that belong to each repair line.

The register's Justification column, and the panel on the repair detail page.
Both used to live on a separate Justifications screen, which was folded into
the register on 08-Oct-2026.

A justification is not recorded against a repair line
------------------------------------------------------
The assistant writes one against a material, a plant and a session, when the
requester goes ahead with a new unit although a repairable one exists. So a
line is given a justification by rule, not by key:

* **Recorded** -- a NEW_ACQUISITION justification for the same material and
  plant was recorded while this line was out: on or after the day it was
  raised, and before the unit came back. The same reading of "out" as
  :func:`app.initiatives.i8.acquisitions.open_repair_at`.
* **Missing** -- an ``UNJUSTIFIED_ACQUISITION`` exception names this line: a new
  unit was bought while it was out and no reason was recorded. Taken from the
  exception queue as built, not re-derived, so the register and the queue
  cannot disagree about which lines are short a reason.

A line can carry both (one purchase explained, another not); it then reads
Missing, because that is the one somebody has to act on.

What this cannot show
----------------------
FR-6 also counts a unit on the shelf as "a repairable unit exists", and the
assistant asks for a justification then too. Nothing is out for repair in that
case, so there is no register line to attach it to. Those justifications appear
only in the log on the Overview, which is why the log stayed when its screen
went.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime

from app.initiatives.i8.acquisitions import JustificationRecord
from app.initiatives.i8.exceptions import ExceptionItem, ExceptionType
from app.initiatives.i8.register import RepairLine

RECORDED = "RECORDED"
MISSING = "MISSING"


@dataclass(frozen=True)
class LineJustification:
    """What the register shows in one line's Justification cell."""

    recorded: tuple[JustificationRecord, ...] = ()
    """Justifications recorded while the line was out, newest first."""

    unjustified: tuple[ExceptionItem, ...] = ()
    """``UNJUSTIFIED_ACQUISITION`` exceptions raised against the line."""

    @property
    def status(self) -> str:
        return MISSING if self.unjustified else RECORDED


def was_out_on(line: RepairLine, day: date) -> bool:
    """Whether the unit on this line was away for repair on ``day``.

    Raised on or before the day, and not yet received back by then. A line with
    no raised date cannot be placed in time, so it is never "out".
    """
    if line.raised_at is None or line.raised_at > day:
        return False
    return line.received_at is None or line.received_at > day


def _newest_first(record: JustificationRecord) -> tuple:
    return (record.recorded_at or datetime.min, record.recorded_on)


def by_line(
    lines: Iterable[RepairLine],
    justifications: Iterable[JustificationRecord],
    exceptions: Iterable[ExceptionItem],
) -> dict[tuple[str, str], LineJustification]:
    """(document, item) -> the line's justification, for every line that has one.

    A line absent from the result has neither: no reason was recorded while it
    was out, and no new purchase overlapped it without one.
    """
    by_part: dict[tuple[str, str | None], list[JustificationRecord]] = {}
    for record in justifications:
        by_part.setdefault((record.material_id, record.plant), []).append(record)

    missing: dict[tuple[str, str], list[ExceptionItem]] = {}
    for item in exceptions:
        if item.type == ExceptionType.UNJUSTIFIED_ACQUISITION.value:
            missing.setdefault((item.purchasing_document, item.item), []).append(item)

    result: dict[tuple[str, str], LineJustification] = {}
    for line in lines:
        recorded = tuple(
            sorted(
                (
                    record
                    # A line with no plant matches nothing: every
                    # justification names one.
                    for record in by_part.get((line.material_id, line.plant), ())
                    if was_out_on(line, record.recorded_on)
                ),
                key=_newest_first,
                reverse=True,
            )
        )
        unjustified = tuple(missing.get(line.key, ()))
        if recorded or unjustified:
            result[line.key] = LineJustification(recorded=recorded, unjustified=unjustified)
    return result


def index_size(index: Mapping[tuple[str, str], LineJustification]) -> dict[str, int]:
    """Counts by status, for the build log."""
    counts: dict[str, int] = {}
    for value in index.values():
        counts[value.status] = counts.get(value.status, 0) + 1
    return counts
