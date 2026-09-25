"""I07 Quarterly Deep-Dive Report -- raw/detail Excel export.

This is NOT the management report. The JSON returned by
``GET /v1/i7/reports/quarterly/{quarter}`` (``QuarterlyReport``, see
``app/schemas/i7/reports.py``) is the management report the frontend renders;
this module produces a secondary, raw-detail workbook for a reader who wants
the underlying ``i7_recommendation`` rows behind Section 12's baseline
comparison in spreadsheet form -- one sheet per section's row-level detail,
not a re-presentation of the JSON's aggregates.

Fresh ``openpyxl`` usage, written for this endpoint. ``scripts/export_verification_sheet.py``
was read only as a style reference for this codebase's existing openpyxl
conventions (header row styling, freeze panes) -- nothing is imported from it
and its structure is not reused; this module has its own, report-specific
sheet layout.
"""

from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.worksheet import Worksheet

from app.schemas.i7.reports import QuarterlyReport

_HEADER_FONT = Font(bold=True)


def _write_header(sheet: Worksheet, headers: list[str]) -> None:
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = _HEADER_FONT
    sheet.freeze_panes = "A2"


def _availability(value) -> str:
    """Never render an unavailable value as blank or 0 in the export either --
    the same discipline the JSON schema enforces (see ``AvailabilityStatus``'s
    docstring)."""
    return "" if value is None else value


def build_quarterly_report_workbook(report: QuarterlyReport) -> BytesIO:
    """Build the raw/detail export workbook for one generated report.

    Sheets:
    - ``Metadata``             report identity + upstream run ids
    - ``Baseline Comparison``  Section 12's four rows, in full, including every
      availability count -- the detail behind the management report's
      headline table
    - ``Scope & Data Quality`` Section 3's population/coverage figures
    - ``Limitations``          Section 14's plain-language items, verbatim

    Returns an in-memory ``BytesIO`` positioned at 0, ready to stream as an
    ``StreamingResponse`` body -- never written to a temp file on disk.
    """
    workbook = Workbook()

    meta_sheet = workbook.active
    meta_sheet.title = "Metadata"
    _write_header(meta_sheet, ["Field", "Value"])
    metadata = report.metadata
    meta_sheet.append(["Quarter", metadata.quarter])
    meta_sheet.append(["Period Start", metadata.period_start.isoformat()])
    meta_sheet.append(["Period End", metadata.period_end.isoformat()])
    meta_sheet.append(["Generated At", metadata.generated_at.isoformat()])
    meta_sheet.append(["Report Version", metadata.report_version])
    meta_sheet.append(["Feature Run ID", _availability(metadata.feature_run_id)])
    meta_sheet.append(["Forecast Run ID", _availability(metadata.forecast_run_id)])
    meta_sheet.append(["Inventory Run ID", _availability(metadata.inventory_run_id)])
    meta_sheet.append(["OAR Run ID", _availability(metadata.oar_run_id)])

    baseline_sheet = workbook.create_sheet("Baseline Comparison")
    _write_header(
        baseline_sheet,
        [
            "Metric",
            "Baseline Value",
            "I07 Recommendation Value",
            "Delta",
            "Delta %",
            "Both Available Count",
            "Baseline Missing Count",
            "Recommendation Missing Count",
            "Not Evaluable Count",
            "Availability Status",
        ],
    )
    for row in report.baseline_comparison.rows:
        baseline_sheet.append(
            [
                row.metric,
                _availability(row.baseline_value),
                _availability(row.recommendation_value),
                _availability(row.delta),
                _availability(row.delta_percentage),
                row.both_available_count,
                row.baseline_missing_count,
                row.recommendation_missing_count,
                row.not_evaluable_count,
                row.availability_status.value,
            ]
        )
    baseline_sheet.append([])
    baseline_sheet.append(["Baseline Lead Time Source", report.baseline_comparison.baseline_lead_time_source])
    baseline_sheet.append(
        ["I07 Lead Time Source", _availability(report.baseline_comparison.i07_lead_time_source)]
    )

    scope_sheet = workbook.create_sheet("Scope & Data Quality")
    _write_header(scope_sheet, ["Metric", "Value"])
    scope = report.scope_and_data_quality
    scope_sheet.append(["Total Records", scope.total_records])
    scope_sheet.append(["Classified Count", scope.classified_count])
    scope_sheet.append(["Classified %", _availability(scope.classified_percentage)])
    scope_sheet.append(["Unclassified Count", scope.unclassified_count])
    scope_sheet.append(["Unclassified %", _availability(scope.unclassified_percentage)])
    scope_sheet.append(["Criticality Populated Count", scope.criticality_populated_count])
    scope_sheet.append(["Criticality Populated %", _availability(scope.criticality_populated_percentage)])
    scope_sheet.append(["Lead Time Populated Count", scope.lead_time_populated_count])
    scope_sheet.append(["Lead Time Populated %", _availability(scope.lead_time_populated_percentage)])
    scope_sheet.append([])
    scope_sheet.append(["History Status", "Count"])
    for entry in scope.history_status_breakdown:
        scope_sheet.append([entry.history_status, entry.count])

    limitations_sheet = workbook.create_sheet("Limitations")
    _write_header(limitations_sheet, ["Limitation"])
    for item in report.limitations.items:
        limitations_sheet.append([item])

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer
