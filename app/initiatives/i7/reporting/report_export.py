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

    Then the technical sections the report carries (``Demand Classification``, ``Forecast Accuracy``, ``OAR``,
    ``Recommendations``, ``Approval Activity``) and -- on a report that has the management view (version 1.2) -- one
    sheet per part of it: ``Period``, ``KPI Cards``, ``Monthly Activity``, ``Position``, ``Trends``, ``Status
    Distribution``, ``Forecast vs Actual``, ``SAP Adoption``, ``Data Coverage`` and ``Definitions``.

    **One source of figures.** Every number on the management sheets is read from the saved report's ``management``
    section -- the same object the page and the PDF are drawn from -- and nothing is recalculated here, so the three cannot
    disagree. A figure that is not available is written as the words "Not available", never as a blank or a 0, and every
    KPI carries its unit, denominator, measurement date and basis.

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

    _write_technical_sheets(workbook, report)
    if report.management is not None:
        _write_management_sheets(workbook, report.management)

    for sheet in workbook.worksheets:
        _autosize(sheet)

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


# --- Shared helpers ---------------------------------------------------------------------------------

NOT_AVAILABLE = "Not available"


def _na(value):
    """A figure, or the words "Not available" -- never a blank or a 0 standing in for a missing value."""
    return NOT_AVAILABLE if value is None else value


def _iso(value):
    return NOT_AVAILABLE if value is None else value.isoformat()


def _table(sheet: Worksheet, headers: list[str], rows: list[list]) -> None:
    """A header row (styled, frozen when it is the sheet's first) followed by the rows."""
    sheet.append(headers)
    for cell in sheet[sheet.max_row]:
        cell.font = _HEADER_FONT
    for row in rows:
        sheet.append(row)


def _facts(sheet: Worksheet, facts: list[tuple[str, object]]) -> None:
    _table(sheet, ["Field", "Value"], [[label, value] for label, value in facts])


def _autosize(sheet: Worksheet) -> None:
    """Column widths that fit the content (capped), so a reader does not have to widen every column by hand."""
    widths: dict[str, int] = {}
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is not None:
                widths[cell.column_letter] = max(widths.get(cell.column_letter, 0), min(len(str(cell.value)), 60))
    for letter, width in widths.items():
        sheet.column_dimensions[letter].width = max(width + 2, 10)


# --- The technical sections the report already carried ---------------------------------------------


def _write_technical_sheets(workbook: Workbook, report: QuarterlyReport) -> None:
    demand = workbook.create_sheet("Demand Classification")
    _table(
        demand, ["Demand class", "Count", "Percentage"],
        [[row.demand_class, row.count, _na(row.percentage)] for row in report.demand_classification.by_class],
    )
    demand.append([])
    demand.append(["Total", report.demand_classification.total])

    forecasting = report.forecasting
    accuracy = workbook.create_sheet("Forecast Accuracy")
    _table(
        accuracy, ["Metric", "Status", "Value", "Populated rows", "Total rows"],
        [
            [name, metric.status.value, _na(metric.value), metric.populated_count, metric.total_count]
            for name, metric in (
                ("Mean absolute error", forecasting.mean_absolute_error),
                ("Pinball loss", forecasting.pinball_loss),
                ("Bias percentage", forecasting.bias_percentage),
                ("Fill rate", forecasting.fill_rate),
                ("Holding cost", forecasting.holding_cost),
            )
        ],
    )
    accuracy.append([])
    accuracy.append(["Total forecasts", forecasting.total_forecasts])
    accuracy.append(["Champion models", forecasting.champion_challenger.champion_count])
    accuracy.append(["Challenger models", forecasting.champion_challenger.challenger_count])
    accuracy.append(["Baseline models", forecasting.champion_challenger.baseline_count])
    accuracy.append(["MAPE", forecasting.mape_status.value])

    oar = workbook.create_sheet("OAR")
    _facts(
        oar,
        [
            ("OAR recommendations", report.oar.is_oar_true_count),
            ("Not OAR", report.oar.is_oar_false_count),
            ("OAR not recorded", report.oar.is_oar_null_count),
        ],
    )
    oar.append([])
    _table(
        oar, ["Min-Max conversion eligibility", "Count"],
        [[_availability(row.conversion_eligibility) or "Not decided", row.count] for row in report.oar.conversion_eligibility_breakdown],
    )

    recommendations = workbook.create_sheet("Recommendations")
    _table(
        recommendations, ["Status", "Count"], [[row.status, row.count] for row in report.recommendations.by_status]
    )
    recommendations.append([])
    recommendations.append(["Total records generated in the period", report.recommendations.total])

    approval = workbook.create_sheet("Approval Activity")
    _facts(
        approval,
        [
            ("Approval-history entries in the period", report.approval.ledger_entry_count),
            ("Distinct recommendations in approval", report.approval.distinct_recommendations_in_approval),
            ("Entries that left a recommendation pending", report.approval.pending_count),
            ("Entries that approved", report.approval.approved_count),
            ("Entries that rejected or sent back", report.approval.rejected_count),
        ],
    )
    approval.append([])
    approval.append(["These count dated approval-history entries, not recommendations by their current status."])


