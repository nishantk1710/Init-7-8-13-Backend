"""FR-6 reconciliation against the two SAP reports, compared like with like.

The validation endpoint used to compare two counts nobody had checked were the
same kind of thing: the procurement chain's row count against a number typed in
as "ZMM065", and the GRNI count against a number typed in as "30-Day GR". Both
reports are now in the database, and once read, neither turned out to measure
what the old comparison assumed.

ZMM065 -- an aging classification, as of the day it was run
-----------------------------------------------------------
Each row carries the report's own class (``stock_type``: Fast / Slow / Non
Moving, or a non-aging status such as OBSOLETE and INSURANCE), the last
goods-issue date and ``days`` since it. ``last_gi_date + days`` is the same day
on every row -- the report's run date -- so the platform's band is computed
**as of that day**, not today, from the same movement history the WATCH bands
use (reversal-aware, ``movements.last_unreversed_date``). Non-aging statuses
are not bands and are left out of the comparison, counted.

It reconciles per band (counts within tolerance) and per material (where the
two disagree, and the likeliest reason). This needs no MARC.DISMM: it compares
the report's own population, whatever its MRP type.

The 30-Day GR Report -- receipts in the last 30 days, not GRs left unissued
---------------------------------------------------------------------------
Its rows are goods receipts posted in the 30 days up to the run date (the
latest ``post_date``), for one plant per export, filtered by criteria the
export does not state -- the July export lists 108 of the ~1,200 receipts the
extract holds for that plant and window. So its total is not comparable with
anything the platform counts, and least of all with the GRNI flag, which is
about receipts *older* than 30 days. What it can reconcile is whether the
platform's SAP data contains each receipt the report lists: the PO line, and a
goods receipt posted on that date. That is the check.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

from app.initiatives.i13.aging import classify_aging_band
from app.initiatives.i13.config import AgingThresholds
from app.initiatives.i13.models import AgingBand, ReconciliationResult
from app.initiatives.i13.reconciliation import reconcile
from app.integrations.sap.postgres_reports import Gr30DayRow, Zmm065Row

Key = tuple[str, str]

#: The report's aging labels, mapped to the platform's bands. Anything else in
#: ``stock_type`` (OBSOLETE, INSURANCE) is a status, not a band.
ZMM065_BANDS: dict[str, AgingBand] = {
    "FAST MOVING": AgingBand.FAST,
    "SLOW MOVING": AgingBand.SLOW,
    "NON MOVING": AgingBand.NON_MOVING,
}

BAND_LABEL: dict[AgingBand, str] = {
    AgingBand.FAST: "Fast moving",
    AgingBand.SLOW: "Slow moving",
    AgingBand.NON_MOVING: "Non-moving",
}

#: How many disagreeing rows a response lists. The counts are always complete.
DETAIL_LIMIT = 200


class Zmm065MismatchReason(str, Enum):
    #: The platform holds no goods movement of any kind for this material-plant,
    #: so it has nothing to compare. Usually a data-coverage gap -- the extract
    #: in the database is not the population the report was run against --
    #: rather than a disagreement about this material.
    MATERIAL_NOT_IN_PLATFORM_DATA = "MATERIAL_NOT_IN_PLATFORM_DATA"
    #: The report's last issue predates the movement history the platform
    #: holds, so the platform cannot see it -- the history-depth gap (FRS 7.2).
    LAST_ISSUE_BEFORE_PLATFORM_HISTORY = "LAST_ISSUE_BEFORE_PLATFORM_HISTORY"
    #: Both know a last issue, and they are different dates.
    LAST_ISSUE_DATE_DIFFERS = "LAST_ISSUE_DATE_DIFFERS"
    #: The report shows no last issue, the platform finds one.
    REPORT_HAS_NO_LAST_ISSUE = "REPORT_HAS_NO_LAST_ISSUE"
    #: Same last-issue date, different class: ZMM065 applied its own rule.
    REPORT_CLASSIFICATION_DIFFERS = "REPORT_CLASSIFICATION_DIFFERS"


class GrConfirmation(str, Enum):
    CONFIRMED = "CONFIRMED"
    #: No PO line with this number/item in the platform's extract for 1300/1500.
    PO_LINE_NOT_FOUND = "PO_LINE_NOT_FOUND"
    #: The PO line is there, but no goods receipt on the report's date.
    NO_RECEIPT_ON_POST_DATE = "NO_RECEIPT_ON_POST_DATE"


@dataclass(frozen=True)
class Zmm065Mismatch:
    material: str
    plant: str
    report_band: AgingBand
    platform_band: AgingBand
    report_last_issue_date: date | None
    platform_last_issue_date: date | None
    reason: Zmm065MismatchReason


@dataclass(frozen=True)
class Zmm065Validation:
    report_date: date | None
    rows_in_report: int
    compared: int
    agreed: int
    excluded_non_aging: Mapping[str, int]
    results: tuple[ReconciliationResult, ...]
    mismatch_reasons: Mapping[str, int]
    mismatches: tuple[Zmm065Mismatch, ...] = field(default=())

    @property
    def agreement_pct(self) -> float | None:
        return round(100 * self.agreed / self.compared, 1) if self.compared else None


@dataclass(frozen=True)
class GrReceiptCheck:
    post_date: date
    material: str
    po_number: str
    po_item: str
    status: GrConfirmation
    plant: str | None


@dataclass(frozen=True)
class Gr30DayValidation:
    report_date: date | None
    window_start: date | None
    rows_in_report: int
    confirmed: int
    plants: Mapping[str, int]
    #: Receipts the platform holds for the report's plant(s) and window -- context
    #: for how filtered the export is, not a reconciled figure.
    platform_receipts_in_window: int
    result: ReconciliationResult
    unconfirmed: tuple[GrReceiptCheck, ...] = field(default=())


def zmm065_report_date(rows: list[Zmm065Row]) -> date | None:
    """The run date: ``last_gi_date + days``, the value most rows agree on."""
    implied = Counter(
        row.last_gi_date + timedelta(days=row.days)
        for row in rows
        if row.last_gi_date is not None and row.days is not None
    )
    return implied.most_common(1)[0][0] if implied else None


def validate_zmm065(
    rows: list[Zmm065Row],
    *,
    last_issue_as_of: Callable[[Key, date], date | None],
    has_movements: Callable[[Key], bool],
    history_start: date | None,
    thresholds: AgingThresholds,
    tolerance_pct: float,
) -> Zmm065Validation:
    """Reconcile the platform's aging band with ZMM065's, as of the report's date.

    ``last_issue_as_of(key, day)`` answers the platform's last unreversed issue
    for a material-plant on that day; ``has_movements(key)`` whether the
    platform holds any goods movement for it at all; ``history_start`` is the
    earliest posting date in the platform's movement history.
    """
    report_date = zmm065_report_date(rows)
    excluded: Counter[str] = Counter()
    report_counts: Counter[AgingBand] = Counter()
    platform_counts: Counter[AgingBand] = Counter()
    reasons: Counter[str] = Counter()
    mismatches: list[Zmm065Mismatch] = []
    agreed = 0

    for row in rows:
        report_band = ZMM065_BANDS.get(row.stock_type.upper())
        if report_band is None:
            excluded[row.stock_type or "(blank)"] += 1
            continue
        if report_date is None:
            continue
        platform_last = last_issue_as_of((row.material, row.plant), report_date)
        days = (report_date - platform_last).days if platform_last else None
        platform_band = classify_aging_band(days, thresholds)

        report_counts[report_band] += 1
        platform_counts[platform_band] += 1
        if platform_band is report_band:
            agreed += 1
            continue

        reason = (
            Zmm065MismatchReason.MATERIAL_NOT_IN_PLATFORM_DATA
            if not has_movements((row.material, row.plant))
            else _mismatch_reason(row.last_gi_date, platform_last, history_start)
        )
        reasons[reason.value] += 1
        if len(mismatches) < DETAIL_LIMIT:
            mismatches.append(
                Zmm065Mismatch(
                    material=row.material,
                    plant=row.plant,
                    report_band=report_band,
                    platform_band=platform_band,
                    report_last_issue_date=row.last_gi_date,
                    platform_last_issue_date=platform_last,
                    reason=reason,
                )
            )

    results = tuple(
        reconcile(
            f"ZMM065 · {BAND_LABEL[band]}",
            platform_counts[band],
            report_counts[band] if report_date is not None else None,
            tolerance_pct=tolerance_pct,
        )
        for band in (AgingBand.FAST, AgingBand.SLOW, AgingBand.NON_MOVING)
    )
    return Zmm065Validation(
        report_date=report_date,
        rows_in_report=len(rows),
        compared=sum(report_counts.values()),
        agreed=agreed,
        excluded_non_aging=dict(excluded),
        results=results,
        mismatch_reasons=dict(reasons),
        mismatches=tuple(mismatches),
    )


def _mismatch_reason(
    report_last: date | None, platform_last: date | None, history_start: date | None
) -> Zmm065MismatchReason:
    if report_last is None:
        return Zmm065MismatchReason.REPORT_HAS_NO_LAST_ISSUE
    if platform_last is None and history_start is not None and report_last < history_start:
        return Zmm065MismatchReason.LAST_ISSUE_BEFORE_PLATFORM_HISTORY
    if platform_last != report_last:
        return Zmm065MismatchReason.LAST_ISSUE_DATE_DIFFERS
    return Zmm065MismatchReason.REPORT_CLASSIFICATION_DIFFERS


def validate_gr_30day(
    rows: list[Gr30DayRow],
    *,
    po_line_plant: Mapping[Key, str],
    receipt_dates: Mapping[Key, frozenset[date] | set[date] | tuple[date, ...]],
    tolerance_pct: float,
) -> Gr30DayValidation:
    """Confirm each receipt the report lists against the platform's SAP data.

    ``po_line_plant`` is (PO, item) -> plant for every in-scope PO line the
    platform holds; ``receipt_dates`` is (PO, item) -> the posting dates of its
    goods receipts (EKBE category E, movement 101).
    """
    report_date = max((row.post_date for row in rows), default=None)
    window_start = report_date - timedelta(days=29) if report_date else None
    checks: list[GrReceiptCheck] = []
    for row in rows:
        key = (row.po_number, row.po_item)
        plant = po_line_plant.get(key)
        if plant is None:
            status = GrConfirmation.PO_LINE_NOT_FOUND
        elif row.post_date not in receipt_dates.get(key, ()):
            status = GrConfirmation.NO_RECEIPT_ON_POST_DATE
        else:
            status = GrConfirmation.CONFIRMED
        checks.append(
            GrReceiptCheck(
                post_date=row.post_date,
                material=row.material,
                po_number=row.po_number,
                po_item=row.po_item,
                status=status,
                plant=plant,
            )
        )

    confirmed = sum(1 for c in checks if c.status is GrConfirmation.CONFIRMED)
    plants = Counter(c.plant for c in checks if c.plant)
    in_window = 0
    if window_start is not None and plants:
        in_window = sum(
            1
            for key, plant in po_line_plant.items()
            if plant in plants and any(window_start <= d <= report_date for d in receipt_dates.get(key, ()))
        )
    return Gr30DayValidation(
        report_date=report_date,
        window_start=window_start,
        rows_in_report=len(rows),
        confirmed=confirmed,
        plants=dict(plants),
        platform_receipts_in_window=in_window,
        result=reconcile(
            "30-Day GR Report · receipts confirmed",
            confirmed,
            len(rows) if rows else None,
            tolerance_pct=tolerance_pct,
        ),
        unconfirmed=tuple(c for c in checks if c.status is not GrConfirmation.CONFIRMED)[:DETAIL_LIMIT],
    )
