"""W5.2 Layer 4 -- vendor turnaround analytics.

Grouped over the register that :mod:`register` already built, not over a fresh
query. The repair-line rule is applied once, in one place, and everything
downstream reads its output -- so a change to the Pstyp convention cannot leave
the register and the vendor analytics disagreeing.

The rule that makes the average honest
--------------------------------------
**Only completed repairs count toward the turnaround average.** Including open
ones biases it toward whatever happens to be in flight, and it biases it in the
worst possible direction: the slowest vendor looks fastest, because its repairs
have not come back yet to be counted. ``openCount`` and ``overdueCount`` report
the in-flight work separately, which is the honest way to show it.

Vendor identity, and what is missing
------------------------------------
Vendors come from the PO header (EKKO), so the 455 repair lines with no header
have no vendor at all and are grouped under :data:`UNKNOWN_VENDOR` rather than
dropped -- work with an unknown vendor is still work in progress, and silently
excluding it would under-report the register.

Names come from LFA1, which in this extract holds 106 vendors and resolves only
4 of the 61 that appear on repair POs. The other 57 are served with their vendor
code as the display name. That is a data gap to report, not a reason to hide the
vendor.

POPIA: this module reads ``v_lfa1``, which promotes four business-entity fields
out of the 179 columns ``raw_lfa1`` carries. Personal contact details, date of
birth and ownership data are not in the view and cannot reach an API response.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean

from app.initiatives.i8.register import RepairLine

UNKNOWN_VENDOR = "UNKNOWN"
"""Bucket for repair lines whose PO header is not in the extract."""


@dataclass(frozen=True)
class VendorTurnaround:
    """Turnaround performance for one vendor."""

    vendor: str
    vendor_name: str | None
    """None where LFA1 does not know this vendor. The UI falls back to the code."""

    total_lines: int
    open_count: int
    overdue_count: int
    received_count: int

    avg_turnaround_days: float | None
    """Mean days from dispatch to receipt, over COMPLETED repairs only.

    None when no completed repair for this vendor has both a dispatch and a
    receipt date -- which is common here, because dispatch movements can be
    attached to only 286 of the 1,225 lines.
    """

    min_turnaround_days: int | None
    max_turnaround_days: int | None
    turnaround_sample: int
    """How many repairs the average is actually computed from. Published so a
    mean of one is not read as a vendor's settled performance."""

    on_time_rate: float | None
    """Received on or before the promised date, over received lines with a date.

    None when no received line for this vendor has a due date to judge against.
    """

    avg_days_open: float | None
    """Mean age of this vendor's OPEN lines. The in-flight picture, kept apart
    from the completed-repair average on purpose."""


def vendor_turnaround(lines: Sequence[RepairLine]) -> list[VendorTurnaround]:
    """Per-vendor analytics over an already-built register.

    Sorted by open count descending, so the vendor holding the most VZI stock
    is the first thing anyone reads.
    """
    grouped: dict[str, list[RepairLine]] = {}
    for line in lines:
        grouped.setdefault(line.vendor or UNKNOWN_VENDOR, []).append(line)

    results: list[VendorTurnaround] = []
    for vendor, vendor_lines in grouped.items():
        received = [line for line in vendor_lines if not line.is_open]
        open_lines = [line for line in vendor_lines if line.is_open]

        # Completed repairs only, and only those where both ends of the clock
        # are known -- days_at_vendor is None when there is no dispatch date.
        turnarounds = [
            line.days_at_vendor
            for line in received
            if line.days_at_vendor is not None and line.dispatched_at is not None
        ]

        judgeable = [line for line in received if line.due_date is not None]
        on_time = [
            line
            for line in judgeable
            if line.received_at is not None and line.received_at <= line.due_date
        ]

        open_ages = [
            line.days_open for line in open_lines if line.days_open is not None
        ]

        name = next(
            (line.vendor_name for line in vendor_lines if line.vendor_name), None
        )

        results.append(
            VendorTurnaround(
                vendor=vendor,
                vendor_name=name,
                total_lines=len(vendor_lines),
                open_count=len(open_lines),
                overdue_count=sum(1 for line in open_lines if line.is_overdue),
                received_count=len(received),
                avg_turnaround_days=(
                    round(mean(turnarounds), 1) if turnarounds else None
                ),
                min_turnaround_days=min(turnarounds) if turnarounds else None,
                max_turnaround_days=max(turnarounds) if turnarounds else None,
                turnaround_sample=len(turnarounds),
                on_time_rate=(
                    round(len(on_time) / len(judgeable), 3) if judgeable else None
                ),
                avg_days_open=round(mean(open_ages), 1) if open_ages else None,
            )
        )

    results.sort(key=lambda v: (-v.open_count, -v.total_lines, v.vendor))
    return results
