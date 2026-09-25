"""One-off export for manually verifying the three data-gap claims:

1. History status distribution (NO_HISTORY / COLD_START / SUFFICIENT)
2. Criticality coverage (NULL vs the five real ZMM065 tiers)
3. Lead-time evaluability (NOT_EVALUABLE_LEAD_TIME and friends)

Reads directly from i7_material_feature (Phase 3), i7_inventory_calculation
(Phase 5, latest run) and raw_marc -- no recomputation, every value is read
exactly as the pipeline persisted it. Writes one .xlsx with:

  Summary                 the three distributions as pivot-style counts
  Detail                  one row per material-plant, every underlying field,
                           with lead time / consumption / criticality
                           highlighted (red = missing, orange = no
                           consumption, amber = lead time blocked, green = OK)
  Has All Three           only the rows where lead time, consumption history
                           AND criticality are all genuinely present
  MARC Coverage By Plant  the actual root cause of most lead-time gaps --
                           raw_marc only has rows for plants 1300/1200, so
                           every other plant is unevaluable by construction,
                           not by a code defect
  Legend                  what each highlight color means

Not part of the application -- a throwaway verification script, run once and
deleted/ignored afterward.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from sqlalchemy import text

from app.core.db import get_sessionmaker

# Highlight fills, applied cell-by-cell as rows are written (not conditional
# formatting rules) so the color reflects the exact value read from the DB,
# not a formula re-evaluating it.
NULL_FILL = PatternFill(start_color="FFCCCC", end_color="FFCCCC", fill_type="solid")  # red-ish: missing/None
NO_CONSUMPTION_FILL = PatternFill(start_color="FFE0B3", end_color="FFE0B3", fill_type="solid")  # orange: no consumption
BLOCKED_LEAD_TIME_FILL = PatternFill(start_color="FFF2B3", end_color="FFF2B3", fill_type="solid")  # amber: lead time blocked
OK_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")  # green: has a real value


def main() -> None:
    session_factory = get_sessionmaker()
    wb = Workbook()

    with session_factory() as session:
        latest_inventory_run = session.execute(
            text("select max(inventory_run_id) from i7_inventory_calculation")
        ).scalar()

        # --- Summary sheet ---------------------------------------------
        summary = wb.active
        summary.title = "Summary"
        bold = Font(bold=True)

        total = session.execute(text("select count(*) from i7_material_feature")).scalar()

        row = 1
        summary.cell(row, 1, "Claim 1: History status distribution").font = bold
        row += 1
        summary.cell(row, 1, "history_status")
        summary.cell(row, 2, "count")
        summary.cell(row, 3, "share")
        row += 1
        for status, count in session.execute(
            text(
                "select history_status, count(*) from i7_material_feature "
                "group by 1 order by 2 desc"
            )
        ):
            summary.cell(row, 1, status)
            summary.cell(row, 2, count)
            summary.cell(row, 3, f"{100 * count / total:.1f}%")
            row += 1
        summary.cell(row, 1, "TOTAL")
        summary.cell(row, 2, total)
        row += 2

        summary.cell(row, 1, "Claim 2: Criticality coverage").font = bold
        row += 1
        summary.cell(row, 1, "criticality")
        summary.cell(row, 2, "count")
        summary.cell(row, 3, "share")
        row += 1
        for criticality, count in session.execute(
            text(
                "select coalesce(criticality, '(NULL)'), count(*) from i7_material_feature "
                "group by 1 order by 2 desc"
            )
        ):
            summary.cell(row, 1, criticality)
            summary.cell(row, 2, count)
            summary.cell(row, 3, f"{100 * count / total:.1f}%")
            row += 1
        summary.cell(row, 1, "TOTAL")
        summary.cell(row, 2, total)
        row += 2

        summary.cell(row, 1, f"Claim 3: Lead-time status (inventory_run_id={latest_inventory_run})").font = bold
        row += 1
        summary.cell(row, 1, "lead_time_status")
        summary.cell(row, 2, "count")
        summary.cell(row, 3, "share")
        row += 1
        inv_total = session.execute(
            text(
                "select count(*) from i7_inventory_calculation "
                "where inventory_run_id = :run"
            ),
            {"run": latest_inventory_run},
        ).scalar()
        for lt_status, count in session.execute(
            text(
                "select lead_time_status, count(*) from i7_inventory_calculation "
                "where inventory_run_id = :run group by 1 order by 2 desc"
            ),
            {"run": latest_inventory_run},
        ):
            summary.cell(row, 1, lt_status)
            summary.cell(row, 2, count)
            summary.cell(row, 3, f"{100 * count / inv_total:.1f}%")
            row += 1
        summary.cell(row, 1, "TOTAL")
        summary.cell(row, 2, inv_total)

        for col, width in {"A": 32, "B": 14, "C": 10}.items():
            summary.column_dimensions[col].width = width

        # --- Detail sheet: one row per material-plant -------------------
        detail = wb.create_sheet("Detail")
        headers = [
            "sap_material_number",
            "sap_plant_code",
            "history_status",
            "total_periods",
            "non_zero_periods",
            "history_months",
            "required_history_months",
            "consumption_count_12m",
            "criticality",
            "has_criticality",
            "mrp_type",
            "demand_class",
            "planned_delivery_time_days",
            "lead_time_days (feature store)",
            "lead_time_source (feature store)",
            "has_lead_time",
            "lead_time_status (inventory calc)",
            "lead_time_method (inventory calc)",
            "lt_avg_days (inventory calc)",
            "po_count",
            "valid_po_count",
        ]
        detail.append(headers)
        for cell in detail[1]:
            cell.font = bold

        rows = session.execute(
            text(
                """
                select
                    f.sap_material_number, f.sap_plant_code, f.history_status,
                    f.total_periods, f.non_zero_periods, f.history_months,
                    f.required_history_months, f.consumption_count_12m,
                    f.criticality, f.has_criticality, f.mrp_type, f.demand_class,
                    f.planned_delivery_time_days, f.lead_time_days, f.lead_time_source,
                    f.has_lead_time,
                    i.lead_time_status, i.lead_time_method, i.lt_avg_days,
                    i.po_count, i.valid_po_count
                from i7_material_feature f
                left join i7_inventory_calculation i
                    on i.sap_material_number = f.sap_material_number
                   and i.sap_plant_code = f.sap_plant_code
                   and i.inventory_run_id = :run
                order by f.sap_material_number, f.sap_plant_code
                """
            ),
            {"run": latest_inventory_run},
        )
        # Column positions (1-based) of the fields being highlighted, resolved
        # from `headers` rather than hardcoded, so a header edit above can't
        # silently mis-color the wrong column.
        col_consumption = headers.index("consumption_count_12m") + 1
        col_criticality = headers.index("criticality") + 1
        col_lead_time_days = headers.index("lead_time_days (feature store)") + 1
        col_lead_time_status = headers.index("lead_time_status (inventory calc)") + 1

        # Rows where lead time, consumption history AND criticality are all
        # genuinely present -- collected while writing Detail so this sheet
        # (and the MARC-coverage sheet below) never re-derive these flags
        # with different logic than what actually colored the cells above.
        has_all_three_rows: list[tuple] = []

        excel_row = 2  # row 1 is the header
        for row_data in rows:
            detail.append(list(row_data))

            consumption = row_data[col_consumption - 1]
            criticality_value = row_data[col_criticality - 1]
            lead_time_days_value = row_data[col_lead_time_days - 1]
            lead_time_status_value = row_data[col_lead_time_status - 1]

            history_status_value = row_data[headers.index("history_status")]
            # "Has consumption history" means the pipeline's own
            # history_status says SUFFICIENT -- consumption_count_12m alone
            # is too narrow a proxy: an INTERMITTENT material can have
            # SUFFICIENT total history (enough periods to classify) while its
            # trailing-12-month issue count is genuinely 0, which is a real
            # state, not missing data. Confirmed against the 3 materials that
            # actually reached SUCCESS on this extract, all of which have
            # consumption_count_12m = 0.
            has_consumption = history_status_value == "SUFFICIENT"
            has_criticality = criticality_value is not None
            has_lead_time = lead_time_days_value is not None and not (
                lead_time_status_value is not None
                and str(lead_time_status_value).startswith("NOT_EVALUABLE")
            )

            consumption_cell = detail.cell(excel_row, col_consumption)
            consumption_cell.fill = OK_FILL if has_consumption else NO_CONSUMPTION_FILL

            criticality_cell = detail.cell(excel_row, col_criticality)
            criticality_cell.fill = OK_FILL if has_criticality else NULL_FILL

            lead_time_cell = detail.cell(excel_row, col_lead_time_days)
            lead_time_cell.fill = NULL_FILL if lead_time_days_value is None else OK_FILL

            lead_time_status_cell = detail.cell(excel_row, col_lead_time_status)
            if lead_time_status_value is None:
                lead_time_status_cell.fill = NULL_FILL
            elif str(lead_time_status_value).startswith("NOT_EVALUABLE"):
                lead_time_status_cell.fill = BLOCKED_LEAD_TIME_FILL
            else:
                lead_time_status_cell.fill = OK_FILL

            if has_consumption and has_criticality and has_lead_time:
                has_all_three_rows.append(row_data)

            excel_row += 1

        for col in "ABCDEFGHIJKLMNOPQRSTU":
            detail.column_dimensions[col].width = 16

        # --- "Has All Three" sheet: only rows with real lead time,
        # consumption history and criticality, all at once -----------------
        has_all_three = wb.create_sheet("Has All Three")
        has_all_three.append(
            [f"Material-plants with real lead time + consumption history + criticality: {len(has_all_three_rows)} of {total}"]
        )
        has_all_three.cell(1, 1).font = bold
        has_all_three.append(headers)
        for cell in has_all_three[2]:
            cell.font = bold
        for row_data in has_all_three_rows:
            has_all_three.append(list(row_data))
        for col in "ABCDEFGHIJKLMNOPQRSTU":
            has_all_three.column_dimensions[col].width = 16

        # --- MARC coverage by plant: the actual root cause of most
        # lead-time gaps -- raw_marc simply has no row for most plants ------
        marc_sheet = wb.create_sheet("MARC Coverage By Plant")
        marc_sheet.append(["sap_plant_code", "material-plants in feature store", "material-plants with a MARC row", "coverage %"])
        for cell in marc_sheet[1]:
            cell.font = bold
        for plant, feature_count, marc_count in session.execute(
            text(
                """
                select f.sap_plant_code, count(*) as feature_count,
                       count(m.material) as marc_count
                from i7_material_feature f
                left join raw_marc m
                    on m.material = f.sap_material_number and m.plant = f.sap_plant_code
                group by f.sap_plant_code
                order by feature_count desc
                """
            )
        ):
            r = marc_sheet.max_row + 1
            marc_sheet.cell(r, 1, plant)
            marc_sheet.cell(r, 2, feature_count)
            marc_sheet.cell(r, 3, marc_count)
            pct = 100 * marc_count / feature_count if feature_count else 0
            marc_sheet.cell(r, 4, f"{pct:.1f}%")
            coverage_cell = marc_sheet.cell(r, 3)
            coverage_cell.fill = OK_FILL if marc_count == feature_count else (
                NULL_FILL if marc_count == 0 else NO_CONSUMPTION_FILL
            )
        for col, width in {"A": 16, "B": 32, "C": 32, "D": 12}.items():
            marc_sheet.column_dimensions[col].width = width

        # --- Legend, so the colors are self-explanatory without this script ---
        legend = wb.create_sheet("Legend")
        legend_rows = [
            ("Color", "Meaning"),
            ("Red", "NULL / missing value (criticality, lead_time_days)"),
            ("Orange", "history_status is not SUFFICIENT -- not enough consumption history to classify (consumption_count_12m can still be 0 even on a SUFFICIENT/green row -- that is a real trailing-12-month count, not missing data)"),
            ("Amber", "lead_time_status starts with NOT_EVALUABLE -- lead time blocked, even if a raw days value exists"),
            ("Green", "A real, present value"),
        ]
        for r_idx, (color_name, meaning) in enumerate(legend_rows, start=1):
            legend.cell(r_idx, 1, color_name)
            legend.cell(r_idx, 2, meaning)
        legend.cell(2, 1).fill = NULL_FILL
        legend.cell(3, 1).fill = NO_CONSUMPTION_FILL
        legend.cell(4, 1).fill = BLOCKED_LEAD_TIME_FILL
        legend.cell(5, 1).fill = OK_FILL
        for cell in legend[1]:
            cell.font = bold
        legend.column_dimensions["A"].width = 14
        legend.column_dimensions["B"].width = 90

    out_path = Path(__file__).resolve().parent.parent / "verification_report.xlsx"
    wb.save(out_path)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