# --- The management view (report version 1.2) ------------------------------------------------------


def _write_management_sheets(workbook: Workbook, management) -> None:
    period = workbook.create_sheet("Period")
    _facts(period, [("Period status", management.period.status.value), ("Cutoff", _iso(management.period.cutoff)), ("Note", management.period.note)])
    period.append([])
    _table(
        period, ["Month", "State", "Has SAP consumption data", "Usable"],
        [[m.label, m.state, "Yes" if m.has_consumption_data else "No", "Yes" if m.usable else "No"] for m in management.period.months],
    )

    kpis = workbook.create_sheet("KPI Cards")
    _table(
        kpis,
        ["Card", "Value", "Unit", "Kind", "Status", "Measured on", "Covers", "Denominator", "Denominator is", "Basis",
         "What it means", "Why not available", "Note"],
        [
            [
                card.title, _na(card.value) if card.status.value == "AVAILABLE" else NOT_AVAILABLE, card.unit, card.kind.value,
                card.status.value, _iso(card.measured_on), card.period_label, _na(card.denominator), card.denominator_label or "",
                card.basis, card.explanation, card.unavailable_reason or "", card.note or "",
            ]
            for card in management.kpis
        ],
    )

    activity = workbook.create_sheet("Monthly Activity")
    labels = [m.label for m in management.monthly_activity.months]
    _table(
        activity, ["Metric", "Unit", "Kind", *labels, "Quarter total", "Status", "Months add up to the total", "Note"],
        [
            [
                row.label, row.unit, row.kind.value, *[_na(value) for value in row.months], _na(row.quarter_total),
                row.status.value, "" if row.reconciles is None else ("Yes" if row.reconciles else "No"), row.note or "",
            ]
            for row in management.monthly_activity.rows
        ],
    )
    activity.append([])
    activity.append(["A cell reading 'Not available' means the month has not started, is not covered by the staged data, or is not tracked. It is not a count of zero."])

    position = workbook.create_sheet("Position")
    _facts(position, [("Basis", management.position_basis), ("What the figures count", "The newest recommendation of each material-plant")])
    position.append([])
    _table(
        position,
        ["Plant", "Material-plants", "At stockout risk", "Judged", "Could not be judged", "Stockout measured as of", "Stockout reading stale",
         "Excess candidates", "Excess opportunity", "Working capital impact (estimate)", "Working capital currency",
         "Working capital: included", "Working capital: left out", "Currency"],
        [
            [
                "All plants" if entry.plant is None else entry.plant, entry.figures.population, entry.figures.stockout.at_risk,
                entry.figures.stockout.evaluated, entry.figures.stockout.insufficient_data, _iso(entry.figures.stockout.as_of),
                "Yes" if entry.figures.stockout.stale else "No", entry.figures.excess_count, _na(entry.figures.excess_opportunity),
                _na(entry.figures.net_rop_value_impact), entry.figures.working_capital_currency or "Not stated",
                entry.figures.priced_count, entry.figures.working_capital_excluded_count, entry.figures.currency or "Not stated",
            ]
            for entry in management.plant_positions
        ],
    )

    status = workbook.create_sheet("Status Distribution")
    _facts(
        status,
        [("Kind", management.status_distribution.kind.value), ("Basis", management.status_distribution.basis),
         ("Measured on", _iso(management.status_distribution.measured_on)), ("Records", management.status_distribution.total)],
    )
    status.append([])
    _table(status, ["Status", "Count", "Percentage"], [[r.status, r.count, _na(r.percentage)] for r in management.status_distribution.rows])

    forecast = management.forecast_actual
    forecast_sheet = workbook.create_sheet("Forecast vs Actual")
    _facts(
        forecast_sheet,
        [("Status", forecast.status.value), ("Forecast run", _na(forecast.forecast_run_id)), ("Forecast total", _na(forecast.forecast_total)),
         ("Actual total", _na(forecast.actual_total)), ("Note", forecast.note)],
    )
    forecast_sheet.append([])
    _table(
        forecast_sheet, ["Month", "Forecast", "Actual", "Material-plants"],
        [[_iso(m.month), _na(m.forecast), _na(m.actual), m.material_count] for m in forecast.months],
    )
    forecast_sheet.append([])
    if forecast.accuracy is not None:
        _facts(
            forecast_sheet,
            [("Observations", forecast.accuracy.observations), ("Mean absolute error", _na(forecast.accuracy.mean_absolute_error)),
             ("Bias as a fraction of actual demand", _na(forecast.accuracy.bias_percentage))],
        )
    else:
        forecast_sheet.append(["Accuracy", NOT_AVAILABLE])

    adoption = management.adoption
    adoption_sheet = workbook.create_sheet("SAP Adoption")
    _facts(
        adoption_sheet,
        [("Status", adoption.status.value), ("Reason", adoption.reason), ("Confirmed adoptions", _na(adoption.confirmed_adoptions)),
         ("Recommendations awaiting a decision", adoption.pending_approvals), ("Recommendations with a change to adopt", _na(adoption.tracked)),
         ("Adopted", _na(adoption.adopted)), ("Partially adopted", _na(adoption.partially_adopted)), ("Not adopted", _na(adoption.not_adopted)),
         ("Pending (no SAP change recorded yet)", _na(adoption.pending))],
    )

    if management.trends is not None:
        _write_trends_sheet(workbook, management.trends)

    coverage = management.data_coverage
    coverage_sheet = workbook.create_sheet("Data Coverage")
    _facts(
        coverage_sheet,
        [("Latest consumption (goods issue)", _iso(coverage.latest_consumption_date)),
         ("Staged consumption ends in", _iso(coverage.latest_consumption_month)),
         ("Stock on hand last loaded", _iso(coverage.latest_stock_loaded_at)),
         ("Stockout usage measured as of", _iso(coverage.stockout_usage_as_of)),
         ("Recommendations counted", coverage.eligible_recommendations), ("Material-plants assessed", coverage.eligible_material_plants),
         ("Figures are", "Saved month-end snapshots" if coverage.basis == "snapshot" else "Current calculations")],
    )
    coverage_sheet.append([])
    _table(
        coverage_sheet, ["Month", "SAP consumption data", "Recommendations generated"],
        [[m.label, m.consumption_status.value, m.recommendations_generated] for m in coverage.months],
    )
    coverage_sheet.append([])
    _table(coverage_sheet, ["Limitation or warning"], [[warning] for warning in coverage.warnings])

    definitions = workbook.create_sheet("Definitions")
    _table(definitions, ["What the figures on these sheets mean"], [[definition] for definition in management.definitions])


