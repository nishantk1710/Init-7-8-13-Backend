"""Adoption results in words a planner can read.

Takes the evaluator's :class:`AdoptionResult` and what SAP's change history says
about each tracked field, and produces one line per field -- which field, what
SAP held before, what it holds now, when it changed and how many days after the
approval. Pure functions: no database, no SAP, no clock (the caller passes the
dates), so every sentence is testable with plain values.

Nothing here decides adoption. The status comes from ``adoption.py``; this
module only describes it. Where SAP gave no evidence about a field the line says
so -- it never reads "not changed" as a fact when the real answer is "nothing was
recorded".
"""

from datetime import date

from app.initiatives.i7.recommendations.types import (
    AdoptionFieldChange,
    AdoptionFieldReport,
    AdoptionResult,
    AdoptionStatus,
)

# expected-name (as the evaluators emit it) -> (key, label, SAP field)
_FIELDS: dict[str, tuple[str, str, str]] = {
    "reorder_point": ("reorder_point", "Reorder point", "minbe"),
    "safety_stock": ("safety_stock", "Safety stock", "eisbe"),
    "maximum_stock": ("maximum_stock", "Max stock", "mabst"),
    "mrp_type": ("mrp_type", "MRP type", "dismm"),
    # The conversion check asks whether these two are filled in.
    "minbe_populated": ("reorder_point", "Reorder point", "minbe"),
    "mabst_populated": ("maximum_stock", "Max stock", "mabst"),
}

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _expected_text(name: str, value: str) -> str:
    if name.endswith("_populated"):
        return "filled in"
    return value


def build_field_reports(
    result: AdoptionResult,
    changes: dict[str, AdoptionFieldChange] | None,
    anchor: date | None,
) -> tuple[AdoptionFieldReport, ...]:
    """One report per field the check looked at, in the order the check lists them."""
    reports: list[AdoptionFieldReport] = []
    for name, value in result.expected:
        mapped = _FIELDS.get(name)
        if mapped is None:
            continue
        key, label, sap_field = mapped
        change = (changes or {}).get(sap_field)

        matched: bool | None
        if name in result.matched_fields:
            matched = True
        elif name in result.mismatched_fields:
            matched = False
        else:
            matched = None

        days_after = (
            (change.changed_on - anchor).days
            if change is not None and change.changed_on is not None and anchor is not None
            else None
        )
        reports.append(
            AdoptionFieldReport(
                key=key,
                label=label,
                expected=_expected_text(name, value),
                before=change.before if change else None,
                now=change.now if change else None,
                changed_on=change.changed_on if change else None,
                changed_time=change.changed_time if change else None,
                days_after=days_after,
                matched=matched,
            )
        )
    return tuple(reports)


def _when(report: AdoptionFieldReport) -> str:
    if report.changed_on is None:
        return ""
    day = f"{report.changed_on.day:02d} {_MONTHS[report.changed_on.month - 1]} {report.changed_on.year}"
    return f" on {day}" + (f", {report.changed_time}" if report.changed_time else "")


def _days(report: AdoptionFieldReport, anchor_name: str) -> str:
    if report.days_after is None:
        return ""
    if report.days_after == 0:
        return f" (the same day as the {anchor_name})"
    plural = "day" if abs(report.days_after) == 1 else "days"
    direction = "after" if report.days_after > 0 else "before"
    return f" ({abs(report.days_after)} {plural} {direction} the {anchor_name})"


def _has_change(report: AdoptionFieldReport) -> bool:
    return report.now is not None


def plain_detail(
    result: AdoptionResult,
    reports: tuple[AdoptionFieldReport, ...],
    *,
    anchor_name: str,
    window_days: int | None,
    window_open: bool,
) -> str:
    """The Detail column: one line per field, or one sentence when nothing changed."""
    if not reports:
        return result.detail

    if not any(_has_change(report) for report in reports):
        if window_open or window_days is None:
            return "No change recorded in SAP yet."
        return f"No change recorded in SAP within {window_days} days of the {anchor_name}."

    lines: list[str] = []
    for report in reports:
        if not _has_change(report):
            lines.append(f"{report.label}: no change recorded in SAP.")
            continue
        before = report.before if report.before not in (None, "") else "—"
        line = f"{report.label}: {before} → {report.now}{_when(report)}{_days(report, anchor_name)}"
        if report.matched is False and report.expected:
            line += f", but {report.expected} was approved"
        lines.append(line + ".")
    return "\n".join(lines)


def check_type_label(reports: tuple[AdoptionFieldReport, ...], is_conversion: bool) -> str:
    """The Check type column: the fields checked, joined with a dot."""
    labels = [report.label for report in reports]
    if not labels:
        return "Min-Max conversion" if is_conversion else "Planning parameters"
    return " · ".join(labels)


def is_pending(status: AdoptionStatus, window_open: bool) -> bool:
    """Whether the row is still waiting for SAP: no evidence, window not over."""
    return status is AdoptionStatus.UNKNOWN and window_open