def _write_trends_sheet(workbook: Workbook, trends) -> None:
    sheet = workbook.create_sheet("Trends")
    _table(
        sheet,
        ["Month", "Snapshot", "Measured on", "Valid for month-end", "Stockout reading usable", "Material-plants", "At stockout risk", "Judged",
         "Could not be judged", "Excess candidates", "Excess opportunity", "Working capital impact (estimate)", "Change from previous month",
         "Currency", "Revision", "Calculation version", "Note"],
        [
            [
                m.label, m.state, _iso(m.measured_on), "Yes" if m.month_end_valid else "No", "Yes" if m.stockout_valid else "No",
                _na(m.population), _na(m.stockout_at_risk), _na(m.stockout_evaluated), _na(m.stockout_insufficient),
                _na(m.excess_count), _na(m.excess_opportunity), _na(m.net_rop_value_impact), _na(m.net_rop_change_from_previous),
                m.currency or "Not stated", _na(m.revision), m.calc_version or "", m.note or "",
            ]
            for m in trends.months
        ],
    )
    sheet.append([])
    sheet.append(["Quarter-end position"])
    for cell in sheet[sheet.max_row]:
        cell.font = _HEADER_FONT
    end = trends.quarter_end
    _facts(
        sheet,
        [("Status", end.status.value), ("Month", end.label), ("Snapshot", end.snapshot_state), ("Measured on", _iso(end.measured_on)),
         ("Why not available", end.reason or ""),
         ("Latest snapshot available", end.latest_available_label or NOT_AVAILABLE),
         ("Latest snapshot measured on", _iso(end.latest_available_measured_on))],
    )
    if end.stock_levels:
        sheet.append([])
        _table(
            sheet, ["Stock level", "Aggregation", "Records", "Current (SAP)", "Recommended"],
            [[lvl.label, lvl.aggregation, lvl.records, _na(lvl.current), _na(lvl.recommended)] for lvl in end.stock_levels],
        )
    sheet.append([])
    _table(sheet, ["Notes"], [[note] for note in trends.notes])
    sheet.append(["Counts are positions on a date: they are shown per month and never added into a quarter total."])
